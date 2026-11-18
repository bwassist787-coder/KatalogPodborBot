"""Client-only Telegram catalog. No supplier prices or internal comments are exposed."""
import asyncio
import logging
import os
import re

import manager_chat
import case_store
import forum_route
import private_client
import group_quick_catalog
import group_dialog

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
from catalog import CATEGORIES, NEARBY, filter_offers, read_offers, save_request

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Never log Bot API token in URLs
TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "").strip()
ALLOWED_IDS = {int(s) for s in os.getenv("ALLOWED_USER_IDS", "").split(",") if s.strip().isdigit()}
VAT = ("Любой", "С НДС", "Без НДС", "По согласованию")


def buttons(rows):
    return Markup([[Button(title, callback_data=key) for title, key in row] for row in rows])


def home():
    return buttons([[("Подобрать компанию", "browse:start")],
                    [("Мои заявки", "dash:list:active")],
                    [("Предложить компанию", "menu:propose")]])


def new_request_menu():
    return buttons([[("Описать своими словами", "menu:free")],
                    [("Подобрать по фильтрам", "menu:filters")],
                    [("Главное меню", "menu:start")]])


def filter_menu():
    return buttons([[("По направлению", "menu:dir")],
                    [("По условиям", "menu:vat")],
                    [("Не нашёл нужное", "menu:missing")],
                    [("Главное меню", "menu:start")]])


def permitted(update):
    return bool(update.effective_user and
                (update.effective_user.id in ALLOWED_IDS or
                 (not TEST_MODE and not ALLOWED_IDS)))


async def reject(update):
    if update.callback_query:
        await update.callback_query.answer("Доступ к тесту ограничен", show_alert=True)
    else:
        await update.effective_message.reply_text(
            f"Тестовый доступ ограничен. Ваш Telegram ID: {update.effective_user.id}"
        )


