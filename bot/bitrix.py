"""Минимальный асинхронный клиент REST API Битрикс24 через входящий вебхук."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

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

        По документации Битрикса такую ссылку берут как есть: в ней уже есть auth/token.
        Относительную ссылку дополняем адресом портала. Если не вышло — пробуем тот же
        метод через путь нашего вебхука.
        """
        http = await self._http()
        hook = urlsplit(self.webhook)
        portal = f"{hook.scheme}://{hook.netloc}"
        url = machine_url if machine_url.startswith("http") else portal + machine_url
        attempts = [url]
        parts = urlsplit(url)
        if "/rest/" in parts.path:
            method = parts.path.rsplit("/", 1)[-1]
            attempts.append(f"{self.webhook}{method}?{parts.query}")
        last = ""
        for u in attempts:
            async with http.get(u, headers={"Referer": portal + "/"}) as resp:
                body = await resp.read()
                ctype = resp.content_type or ""
                if resp.status == 200 and "json" not in ctype and "html" not in ctype:
                    return body
                last = f"HTTP {resp.status}, {ctype}, {body[:200]!r}"
                log.warning("Скачивание файла не удалось (%s), пробую следующий способ", last)
        raise BitrixError(f"Не удалось скачать файл: {last}")
