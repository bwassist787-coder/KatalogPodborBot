"""Manager actions in one private Telegram group, with explicit client relaying."""
from __future__ import annotations

import asyncio
import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
import case_store as store
import forum_route

log = logging.getLogger(__name__)


def group_id():
    try:
        return int(os.getenv("MANAGER_CHAT_ID", "") or "0")
    except ValueError:
        return 0


def keyboard(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
    ])


def customer_keyboard(rid, *, offer=False):
    case = store.find(rid)
    is_proposal = bool(case and case["kind"] == "Предложение компании")
    rows = []
    if offer:
        if is_proposal:
            rows.append([("Есть интерес", "cli:ok:" + rid)])
        else:
            rows.append([("Подходит", "cli:ok:" + rid),
                         ("Другой вариант", "cli:other:" + rid)])
    rows.append([("Задать вопрос", "cli:ask:" + rid)])
    if is_proposal:
        rows.append([("Предложить ещё компанию", "cli:propose")])
    else:
        rows.append([("Новый подбор", "cli:new")])
    rows.append([("Отменить заявку", "cli:cancel:" + rid)])
    return keyboard(rows)


def manager_keyboard(case):
    rid, status = case["id"], case["status"]
    if status == "Новая":
        return keyboard([[("Взять в работу", "mgr:take:" + rid)]])
    if status == "В работе":
        return keyboard([
            [("Написать клиенту", "mgr:write:" + rid)],
            [("Освободить", "mgr:release:" + rid)],
            [("Завершить", "mgr:askfinish:" + rid),
             ("Отменить", "mgr:askcancel:" + rid)],
        ])
    return None


def card(case):
    owner = case["owner_name"] or "Не назначен"
    if case.get("origin_chat_id"):
        group_link = forum_route.private_group_url(case)
        display = (f"Чат: {case.get('origin_chat_title') or 'Тестовый чат'}\n"
                   + (f"Перейти в чат: {group_link}\n" if group_link else "")
                   + (f"{case['origin_username']}\n" if case.get('origin_username') else "")
                   + ("Предложение клиента\n" if case['kind'] == 'Предложение компании'
                      else "Наше предложение / подбор\n"))
        return (display + (case.get("client_summary") or case["summary"]) +
                f"\n\nСтатус: {case['status']}\nОтветственный: {owner}"
                f"\n\nID: {case['id']}")
    if case.get("client_group_id"):
        nickname = case.get("origin_username") or "Без ника"
        header = ("Чат: " + (case.get("client_group_title") or "Клиентская группа")
                  + "\n" + nickname + "\n"
                  + ("Предложение клиента" if case["kind"] == "Предложение компании"
                     else ("Свободный запрос" if case["kind"] == "Свободный запрос"
                           else "Подбор компании")) + "\n")
        return (header + (case.get("client_summary") or case["summary"])
                + "\n\nСтатус: " + case["status"]
                + "\nОтветственный: " + owner
                + "\n\nID: " + case["id"])
    return (f"Заявка {case['id']}\n"
            f"Тип: {case['kind']}\n"
            f"{case['summary']}\n\n"
            f"Статус: {case['status']}\n"
            f"Ответственный: {owner}")


async def update_card(bot, rid):
    case = store.find(rid)
    if not case:
        return
    if case.get("chat_message_id"):
        try:
            await bot.edit_message_text(
                chat_id=case["chat_id"], message_id=case["chat_message_id"],
                text=card(case), reply_markup=manager_keyboard(case))
        except Exception:
            log.exception("Could not refresh manager card for %s", rid)
    if case.get("origin_message_id"):
        await forum_route.update_group_card(bot, case)


async def notify_group(bot, rid, text):
    case = store.find(rid)
    if not case:
        return
    args = {"chat_id": case["chat_id"], "text": f"Заявка {rid}\n{text}"}
    if case.get("chat_message_id"):
        args["reply_to_message_id"] = case["chat_message_id"]
        args["allow_sending_without_reply"] = True
    await bot.send_message(**args)


