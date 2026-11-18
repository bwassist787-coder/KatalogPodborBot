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

    async def test_private_start_cannot_create_case_or_show_catalog(self):
        await bot_module.start(self.update(), self.context)
        self.assertIn("только", self.reply.await_args.args[0])
        self.manager_send.assert_not_awaited()
        self.assertEqual(case_store.client_cases(42, -1003794002701), [])

    async def test_old_private_buttons_cannot_open_case_or_submit(self):
        for data in ("browse:start", "menu:propose", "menu:free",
                     "lead:send", "dash:list:active", "dash:view:R-FOREIGN"):
            update = self.update(callback=data)
            await bot_module.callback(update, self.context)
            update.callback_query.answer.assert_awaited()
        self.assertEqual(self.context.user_data, {})
        self.manager_send.assert_not_awaited()

    async def test_old_private_text_cannot_submit_request(self):
        self.context.user_data.update(stage="lead_comment", selected_id="DEMO-001")
        await bot_module.message(self.update(text="Компания на 500000"), self.context)
        self.assertIn("только", self.reply.await_args.args[0])
        self.assertEqual(self.context.user_data, {})
        self.manager_send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
