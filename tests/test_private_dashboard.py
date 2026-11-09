import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot as bot_module
import case_store
import private_client


class ClientPrivateDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true",
            "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2",
            "TEST_CLIENT_TOPIC_OUT_ID": "3",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = case_store.DB_FILE
        case_store.DB_FILE = Path(self.tmp.name) / "cases.sqlite3"
        self.user = SimpleNamespace(id=42, username="clientA", full_name="Client A")
        self.stranger = SimpleNamespace(id=43, username="stranger", full_name="Stranger")
        self.reply = AsyncMock()
        self.manager_send = AsyncMock(return_value=SimpleNamespace(message_id=912))
        self.bot = SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            get_chat=AsyncMock(return_value=SimpleNamespace(title="ТЕСТ Бот")),
            send_message=self.manager_send)
        self.context = SimpleNamespace(bot=self.bot, user_data={}, args=[])

    def tearDown(self):
        case_store.DB_FILE = self.old_path
        self.tmp.cleanup()
        self.env.stop()

    def update(self, callback=None, text=None, user=None):
        user = user or self.user
        message = SimpleNamespace(
            reply_text=self.reply, text=text, chat_id=user.id)
        if callback:
            q = SimpleNamespace(data=callback, from_user=user, message=message,
                                answer=AsyncMock())
            return SimpleNamespace(
                callback_query=q,
                effective_chat=SimpleNamespace(type="private", id=user.id),
                effective_user=user, effective_message=message,
                message=message)
        return SimpleNamespace(
            callback_query=None,
            effective_chat=SimpleNamespace(type="private", id=user.id),
            effective_user=user, effective_message=message, message=message)

    async def test_only_verified_group_member_can_enter(self):
        self.assertEqual(
            (await private_client.resolve_group(self.bot, self.user))["chat_id"],
            -1003794002701)
        self.bot.get_chat_member.return_value = SimpleNamespace(status="left")
        self.assertIsNone(await private_client.resolve_group(self.bot, self.stranger))

    def test_client_dashboard_is_scoped_by_user_and_exact_group(self):
        case_store.register("R1", 42, -1003759930340, "Свободный запрос", "first",
                            client_group_id=-1003794002701, client_group_title="ТЕСТ Бот")
        case_store.register("R2", 43, -1003759930340, "Свободный запрос", "other",
                            client_group_id=-1003794002701, client_group_title="ТЕСТ Бот")
        case_store.register("R3", 42, -1003759930340, "Свободный запрос", "wrong group",
                            client_group_id=-1003794002702, client_group_title="Другой")
        cases = case_store.client_cases(42, -1003794002701)
        self.assertEqual([x["id"] for x in cases], ["R1"])
        self.assertTrue(private_client.case_belongs_to_client(
            case_store.find("R1"), 42, -1003794002701))
        self.assertFalse(private_client.case_belongs_to_client(
            case_store.find("R2"), 42, -1003794002701))
        self.assertFalse(private_client.case_belongs_to_client(
            case_store.find("R3"), 42, -1003794002701))

    async def test_full_freeform_request_stays_private_and_reaches_manager(self):
        with (patch.object(bot_module, "MANAGER_CHAT_ID", "-1003759930340"),
              patch.object(bot_module, "save_request", return_value="REQ-PRIVATE-1")):
            await bot_module.callback(self.update(callback="menu:free"), self.context)
            self.assertEqual(self.context.user_data["stage"], "free_text")
            await bot_module.message(self.update(
                text="Нужна строительная компания на 800000 с НДС, документы обсудим."),
                self.context)
            self.assertEqual(self.context.user_data["stage"], "confirm")
            await bot_module.callback(self.update(callback="lead:send"), self.context)
            self.assertNotIn("stage", self.context.user_data)
        case = case_store.find("REQ-PRIVATE-1")
        self.assertEqual(case["client_group_id"], -1003794002701)
        self.assertIsNone(case["origin_chat_id"])
        self.assertEqual(case["kind"], "Свободный запрос")
        self.assertEqual(case["client_id"], 42)
        self.assertTrue(case["chat_message_id"])
        self.manager_send.assert_awaited_once()
        self.assertEqual(self.manager_send.await_args.kwargs["chat_id"], -1003759930340)
        self.assertNotIn("ИНН", case["client_summary"])
        self.assertIn("строительная компания", case["client_summary"])
        await bot_module.callback(self.update(callback="dash:list:active"), self.context)
        args = self.reply.await_args.kwargs
        self.assertIn("reply_markup", args)

    async def test_private_dashboard_never_opens_other_clients_case(self):
        case_store.register("R-FOREIGN", 43, -1003759930340, "Заявка", "secret",
                            client_group_id=-1003794002701)
        await bot_module.callback(
            self.update(callback="dash:view:R-FOREIGN"), self.context)
        self.assertIn("недоступна", self.reply.await_args.args[0])

    async def test_legacy_group_button_never_activates_old_group_form(self):
        query = SimpleNamespace(
            data="gf:start:search", from_user=self.user,
            message=SimpleNamespace(reply_text=AsyncMock()), answer=AsyncMock())
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=-1003794002701))
        await bot_module.legacy_group_button(update, self.context)
        query.answer.assert_awaited_once()
        self.assertEqual(case_store.client_cases(42, -1003794002701), [])
        self.manager_send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
