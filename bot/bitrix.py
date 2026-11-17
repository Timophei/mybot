"""Минимальный асинхронный клиент REST API Битрикс24 через входящий вебхук."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp

log = logging.getLogger(__name__)


class BitrixError(RuntimeError):
    pass


class Bitrix:
    def __init__(self, webhook_url: str):
        self.webhook = webhook_url  # заканчивается на "/"
        self._session: aiohttp.ClientSession | None = None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Вызов метода. Возвращает поле result (а для списков — весь ответ см. call_raw)."""
        data = await self.call_raw(method, params)
        return data.get("result")

    async def call_raw(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        http = await self._http()
        url = f"{self.webhook}{method}.json"
        async with http.post(url, json=params or {}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                text = await resp.text()
                raise BitrixError(f"{method}: HTTP {resp.status}, не JSON: {text[:300]}") from exc
        if "error" in data:
            raise BitrixError(f"{method}: {data.get('error')} — {data.get('error_description', '')}")
        return data

    async def list_all(self, method: str, params: dict[str, Any] | None = None,
                       items_key: str | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        """Постраничная выборка (по 50 записей)."""
        params = dict(params or {})
        out: list[dict[str, Any]] = []
        start: int | None = 0
        while start is not None and len(out) < limit:
            params["start"] = start
            data = await self.call_raw(method, params)
            result = data.get("result") or []
            if items_key and isinstance(result, dict):
                result = result.get(items_key) or []
            out.extend(result)
            start = data.get("next")
        return out

    async def download(self, machine_url: str) -> bytes:
        """Скачать файл по *UrlMachine из documentgenerator.

        Такие ссылки указывают на REST-метод (…/rest/<method>?token=…).
        Вызываем этот метод через наш вебхук, чтобы пройти авторизацию.
        """
        http = await self._http()
        parts = urlsplit(machine_url)
        if "/rest/" in parts.path:
            method = parts.path.rsplit("/", 1)[-1]
            if method.endswith(".json"):
                method = method[:-5]
            url = f"{self.webhook}{method}"
            params = dict(parse_qsl(parts.query))
        else:
            url, params = machine_url, {}
        async with http.get(url, params=params) as resp:
            body = await resp.read()
            if resp.status != 200:
                raise BitrixError(f"Не удалось скачать файл: HTTP {resp.status}")
            if resp.content_type == "application/json" and body.lstrip().startswith(b"{"):
                raise BitrixError(f"Вместо файла пришла ошибка: {body[:300]!r}")
            return body