async def notify_client(bot, rid, text, offer=False):
    case = store.find(rid)
    if not case:
        return
    if case.get("origin_chat_id"):
        if not forum_route.configured() or case["origin_chat_id"] != forum_route.client_chat_id():
            raise ValueError("Unknown client chat route: blocked")
        if case.get("origin_topic_id") != forum_route.topic_id(
                "offer" if case["kind"] == "Предложение компании" else "search"):
            raise ValueError("Client topic mismatch: blocked")
        await bot.send_message(
            chat_id=case["origin_chat_id"],
            message_thread_id=case["origin_topic_id"], text=text)
    else:
        await bot.send_message(
            chat_id=case["client_id"], text=text,
            reply_markup=customer_keyboard(rid, offer=offer))


def manager_label(user):
    return ("@" + user.username) if user.username else user.full_name[:80]


async def manager_callback(update, context):
    q = update.callback_query
    if not q or not q.data:
        return
    if not update.effective_chat:
        await q.answer("Неизвестный чат", show_alert=True)
        return
    parts = q.data.split(":", 2)
    if len(parts) != 3:
        await q.answer("Некорректная кнопка", show_alert=True)
        return
    _, action, rid = parts
    actor = q.from_user.id
    case = store.find(rid) if action not in ("confirm", "discard") else None
    in_manager_group = update.effective_chat.id == group_id()
    # In a client forum only explicit, allowlisted managers may claim a case.
    allowed_manager_ids = {
        int(s) for s in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if s.strip().isdigit()
    }
    in_origin_group = bool(
        case and action == "take" and case.get("origin_chat_id") ==
        update.effective_chat.id and forum_route.configured() and
        actor in allowed_manager_ids
    )
    if not in_manager_group and not in_origin_group:
        await q.answer("Это действие доступно только менеджерам.", show_alert=True)
        return
    if action in ("confirm", "discard"):
        if action == "discard":
            # Pending draft remains inaccessible for sending; no message is sent.
            await q.answer("Черновик не отправлен")
            await q.edit_message_reply_markup(reply_markup=None)
            return
        draft = store.draft_claim(rid, actor)
        if not draft:
            await q.answer("Черновик устарел или недоступен", show_alert=True)
            return
        await q.answer("Отправляю...")
        request_id = draft["case_id"]
        try:
            await notify_client(context.bot, request_id,
                                "Сообщение менеджера по заявке " + request_id +
                                ":\n\n" + draft["text"], offer=True)
        except Exception:
            store.draft_result(rid, False)
            log.exception("Could not send draft %s", rid)
            await q.message.reply_text("Не удалось доставить сообщение клиенту. Повторите отправку.")
            return
        store.draft_result(rid, True)
        store.note(request_id, actor, "manager_message")
        await q.edit_message_reply_markup(reply_markup=None)
        await notify_group(context.bot, request_id,
                           "Менеджер " + manager_label(q.from_user) +
                           " отправил клиенту сообщение:\n" + draft["text"][:1000])
        return
    if not case or case["chat_id"] != group_id():
        await q.answer("Заявка не найдена в тестовой базе.", show_alert=True)
        return
    if action == "take":
        status, updated = store.take(rid, actor, manager_label(q.from_user))
        if status != "ok":
            await q.answer("Заявка уже занята или закрыта.", show_alert=True)
            return
        await q.answer("Заявка ваша")
        await update_card(context.bot, rid)
        await notify_group(context.bot, rid, "Принята в работу: " + updated["owner_name"])
        try:
            await notify_client(context.bot, rid,
                                "Заявка " + rid + " принята в работу.\n"
                                "Менеджер свяжется с вами через бота.")
        except Exception:
            log.exception("Failed to notify client %s about take", rid)
        return
    if action == "write":
        if case["status"] != "В работе" or case["owner_id"] != actor:
            await q.answer("Только ответственный менеджер может писать клиенту.",
                           show_alert=True)
            return
        await q.answer()
        prompt = await q.message.reply_text(
            "Заявка " + rid + "\n" + manager_label(q.from_user) +
            ", ответьте на ЭТО сообщение текстом для клиента.\n"
            "Сначала появится предпросмотр, отправка только по подтверждению.\n"
            "Перед отправкой проверьте ЗСК и не добавляйте внутренние данные.")
        store.prompt_add(prompt.message_id, rid, actor)
        return
    if action in ("askcancel", "askfinish"):
        if case["status"] != "В работе" or case["owner_id"] != actor:
            await q.answer("Доступно только ответственному менеджеру.", show_alert=True)
            return
        target = "cancel" if action == "askcancel" else "finish"
        label = "отменить" if target == "cancel" else "завершить"
        await q.answer()
        await q.message.reply_text(
            f"Точно {label} заявку {rid}?",
            reply_markup=keyboard([
                [("Подтвердить", "mgr:" + target + ":" + rid)],
                [("Оставить без изменений", "mgr:noop:" + rid)],
            ]))
        return
    if action == "noop":
        await q.answer("Без изменений")
        await q.edit_message_reply_markup(reply_markup=None)
        return
    if action in ("release", "finish", "cancel"):
        status, changed = store.transition(rid, actor, action)
        if status != "ok":
            await q.answer("Действие недоступно: заявка изменилась.", show_alert=True)
            return
        await q.answer("Статус обновлён")
        await update_card(context.bot, rid)
        words = {"release": "Снова свободна для менеджеров",
                 "finish": "Завершена",
                 "cancel": "Отменена"}
        await notify_group(context.bot, rid,
                           words[action] + " (" + manager_label(q.from_user) + ")")
        if action != "release":
            try:
                await notify_client(context.bot, rid,
                                    "Заявка " + rid + ": " +
                                    ("завершена." if action == "finish" else "отменена."))
            except Exception:
                log.exception("Client notification failed after %s", action)
        return
    await q.answer("Неизвестное действие", show_alert=True)


