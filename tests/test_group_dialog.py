import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import case_store
import forum_route
import group_dialog
import group_forms


class InGroupDialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true",
            "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2",
            "TEST_CLIENT_TOPIC_OUT_ID": "3",
            "MANAGER_CHAT_ID": "-1003759930340",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = case_store.DB_FILE
        case_store.DB_FILE = Path(self.tmp.name) / "dialogs.sqlite3"
        self.user = SimpleNamespace(id=41, username="clientA", full_name="Client A")
        self.other = SimpleNamespace(id=42, username="clientB", full_name="Client B")
        self.sent = 0

        async def send_message(**kwargs):
            self.sent += 1
            return SimpleNamespace(message_id=100 + self.sent, **kwargs)
        self.bot = SimpleNamespace(send_message=AsyncMock(side_effect=send_message))
        self.context = SimpleNamespace(bot=self.bot)

    def tearDown(self):
        case_store.DB_FILE = self.old_db
        self.tmp.cleanup()
        self.env.stop()

    def fake_update(self, who, code, chat=-1003794002701, message_id=9):
        query = SimpleNamespace(
            data=code, from_user=who, answer=AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(id=chat),
                                    message_id=message_id),
        )
        return SimpleNamespace(callback_query=query), query

    async def test_launch_search_uses_group_topic_not_private_chat(self):
        update, q = self.fake_update(self.user, "gf:start:search")
        await group_dialog.callback(update, self.context)
        form = group_forms.read(-1003794002701, 3, 41)
        self.assertEqual(form["stage"], "search_cat")
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"],
                         -1003794002701)
        self.assertEqual(self.bot.send_message.await_args.kwargs["message_thread_id"], 3)
        self.assertEqual(self.bot.send_message.await_args.kwargs["reply_markup"]
                         .inline_keyboard[0][0].callback_data[:14], "gf:search_cat:")

    async def test_offer_and_search_states_are_isolated_by_topic_and_user(self):
        for who, kind in [(self.user, "search"), (self.user, "offer"),
                          (self.other, "search")]:
            update, _ = self.fake_update(who, "gf:start:" + kind)
            await group_dialog.callback(update, self.context)
        self.assertEqual(group_forms.read(-1003794002701, 3, 41)["stage"], "search_cat")
        self.assertEqual(group_forms.read(-1003794002701, 2, 41)["stage"], "offer_name")
        self.assertEqual(group_forms.read(-1003794002701, 3, 42)["stage"], "search_cat")

    async def test_another_client_cannot_use_someone_elses_form_buttons(self):
        update, _ = self.fake_update(self.user, "gf:start:search")
        await group_dialog.callback(update, self.context)
        flow = group_forms.read(-1003794002701, 3, 41)
        update2, q = self.fake_update(
            self.other, "gf:search_cat:" + flow["token"] + ":2",
            message_id=flow["prompt_id"])
        previous = self.bot.send_message.await_count
        await group_dialog.callback(update2, self.context)
        self.assertEqual(self.bot.send_message.await_count, previous)
        q.answer.assert_awaited()
        self.assertEqual(group_forms.read(-1003794002701, 3, 41)["stage"], "search_cat")

    async def test_replies_must_target_same_users_last_prompt(self):
        update, _ = self.fake_update(self.user, "gf:start:offer")
        await group_dialog.callback(update, self.context)
        form = group_forms.read(-1003794002701, 2, 41)
        msg = SimpleNamespace(
            text="Тестовая компания",
            reply_to_message=SimpleNamespace(
                message_id=form["prompt_id"], from_user=SimpleNamespace(is_bot=True)),
        )
        incoming = SimpleNamespace(
            effective_message=msg,
            effective_user=self.other,
            effective_chat=SimpleNamespace(id=-1003794002701))
        self.assertFalse(await group_dialog.text_input(incoming, self.context))
        incoming.effective_user = self.user
        self.assertTrue(await group_dialog.text_input(incoming, self.context))
        next_form = group_forms.read(-1003794002701, 2, 41)
        self.assertEqual(next_form["stage"], "offer_inn")
        self.assertEqual(next_form["data"]["company"], "Тестовая компания")

    def test_confirm_cannot_be_submitted_twice(self):
        token = group_forms.begin(-1003794002701, 3, 41, "confirm",
                                  {"kind": "search", "amount": 500000})
        self.assertTrue(group_forms.advance(-1003794002701, 3, 41,
                                            token, "confirm", {"kind": "search"}, 100))
        self.assertTrue(group_forms.claim_submit(-1003794002701, 3, 41, token, 100))
        self.assertFalse(group_forms.claim_submit(-1003794002701, 3, 41, token, 100))

    async def test_non_allowlisted_chat_does_not_start_form(self):
        update, q = self.fake_update(self.user, "gf:start:offer",
                                     chat=-1009990009990)
        await group_dialog.callback(update, self.context)
        self.bot.send_message.assert_not_awaited()
        q.answer.assert_awaited()
        self.assertIsNone(group_forms.read(-1003794002701, 2, 41))

    def test_legacy_group_case_card_redirects_client_to_private_bot(self):
        case = {"id": "REQ-TEST", "kind": "Заявка", "status": "Новая"}
        markup = forum_route.client_keyboard(case)
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertIn("mgr:take:REQ-TEST", [b.callback_data for b in buttons])
        self.assertIn("https://t.me/KatalogPodborBot?start=question_REQ-TEST",
                      [b.url for b in buttons])
        self.assertIn("https://t.me/KatalogPodborBot?start=route_s",
                      [b.url for b in buttons])
        self.assertTrue(all(
            not (b.callback_data or "").startswith("gf:") for b in buttons))


if __name__ == "__main__":
    unittest.main()
