"""Opt-in forum routing for ONE test client group.

Client inputs are collected in the bot's private chat. Only the confirmed,
client-safe request card is published to the configured forum topic. Manager
routing remains separate. This module never reads internal catalog rates.
"""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus

log = logging.getLogger(__name__)
BOT_NAME = "KatalogPodborBot"


def client_chat_id() -> int:
    try:
        return int(os.getenv("TEST_CLIENT_CHAT_ID", "0"))
    except ValueError:
        return 0


def topic_id(kind: str) -> int:
    key = "TEST_CLIENT_TOPIC_IN_ID" if kind == "offer" else "TEST_CLIENT_TOPIC_OUT_ID"
    try:
        return int(os.getenv(key, "0"))
    except ValueError:
        return 0


def configured() -> bool:
    return bool(client_chat_id() < 0 and topic_id("offer") > 1 and topic_id("search") > 1
                and topic_id("offer") != topic_id("search")
                and os.getenv("TEST_MODE", "true").lower() == "true")


def link(payload: str) -> str:
    return f"https://t.me/{BOT_NAME}?start={payload}"


def origin(kind: str) -> dict:
    if kind not in {"search", "offer"} or not configured():
        raise ValueError("Forum route not configured")
    return {"chat_id": client_chat_id(), "thread_id": topic_id(kind), "kind": kind}


async def member_allowed(bot, user_id: int) -> bool:
    """Fail closed if Telegram cannot verify membership."""
    if not configured():
        return False
    try:
        member = await bot.get_chat_member(client_chat_id(), user_id)
    except Exception:
        log.warning("Could not verify test client chat membership")
        return False
    return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and (
        member.status != ChatMemberStatus.RESTRICTED or member.is_member
    )


def group_topic_matches(chat_id: int, thread_id: int | None, kind: str) -> bool:
    return configured() and chat_id == client_chat_id() and thread_id == topic_id(kind)


def button_menu(kind: str) -> tuple[str, InlineKeyboardMarkup]:
    """User-facing, command-free entry menus; no internal company data."""
    if kind == "search":
        return (
            "Наши предложения\n\n"
            "Нажмите кнопку, чтобы подобрать компанию по направлению, НДС и сумме. "
            "Оформление откроется в личном чате с ботом; подтверждённая заявка появится здесь.",
            InlineKeyboardMarkup([[InlineKeyboardButton(
                "Подобрать компанию", url=link("route_s"))]]),
        )
    if kind == "offer":
        return (
            "Предложения клиента\n\n"
            "Нажмите кнопку, чтобы предложить компанию и указать условия. "
            "Предложение отправится менеджерам на проверку, без автоматической публикации в каталоге.",
            InlineKeyboardMarkup([[InlineKeyboardButton(
                "Предложить компанию", url=link("route_p"))]]),
        )
    if kind == "general":
        return (
            "Каталог компаний\n\n"
            "Выберите действие. Поиск и заполнение заявки откроются в личном диалоге с ботом. "
            "Готовая заявка появится в соответствующей теме группы.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("Подобрать компанию", url=link("route_s"))],
                [InlineKeyboardButton("Предложить компанию", url=link("route_p"))],
            ]),
        )
    raise ValueError("Unsupported menu type")


async def install_menus(bot) -> dict[str, int]:
    """Post entry menus into the TWO exact topics and General exactly once per invocation.

    Explicit operator action is required; never post at every Render restart.
    """
    if not configured():
        raise ValueError("Test forum route is not configured")
    target_chat = client_chat_id()
    messages = {}
    for key in ("search", "offer"):
        text, keyboard = button_menu(key)
        posted = await bot.send_message(
            chat_id=target_chat,
            message_thread_id=topic_id(key),
            text=text,
            reply_markup=keyboard,
        )
        messages[key] = posted.message_id
    text, keyboard = button_menu("general")
    # Telegram routes a supergroup message without message_thread_id to General.
    posted = await bot.send_message(
        chat_id=target_chat, text=text, reply_markup=keyboard,
    )
    messages["general"] = posted.message_id
    return messages


async def entry(update, context):
    """Display a private-bot launch button ONLY in the configured client group."""
    chat, message = update.effective_chat, update.effective_message
    if not chat or not message or chat.id != client_chat_id() or not configured():
        return
    command = (message.text or "").split()[0].split("@")[0].lower()
    kind = "offer" if command == "/offer" else "search"
    expected = topic_id(kind)
    # Do not depend on Telegram's incoming message_thread_id: for General
    # it can be absent; in client groups the correct outbound topic is known.
    menu_text, menu_keyboard = button_menu(kind)
    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=expected,
        text=menu_text,
        reply_markup=menu_keyboard,
    )


def client_card(case: dict) -> str:
    owner = case.get("owner_name") or "Не назначен"
    return (
        f"{'Предложение клиента' if case['kind'] == 'Предложение компании' else 'Подбор компании'}\n"
        f"{case.get('client_summary') or 'Параметры заявки уточняются'}\n\n"
        f"Статус: {case['status']}\n"
        f"Ответственный: {owner}\n"
        f"ID: {case['id']}"
    )


def client_keyboard(case: dict):
    rid = case["id"]
    rows = []
    if case["status"] == "Новая":
        rows.append([InlineKeyboardButton("Взять в работу", callback_data="mgr:take:" + rid)])
    if case["status"] not in ("Отменена", "Завершена"):
        rows.append([InlineKeyboardButton("Задать вопрос", url=link("question_" + rid))])
    kind = "route_p" if case["kind"] == "Предложение компании" else "route_s"
    rows.append([InlineKeyboardButton(
        "Предложить ещё компанию" if kind == "route_p" else "Новый подбор",
        url=link(kind))])
    return InlineKeyboardMarkup(rows)


def private_group_url(case: dict) -> str | None:
    cid = case.get("origin_chat_id")
    message_id = case.get("origin_message_id")
    if not cid or not message_id or cid >= -1000000000000:
        return None
    # Telegram's private supergroup message-link syntax.
    group_number = -cid - 1000000000000
    return f"https://t.me/c/{group_number}/{message_id}"


async def update_group_card(bot, case: dict):
    if not case.get("origin_chat_id") or not case.get("origin_message_id"):
        return
    try:
        await bot.edit_message_text(
            chat_id=case["origin_chat_id"],
            message_id=case["origin_message_id"],
            text=client_card(case),
            reply_markup=client_keyboard(case),
        )
    except Exception:
        log.exception("Failed to refresh client card for %s", case["id"])


async def post_group_card(bot, case: dict):
    if (case.get("origin_chat_id") != client_chat_id()
            or case.get("origin_topic_id") != topic_id(
                "offer" if case["kind"] == "Предложение компании" else "search")):
        raise ValueError("Client topic routing mismatch")
    sent = await bot.send_message(
        chat_id=case["origin_chat_id"],
        message_thread_id=case["origin_topic_id"],
        text=client_card(case),
        reply_markup=client_keyboard(case),
    )
    return sent.message_id
