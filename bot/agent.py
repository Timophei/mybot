"""Разбор команд сотрудника моделью Claude с инструментами Битрикс24."""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .bitrix import BitrixError
from .invoices import Draft, InvoiceService
from .staff import Employee

log = logging.getLogger(__name__)

MAX_STEPS = 10
HISTORY_LEN = 8  # сколько последних реплик помнить в чате

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]

SYSTEM = """Ты — рабочий помощник сотрудников компании в Telegram. Ты работаешь с их порталом Битрикс24.
Сегодня {today} ({month} {year} г.), часовой пояс {tz}. Сотрудник: {employee}.

Главная задача — выставлять счета клиентам по образцу прошлых.
Типичная команда: «выстави счёт Русирке как в прошлый раз за поддержку, только месяц поменяй».
Порядок действий:
1. Найди компанию через search_companies. Названия пишут неточно, сокращённо, в другом падеже
   («Русирке» → ищи «Русирк» / «Rusirk»). Если подходят несколько — спроси, какую.
2. Найди её последние счета через list_invoices (если упомянута услуга — передай часть слова в title_contains,
   а если ничего не нашлось — повтори без фильтра).
3. Открой подходящий счёт через get_invoice, посмотри строки.
4. Подготовь черновик через draft_invoice: перенеси строки (source_index), поменяй только то,
   что просили. «Поменяй месяц» = замени упоминание месяца/периода в названии счёта и строк на
   текущий месяц (если не сказано иное), в том же падеже и формате, что был. Суммы не трогай,
   если о них не просили.
5. После draft_invoice коротко опиши, что получилось — бот сам покажет кнопки «Создать / Отмена».
   Счёт создаётся только после нажатия кнопки, сам ты его не создаёшь.

Если не хватает данных или есть сомнения — задай один короткий вопрос, не выдумывай.
Отвечай по-русски, кратко, без markdown-таблиц.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_companies",
        "description": "Поиск компаний CRM по части названия. Возвращает id и название.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Часть названия, лучше корень слова"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_invoices",
        "description": "Последние счета компании (новые сверху).",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "title_contains": {"type": "string", "description": "Фильтр по части названия счёта"},
                "limit": {"type": "integer", "default": 5},
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
    def __init__(self, api_key: str, model: str, invoices: InvoiceService, tz: str):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.inv = invoices
        self.tz = tz
        self.history: dict[int, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=HISTORY_LEN))

    def reset(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    def _system(self, emp: Employee) -> str:
        now = datetime.now(ZoneInfo(self.tz))
        return SYSTEM.format(today=now.strftime("%d.%m.%Y"), month=MONTHS[now.month - 1],
                             year=now.year, tz=self.tz, employee=f"{emp.name} (id {emp.id})")

    async def _run_tool(self, name: str, args: dict[str, Any], emp: Employee,
                        state: dict[str, Any]) -> Any:
        if name == "search_companies":
            return await self.inv.search_companies(args["query"])
        if name == "list_invoices":
            return await self.inv.list_invoices(int(args["company_id"]), args.get("title_contains"),
                                                int(args.get("limit") or 5))
        if name == "get_invoice":
            return await self.inv.get_invoice(int(args["invoice_id"]))
        if name == "draft_invoice":
            d = await self.inv.draft_invoice(emp.id, int(args["source_invoice_id"]), args["title"],
                                             args.get("rows") or [], args.get("comment") or "")
            state["draft"] = d
            return {"draft_id": d.id, "total": d.total, "rows": len(d.rows),
                    "note": "Черновик готов, сотрудник увидит кнопки подтверждения."}
        raise ValueError(f"Неизвестный инструмент {name}")

    async def handle(self, chat_id: int, emp: Employee, text: str) -> AgentReply:
        hist = self.history[chat_id]
        messages: list[dict[str, Any]] = list(hist) + [{"role": "user", "content": text}]
        state: dict[str, Any] = {}
        final_text = ""

        for _ in range(MAX_STEPS):
            resp = await self.client.messages.create(
                model=self.model, max_tokens=2048, system=self._system(emp),
                tools=TOOLS, messages=messages,
            )
            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                try:
                    out = await self._run_tool(tu.name, dict(tu.input), emp, state)
                    content, is_error = json.dumps(out, ensure_ascii=False, default=str), False
                except (BitrixError, ValueError, KeyError) as exc:
                    log.warning("Инструмент %s упал: %s", tu.name, exc)
                    content, is_error = f"Ошибка: {exc}", True
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
        else:
            final_text = final_text or "Не получилось довести дело до конца — попробуй сформулировать иначе."

        # В истории храним только текст, без промежуточных вызовов инструментов
        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": final_text or "…"})
        return AgentReply(text=final_text, draft=state.get("draft"))
