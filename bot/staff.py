"""Определяем, что в бот пишет сотрудник портала.

В профиле сотрудника есть строковое поле (по умолчанию UF_USR_1794934830441).
В него можно записать любой из вариантов, можно несколько через пробел/запятую:
  ivanov   @ivanov   https://t.me/ivanov   123456789 (числовой Telegram ID)
Сравнение без учёта регистра. Список активных сотрудников кешируется на 10 минут.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .bitrix import Bitrix

CACHE_TTL = 600


@dataclass(frozen=True)
class Employee:
    id: int
    name: str


def _tokens(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    out = set()
    for tok in re.split(r"[\s,;]+", str(raw)):
        tok = tok.strip().lower()
        tok = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", tok)
        tok = tok.lstrip("@").strip("/")
        if tok:
            out.add(tok)
    return out


class StaffDirectory:
    def __init__(self, bx: Bitrix, field: str):
        self.bx = bx
        self.field = field
        self._index: dict[str, Employee] = {}
        self._loaded_at: float | None = None

    async def _reload(self) -> None:
        users = await self.bx.list_all("user.get", {"FILTER": {"ACTIVE": True}})
        index: dict[str, Employee] = {}
        for u in users:
            if u.get("USER_TYPE") not in (None, "", "employee"):
                continue  # экстранет и прочие внешние пользователи — мимо
            name = " ".join(x for x in (u.get("NAME"), u.get("LAST_NAME")) if x) or f"#{u['ID']}"
            for tok in _tokens(u.get(self.field)):
                index[tok] = Employee(id=int(u["ID"]), name=name)
        self._index = index
        self._loaded_at = time.monotonic()

    async def find(self, tg_user_id: int, tg_username: str | None) -> Employee | None:
        keys = [str(tg_user_id)] + ([tg_username.lower()] if tg_username else [])
        if self._loaded_at is None or time.monotonic() - self._loaded_at > CACHE_TTL:
            await self._reload()
        for k in keys:
            if k in self._index:
                return self._index[k]
        # Не нашли — возможно, поле только что заполнили: перечитываем один раз.
        if time.monotonic() - self._loaded_at > 30:
            await self._reload()
            for k in keys:
                if k in self._index:
                    return self._index[k]
        return None