async def manager_text(update, context):
    msg = update.effective_message
    if update.effective_chat.id != group_id() or not msg.reply_to_message:
        return
    prompt = store.prompt_get(msg.reply_to_message.message_id)
    if not prompt:
        return
    if msg.from_user.id != prompt["manager_id"]:
        await msg.reply_text("Написать клиенту может только менеджер, открывший этот запрос.")
        return
    case = store.find(prompt["case_id"])
    if not case or case["status"] != "В работе" or case["owner_id"] != msg.from_user.id:
        await msg.reply_text("Заявка уже не у вас в работе.")
        return
    content = (msg.text or "").strip()
    if not content or len(content) > 1400:
        await msg.reply_text("Нужен текст до 1400 символов.")
        return
    draft_id = store.draft_add(case["id"], msg.from_user.id, content)
    await msg.reply_text(
        "Предпросмотр сообщения клиенту:\n\n" + content +
        "\n\nОтправить?",
        reply_markup=keyboard([
            [("Отправить клиенту", "mgr:confirm:" + draft_id)],
            [("Не отправлять", "mgr:discard:" + draft_id)],
        ]))


async def my_cases(update, context):
    if update.effective_chat.id != group_id():
        return
    cases = store.my_cases(update.effective_user.id)
    if not cases:
        await update.message.reply_text("У вас пока нет заявок в работе.")
        return
    await update.message.reply_text(
        "Ваши заявки:\n" + "\n".join(x["id"] + " — " +
                                     x["summary"].replace("\n", " ")[:90]
                                     for x in cases))


