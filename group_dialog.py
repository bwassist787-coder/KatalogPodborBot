"""Fully in-group client dialogs for the allowlisted TEST Telegram forum.

Clients use inline keyboards in the two forum topics; free text uses replies
to bot prompts. One user cannot manipulate another user's form.
No production or other-client chat routes are enabled.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup

import case_store
import forum_route
import group_forms
import manager_chat
from catalog import CATEGORIES, filter_offers, read_offers, save_request

log = logging.getLogger(__name__)
VAT = ["Любой", "С НДС", "Без НДС", "По согласованию"]
ALLOWED_STAGES = {"search_cat", "search_vat", "search_amount", "results",
                  "search_comment", "confirm", "offer_name", "offer_inn",
                  "offer_category", "offer_vat", "offer_amount",
                  "offer_comment", "question"}


def _markup(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=value) for label, value in row]
        for row in rows
    ])


def _fmt(number: int):
    return f"{int(number):,}".replace(",", " ") + " ₽"


def _display(user):
    return "@" + user.username if user.username else user.full_name[:60]


def _chat_ok(chat):
    return bool(chat and forum_route.configured()
                and chat.id == forum_route.client_chat_id())


async def _send(bot, chat, topic, text, markup=None):
    return await bot.send_message(chat_id=chat, message_thread_id=topic,
                                  text=text[:4000], reply_markup=markup)


async def _choice(bot, chat, topic, user, token, stage, data, title, items):
    rows = [[(label[:55], f"gf:{stage}:{token}:{key}")] for label, key in items]
    rows.append([("Отмена", f"gf:stop:{token}")])
    sent = await _send(bot, chat, topic, f"{_display(user)}, {title}", _markup(rows))
    group_forms.advance(chat, topic, user.id, token, stage, data, sent.message_id)


async def _prompt(bot, chat, topic, user, token, stage, data, title):
    mention = _display(user)
    # Selective ForceReply targets a username mention; no usernames -> regular ForceReply.
    markup = ForceReply(selective=bool(user.username),
                        input_field_placeholder="Ответьте на это сообщение")
    sent = await _send(bot, chat, topic, f"{mention}, {title}\n"
        "Ответьте на это сообщение бота. Сообщение увидят участники группы.", markup)
    group_forms.advance(chat, topic, user.id, token, stage, data, sent.message_id)


async def _confirm(bot, chat, topic, user, token, data):
    kind = data["kind"]
    amount = _fmt(data["amount"])
    if kind == "offer":
        preview = (f"Предложение клиента\nКомпания: {data['company']}\n"
                   f"ИНН компании: {data['inn']}\nНаправление: {data['category']}\n"
                   f"НДС: {data['vat']}\nСумма: {amount}\n"
                   f"Условия: {data['comment']}")
    else:
        preview = (f"Подбор компании\nНаправление: {data['category']}\n"
                   f"НДС: {data['vat']}\nСумма: {amount}\n"
                   f"Предложение: {data.get('offer_company') or 'Не найдено'}\n"
                   f"Комментарий: {data.get('comment') or 'Нет'}")
    markup = _markup([
        [("Подтвердить заявку", f"gf:submit:{token}")],
        [("Отмена", f"gf:stop:{token}")],
    ])
    sent = await _send(bot, chat, topic,
                       f"{_display(user)}, проверьте заявку:\n\n{preview}\n\nОтправить?",
                       markup)
    group_forms.advance(chat, topic, user.id, token, "confirm", data, sent.message_id)


async def _results(bot, chat, topic, user, token, data, page=0):
    try:
        offers = await asyncio.to_thread(read_offers)
    except Exception:
        log.exception("Catalog read failed during forum search")
        await _send(bot, chat, topic, "Каталог временно недоступен. Повторите позже.")
        return
    matched = filter_offers(
        offers, category=data["category"],
        vat=data["vat"], amount=data["amount"])
    ids = [o.id for o in matched]
    data["results"] = ids
    if not matched:
        data["offer_id"] = ""
        data["offer_company"] = ""
        await _prompt(bot, chat, topic, user, token, "search_comment", data,
                      "Точных совпадений нет. Уточните, какую компанию ищете.")
        return
    page = max(0, min(page, (len(matched) - 1) // 4))
    offset = page * 4
    visible = matched[offset:offset+4]
    lines = [f"Найдено {len(matched)} предложений (страница {page+1})."]
    rows = []
    for index, item in enumerate(visible, offset):
        lines.append(
            f"\n{index+1}. {item.company[:70]}\n"
            f"{item.purpose[:130]}\n"
            f"НДС: {item.vat} | {_fmt(item.min_sum or 0)} — "
            f"{_fmt(item.max_sum) if item.max_sum is not None else 'без верхней границы'}\n"
            f"Условия: {item.terms[:110] or 'по согласованию'}")
        rows.append([(f"Выбрать №{index+1}", f"gf:choose:{token}:{index}")])
    pages = []
    if page:
        pages.append(("Назад", f"gf:page:{token}:{page-1}"))
    if offset + 4 < len(matched):
        pages.append(("Далее", f"gf:page:{token}:{page+1}"))
    if pages:
        rows.append(pages)
    rows.append([("Оставить запрос без предложения", f"gf:missing:{token}")])
    rows.append([("Отмена", f"gf:stop:{token}")])
    sent = await _send(bot, chat, topic, "\n".join(lines), _markup(rows))
    group_forms.advance(chat, topic, user.id, token, "results", data, sent.message_id)


async def _start(q, kind):
    chat = q.message.chat
    user = q.from_user
    thread = forum_route.topic_id("offer" if kind == "offer" else "search")
    data = {"kind": kind}
    token = group_forms.begin(chat.id, thread, user.id,
                              "offer_name" if kind == "offer" else "search_cat", data)
    await q.answer("Форма открыта в нужной теме.")
    if kind == "offer":
        await _prompt(q.get_bot(), chat.id, thread, user, token,
                      "offer_name", data, "введите название предлагаемой компании.")
    else:
        await _choice(q.get_bot(), chat.id, thread, user, token,
                      "search_cat", data, "выберите направление:",
                      [(name, str(i)) for i, name in enumerate(CATEGORIES)])


async def callback(update, context):
    q = update.callback_query
    if not q or not q.message or not _chat_ok(q.message.chat):
        if q:
            await q.answer("Кнопка недоступна в этой группе.", show_alert=True)
        return
    code = q.data or ""
    bits = code.split(":")
    user = q.from_user
    chat = q.message.chat.id

    if len(bits) == 3 and bits[1] == "start" and bits[2] in ("search", "offer"):
        await _start(q, bits[2])
        return

    if len(bits) == 3 and bits[1] in ("ask", "cancel"):
        rid = bits[2]
        case = case_store.find(rid)
        if (not case or case.get("origin_chat_id") != chat or
            case.get("origin_message_id") != q.message.message_id or
                case["client_id"] != user.id):
            await q.answer("Эта заявка недоступна.", show_alert=True)
            return
        if case["status"] not in ("Новая", "В работе"):
            await q.answer("Заявка уже закрыта.", show_alert=True)
            return
        if bits[1] == "cancel":
            result, _ = case_store.transition(rid, user.id, "cancel")
            if result == "ok":
                await q.answer("Заявка отменена")
                await manager_chat.update_card(context.bot, rid)
                await manager_chat.notify_group(
                    context.bot, rid, "Клиент отменил заявку.")
            else:
                await q.answer("Статус уже изменился.", show_alert=True)
            return
        topic = case["origin_topic_id"]
        token = group_forms.begin(chat, topic, user.id, "question", {"rid": rid})
        await q.answer("Напишите вопрос в этой теме.")
        await _prompt(context.bot, chat, topic, user, token,
                      "question", {"rid": rid}, "задайте вопрос по заявке.")
        return

    if len(bits) < 3:
        await q.answer("Неизвестная кнопка", show_alert=True)
        return
    action, nonce = bits[1], bits[2]
    # Telegram might omit message_thread_id in some updates. Find the active
    # user-scoped flow by token rather than assuming Telegram's incoming topic.
    form = group_forms.read_token(chat, user.id, nonce)
    if (not form or form["stage"] not in ALLOWED_STAGES or
            form["prompt_id"] != q.message.message_id):
        await q.answer("Кнопка устарела или относится к другой заявке.",
                       show_alert=True)
        return
    topic, data = form["topic_id"], form["data"]
    stage = form["stage"]
    if action == "stop":
        group_forms.complete(chat, topic, user.id, nonce)
        await q.answer("Отменено")
        await _send(context.bot, chat, topic, f"{_display(user)}, оформление отменено.")
        return
    if action == "search_cat" and stage == "search_cat" and len(bits) == 4:
        try:
            data["category"] = CATEGORIES[int(bits[3])]
        except (ValueError, IndexError):
            await q.answer("Неверное направление", show_alert=True)
            return
        await q.answer()
        await _choice(context.bot, chat, topic, user, nonce, "search_vat", data,
                      "выберите НДС:", [(value, str(i)) for i, value in enumerate(VAT)])
    elif action == "search_vat" and stage == "search_vat" and len(bits) == 4:
        try:
            data["vat"] = VAT[int(bits[3])]
        except (ValueError, IndexError):
            await q.answer("Неверные условия", show_alert=True)
            return
        await q.answer()
        await _prompt(context.bot, chat, topic, user, nonce, "search_amount",
                      data, "введите сумму подбора в рублях (только цифры).")
    elif action == "offer_category" and stage == "offer_category" and len(bits) == 4:
        try:
            data["category"] = CATEGORIES[int(bits[3])]
        except (ValueError, IndexError):
            await q.answer("Неверное направление", show_alert=True)
            return
        await q.answer()
        await _choice(context.bot, chat, topic, user, nonce,
                      "offer_vat", data, "условия НДС:",
                      [(v, str(i)) for i, v in enumerate(VAT[1:])])
    elif action == "offer_vat" and stage == "offer_vat" and len(bits) == 4:
        try:
            data["vat"] = VAT[1:][int(bits[3])]
        except (ValueError, IndexError):
            await q.answer("Неверные условия", show_alert=True)
            return
        await q.answer()
        await _prompt(context.bot, chat, topic, user, nonce,
                      "offer_amount", data, "введите сумму предложения в рублях.")
    elif action == "page" and stage == "results" and len(bits) == 4:
        await q.answer()
        try:
            page = int(bits[3])
        except ValueError:
            return
        await _results(context.bot, chat, topic, user, nonce, data, page)
    elif action == "choose" and stage == "results" and len(bits) == 4:
        try:
            index = int(bits[3])
            oid = data["results"][index]
        except (KeyError, ValueError, IndexError):
            await q.answer("Устаревшее предложение", show_alert=True)
            return
        try:
            current = next(x for x in await asyncio.to_thread(read_offers) if x.id == oid)
        except (StopIteration, Exception):
            await q.answer("Не удалось проверить предложение.", show_alert=True)
            return
        if not filter_offers([current], category=data["category"],
                             vat=data["vat"], amount=data["amount"]):
            await q.answer("Предложение изменилось.", show_alert=True)
            return
        data["offer_id"] = current.id
        data["offer_company"] = current.company
        await q.answer()
        await _prompt(context.bot, chat, topic, user, nonce,
                      "search_comment", data,
                      "добавьте комментарий или напишите «-», если его нет.")
    elif action == "missing" and stage == "results":
        data["offer_id"] = ""
        data["offer_company"] = ""
        await q.answer()
        await _prompt(context.bot, chat, topic, user, nonce, "search_comment", data,
                      "опишите, что нужно найти (до 500 символов).")
    elif action == "submit" and stage == "confirm":
        if not group_forms.claim_submit(chat, topic, user.id, nonce, q.message.message_id):
            await q.answer("Заявка уже отправляется.", show_alert=True)
            return
        await q.answer("Сохраняю заявку")
        try:
            await _submit(context.bot, chat, topic, user, data)
        except Exception:
            group_forms.reset_after_failure(chat, topic, user.id, nonce)
            log.exception("Could not submit group form")
            await _send(context.bot, chat, topic,
                        "Не удалось обработать заявку. Проверьте маршрут; не создавайте дубликат.")
            return
        group_forms.complete(chat, topic, user.id, nonce)
    else:
        await q.answer("Кнопка устарела. Начните заново.", show_alert=True)


async def text_input(update, context) -> bool:
    """Handle only a reply to THIS user's last prompt in the allowlisted group."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not user or not _chat_ok(chat):
        return False
    reply = message.reply_to_message
    if not reply or not reply.from_user or not reply.from_user.is_bot:
        return False
    form = group_forms.read_prompt(chat.id, user.id, reply.message_id)
    if not form or form["stage"] not in {
        "search_amount", "search_comment", "offer_name", "offer_inn",
        "offer_amount", "offer_comment", "question"
    }:
        return False
    topic, nonce, stage, data = (form["topic_id"], form["token"],
                                 form["stage"], form["data"])
    text = (message.text or "").strip()
    if stage in ("search_amount", "offer_amount"):
        cleaned = text.replace(" ", "").replace("₽", "")
        if not cleaned.isdecimal() or not 0 < int(cleaned) <= 10**12:
            await _prompt(context.bot, chat.id, topic, user, nonce, stage, data,
                          "укажите положительную сумму цифрами до 1 трлн ₽.")
            return True
        data["amount"] = int(cleaned)
        if stage == "search_amount":
            await _results(context.bot, chat.id, topic, user, nonce, data)
        else:
            await _prompt(context.bot, chat.id, topic, user, nonce,
                          "offer_comment", data, "напишите условия предложения (до 300 символов).")
    elif stage == "offer_name":
        if not 3 <= len(text) <= 90:
            await _prompt(context.bot, chat.id, topic, user, nonce, stage, data,
                          "введите название компании от 3 до 90 символов.")
            return True
        data["company"] = text
        await _prompt(context.bot, chat.id, topic, user, nonce, "offer_inn", data,
                      "введите ИНН компании (10 или 12 цифр).")
    elif stage == "offer_inn":
        if not re.fullmatch(r"(?:\d{10}|\d{12})", text):
            await _prompt(context.bot, chat.id, topic, user, nonce, stage, data,
                          "ИНН должен содержать 10 или 12 цифр.")
            return True
        data["inn"] = text
        await _choice(context.bot, chat.id, topic, user, nonce, "offer_category",
                      data, "выберите направление:",
                      [(name, str(i)) for i, name in enumerate(CATEGORIES)])
    elif stage in ("offer_comment", "search_comment"):
        upper = 300 if stage == "offer_comment" else 500
        if len(text) > upper or (text in ("", "-") and
             (stage == "offer_comment" or not data.get("offer_id"))):
            await _prompt(context.bot, chat.id, topic, user, nonce, stage, data,
                          f"введите уточнение до {upper} символов.")
            return True
        data["comment"] = "" if text == "-" else text
        await _confirm(context.bot, chat.id, topic, user, nonce, data)
    elif stage == "question":
        rid = data["rid"]
        case = case_store.find(rid)
        if (not case or case["client_id"] != user.id or
                case["origin_chat_id"] != chat.id or
                case["origin_topic_id"] != topic or
                case["status"] not in ("Новая", "В работе")):
            group_forms.complete(chat.id, topic, user.id, nonce)
            await _send(context.bot, chat.id, topic, "Заявка закрыта или недоступна.")
            return True
        if not 1 <= len(text) <= 1200:
            await _prompt(context.bot, chat.id, topic, user, nonce, stage, data,
                          "введите вопрос до 1200 символов.")
            return True
        try:
            await manager_chat.notify_group(context.bot, rid,
                                             "Вопрос клиента:\n" + text)
        except Exception:
            log.exception("Question delivery failed")
            await _send(context.bot, chat.id, topic,
                        "Вопрос не доставлен менеджерам. Повторите позже.")
            return True
        case_store.note(rid, user.id, "client_question")
        group_forms.complete(chat.id, topic, user.id, nonce)
        await _send(context.bot, chat.id, topic,
                    f"{_display(user)}, вопрос передан менеджерам.")
    return True