def reset_flow(state):
    """Clear the previous intake flow; no implied group routing."""
    state.clear()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Client features are exclusively available in the allowlisted group."""
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Подбор компаний и создание заявок доступны только в вашем "
            "клиентском Telegram-чате. Откройте группу и нажмите "
            "«Подобрать компанию» или «Предложить компанию». "
            "В личных сообщениях бот заявки не принимает.")
        return
    if not forum_route.configured() or chat.id != forum_route.client_chat_id():
        return
    if not await forum_route.member_allowed(context.bot, update.effective_user.id):
        return
    text, keyboard = forum_route.button_menu("general")
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")



async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow an authorized operator to identify the manager group safely."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None or user.id not in ALLOWED_IDS:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text(
            "Добавьте бота в закрытую группу менеджеров и отправьте /chatid там."
        )
        return
    await update.effective_message.reply_text(
        f"Chat ID для MANAGER_CHAT_ID: {chat.id}"
    )



async def topicid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show forum routing IDs to an allowlisted operator; read-only."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message or user.id not in ALLOWED_IDS:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text("Отправьте /topicid в нужной теме тестовой группы.")
        return
    thread = message.message_thread_id
    # The General topic often has thread ID 1; Telegram may omit it.
    topic = str(thread) if thread is not None else "General / не указан Telegram"
    await message.reply_text(
        f"Группа: {chat.title or 'без названия'}\n"
        f"Chat ID: {chat.id}\n"
        f"Topic ID: {topic}"
    )


async def setup_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-time operator action: publish clickable menus to three test topics."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if (not user or not chat or not message or
            user.id not in ALLOWED_IDS or
            chat.id != forum_route.client_chat_id()):
        return
    if not forum_route.configured():
        await message.reply_text("Маршрут тестовой группы не настроен. Нужна проверка.")
        return
    try:
        result = await forum_route.install_menus(context.bot)
    except Exception:
        log.exception("Could not publish test forum menus")
        await message.reply_text(
            "Не удалось опубликовать все кнопки. Проверьте темы и права бота.")
        return
    await message.reply_text(
        "Кнопки опубликованы в General, «Предложения от Нас» и "
        "«Предложения от Клиента». Закрепите эти сообщения в темах. "
        "Повторять настройку не нужно."
    )


async def offers():
    return await asyncio.to_thread(read_offers)


def by_state(rows, state):
    filtered = filter_offers(rows, category=state.get("category"),
                            vat=state.get("vat"), amount=state.get("filter_amount"))
    if state.get("subcategory"):
        filtered = [o for o in filtered if o.subcategory == state["subcategory"]]
    return filtered


def rub(amount):
    return f"{amount:,}".replace(",", " ") + " ₽" if amount is not None else "не указано"


async def show_results(message, context, page=0):
    try:
        all_offers = await offers()
    except Exception:
        log.exception("Catalog is unavailable")
        await message.reply_text("Каталог временно недоступен. Попробуйте позже.")
        return
    state = context.user_data
    found = by_state(all_offers, state)
    if not found:
        rows = []
        for name in NEARBY.get(state.get("category"), [])[:3]:
            if filter_offers(all_offers, category=name, vat=state.get("vat"),
                             amount=state.get("filter_amount")):
                rows.append([("Похожее: " + name, f"cat:{CATEGORIES.index(name)}")])
        rows += [[("Оставить запрос", "menu:missing")],
                 [("Главное меню", "menu:start")]]
        await message.reply_text("Подходящих предложений пока нет.", reply_markup=buttons(rows))
        return
    page = max(0, min(page, (len(found) - 1) // 3))
    first = page * 3
    batch = found[first:first+3]
    cards = []
    actions = []
    for i, offer in enumerate(batch, first):
        cards.append(
            f"{i+1}. {offer.company[:80]}\n"
            f"Направление: {offer.category[:70]} / {offer.subcategory[:60]}\n"
            f"Назначение: {offer.purpose[:220]}\n"
            f"НДС: {offer.vat[:40]}\n"
            f"Сумма: {rub(offer.min_sum)} — {rub(offer.max_sum)}\n"
            f"Цена: {offer.client_percent[:40]}\n"
            f"Срок: {offer.days[:35]} дн.\n"
            f"Условия: {offer.terms[:180] or 'по согласованию'}"
        )
        actions.append([(f"Выбрать №{i+1}", f"pick:{i}")])
    nav = []
    if page:
        nav.append(("Назад", f"page:{page-1}"))
    if first + 3 < len(found):
        nav.append(("Далее", f"page:{page+1}"))
    if nav:
        actions.append(nav)
    actions.append([("Главное меню", "menu:start")])
    await message.reply_text(
        (f"Найдено: {len(found)}\n\n" + "\n\n".join(cards))[:4000],
        reply_markup=buttons(actions)
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Old private catalog buttons never create requests."""
    q = update.callback_query
    if not q:
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        await q.answer(
            "Подбор доступен только в клиентской группе.", show_alert=True)
    else:
        await q.answer(
            "Это старое меню. Откройте обновлённое меню группы.",
            show_alert=True)


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Заявки и подбор компаний оформляются только в клиентской группе. "
            "Перейдите в чат и используйте кнопки меню.")
        return
    if (forum_route.configured() and
            chat.id == forum_route.client_chat_id()):
        if await group_dialog.text_input(update, context):
            return
        return
    if chat.id == manager_chat.group_id():
        await manager_chat.manager_text(update, context)


async def on_error(update, context):
    log.error("Unexpected error: %s", context.error, exc_info=context.error)


def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is missing. Set it in your hosting secrets.")
    if not TEST_MODE and not MANAGER_CHAT_ID:
        raise SystemExit("Private mode requires MANAGER_CHAT_ID")
    if not TEST_MODE and not (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    ):
        raise SystemExit("Private mode requires a Google service account")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("topicid", topicid))
    app.add_handler(CommandHandler("setup_topics", setup_topics))
    app.add_handler(CommandHandler("catalog", forum_route.entry))
    app.add_handler(CommandHandler("offer", forum_route.entry))
    app.add_handler(CommandHandler("my", manager_chat.my_cases))
    app.add_handler(CallbackQueryHandler(group_quick_catalog.callback, pattern=r"^qg:"))
    app.add_handler(CallbackQueryHandler(group_dialog.callback, pattern=r"^gf:"))
    app.add_handler(CallbackQueryHandler(manager_chat.manager_callback, pattern=r"^mgr:"))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_error_handler(on_error)
    log.info("Starting catalog in %s mode", "test" if TEST_MODE else "private")
    webhook_base = os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/")
    if webhook_base:
        if not webhook_base.startswith("https://"):
            raise SystemExit("WEBHOOK_BASE_URL must be HTTPS")
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        if not secret:
            raise SystemExit("WEBHOOK_SECRET is required for webhook mode")
        port = int(os.getenv("PORT", "10000"))
        from webhook_server import run_server
        run_server(app, port=port, webhook_url=f"{webhook_base}/telegram", secret=secret,
                   on_ready=manager_chat.reminder_loop)
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