async def customer_callback(update, context):
    q = update.callback_query
    if update.effective_chat.type != ChatType.PRIVATE:
        await q.answer("Только в личном чате", show_alert=True)
        return
    from private_client import resolve_group, case_belongs_to_client
    active_group = await resolve_group(context.bot, q.from_user)
    if not active_group:
        await q.answer("Клиентская группа не подтверждена.", show_alert=True)
        return
    code = q.data or ""
    if code == "cli:propose":
        await q.answer()
        context.user_data.clear()
        context.user_data["request_type"] = "Предложение компании"
        context.user_data["stage"] = "prop_name"
        await q.message.reply_text("Введите название компании, которую предлагаете:")
        return
    if code == "cli:new":
        await q.answer()
        context.user_data.clear()
        # Imported here to avoid a circular import at module load.
        from bot import home
        await q.message.reply_text("Выберите действие:", reply_markup=home())
        return
    parts = code.split(":", 2)
    if len(parts) != 3:
        await q.answer("Некорректное действие", show_alert=True)
        return
    _, action, rid = parts
    case = store.find(rid)
    if not case_belongs_to_client(case, q.from_user.id, active_group["chat_id"]):
        await q.answer("Заявка другого клиента или группы недоступна.",
                       show_alert=True)
        return
    if case["status"] not in ("Новая", "В работе"):
        await q.answer("Заявка уже закрыта", show_alert=True)
        return
    if action == "ask":
        await q.answer()
        context.user_data.clear()
        context.user_data["stage"] = "client_question"
        context.user_data["question_case"] = rid
        await q.message.reply_text(
            "Напишите ваш вопрос по заявке " + rid + ".\n"
            "Бот передаст его менеджерам.")
    elif action in ("ok", "other"):
        await q.answer("Передано менеджерам")
        label = ("Клиент подтвердил интерес к предложению."
                 if action == "ok" else "Клиент просит другой вариант.")
        store.note(rid, q.from_user.id, "client_" + action)
        await notify_group(context.bot, rid, label)
        await q.message.reply_text("Ответ передан менеджеру.",
                                   reply_markup=customer_keyboard(rid))
    elif action == "cancel":
        result, _ = store.transition(rid, q.from_user.id, "cancel")
        await q.answer("Заявка отменена" if result == "ok" else "Статус уже изменился")
        if result == "ok":
            await update_card(context.bot, rid)
            await notify_group(context.bot, rid, "Клиент отменил заявку.")
            from bot import home
            await q.message.reply_text("Заявка отменена. Можете начать новый подбор.",
                                       reply_markup=home())
    else:
        await q.answer("Неизвестная кнопка", show_alert=True)


async def client_question(update, context):
    from private_client import resolve_group, case_belongs_to_client
    verified = await resolve_group(context.bot, update.effective_user)
    rid = context.user_data.get("question_case")
    case = store.find(rid) if rid else None
    if (not verified or not case_belongs_to_client(
            case, update.effective_user.id, verified["chat_id"]) or
            case["status"] not in ("Новая", "В работе")):
        context.user_data.clear()
        await update.message.reply_text("Эта заявка недоступна. Начните заново: /start")
        return
    value = (update.message.text or "").strip()
    if not value or len(value) > 1200:
        await update.message.reply_text("Напишите вопрос текстом, не более 1200 символов.")
        return
    try:
        await notify_group(context.bot, rid, "Вопрос клиента:\n" + value)
    except Exception:
        log.exception("Failed to forward client question for %s", rid)
        await update.message.reply_text("Не удалось передать вопрос. Попробуйте ещё раз.")
        return
    store.note(rid, case["client_id"], "client_question")
    context.user_data.clear()
    await update.message.reply_text(
        "Вопрос передан менеджерам.",
        reply_markup=customer_keyboard(rid))


async def reminder_loop(app):
    """Best effort reminders while the Free Render instance is awake.

    Sleep/restart can delay them. A durable scheduler is needed for production.
    """
    while True:
        await asyncio.sleep(40)
        if not group_id():
            continue
        try:
            for case, minutes, previous_stage in store.due_reminders():
                try:
                    await notify_group(
                        app.bot, case["id"],
                        f"Напоминание: заявка не взята в работу уже {minutes} минут.\n"
                        "Нажмите «Взять в работу» на карточке заявки.")
                except Exception:
                    store.reset_reminder(case["id"], previous_stage)
                    log.exception("Could not send reminder for %s", case["id"])
        except Exception:
            log.exception("Reminder worker failed")