async def _submit(bot, chat, topic, user, data):
    """Revalidate test route and catalog at the commit point."""
    if not forum_route.configured() or chat != forum_route.client_chat_id():
        raise ValueError("Test client group route is not verified")
    route_kind = "offer" if data["kind"] == "offer" else "search"
    if topic != forum_route.topic_id(route_kind):
        raise ValueError("Wrong client topic")
    manager_chat_id = manager_chat.group_id()
    if not manager_chat_id:
        raise ValueError("Manager group not configured")
    selected = None
    if data["kind"] == "search" and data.get("offer_id"):
        offers = await asyncio.to_thread(read_offers)
        selected = next((x for x in offers if x.id == data["offer_id"]), None)
        if not selected or not filter_offers(
            [selected], category=data["category"],
            vat=data["vat"], amount=data["amount"]):
            await _send(bot, chat, topic,
                        "Предложение больше не соответствует условиям. Заявка заблокирована.")
            raise ValueError("Catalog offer changed or was withdrawn")
    if data["kind"] == "offer":
        case_type = "Предложение компании"
        comment = (f"Компания: {data['company']}; направление: {data['category']}; "
                   f"НДС: {data['vat']}; условия: {data['comment']}")
        inn = data["inn"]
        summary = (f"Компания: {data['company']}\nИНН компании: {inn}\n"
                   f"Направление: {data['category']}\nНДС: {data['vat']}\n"
                   f"Сумма: {_fmt(data['amount'])}\nУсловия: {data['comment']}")
    else:
        case_type = "Заявка" if selected else "Не нашёл нужное"
        comment = data.get("comment", "")
        inn = ""  # A client-group search does not solicit sensitive client INNs.
        summary = (f"Направление: {data['category']}\nНДС: {data['vat']}\n"
                   f"Сумма: {_fmt(data['amount'])}"
                   + (f"\nПредложение: {selected.company}" if selected else "")
                   + (f"\nКомментарий: {comment}" if comment else ""))
    rid = await asyncio.to_thread(
        save_request, user.id, user.username or "",
        case_type, selected, inn, data["amount"], comment)
    try:
        chat_info = await bot.get_chat(chat)
        chat_title = chat_info.title or "Тестовый клиент"
    except Exception:
        chat_title = "Тестовый клиент"
    # The internal summary includes route ID, not any other client information.
    await asyncio.to_thread(
        case_store.register, rid, user.id, manager_chat_id, case_type,
        f"Telegram: {_display(user)}\n" + summary,
        origin_chat_id=chat, origin_topic_id=topic,
        origin_chat_title=chat_title, origin_username=_display(user),
        client_summary=summary,
    )
    case = case_store.find(rid)
    client_ok, manager_ok = False, False
    try:
        posted = await forum_route.post_group_card(bot, case)
        case_store.set_origin_message(rid, posted)
        client_ok = True
    except Exception:
        log.exception("Client card delivery failed for %s", rid)
    try:
        case = case_store.find(rid)
        posted = await bot.send_message(
            chat_id=manager_chat_id,
            text=manager_chat.card(case)
            + ("\n\nНужна проверка маршрута: клиентская карточка не отправлена."
               if not client_ok else ""),
            reply_markup=manager_chat.manager_keyboard(case) if client_ok else None)
        case_store.set_group_message(rid, posted.message_id)
        manager_ok = True
    except Exception:
        log.exception("Manager card delivery failed for %s", rid)
    if client_ok and manager_ok:
        await _send(bot, chat, topic,
                    f"{_display(user)}, заявка отправлена. "
                    "Когда менеджер возьмёт её в работу, статус карточки обновится.")
    else:
        if client_ok:
            # Prevent misleading 'New' card if manager delivery failed.
            await _send(bot, chat, topic,
                        "Заявка сохранена, но связь с менеджерами не подтверждена. "
                        "Нужна проверка маршрута. Не создавайте повторную заявку.")
        else:
            await _send(bot, chat, topic,
                        "Заявка сохранена, но доставка не подтверждена. "
                        "Нужна проверка маршрута. Не создавайте повторную заявку.")
