"""Разбор команд сотрудника моделью Claude с инструментами Битрикс24."""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


from .bitrix import BitrixError
from .invoices import Draft, InvoiceService
from .staff import Employee

log = logging.getLogger(__name__)

MAX_STEPS = 10
HISTORY_LEN = 8  # сколько последних реплик помнить в чате

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]

SYSTEM = """Ты — рабочий помощник сотрудников компании в Telegram. Ты работаешь с их порталом Битрикс24 ({portal}).
Сегодня {today} ({month} {year} г.), часовой пояс {tz}. Сотрудник: {employee}.

Главная задача — выставлять счета клиентам по образцу прошлых.
Типичная команда: «выстави счёт Русирке как в прошлый раз за поддержку, только месяц поменяй».

Как действовать:
1. Компания. Ищи через search_companies — поиск нечёткий, сам справляется с опечатками и падежами,
   передавай название как написал сотрудник. Берёшь лучший вариант, если он явно лидирует (match ≥ 0.8
   и заметно выше остальных); иначе перечисли 2–3 варианта и спроси. Не говори «не нашёл»,
   пока не попробовал хотя бы 2 формулировки.
2. Ссылки. Сотрудник может прислать ссылку на портал — разбирай её сам, без вопросов:
   …/crm/company/details/694/ → компания 694; …/crm/type/31/details/361/ → счёт 361;
   …/crm/deal/details/N/ → сделка N. Если сказано «счёт 361» или «по образцу 361» — это id счёта,
   сразу открывай его через get_invoice.
3. Образец. list_invoices даёт последние счета компании с их строками. Выбирай по смыслу строк,
   а не по слову в названии: «поддержка», «обслуживание», «сопровождение», «абонемент» — одно и то же.
   При прочих равных бери самый свежий. Если сотрудник назвал месяц образца («похожий был в октябре») —
   бери счёт за тот месяц. Если подходящих несколько и они разные (суммы, тарифы) — коротко перечисли
   (номер, дата, сумма) и спроси.
4. Черновик. draft_invoice: перенеси строки (source_index), поменяй только то, что просили.
   «Поменяй месяц» / «за ноябрь» = замени месяц в названии счёта и строк на нужный, в том же
   формате. Суммы не трогай, если о них не просили.
5. Правки. Если после черновика просят что-то поменять («не 15, а 20 т.р.», «другой месяц») —
   сразу сделай новый черновик с правкой по тому же образцу, ничего не переспрашивая.
6. После draft_invoice ответь одной-двумя строками: какой счёт взял за образец (номер, название, дата)
   и что поменял. Кнопки «Создать / Отмена» бот покажет сам; счёт создаётся только по кнопке.

Не выдумывай: если чего-то не хватает — один короткий вопрос. Отвечай по-русски, кратко, без markdown.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_companies",
        "description": ("Нечёткий поиск компаний CRM по названию (опечатки, падежи, сокращения). "
                        "Возвращает до 8 кандидатов с оценкой совпадения match от 0 до 1."),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Название, как его написал сотрудник"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_invoices",
        "description": "Последние счета компании (новые сверху) с датой, суммой и строками.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "Сколько счетов, по умолчанию 15"},
            },
            "required": ["company_id"],
        },
    },
    {
        "name": "get_invoice",
        "description": "Счёт целиком: реквизиты и товарные строки с индексами.",
        "input_schema": {
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    },
    {
        "name": "draft_invoice",
        "description": ("Подготовить черновик нового счёта по образцу существующего: та же компания, "
                        "наши реквизиты, контакт, валюта, сделка. Сотрудник подтверждает его кнопкой."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_invoice_id": {"type": "integer"},
                "title": {"type": "string", "description": "Название нового счёта"},
                "rows": {
                    "type": "array",
                    "description": "Строки нового счёта. Для строки из образца укажи source_index и только изменённые поля.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_index": {"type": "integer"},
                            "productName": {"type": "string"},
                            "price": {"type": "number"},
                            "quantity": {"type": "number"},
                        },
                    },
                },
                "comment": {"type": "string"},
            },
            "required": ["source_invoice_id", "title", "rows"],
        },
    },
]


@dataclass
class AgentReply:
    text: str
    draft: Draft | None = None


class Agent:
    def __init__(self, cfg, invoices: InvoiceService):
        self.provider = cfg.llm_provider
        if self.provider == "openai":
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url or None)
            self.model = cfg.openai_model
            self.api_mode = cfg.openai_api_mode
            self.reasoning_effort = cfg.openai_reasoning_effort or (
                "low" if self.api_mode == "responses" else "none")
        else:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
            self.model = cfg.anthropic_model
        self.inv = invoices
        self.tz = cfg.timezone
        self.portal = cfg.bitrix_portal_url
        self.history: dict[int, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))

    def reset(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    def _system(self, emp: Employee) -> str:
        now = datetime.now(ZoneInfo(self.tz))
        return SYSTEM.format(today=now.strftime("%d.%m.%Y"), month=MONTHS[now.month - 1],
                             year=now.year, tz=self.tz, portal=self.portal, employee=f"{emp.name} (id {emp.id})")

    async def _run_tool(self, name: str, args: dict[str, Any], emp: Employee,
                        state: dict[str, Any]) -> tuple[str, bool]:
        """Выполнить инструмент. Возвращает (json-строка результата, была ли ошибка)."""
        try:
            if name == "search_companies":
                out: Any = await self.inv.search_companies(args["query"])
            elif name == "list_invoices":
                out = await self.inv.list_invoices(int(args["company_id"]), int(args.get("limit") or 15))
            elif name == "get_invoice":
                out = await self.inv.get_invoice(int(args["invoice_id"]))
            elif name == "draft_invoice":
                d = await self.inv.draft_invoice(emp.id, int(args["source_invoice_id"]), args["title"],
                                                 args.get("rows") or [], args.get("comment") or "")
                state["draft"] = d
                out = {"draft_id": d.id, "source_invoice": d.source.get("id"),
                       "source_title": d.source.get("title"), "total": d.total, "rows": len(d.rows),
                       "note": "Черновик готов, сотрудник увидит кнопки подтверждения."}
            else:
                raise ValueError(f"Неизвестный инструмент {name}")
            return json.dumps(out, ensure_ascii=False, default=str), False
        except Exception as exc:  # noqa: BLE001 — ошибку отдаём модели, а не роняем разговор
            log.warning("Инструмент %s упал: %s", name, exc, exc_info=not isinstance(exc, BitrixError))
            return f"Ошибка: {exc}", True

    async def handle(self, chat_id: int, emp: Employee, text: str) -> AgentReply:
        hist = self.history[chat_id]
        state: dict[str, Any] = {}
        if self.provider == "openai" and self.api_mode == "responses":
            final_text = await self._loop_responses(list(hist), text, emp, state)
        elif self.provider == "openai":
            final_text = await self._loop_openai(list(hist), text, emp, state)
        else:
            final_text = await self._loop_anthropic(list(hist), text, emp, state)
        final_text = final_text or ("" if state.get("draft") else
                                    "Не получилось довести дело до конца — попробуй сформулировать иначе.")
        # В истории — только текст, но с кратким описанием черновика, чтобы правки
        # («не 15, а 20») делались по тому же образцу
        memo = final_text or "Подготовил черновик счёта."
        d = state.get("draft")
        if d:
            rows = "; ".join(f"[{r.get('productName')} — {r.get('quantity')} × {r.get('price')}]" for r in d.rows)
            memo += (f"\n(служебно: черновик по образцу счёта id={d.source.get('id')} "
                     f"«{d.source.get('title')}», компания id={d.source.get('companyId')}; "
                     f"название «{d.title}»; строки: {rows})")
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": memo})
        return AgentReply(text=final_text, draft=state.get("draft"))

    # ---------- OpenAI Responses API (рассуждения + функции) ----------

    async def _loop_responses(self, hist: list, text: str, emp: Employee, state: dict) -> str:
        tools = [{"type": "function", "name": t["name"], "description": t["description"],
                  "parameters": t["input_schema"]} for t in TOOLS]
        inputs: list[dict[str, Any]] = hist + [{"role": "user", "content": text}]
        prev_id: str | None = None
        final_text = ""
        for _ in range(MAX_STEPS):
            kwargs: dict[str, Any] = dict(model=self.model, instructions=self._system(emp),
                                          input=inputs, tools=tools, max_output_tokens=8000)
            if self.reasoning_effort:
                kwargs["reasoning"] = {"effort": self.reasoning_effort}
            if prev_id:
                kwargs["previous_response_id"] = prev_id
            resp = await self.client.responses.create(**kwargs)
            prev_id = resp.id
            final_text = (resp.output_text or "").strip()
            calls = [o for o in resp.output if o.type == "function_call"]
            if not calls:
                break
            inputs = []
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                content, _err = await self._run_tool(c.name, args, emp, state)
                inputs.append({"type": "function_call_output", "call_id": c.call_id, "output": content})
        return final_text

    # ---------- OpenAI (Chat Completions + function calling) ----------

    async def _loop_openai(self, hist: list, text: str, emp: Employee, state: dict) -> str:
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in TOOLS]
        messages: list[dict[str, Any]] = (
            [{"role": "system", "content": self._system(emp)}] + hist + [{"role": "user", "content": text}])
        final_text = ""
        for _ in range(MAX_STEPS):
            kwargs: dict[str, Any] = dict(model=self.model, messages=messages, tools=tools,
                                          max_completion_tokens=4096)
            if self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                # Модели без «размышлений» не знают этот параметр — повторяем без него
                if self.reasoning_effort and "reasoning_effort" in str(exc) and "not supported" not in str(exc):
                    log.info("Модель не принимает reasoning_effort, отключаю: %s", exc)
                    self.reasoning_effort = ""
                    kwargs.pop("reasoning_effort")
                    resp = await self.client.chat.completions.create(**kwargs)
                else:
                    raise
            msg = resp.choices[0].message
            final_text = (msg.content or "").strip()
            calls = msg.tool_calls or []
            if not calls:
                break
            messages.append({"role": "assistant", "content": msg.content or None, "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                content, _err = await self._run_tool(c.function.name, args, emp, state)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": content})
        return final_text

    # ---------- Anthropic (Messages + tool use) ----------

    async def _loop_anthropic(self, hist: list, text: str, emp: Employee, state: dict) -> str:
        messages: list[dict[str, Any]] = hist + [{"role": "user", "content": text}]
        final_text = ""
        for _ in range(MAX_STEPS):
            resp = await self.client.messages.create(
                model=self.model, max_tokens=2048, system=self._system(emp),
                tools=TOOLS, messages=messages)
            messages.append({"role": "assistant",
                             "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                content, is_error = await self._run_tool(tu.name, dict(tu.input), emp, state)
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        return final_text
