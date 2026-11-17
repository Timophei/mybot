"""Распознавание голосовых через OpenAI (модель gpt-transcribe)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Подсказка модели: термины, которые часто звучат в командах
HINT = ("Команды сотрудника боту для Битрикс24: выстави счёт, как в прошлый раз, поддержка, "
        "обслуживание, сопровождение, т.р. (тысяч рублей), ИП, ООО, Битрикс24, воронка, КП, печатная форма.")


class Transcriber:
    def __init__(self, cfg):
        self.enabled = bool(cfg.openai_api_key)
        self.model = cfg.openai_transcribe_model
        self._hint = True
        if self.enabled:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url or None)

    async def transcribe(self, data: bytes, filename: str) -> str:
        kwargs = dict(model=self.model, file=(filename, data), language="ru")
        if self._hint:
            kwargs["prompt"] = HINT
        try:
            res = await self.client.audio.transcriptions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if "prompt" in kwargs and "prompt" in str(exc):
                log.info("Модель не принимает prompt, распознаю без подсказки: %s", exc)
                self._hint = False
                kwargs.pop("prompt")
                res = await self.client.audio.transcriptions.create(**kwargs)
            else:
                raise
        return (getattr(res, "text", None) or "").strip()
