"""Настройки бота. Всё берётся из переменных окружения (файл .env на сервере)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_token: str = field(default_factory=lambda: _req("TELEGRAM_BOT_TOKEN"))
    webhook_base_url: str = field(default_factory=lambda: _req("WEBHOOK_BASE_URL").rstrip("/"))
    webhook_path: str = field(default_factory=lambda: os.getenv("WEBHOOK_PATH", "/mybot/tg"))
    webhook_secret: str = field(default_factory=lambda: _req("WEBHOOK_SECRET"))
    listen_host: str = field(default_factory=lambda: os.getenv("LISTEN_HOST", "127.0.0.1"))
    listen_port: int = field(default_factory=lambda: _int("LISTEN_PORT", 8085))

    # Битрикс24: входящий вебхук вида https://bitrix.b-integration.ru/rest/1/xxxxxxxx/
    bitrix_webhook: str = field(default_factory=lambda: _req("BITRIX_WEBHOOK_URL").rstrip("/") + "/")
    # Поле сотрудника, где хранится Telegram (логин, @логин, ссылка t.me/… или числовой ID)
    telegram_user_field: str = field(
        default_factory=lambda: os.getenv("BITRIX_TELEGRAM_FIELD", "UF_USR_1794934830441")
    )
    # 31 = смарт-счета (новые счета Битрикс24)
    invoice_entity_type_id: int = field(default_factory=lambda: _int("INVOICE_ENTITY_TYPE_ID", 31))
    # ID шаблона печатной формы; 0 = взять первый активный шаблон для счетов
    invoice_template_id: int = field(default_factory=lambda: _int("INVOICE_TEMPLATE_ID", 0))
    # Срок оплаты по умолчанию, если у исходного счёта его нет
    invoice_pay_days: int = field(default_factory=lambda: _int("INVOICE_PAY_DAYS", 5))

    # Модель: openai или anthropic. По умолчанию — та, чей ключ задан.
    llm_provider: str = field(default_factory=lambda: (
        os.getenv("LLM_PROVIDER", "").strip().lower()
        or ("openai" if os.getenv("OPENAI_API_KEY", "").strip() else "anthropic")))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip())
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-5.6-terra"))
    # responses (рекомендуется: модель «думает» и вызывает функции) или chat (Chat Completions)
    openai_api_mode: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_MODE", "responses").strip().lower() or "responses")
    # none/low/medium/high; пусто = low для responses, none для chat
    # (в chat у GPT-5.x функции работают только с none)
    openai_reasoning_effort: str = field(
        default_factory=lambda: os.getenv("OPENAI_REASONING_EFFORT", "").strip())
    # Для прокси или совместимого API; пусто = api.openai.com
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "").strip())
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "").strip())
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"))

    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Irkutsk"))

    def __post_init__(self) -> None:
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai, но не задан OPENAI_API_KEY")
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise RuntimeError("Не задан ни OPENAI_API_KEY, ни ANTHROPIC_API_KEY")
        if self.llm_provider not in ("openai", "anthropic"):
            raise RuntimeError("LLM_PROVIDER должен быть openai или anthropic")

    @property
    def bitrix_portal_url(self) -> str:
        parts = urlsplit(self.bitrix_webhook)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def webhook_url(self) -> str:
        return self.webhook_base_url + self.webhook_path


settings = Settings()
