"""Safe client-facing private request dashboard for the single configured TEST group.

A client is accepted only after Telegram confirms membership in the precise
configured test group. No guessing by nickname, group title or shared words.
Multiple client groups require an explicit user-to-group mapping before launch.
"""
from __future__ import annotations

import logging

import case_store
import forum_route
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)


async def resolve_group(bot, user):
    """Return a verified (chat_id, title) or None. Fail closed."""
    if not user or not forum_route.configured():
        return None
    if not await forum_route.member_allowed(bot, user.id):
        return None
    chat_id = forum_route.client_chat_id()
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or "Клиентская группа"
    except Exception:
        log.warning("Verified group title unavailable for private user %s", user.id)
        title = "Клиентская группа"
    return {"chat_id": chat_id, "title": title}


def my_cases_markup(rows, state):
    buttons = [[InlineKeyboardButton(
        (("◉ " if c["status"] in ("Новая", "В работе") else "")
         + ("Предложение" if c["kind"] == "Предложение компании"
            else "Свободный запрос" if c["kind"] == "Свободный запрос"
            else "Подбор")
         + " · " + c["status"])[:58],
        callback_data="dash:view:" + c["id"])] for c in rows]
    if state == "active":
        buttons.append([InlineKeyboardButton("Завершённые", callback_data="dash:list:closed")])
    else:
        buttons.append([InlineKeyboardButton("Активные", callback_data="dash:list:active")])
    buttons.append([InlineKeyboardButton("Главное меню", callback_data="menu:start")])
    return InlineKeyboardMarkup(buttons)


async def show_my_cases(message, context, group, *, state="active"):
    cases = case_store.client_cases(message.chat_id, group["chat_id"], limit=50)
    if state == "active":
        cases = [x for x in cases if x["status"] in ("Новая", "В работе")]
    else:
        cases = [x for x in cases if x["status"] not in ("Новая", "В работе")]
    visible = cases[:12]
    await message.reply_text(
        "Ваши заявки (" + ("активные" if state == "active" else "завершённые") + ")"
        + ("\n" + group["title"] if group.get("title") else "")
        + ("\nВыберите заявку для просмотра." if visible else
           "\nПока заявок в этом разделе нет."),
        reply_markup=my_cases_markup(visible, state),
    )


def case_belongs_to_client(case, user_id, group_id):
    return bool(
        case and case["client_id"] == user_id and
        (case.get("client_group_id") == group_id or
         (case.get("client_group_id") is None and case.get("origin_chat_id") == group_id))
    )


async def show_case(message, context, rid, user, group):
    case = case_store.find(rid)
    if not case_belongs_to_client(case, user.id, group["chat_id"]):
        await message.reply_text("Заявка недоступна. Вернитесь в «Мои заявки».")
        return
    kind = ("Предложение клиента" if case["kind"] == "Предложение компании"
            else "Свободный запрос" if case["kind"] == "Свободный запрос"
            else "Подбор компании")
    summary = case.get("client_summary") or "Описание недоступно"
    from manager_chat import customer_keyboard
    await message.reply_text(
        f"{kind}\n\n{summary[:2000]}\n\n"
        f"Статус: {case['status']}\n"
        f"Ответственный: {case.get('owner_name') or 'Не назначен'}\n"
        f"ID: {rid}",
        reply_markup=(customer_keyboard(rid) if case["status"] in ("Новая", "В работе")
                      else my_cases_markup([], "active")))
