"""Точка входа: Telegram-бот на вебхуке (aiohttp) за nginx."""
from __future__ import annotations

import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .agent import Agent
from .bitrix import Bitrix, BitrixError
from .config import settings
from .invoices import Draft, InvoiceService
from .staff import Employee, StaffDirectory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mybot")

bx = Bitrix(settings.bitrix_webhook)
staff = StaffDirectory(bx, settings.telegram_user_field)
invoices = InvoiceService(bx, settings.invoice_entity_type_id, settings.invoice_template_id,
                          settings.invoice_pay_days, settings.timezone)
agent = Agent(settings, invoices)
router = Router()

DENIED = ("Бот только для сотрудников. Попросите администратора вписать ваш Telegram "
          "в профиль на портале (поле «telega»): <code>{ident}</code>")


async def who(user) -> Employee | None:
    try:
        return await staff.find(user.id, user.username)
    except BitrixError:
        log.exception("Не удалось получить сотрудников")
        return None


def ident(user) -> str:
    return f"@{user.username}" if user.username else str(user.id)


def fmt_money(x: float, cur: str | None) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {cur or ''}".strip()


async def draft_card(d: Draft) -> tuple[str, InlineKeyboardMarkup]:
    begin, close = invoices.dates_for(d.source)
    cur = d.source.get("currencyId")
    lines = [
        "<b>Черновик счёта</b>",
        f"Клиент: {html.escape(await invoices.company_title(d.source.get('companyId')))}",
        f"Название: {html.escape(d.title)}",
        f"Дата: {begin:%d.%m.%Y}, оплатить до {close:%d.%m.%Y}",
        "",
    ]
    for n, r in enumerate(d.rows, 1):
        qty = float(r.get("quantity") or 0)
        qty_s = f"{qty:g}"
        lines.append(f"{n}. {html.escape(str(r.get('productName')))} — {qty_s} × "
                     f"{fmt_money(float(r.get('price') or 0), cur)}")
    lines += ["", f"<b>Итого: {fmt_money(d.total, cur)}</b>",
              f"По образцу счёта №{d.source.get('id')} «{html.escape(str(d.source.get('title') or ''))}» "
              f"от {'.'.join(reversed(str(d.source.get('begindate') or '')[:10].split('-')))}, "
              f"{fmt_money(float(d.source.get('opportunity') or 0), cur)}"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Создать", callback_data=f"inv:ok:{d.id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"inv:no:{d.id}"),
    ]])
    return "\n".join(lines), kb


@router.message(CommandStart())
async def on_start(msg: Message) -> None:
    emp = await who(msg.from_user)
    if not emp:
        await msg.answer(DENIED.format(ident=ident(msg.from_user)))
        return
    await msg.answer(f"Привет, {html.escape(emp.name)}! Пиши задачу обычным текстом, например:\n"
                     "<i>выстави счёт Русирке как в прошлый раз за поддержку, только месяц поменяй</i>\n\n"
                     "/new — начать разговор заново\n/print 365 — печатная форма счёта по номеру")


@router.message(Command("new"))
async def on_new(msg: Message) -> None:
    agent.reset(msg.chat.id)
    await msg.answer("Ок, начинаем с чистого листа.")


async def send_print_form(message: Message, bot: Bot, invoice_id: int, caption: str) -> None:
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_DOCUMENT)
    try:
        content, ext = await invoices.print_form(invoice_id)
    except BitrixError as exc:
        log.exception("Печатная форма")
        await message.answer(f"Печатная форма не собралась: {html.escape(str(exc))}")
        return
    note = "" if ext == "pdf" else "\n(PDF портал не отдал — прислал Word)"
    await message.answer_document(
        BufferedInputFile(content, filename=f"Счёт {invoice_id}.{ext}"),
        caption=f"{html.escape(caption)}{note}",
    )


@router.message(Command("print"))
async def on_print(msg: Message, bot: Bot) -> None:
    emp = await who(msg.from_user)
    if not emp:
        await msg.answer(DENIED.format(ident=ident(msg.from_user)))
        return
    arg = (msg.text or "").split(maxsplit=1)[1:]
    if not arg or not arg[0].strip().isdigit():
        await msg.answer("Укажи номер счёта: <code>/print 365</code>")
        return
    await send_print_form(msg, bot, int(arg[0]), f"Счёт №{arg[0]}")


@router.message(F.text)
async def on_text(msg: Message, bot: Bot) -> None:
    emp = await who(msg.from_user)
    if not emp:
        await msg.answer(DENIED.format(ident=ident(msg.from_user)))
        return
    await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    try:
        reply = await agent.handle(msg.chat.id, emp, msg.text)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки сообщения")
        await msg.answer("Что-то сломалось при обработке, подробности в логе сервера.")
        return
    if reply.text:
        await msg.answer(html.escape(reply.text))
    if reply.draft:
        text, kb = await draft_card(reply.draft)
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("inv:"))
async def on_invoice_button(cb: CallbackQuery, bot: Bot) -> None:
    _, action, draft_id = cb.data.split(":", 2)
    emp = await who(cb.from_user)
    d = invoices.drafts.get(draft_id)
    if not emp or not d or d.employee_id != emp.id:
        await cb.answer("Черновик устарел или чужой", show_alert=True)
        return
    if action == "no":
        invoices.drafts.pop(draft_id, None)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer("Отменено")
        return

    await cb.answer("Создаю счёт…")
    await cb.message.edit_reply_markup(reply_markup=None)
    try:
        new_id = await invoices.create(d)
    except BitrixError as exc:
        log.exception("Создание счёта")
        await cb.message.answer(f"Битрикс не дал создать счёт: {html.escape(str(exc))}")
        return

    link = f"{settings.bitrix_portal_url}/crm/type/{settings.invoice_entity_type_id}/details/{new_id}/"
    await cb.message.answer(f"Счёт №{new_id} создан: {link}\nФормирую печатную форму…")
    await send_print_form(cb.message, bot, new_id, d.title)


async def on_startup(bot: Bot) -> None:
    await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret,
                          allowed_updates=["message", "callback_query"], drop_pending_updates=True)
    log.info("Вебхук установлен: %s", settings.webhook_url)


async def on_shutdown(bot: Bot) -> None:
    await bx.close()


def main() -> None:
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.webhook_secret).register(
        app, path=settings.webhook_path)
    app.router.add_get(settings.webhook_path.rsplit("/", 1)[0] + "/health",
                       lambda _: web.Response(text="ok"))
    setup_application(app, dp, bot=bot)
    web.run_app(app, host=settings.listen_host, port=settings.listen_port)


if __name__ == "__main__":
    main()
