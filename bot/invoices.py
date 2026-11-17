"""Работа со счетами: поиск, черновик «как прошлый», создание и печатная форма."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from .bitrix import Bitrix, BitrixError

log = logging.getLogger(__name__)

DRAFT_TTL = 3600

# Поля товарной строки, которые переносим из исходного счёта как есть
ROW_COPY_FIELDS = (
    "productId", "productName", "price", "quantity", "discountTypeId", "discountRate",
    "discountSum", "taxRate", "taxIncluded", "measureCode", "measureName", "type", "sort",
)


def owner_type_for(entity_type_id: int) -> str:
    """Короткий код владельца товарных строк для crm.item.productrow.*"""
    return {31: "SI", 2: "D", 1: "L", 7: "Q"}.get(entity_type_id, f"T{entity_type_id:x}")


_LEGAL = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|нко|ано|llc|ltd)\b")


def _norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[«»\"'“”„.,()\-–—]", " ", t)
    t = _LEGAL.sub(" ", t)
    return " ".join(t.split())


def _d(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


@dataclass
class Draft:
    id: str
    employee_id: int
    source: dict[str, Any]
    title: str
    rows: list[dict[str, Any]]
    comment: str = ""
    created: float = field(default_factory=time.monotonic)

    @property
    def total(self) -> float:
        return sum(float(r.get("price") or 0) * float(r.get("quantity") or 0) for r in self.rows)


class InvoiceService:
    def __init__(self, bx: Bitrix, entity_type_id: int, template_id: int, pay_days: int,
                 tz: str = "Asia/Irkutsk"):
        self.bx = bx
        self.tz = ZoneInfo(tz)
        self.etid = entity_type_id
        self.owner_type = owner_type_for(entity_type_id)
        self.template_id = template_id
        self.pay_days = pay_days
        self.drafts: dict[str, Draft] = {}
        self._companies: dict[int, str] = {}
        self._company_list: list[tuple[int, str]] | None = None
        self._company_list_at = 0.0

    # ---------- инструменты для модели ----------

    async def _all_companies(self) -> list[tuple[int, str]]:
        """Все компании (id, название), кеш на 10 минут — для нечёткого поиска."""
        if self._company_list is None or time.monotonic() - self._company_list_at > 600:
            rows = await self.bx.list_all("crm.company.list", {"select": ["ID", "TITLE"],
                                                               "order": {"ID": "DESC"}}, limit=20000)
            self._company_list = [(int(c["ID"]), c.get("TITLE") or "") for c in rows]
            self._company_list_at = time.monotonic()
            for cid, title in self._company_list:
                self._companies[cid] = title
        return self._company_list

    async def search_companies(self, query: str) -> list[dict[str, Any]]:
        """Нечёткий поиск: опечатки, падежи, переставленные буквы («асит туру» → «Аист Тур»)."""
        companies = await self._all_companies()
        q = _norm(query)
        q_words = [w for w in q.split() if len(w) > 1]
        scored = []
        for cid, title in companies:
            t = _norm(title)
            if not t:
                continue
            score = SequenceMatcher(None, q, t).ratio()
            if q and q in t:
                score = max(score, 0.9)
            if q_words:
                t_words = t.split()
                word_scores = [max((SequenceMatcher(None, w, tw).ratio() for tw in t_words), default=0)
                               for w in q_words]
                score = max(score, sum(word_scores) / len(word_scores))
            scored.append((score, cid, title))
        scored.sort(reverse=True)
        return [{"id": cid, "title": title, "match": round(sc, 2)}
                for sc, cid, title in scored[:8] if sc >= 0.5]

    async def list_invoices(self, company_id: int, limit: int = 15) -> list[dict[str, Any]]:
        """Последние счета компании вместе с названиями строк (одним batch-запросом)."""
        res = await self.bx.call("crm.item.list", {
            "entityTypeId": self.etid,
            "filter": {"=companyId": company_id},
            "order": {"id": "DESC"},
            "select": ["id", "title", "opportunity", "currencyId", "begindate", "stageId"],
        }) or {}
        items = res.get("items", [])[: max(1, min(limit, 30))]
        rows_by_id: dict[int, list] = {}
        if items:
            cmd = {f"r{i['id']}": (f"crm.item.productrow.list?filter[=ownerType]={self.owner_type}"
                                   f"&filter[=ownerId]={i['id']}") for i in items}
            try:
                batch = await self.bx.call("batch", {"halt": 0, "cmd": cmd})
                for key, val in (batch or {}).get("result", {}).items():
                    rows_by_id[int(key[1:])] = (val or {}).get("productRows", [])
            except BitrixError as exc:
                log.info("batch строк не прошёл: %s", exc)
        return [{
            "id": i["id"], "title": i.get("title"), "sum": i.get("opportunity"),
            "currency": i.get("currencyId"), "date": (i.get("begindate") or "")[:10],
            "stage": i.get("stageId"),
            "rows": [f"{r.get('productName')} — {r.get('quantity')} × {r.get('price')}"
                     for r in rows_by_id.get(i["id"], [])],
        } for i in items]

    async def _rows(self, invoice_id: int) -> list[dict[str, Any]]:
        res = await self.bx.call("crm.item.productrow.list", {
            "filter": {"=ownerType": self.owner_type, "=ownerId": invoice_id},
        }) or {}
        return res.get("productRows", [])

    async def get_invoice(self, invoice_id: int) -> dict[str, Any]:
        item = (await self.bx.call("crm.item.get", {"entityTypeId": self.etid, "id": invoice_id}))["item"]
        rows = await self._rows(invoice_id)
        return {
            "id": item["id"], "title": item.get("title"), "company_id": item.get("companyId"),
            "sum": item.get("opportunity"), "currency": item.get("currencyId"),
            "date": item.get("begindate"), "pay_until": item.get("closedate"),
            "comment": item.get("comments"),
            "rows": [{"index": n, "productName": r.get("productName"), "price": r.get("price"),
                      "quantity": r.get("quantity"), "measure": r.get("measureName")}
                     for n, r in enumerate(rows)],
        }

    async def draft_invoice(self, employee_id: int, source_invoice_id: int, title: str,
                            rows: list[dict[str, Any]], comment: str = "") -> Draft:
        item = (await self.bx.call("crm.item.get", {"entityTypeId": self.etid, "id": source_invoice_id}))["item"]
        src_rows = await self._rows(source_invoice_id)
        new_rows: list[dict[str, Any]] = []
        for r in rows:
            idx = r.get("source_index")
            base: dict[str, Any] = {}
            from_source = isinstance(idx, int) and 0 <= idx < len(src_rows)
            if from_source:
                base = {k: src_rows[idx][k] for k in ROW_COPY_FIELDS if k in src_rows[idx]}
            if from_source and r.get("price") is not None and float(r["price"]) != float(base.get("price") or 0):
                for k in ("discountTypeId", "discountRate", "discountSum"):
                    base.pop(k, None)  # цена задана заново — старую скидку не тянем
            for k in ("productName", "price", "quantity"):
                if r.get(k) is not None:
                    base[k] = r[k]
            if not base.get("productName"):
                raise ValueError("У каждой строки должно быть название")
            base.setdefault("quantity", 1)
            base.setdefault("price", 0)
            if not from_source:
                base["productId"] = 0  # произвольная строка без товара из каталога
            new_rows.append(base)
        if not new_rows:
            raise ValueError("В счёте нет ни одной строки")
        self._gc()
        d = Draft(id=secrets.token_hex(6), employee_id=employee_id, source=item,
                  title=title, rows=new_rows, comment=comment)
        self.drafts[d.id] = d
        return d

    # ---------- создание и печатная форма ----------

    def _gc(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self.drafts.items() if now - v.created > DRAFT_TTL]:
            del self.drafts[k]

    async def company_title(self, company_id: int | None) -> str:
        if not company_id:
            return "—"
        if company_id not in self._companies:
            try:
                c = await self.bx.call("crm.company.get", {"id": company_id})
                self._companies[company_id] = c.get("TITLE") or f"#{company_id}"
            except BitrixError:
                return f"#{company_id}"
        return self._companies[company_id]

    def dates_for(self, src: dict[str, Any]) -> tuple[date, date]:
        today = datetime.now(self.tz).date()
        b, c = _d(src.get("begindate")), _d(src.get("closedate"))
        days = (c - b).days if b and c and c >= b else self.pay_days
        return today, today + timedelta(days=days)

    async def create(self, d: Draft) -> int:
        src = d.source
        begin, close = self.dates_for(src)
        fields: dict[str, Any] = {
            "title": d.title,
            "companyId": src.get("companyId"),
            "mycompanyId": src.get("mycompanyId"),
            "currencyId": src.get("currencyId"),
            "assignedById": src.get("assignedById") or d.employee_id,
            "begindate": begin.isoformat(),
            "closedate": close.isoformat(),
            "isManualOpportunity": "N",
        }
        if src.get("contactIds"):
            fields["contactIds"] = src["contactIds"]
        elif src.get("contactId"):
            fields["contactId"] = src["contactId"]
        # привязка к сделке, как у исходного счёта
        if src.get("parentId2"):
            fields["parentId2"] = src["parentId2"]
        if d.comment:
            fields["comments"] = d.comment
        fields = {k: v for k, v in fields.items() if v not in (None, "", 0)}

        res = await self.bx.call("crm.item.add", {"entityTypeId": self.etid, "fields": fields})
        new_id = int(res["item"]["id"])

        await self.bx.call("crm.item.productrow.set", {
            "ownerType": self.owner_type, "ownerId": new_id, "productRows": d.rows,
        })
        await self._copy_requisites(int(src["id"]), new_id)
        self.drafts.pop(d.id, None)
        return new_id

    async def _copy_requisites(self, src_id: int, new_id: int) -> None:
        """Переносим выбранные реквизиты (наши и клиента) — от них зависит печатная форма."""
        try:
            link = await self.bx.call("crm.requisite.link.get", {"entityTypeId": self.etid, "entityId": src_id})
        except BitrixError as exc:
            log.info("Реквизиты исходного счёта не получены: %s", exc)
            return
        if not link:
            return
        params = {"fields": {
            "ENTITY_TYPE_ID": self.etid, "ENTITY_ID": new_id,
            "REQUISITE_ID": link.get("REQUISITE_ID", 0), "BANK_DETAIL_ID": link.get("BANK_DETAIL_ID", 0),
            "MC_REQUISITE_ID": link.get("MC_REQUISITE_ID", 0), "MC_BANK_DETAIL_ID": link.get("MC_BANK_DETAIL_ID", 0),
        }}
        try:
            await self.bx.call("crm.requisite.link.register", params)
        except BitrixError as exc:
            log.warning("Не удалось перенести реквизиты: %s", exc)

    async def _template(self) -> int:
        if self.template_id:
            return self.template_id
        try:
            res = await self.bx.call("crm.documentgenerator.template.list", {
                "filter": {"entityTypeId": str(self.etid), "active": "Y"},
            })
        except BitrixError:
            res = await self.bx.call("crm.documentgenerator.template.list", {})
        templates = (res or {}).get("templates", [])
        if not templates:
            raise BitrixError("На портале нет шаблонов печатных форм для счетов")
        for t in templates:
            if "сч" in (t.get("name") or "").lower():
                self.template_id = int(t["id"])
                return self.template_id
        self.template_id = int(templates[0]["id"])
        return self.template_id

    async def print_form(self, invoice_id: int) -> tuple[bytes, str]:
        """Возвращает (содержимое, расширение). PDF, если портал умеет конвертировать, иначе DOCX."""
        res = await self.bx.call("crm.documentgenerator.document.add", {
            "templateId": await self._template(),
            "entityTypeId": self.etid,
            "entityId": invoice_id,
        })
        doc = res["document"]
        for _ in range(15):
            if doc.get("pdfUrlMachine"):
                try:
                    return await self.bx.download(doc["pdfUrlMachine"]), "pdf"
                except BitrixError as exc:
                    log.warning("PDF не скачался, беру Word: %s", exc)
                    break
            await asyncio.sleep(2)
            doc = (await self.bx.call("crm.documentgenerator.document.get", {"id": doc["id"]}))["document"]
        url = doc.get("downloadUrlMachine")
        if not url:
            raise BitrixError("Документ создан, но ссылка на файл не пришла")
        return await self.bx.download(url), "docx"
