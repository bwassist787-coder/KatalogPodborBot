"""Group-only category browsing: five client-priced offers at a time.

Only works in a single explicitly allowlisted TEST forum, topic 'Our offers'.
State and callback authorization are scoped to (chat, topic, Telegram user).
There are NO Telegram private-chat steps.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import case_store
import forum_route
import group_forms
import manager_chat
from catalog import read_offers, save_request
from quick_catalog import CATEGORIES, PAGE_SIZE, selected_group

log = logging.getLogger(__name__)


def menu(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(title, callback_data=data) for title, data in row]
        for row in rows
    ])


def safe(value, limit):
    return " ".join(str(value or "").split())[:limit]


def card(offer, number):
    return (
        f"{number}. {safe(offer.company, 90)}\n"
        f"ИНН: {offer.inn}\n"
        f"Цена: {safe(offer.client_percent, 32)}\n"
        f"Срок: {safe(offer.days, 25)} дн."
    )


def _search_topic():
    return forum_route.topic_id("search")


async def _post(bot, text, markup=None):
    return await bot.send_message(
        chat_id=forum_route.client_chat_id(),
        message_thread_id=_search_topic(),
        text=text,
        reply_markup=markup,
    )


async def _replace(q, context, text, markup):
    try:
        await q.edit_message_text(text=text, reply_markup=markup)
        return q.message.message_id
    except Exception:
        log.warning("Could not edit a test forum catalog message")
        created = await _post(context.bot, text, markup)
        return created.message_id


async def start(q, context):
    chat_id = forum_route.client_chat_id()
    topic = _search_topic()
    user_id = q.from_user.id
    data = {"category": None, "label": "", "page": 0,
            "page_offer_ids": [], "selected_id": None, "choice": ""}
    token = group_forms.begin(chat_id, topic, user_id, "quick_category", data)
    rows = [[(label, f"qg:cat:{token}:{i}")]
            for i, (label, _) in enumerate(CATEGORIES)]
    rows.append([("Мои заявки", "qg:mine")])
    sent = await _post(context.bot,
                       f"{safe(q.from_user.full_name, 70)}, выберите категорию:",
                       menu(rows))
    group_forms.advance(chat_id, topic, user_id, token,
                        "quick_category", data, sent.message_id)


async def show_page(q, context, form, page):
    data = form["data"]
    try:
        catalog = await asyncio.to_thread(read_offers)
    except Exception:
        log.exception("Could not read approved catalog")
        await q.message.reply_text("Каталог временно недоступен.")
        return
    offers = selected_group(catalog, data["category"])
    total = len(offers)
    max_page = max(0, (total-1)//PAGE_SIZE)
    if page < 0 or page > max_page:
        await q.answer("Следующих компаний нет.", show_alert=True)
        return
    batch = offers[page * PAGE_SIZE:(page+1) * PAGE_SIZE]
    ids = [x.id for x in batch]
    data.update(page=page, page_offer_ids=ids, selected_id=None, choice="")
    begin = page*PAGE_SIZE + 1
    heading = f"{data['label']}\n"
    if batch:
        body = (f"Компании {begin}–{begin+len(batch)-1} из {total}\n\n"
                + "\n\n".join(card(o, begin+i) for i, o in enumerate(batch)))
    else:
        body = "В этой категории пока нет доступных компаний."
    text = heading + body
    rows = [[(f"Выбрать №{begin+i}", f"qg:pick:{form['token']}:{page}:{i}")]
            for i in range(len(batch))]
    nav = []
    if page > 0:
        nav.append(("Предыдущие 5", f"qg:page:{form['token']}:{page}:{page-1}"))
    if (page+1)*PAGE_SIZE < total:
        nav.append(("Ещё компании", f"qg:page:{form['token']}:{page}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([("Нужен менеджер", f"qg:manager:{form['token']}")])
    rows.append([("Другие категории", "qg:start:search")])
    message_id = await _replace(q, context, text, menu(rows))
    group_forms.advance(forum_route.client_chat_id(), _search_topic(),
                        form["user_id"], form["token"], "quick_results",
                        data, message_id)


async def confirm(q, context, form, offer=None):
    data = form["data"]
    data["choice"] = "company" if offer else "manager"
    data["selected_id"] = offer.id if offer else None
    if offer:
        intro = "Вы выбрали компанию:\n\n" + card(offer, 1)[3:]
    else:
        intro = ("Нужна помощь менеджера\n\n"
                 f"Категория: {data['label']}\n"
                 "Менеджер поможет уточнить условия.")
    markup = menu([
        [("Подтвердить заявку", f"qg:send:{form['token']}")],
        [("Вернуться к списку", f"qg:back:{form['token']}")],
    ])
    message_id = await _replace(q, context, intro +
                                "\n\nПередать заявку менеджеру?", markup)
    group_forms.advance(forum_route.client_chat_id(), _search_topic(),
                        form["user_id"], form["token"], "quick_confirm",
                        data, message_id)


async def send_request(q, context, form):
    """Confirm once; on uncertain save/delivery DO NOT offer a duplicate retry."""
    data = form["data"]
    token = form["token"]
    group = forum_route.client_chat_id()
    topic = _search_topic()
    manager_group = manager_chat.group_id()
    if not manager_group or not forum_route.configured():
        await _post(context.bot, "Нужна проверка маршрута. Заявка не отправлена.")
        return
    if not await forum_route.member_allowed(context.bot, q.from_user.id):
        await _post(context.bot, "Доступ не подтверждён. Заявка не отправлена.")
        return
    offer = None
    if data.get("choice") == "company":
        try:
            current = await asyncio.to_thread(read_offers)
            offer = next((item for item in selected_group(current, data["category"])
                          if item.id == data.get("selected_id")), None)
        except Exception:
            log.exception("Could not revalidate selected offer")
            await _post(context.bot, "Нужна проверка каталога. Заявка не отправлена.")
            return
        if not offer:
            await _post(context.bot, "Компания больше не доступна. Начните подбор заново.",
                        menu([[("Новый подбор", "qg:start:search")]]))
            return
    elif data.get("choice") != "manager":
        await _post(context.bot, "Выбор не подтверждён.")
        return
    kind = "Заявка" if offer else "Не нашёл нужное"
    summary = (card(offer, 1)[3:] +
               f"\nКатегория: {data['label']}" if offer else
               f"Подбор менеджером\nКатегория: {data['label']}")
    try:
        rid = await asyncio.to_thread(
            save_request, q.from_user.id, q.from_user.username or "",
            kind, offer, "", None,
            f"Категория: {data['label']}; "
            + ("Выбранное предложение: " + offer.id if offer else "Нужен менеджер"))
        try:
            group_info = await context.bot.get_chat(group)
            group_title = safe(group_info.title, 100)
        except Exception:
            group_title = "ТЕСТ Бот"
        await asyncio.to_thread(
            case_store.register, rid, q.from_user.id, manager_group, kind,
            "Источник: клиентская группа\n" + summary,
            origin_chat_id=group,
            origin_topic_id=topic,
            origin_chat_title=group_title,
            origin_username=("@" + q.from_user.username if q.from_user.username
                             else safe(q.from_user.full_name, 70)),
            client_summary=summary,
            client_group_id=group,
            client_group_title=group_title)
    except Exception:
        log.exception("Quick group request save/registration uncertain")
        await _post(context.bot,
                    "Заявка могла сохраниться, но маршрут не подтверждён. "
                    "Нужна проверка, не отправляйте повторно.")
        return

    record = case_store.find(rid)
    client_ok, manager_ok = False, False
    try:
        client_message = await forum_route.post_group_card(context.bot, record)
        case_store.set_origin_message(rid, client_message)
        client_ok = True
    except Exception:
        log.exception("Could not deliver client status card for %s", rid)
    try:
        record = case_store.find(rid)
        posted = await context.bot.send_message(
            chat_id=manager_group,
            text=manager_chat.card(record) +
                 ("\n\nНужна проверка маршрута: клиентская карточка не доставлена."
                  if not client_ok else ""),
            reply_markup=(manager_chat.manager_keyboard(record)
                          if client_ok else None))
        case_store.set_group_message(rid, posted.message_id)
        manager_ok = True
    except Exception:
        log.exception("Could not deliver manager card for %s", rid)

    if client_ok and manager_ok:
        await _post(context.bot, "Заявка передана менеджеру.",
                    menu([[("Новый подбор", "qg:start:search")],
                          [("Мои заявки", "qg:mine")]]))
    else:
        await _post(context.bot,
                    "Заявка сохранена, но доставка подтверждена не полностью. "
                    "Нужна проверка маршрута. Не создавайте повторную заявку.")


async def show_mine(q, context):
    group_id = forum_route.client_chat_id()
    cases = case_store.client_cases(q.from_user.id, group_id, limit=10)
    rows = []
    for case in cases:
        label = ("Подбор" if case["kind"] != "Предложение компании"
                 else "Предложение клиента")
        rows.append([(f"{label} · {case['status']}"[:55],
                      "qg:view:" + case["id"])])
    rows.append([("Новый подбор", "qg:start:search")])
    await _post(context.bot,
                f"{safe(q.from_user.full_name, 50)}, ваши заявки:"
                + ("\nВыберите заявку для просмотра." if cases
                   else "\nПока заявок нет."),
                menu(rows))


async def show_case(q, context, rid):
    case = case_store.find(rid)
    if (not case or case["client_id"] != q.from_user.id or
            case.get("origin_chat_id") != forum_route.client_chat_id() or
            case.get("origin_topic_id") not in (
                forum_route.topic_id("search"), forum_route.topic_id("offer"))):
        await q.answer("Заявка недоступна.", show_alert=True)
        return
    message = ((case.get("client_summary") or "Описание недоступно")[:1300]
               + "\n\nСтатус: " + case["status"]
               + "\nОтветственный: " + (case.get("owner_name") or "Не назначен"))
    await _post(context.bot, message,
                menu([[("Мои заявки", "qg:mine")],
                      [("Новый подбор", "qg:start:search")]]))


async def callback(update, context):
    q = update.callback_query
    if (not q or not q.message or not forum_route.configured() or
            q.message.chat.id != forum_route.client_chat_id()):
        if q:
            await q.answer("Доступно только в клиентской группе.", show_alert=True)
        return
    # Even a valid old button cannot be used by a former group member.
    if not await forum_route.member_allowed(context.bot, q.from_user.id):
        await q.answer("Нет доступа к клиентской группе.", show_alert=True)
        return
    code = (q.data or "").split(":")
    if code == ["qg", "start", "search"]:
        await q.answer("Подбор откроется в теме «Предложения от Нас».")
        await start(q, context)
        return
    if code == ["qg", "mine"]:
        await q.answer()
        await show_mine(q, context)
        return
    if len(code) == 3 and code[:2] == ["qg", "view"]:
        await show_case(q, context, code[2])
        await q.answer()
        return
    if len(code) < 3:
        await q.answer("Кнопка устарела.", show_alert=True)
        return
    action, token = code[1:3]
    form = group_forms.read_token(forum_route.client_chat_id(), q.from_user.id, token)
    if (not form or form["topic_id"] != _search_topic() or
            form.get("prompt_id") != q.message.message_id):
        await q.answer("Это кнопка предыдущего подбора.", show_alert=True)
        return
    if action == "cat" and form["stage"] == "quick_category" and len(code) == 4:
        try:
            label, category = CATEGORIES[int(code[3])]
        except (ValueError, IndexError):
            await q.answer("Категория недоступна.", show_alert=True)
            return
        form["data"].update(label=label, category=category)
        await q.answer()
        await show_page(q, context, form, 0)
    elif action == "page" and form["stage"] == "quick_results" and len(code) == 5:
        try:
            page, requested = int(code[3]), int(code[4])
        except ValueError:
            await q.answer("Неправильная страница.", show_alert=True)
            return
        if page != form["data"]["page"] or abs(requested-page) != 1:
            await q.answer("Кнопка устарела.", show_alert=True)
            return
        await q.answer()
        await show_page(q, context, form, requested)
    elif action == "pick" and form["stage"] == "quick_results" and len(code) == 5:
        try:
            page, i = int(code[3]), int(code[4])
            if page != form["data"]["page"]:
                raise ValueError
            selected_id = form["data"]["page_offer_ids"][i]
            if i < 0:
                raise ValueError
        except (ValueError, IndexError):
            await q.answer("Компания не найдена.", show_alert=True)
            return
        try:
            offers = await asyncio.to_thread(read_offers)
            offer = next((x for x in selected_group(offers, form["data"]["category"])
                          if x.id == selected_id), None)
        except Exception:
            log.exception("Could not verify chosen company")
            await q.answer("Каталог временно недоступен.", show_alert=True)
            return
        if offer is None:
            await q.answer("Компания больше не доступна.", show_alert=True)
            return
        await q.answer()
        await confirm(q, context, form, offer)
    elif action == "manager" and form["stage"] == "quick_results":
        await q.answer()
        await confirm(q, context, form)
    elif action == "back" and form["stage"] == "quick_confirm":
        await q.answer()
        await show_page(q, context, form, form["data"]["page"])
    elif action == "send" and form["stage"] == "quick_confirm":
        if not group_forms.claim_submit(
                forum_route.client_chat_id(), _search_topic(),
                q.from_user.id, token, q.message.message_id):
            await q.answer("Заявка уже отправляется.", show_alert=True)
            return
        await q.answer("Передаю заявку.")
        try:
            await send_request(q, context, form)
        finally:
            # Once save begins, retry is unsafe if a remote write succeeded.
            group_forms.complete(forum_route.client_chat_id(), _search_topic(),
                                 q.from_user.id, token)
    else:
        await q.answer("Кнопка устарела.", show_alert=True)
