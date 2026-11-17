"""Five-company, category-first catalog for verified private client conversations.

Only customer-facing prices from 'Каталог_бот' (K, Цена %) are rendered.
Does not ask for client INN, payment amount or VAT before showing options.
Every request is scoped to a Telegram-verified client group and is
sent only to the configured internal manager group.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import case_store
import manager_chat
from catalog import filter_offers, read_offers, save_request

log = logging.getLogger(__name__)
PAGE_SIZE = 5

# Display name -> exact approved catalog category; do not guess using similar names.
CATEGORIES = (
    ("Строительство и СМР", "Строительство и СМР"),
    ("Стройматериалы", "Стройматериалы"),
    ("Техника и оборудование", "Оборудование"),
    ("Электроника", "Электроника"),
    ("IT и ПО", "IT и ПО"),
    ("Транспорт и логистика", "Транспорт и логистика"),
    ("Авто и запчасти", "Авто и запчасти"),
    ("Товары", "Товары"),
    ("Деловые услуги", "Деловые услуги"),
    ("Аренда", "Аренда"),
)


def markup(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(title, callback_data=data) for title, data in row]
        for row in rows
    ])


def selected_group(offers, category):
    """Only exact-category offers; show a distinct company once per five-card view."""
    eligible = filter_offers(offers, category=category)
    result = []
    seen_inns = set()
    for offer in eligible:
        if offer.inn in seen_inns:
            continue
        seen_inns.add(offer.inn)
        result.append(offer)
    return result


def _current(state, nonce):
    session = state.get("quick_catalog")
    return session if session and session.get("nonce") == nonce else None


def _token():
    return secrets.token_hex(4)


def _card(offer, number):
    return (f"{number}. {offer.company[:95]}\n"
            f"ИНН: {offer.inn}\n"
            f"Цена: {offer.client_percent[:32]}\n"
            f"Срок: {offer.days[:25]} дн.")


async def _replace_or_reply(query, text, reply_markup):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except Exception:
        log.exception("Cannot edit quick catalog message; sending a new one")
        await query.message.reply_text(text, reply_markup=reply_markup)


async def start(message, context):
    """Start a new browse flow; old inline buttons automatically become invalid."""
    nonce = _token()
    context.user_data.clear()
    context.user_data["quick_catalog"] = {
        "nonce": nonce, "stage": "category", "category": None, "page": 0,
        "page_offer_ids": [], "selected_id": None,
    }
    rows = [
        [(label, f"browse:cat:{nonce}:{i}")]
        for i, (label, _) in enumerate(CATEGORIES)
    ]
    rows.append([("Мои заявки", "dash:list:active")])
    rows.append([("Главное меню", "menu:start")])
    await message.reply_text(
        "Выберите категорию компании:", reply_markup=markup(rows)
    )


async def _render_page(query, context, session, page):
    try:
        all_offers = await asyncio.to_thread(read_offers)
    except Exception:
        log.exception("Catalog unavailable")
        await query.message.reply_text(
            "Каталог временно недоступен. Попробуйте позже.")
        return
    offers = selected_group(all_offers, session["category"])
    count = len(offers)
    last_page = max(0, (count - 1) // PAGE_SIZE)
    if page < 0 or page > last_page:
        await query.answer("Больше компаний нет.", show_alert=True)
        return
    page_items = offers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    session.update(
        stage="results", page=page,
        page_offer_ids=[offer.id for offer in page_items],
        selected_id=None,
    )
    nonce = session["nonce"]
    if not page_items:
        text = (f"{session['label']}\n\n"
                "Пока нет доступных компаний в этой категории. "
                "Можно обратиться к менеджеру.")
    else:
        start_no = page * PAGE_SIZE + 1
        end_no = start_no + len(page_items) - 1
        text = (f"{session['label']}\n"
                f"Компании {start_no}–{end_no} из {count}\n\n"
                + "\n\n".join(
                    _card(item, start_no+i) for i, item in enumerate(page_items)
                ))
    rows = []
    for i in range(len(page_items)):
        rows.append([(
            f"Выбрать №{page * PAGE_SIZE + i + 1}",
            f"browse:pick:{nonce}:{page}:{i}",
        )])
    navigation = []
    if page > 0:
        navigation.append(("Назад к 5 компаниям", f"browse:page:{nonce}:{page}:{page-1}"))
    if (page + 1) * PAGE_SIZE < count:
        navigation.append(("Ещё компании", f"browse:page:{nonce}:{page}:{page+1}"))
    if navigation:
        rows.append(navigation)
    rows.append([("Нужен менеджер", f"browse:manager:{nonce}")])
    rows.append([("Другие категории", "browse:start")])
    await _replace_or_reply(query, text, markup(rows))


async def _confirmation(query, session, offer):
    nonce = session["nonce"]
    session["stage"] = "confirm"
    session["choice"] = "company" if offer else "manager"
    session["selected_id"] = offer.id if offer else None
    if offer:
        title = "Вы выбрали компанию:\n\n" + _card(offer, 1)[3:]
    else:
        title = ("Обращение к менеджеру\n\n"
                 f"Категория: {session['label']}\n"
                 "Менеджер поможет подобрать компанию и уточнить условия.")
    await _replace_or_reply(
        query, title + "\n\nОтправить заявку менеджеру?",
        markup([
            [("Подтвердить", f"browse:send:{nonce}")],
            [("Вернуться к компаниям", f"browse:back:{nonce}")],
            [("Другие категории", "browse:start")],
        ]))


async def _send_request(query, context, group, session):
    """Register once, then notify managers. Do not retry after uncertain save."""
    from bot import MANAGER_CHAT_ID
    if not MANAGER_CHAT_ID or not group.get("chat_id"):
        await query.message.reply_text(
            "Маршрут менеджера не подтверждён. Заявка не отправлена.")
        return
    nonce = session["nonce"]
    session["stage"] = "saving"
    choice = session.get("choice")
    category = session.get("category")
    offer = None
    if choice == "company":
        try:
            offers = await asyncio.to_thread(read_offers)
            offer = next((x for x in selected_group(offers, category)
                          if x.id == session.get("selected_id")), None)
        except Exception:
            log.exception("Unable to recheck catalog offer")
            session["stage"] = "confirm"
            await query.message.reply_text(
                "Не удалось перепроверить компанию. Попробуйте ещё раз.")
            return
        if offer is None:
            session["stage"] = "results"
            await query.message.reply_text(
                "Компания больше недоступна в каталоге. Выберите другую.",
                reply_markup=markup([
                    [("К списку", f"browse:back:{nonce}")],
                ]))
            return
    elif choice != "manager":
        session["stage"] = "confirm"
        await query.message.reply_text("Выберите компанию или менеджера заново.")
        return
    summary = (
        ("Выбранная компания\n" + _card(offer, 1)[3:])
        if offer else f"Требуется подбор менеджером\nКатегория: {session['label']}"
    )
    try:
        request_id = await asyncio.to_thread(
            save_request,
            query.from_user.id,
            query.from_user.username or "",
            "Заявка" if offer else "Не нашёл нужное",
            offer, "", None,
            f"Категория: {session['label']}; "
            + ("Выбор компании: " + offer.id if offer else "Нужен менеджер"),
        )
    except Exception:
        session["stage"] = "confirm"
        log.exception("Cannot save quick request")
        await query.message.reply_text("Не удалось сохранить заявку. Попробуйте позже.")
        return

    try:
        await asyncio.to_thread(
            case_store.register,
            request_id, query.from_user.id, int(MANAGER_CHAT_ID),
            "Заявка" if offer else "Не нашёл нужное",
            "Telegram ID: " + str(query.from_user.id) + "\n" + summary,
            origin_username=("@" + query.from_user.username
                             if query.from_user.username
                             else query.from_user.full_name[:80]),
            client_summary=summary,
            client_group_id=group["chat_id"],
            client_group_title=group["title"],
        )
        case = case_store.find(request_id)
        sent = await context.bot.send_message(
            chat_id=int(MANAGER_CHAT_ID),
            text=manager_chat.card(case),
            reply_markup=manager_chat.manager_keyboard(case))
        case_store.set_group_message(request_id, sent.message_id)
    except Exception:
        log.exception("Quick request delivery uncertain: %s", request_id)
        # Prevent user from retrying a request which may already be persisted.
        context.user_data.clear()
        await query.message.reply_text(
            "Заявка сохранена, но доставка менеджеру не подтверждена. "
            "Не оформляйте повторную заявку, потребуется проверка.")
        return

    context.user_data.clear()
    await query.message.reply_text(
        "Заявка отправлена менеджерам. Отслеживать её можно в «Мои заявки».",
        reply_markup=markup([
            [("Мои заявки", "dash:list:active")],
            [("Подобрать ещё компанию", "browse:start")],
        ]),
    )


async def callback(query, context, group):
    """All callbacks are per-user and verified by caller before this handler."""
    data = (query.data or "").split(":")
    if data == ["browse", "start"]:
        await query.answer()
        await start(query.message, context)
        return
    if len(data) < 3:
        await query.answer("Устаревшая кнопка.", show_alert=True)
        return
    action, nonce = data[1:3]
    session = _current(context.user_data, nonce)
    if session is None:
        await query.answer("Кнопка от прошлого подбора. Начните заново.",
                           show_alert=True)
        return
    await query.answer()
    if action == "cat" and session["stage"] == "category" and len(data) == 4:
        try:
            label, category = CATEGORIES[int(data[3])]
        except (ValueError, IndexError):
            await query.message.reply_text("Неизвестная категория.")
            return
        session.update(label=label, category=category)
        await _render_page(query, context, session, 0)
    elif action == "page" and session["stage"] == "results" and len(data) == 5:
        try:
            page, target = int(data[3]), int(data[4])
        except ValueError:
            return
        if page != session["page"] or abs(target - page) != 1:
            await query.message.reply_text("Эта страница устарела.")
            return
        await _render_page(query, context, session, target)
    elif action == "pick" and session["stage"] == "results" and len(data) == 5:
        try:
            page, index = int(data[3]), int(data[4])
            if page != session["page"] or not 0 <= index < len(session["page_offer_ids"]):
                raise ValueError
            offer_id = session["page_offer_ids"][index]
        except ValueError:
            await query.message.reply_text("Компания на этой странице больше не доступна.")
            return
        try:
            current = await asyncio.to_thread(read_offers)
            offer = next((o for o in selected_group(current, session["category"])
                          if o.id == offer_id), None)
        except Exception:
            log.exception("Cannot confirm offered company")
            await query.message.reply_text("Не удалось проверить компанию. Повторите позже.")
            return
        if offer is None:
            await query.message.reply_text("Компания снята с каталога. Выберите другую.")
            return
        await _confirmation(query, session, offer)
    elif action == "manager" and session["stage"] == "results":
        await _confirmation(query, session, None)
    elif action == "back" and session["stage"] in ("confirm", "results"):
        session["stage"] = "results"
        await _render_page(query, context, session, session["page"])
    elif action == "send" and session["stage"] == "confirm":
        await _send_request(query, context, group, session)
    else:
        await query.message.reply_text("Кнопка устарела. Начните подбор заново.",
                                       reply_markup=markup([
                                           [("Подобрать компанию", "browse:start")]
                                       ]))
