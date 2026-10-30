"""Client-only Telegram catalog. No supplier prices or internal comments are exposed."""
import asyncio
import logging
import os
import re

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
from catalog import CATEGORIES, NEARBY, filter_offers, read_offers, save_request

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "").strip()
ALLOWED_IDS = {int(s) for s in os.getenv("ALLOWED_USER_IDS", "").split(",") if s.strip().isdigit()}
VAT = ("Любой", "С НДС", "Без НДС", "По согласованию")


def buttons(rows):
    return Markup([[Button(title, callback_data=key) for title, key in row] for row in rows])


def home():
    return buttons([[("По направлению", "menu:dir")],
                    [("По условиям", "menu:vat")],
                    [("Не нашёл нужное", "menu:missing")]])


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
                await context.bot.send_message(
                    chat_id=MANAGER_CHAT_ID,
                    text=(f"Новая заявка: {request_id}\nТип: {kind}\n"
                          f"ID предложения: {chosen.id if chosen else 'нет'}\n"
                          f"Telegram ID: {update.effective_user.id}\n"
                          f"ИНН клиента: {state['lead_inn']}\nСумма: {rub(amount)}\n"
                          f"Комментарий: {state['lead_comment'][:600]}")
                )
            except Exception:
                log.exception("Request %s saved but manager was not notified", request_id)
        state.clear()
        await msg.reply_text(f"Заявка №{request_id} сохранена.", reply_markup=home())
    else:
        await msg.reply_text("Начните поиск заново.", reply_markup=home())


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    if not permitted(update):
        await reject(update)
        return
    state = context.user_data
    txt = (update.effective_message.text or "").strip()
    stage = state.get("stage")
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
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_error_handler(on_error)
    log.info("Starting catalog in %s mode", "test" if TEST_MODE else "private")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
