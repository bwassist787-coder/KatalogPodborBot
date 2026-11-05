"""Client-only Telegram catalog. No supplier prices or internal comments are exposed."""
import asyncio
import logging
import os
import re

import manager_chat
import case_store

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
    return buttons([[("По направлению", "menu:dir")],
                    [("По условиям", "menu:vat")],
                    [("Не нашёл нужное", "menu:missing")],
                    [("Предложить компанию", "menu:propose")]])


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not permitted(update):
        await reject(update)
        return
    context.user_data.clear()
    prefix = "Тестовый каталог. Все предложения демонстрационные.\n\n" if TEST_MODE else ""
    await update.effective_message.reply_text(prefix + "Выберите способ поиска:", reply_markup=home())


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
    q = update.callback_query
    if update.effective_chat.type != ChatType.PRIVATE or not permitted(update):
        await reject(update)
        return
    await q.answer()
    state, code = context.user_data, q.data or ""
    msg = q.message
    if code == "menu:start":
        state.clear()
        await msg.reply_text("Выберите способ поиска:", reply_markup=home())
    elif code == "menu:dir":
        state.clear()
        await msg.reply_text("Выберите направление:", reply_markup=buttons(
            [[(name, f"cat:{i}")] for i, name in enumerate(CATEGORIES)]
            + [[("Главное меню", "menu:start")]]
        ))
    elif code == "menu:vat":
        state.clear()
        await msg.reply_text("Выберите НДС:", reply_markup=buttons(
            [[(name, f"vat:{i}")] for i, name in enumerate(VAT)]
            + [[("Главное меню", "menu:start")]]
        ))
    elif code.startswith("cat:"):
        try:
            index = int(code[4:])
            if not 0 <= index < len(CATEGORIES):
                raise ValueError
            category = CATEGORIES[index]
        except ValueError:
            await msg.reply_text("Неизвестная категория. Начните заново.", reply_markup=home())
            return
        state.clear()
        state["category"] = category
        try:
            subcategories = sorted({o.subcategory for o in filter_offers(await offers(), category=category)
                                    if o.subcategory})
        except Exception:
            log.exception("Catalog failed")
            await msg.reply_text("Каталог временно недоступен.")
            return
        if not subcategories:
            await show_results(msg, context)
            return
        state["sub_options"] = subcategories[:40]
        await msg.reply_text("Выберите подкатегорию:", reply_markup=buttons(
            [[(name[:55], f"sub:{i}")] for i, name in enumerate(state["sub_options"])]
            + [[("Все предложения", "sub:all")], [("Главное меню", "menu:start")]]
        ))
    elif code.startswith("sub:"):
        if "category" not in state:
            await msg.reply_text("Выберите направление заново.", reply_markup=home())
            return
        if code == "sub:all":
            state.pop("subcategory", None)
        else:
            try:
                index = int(code[4:])
                if not 0 <= index < len(state["sub_options"]):
                    raise ValueError
                state["subcategory"] = state["sub_options"][index]
            except (KeyError, ValueError):
                await msg.reply_text("Начните поиск заново.", reply_markup=home())
                return
        await show_results(msg, context)
    elif code.startswith("vat:"):
        try:
            index = int(code[4:])
            if not 0 <= index < len(VAT):
                raise ValueError
            state["vat"] = VAT[index]
        except ValueError:
            await msg.reply_text("Начните поиск заново.", reply_markup=home())
            return
        state["stage"] = "filter_amount"
        await msg.reply_text("Введите сумму в рублях, например 500000:",
                             reply_markup=buttons([[("Любая сумма", "amount:any")]]))
    elif code == "amount:any":
        if state.get("stage") != "filter_amount":
            await msg.reply_text("Начните поиск заново.", reply_markup=home())
            return
        state.pop("stage", None)
        state.pop("filter_amount", None)
        await show_results(msg, context)
    elif code.startswith("page:"):
        try:
            page = int(code[5:])
        except ValueError:
            return
        if not any(k in state for k in ("category", "vat")):
            await msg.reply_text("Начните поиск заново.", reply_markup=home())
            return
        await show_results(msg, context, page)
    elif code.startswith("pick:"):
        if not any(k in state for k in ("category", "vat")):
            await msg.reply_text("Начните поиск заново.", reply_markup=home())
            return
        try:
            index = int(code[5:])
            filtered = by_state(await offers(), state)
            if not 0 <= index < len(filtered):
                raise ValueError
            selected = filtered[index]
        except ValueError:
            await msg.reply_text("Предложение изменилось. Начните заново.", reply_markup=home())
            return
        except Exception:
            log.exception("Could not verify offer")
            await msg.reply_text("Не удалось проверить предложение. Попробуйте позже.")
            return
        state["selected_id"] = selected.id
        state["stage"] = "lead_inn"
        await msg.reply_text("Введите ИНН вашей компании (10 или 12 цифр):")
    elif code == "menu:propose":
        state.clear()
        state["request_type"] = "Предложение компании"
        state["stage"] = "prop_name"
        await msg.reply_text("Введите название компании, которую предлагаете:")
    elif code.startswith("prop:cat:"):
        if state.get("stage") != "prop_category":
            await msg.reply_text("Начните предложение заново.", reply_markup=home())
            return
        try:
            state["prop_category"] = CATEGORIES[int(code.split(":")[-1])]
        except (ValueError, IndexError):
            await msg.reply_text("Неизвестное направление.")
            return
        state["stage"] = "prop_vat"
        await msg.reply_text("Условия НДС:", reply_markup=buttons(
            [[(v, "prop:vat:" + str(i))] for i, v in enumerate(VAT[1:])]))
    elif code.startswith("prop:vat:"):
        if state.get("stage") != "prop_vat":
            await msg.reply_text("Начните предложение заново.", reply_markup=home())
            return
        try:
            state["prop_vat"] = VAT[1:][int(code.split(":")[-1])]
        except (ValueError, IndexError):
            await msg.reply_text("Неизвестный вариант НДС.")
            return
        state["stage"] = "prop_amount"
        await msg.reply_text("На какую сумму можете предложить компанию? Введите число в рублях:")
    elif code == "menu:missing":
        state.clear()
        state["request_type"] = "Не нашёл нужное"
        state["stage"] = "lead_inn"
        await msg.reply_text("Введите ИНН вашей компании (10 или 12 цифр):")
    elif code == "lead:send":
        if state.get("stage") != "confirm":
            await msg.reply_text("Начните оформление заново.", reply_markup=home())
            return
        state["stage"] = "saving"
        chosen = None
        if state.get("selected_id"):
            try:
                chosen = next((o for o in await offers() if o.id == state["selected_id"]), None)
            except Exception:
                log.exception("Cannot recheck offer")
                state["stage"] = "confirm"
                await msg.reply_text("Проверка предложения недоступна. Повторите позже.")
                return
            if chosen is None:
                state.clear()
                await msg.reply_text("Предложение снято с каталога. Заявка не отправлена.",
                                     reply_markup=home())
                return
        amount = state["lead_amount"]
        if chosen and ((chosen.min_sum is not None and amount < chosen.min_sum)
                       or (chosen.max_sum is not None and amount > chosen.max_sum)):
            state.clear()
            await msg.reply_text("Условия предложения изменились. Заявка не отправлена.",
                                 reply_markup=home())
            return
        kind = state.get("request_type", "Заявка")
        try:
            request_id = await asyncio.to_thread(
                save_request, update.effective_user.id, update.effective_user.username or "",
                kind, chosen, state["lead_inn"], amount, state["lead_comment"]
            )
        except Exception:
            log.exception("Request was not saved")
            state["stage"] = "confirm"
            await msg.reply_text("Не удалось сохранить заявку. Повторите отправку.")
            return
        if MANAGER_CHAT_ID:
            try:
                summary = (
                    f"ID предложения: {chosen.id if chosen else 'нет'}\n"
                    f"Telegram ID: {update.effective_user.id}\n"
                    f"ИНН клиента: {state['lead_inn']}\n"
                    f"Сумма: {rub(amount)}\n"
                    f"Комментарий: {state['lead_comment'][:600]}")
                await asyncio.to_thread(
                    case_store.register, request_id, update.effective_user.id,
                    int(MANAGER_CHAT_ID), kind, summary)
                record = case_store.find(request_id)
                posted = await context.bot.send_message(
                    chat_id=int(MANAGER_CHAT_ID),
                    text=manager_chat.card(record),
                    reply_markup=manager_chat.manager_keyboard(record))
                case_store.set_group_message(request_id, posted.message_id)
            except Exception:
                log.exception("Request %s saved but manager was not notified", request_id)
        state.clear()
        await msg.reply_text(
            f"Заявка №{request_id} сохранена и отправлена менеджерам.\n"
            "Когда менеджер возьмёт её в работу, придёт уведомление.",
            reply_markup=manager_chat.customer_keyboard(request_id))
    else:
        await msg.reply_text("Начните поиск заново.", reply_markup=home())


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await manager_chat.manager_text(update, context)
        return
    if not permitted(update):
        await reject(update)
        return
    state = context.user_data
    txt = (update.effective_message.text or "").strip()
    stage = state.get("stage")
    if stage == "client_question":
        await manager_chat.client_question(update, context)
        return
    if stage == "prop_name":
        if not 3 <= len(txt) <= 90:
            await update.message.reply_text("Введите название от 3 до 90 символов.")
            return
        state["prop_name"] = txt
        state["stage"] = "prop_inn"
        await update.message.reply_text("Введите ИНН предлагаемой компании (10 или 12 цифр):")
        return
    if stage == "prop_inn":
        if not re.fullmatch(r"(?:\d{10}|\d{12})", txt):
            await update.message.reply_text("ИНН должен содержать 10 или 12 цифр.")
            return
        state["lead_inn"] = txt
        state["stage"] = "prop_category"
        await update.message.reply_text(
            "Выберите направление компании:",
            reply_markup=buttons([[(name, f"prop:cat:{i}")]
                                  for i, name in enumerate(CATEGORIES)]))
        return
    if stage == "prop_amount":
        cleaned = txt.replace(" ", "").replace("₽", "")
        if not cleaned.isdecimal() or int(cleaned) <= 0:
            await update.message.reply_text("Введите положительную сумму цифрами.")
            return
        state["lead_amount"] = int(cleaned)
        state["stage"] = "prop_comment"
        await update.message.reply_text("Напишите условия предложения (до 300 символов):")
        return
    if stage == "prop_comment":
        if not 1 <= len(txt) <= 300:
            await update.message.reply_text("Напишите условия до 300 символов.")
            return
        state["lead_comment"] = (
            f"Компания: {state['prop_name']}; "
            f"Направление: {state['prop_category']}; "
            f"НДС: {state['prop_vat']}; "
            f"Условия: {txt}")
        state["stage"] = "confirm"
        await update.message.reply_text(
            "Проверьте предложение:\n"
            f"{state['lead_comment']}\n"
            f"ИНН: {state['lead_inn']}\n"
            f"Сумма: {rub(state['lead_amount'])}\n"
            "Отправить менеджерам? (в каталог автоматически не попадёт)",
            reply_markup=buttons([[("Подтвердить", "lead:send")],
                                  [("Отмена", "menu:start")]]))
        return
    if stage in ("filter_amount", "lead_amount"):
        cleaned = txt.replace(" ", "").replace("₽", "")
        if not cleaned.isdecimal() or int(cleaned) <= 0:
            await update.message.reply_text("Введите сумму цифрами, например 500000.")
            return
        if stage == "filter_amount":
            state["filter_amount"] = int(cleaned)
            state.pop("stage", None)
            await show_results(update.message, context)
        else:
            state["lead_amount"] = int(cleaned)
            state["stage"] = "lead_comment"
            await update.message.reply_text("Напишите комментарий (или «-», если его нет):")
    elif stage == "lead_inn":
        if not re.fullmatch(r"(?:\d{10}|\d{12})", txt):
            await update.message.reply_text("ИНН: только 10 или 12 цифр. Повторите ввод.")
            return
        state["lead_inn"] = txt
        state["stage"] = "lead_amount"
        await update.message.reply_text("Введите сумму заявки в рублях:")
    elif stage == "lead_comment":
        if len(txt) > 600 or (state.get("request_type") == "Не нашёл нужное" and txt == "-"):
            await update.message.reply_text("Уточните, что нужно найти (до 600 символов).")
            return
        state["lead_comment"] = "" if txt == "-" else txt
        state["stage"] = "confirm"
        await update.message.reply_text(
            f"Проверьте заявку:\nИНН: {state['lead_inn']}\n"
            f"Сумма: {rub(state['lead_amount'])}\n"
            f"Комментарий: {state['lead_comment'] or 'нет'}\nОтправить?",
            reply_markup=buttons([[("Подтвердить", "lead:send")],
                                  [("Отмена", "menu:start")]])
        )
    else:
        await update.message.reply_text("Откройте меню командой /start.", reply_markup=home())


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
    app.add_handler(CommandHandler("my", manager_chat.my_cases))
    app.add_handler(CallbackQueryHandler(manager_chat.manager_callback, pattern=r"^mgr:"))
    app.add_handler(CallbackQueryHandler(manager_chat.customer_callback, pattern=r"^cli:"))
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
