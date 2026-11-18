import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import case_store
import forum_route
import group_forms
import group_quick_catalog as quick
from catalog import Offer


def offer(n, *, category="Строительство и СМР", inn=None):
    return Offer(
        id=f"TEST-{n:02d}", company=f"Тестовая компания {n}",
        inn=inn or f"{n:010d}", category=category,
        subcategory="Демо", purpose="Тестовые работы", vat="С НДС",
        min_sum=None, max_sum=None,
        client_percent=f"{10+n/10:.1f}%".replace(".", ","),
        days=str(n), terms="Демо")


class GroupOnlyBrowseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true", "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2", "TEST_CLIENT_TOPIC_OUT_ID": "3",
            "MANAGER_CHAT_ID": "-1003759930340",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = case_store.DB_FILE
        case_store.DB_FILE = Path(self.tmp.name) / "cases.sqlite"
        self.user = SimpleNamespace(id=42, username="customer", full_name="Клиент тест")
        self.other = SimpleNamespace(id=99, username="stranger", full_name="Другой")
        self.messages = []
        async def send_message(**kwargs):
            msg = SimpleNamespace(message_id=101+len(self.messages), **kwargs)
            self.messages.append(msg)
            return msg
        self.api = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            get_chat=AsyncMock(return_value=SimpleNamespace(title="ТЕСТ Бот")),
        )
        self.ctx = SimpleNamespace(bot=self.api, user_data={})
        self.offers = [offer(i) for i in range(1, 13)]

    def tearDown(self):
        case_store.DB_FILE = self.prev
        self.tmp.cleanup()
        self.env.stop()

    def query(self, data, user=None, message_id=1, chat_id=-1003794002701):
        q = SimpleNamespace(
            data=data, from_user=user or self.user,
            message=SimpleNamespace(
                chat=SimpleNamespace(id=chat_id), message_id=message_id,
                reply_text=AsyncMock(),
            ),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        return SimpleNamespace(callback_query=q), q

    async def test_all_client_menu_buttons_are_in_group_callbacks(self):
        for kind in ("general", "search", "offer"):
            text, keyboard = forum_route.button_menu(kind)
            self.assertNotIn("личном чате с ботом", text)
            buttons = [b for row in keyboard.inline_keyboard for b in row]
            self.assertTrue(all(b.url is None for b in buttons))
        self.assertEqual(
            forum_route.button_menu("search")[1].inline_keyboard[0][0].callback_data,
            "qg:start:search")

    async def start_and_select(self, category_index=0):
        update, q = self.query("qg:start:search")
        await quick.callback(update, self.ctx)
        flow = group_forms.read(-1003794002701, 3, 42)
        self.assertEqual(flow["stage"], "quick_category")
        self.assertEqual(self.messages[-1].chat_id, -1003794002701)
        self.assertEqual(self.messages[-1].message_thread_id, 3)
        with patch.object(quick, "read_offers", return_value=self.offers):
            update, q = self.query(
                f"qg:cat:{flow['token']}:{category_index}",
                message_id=flow["prompt_id"])
            await quick.callback(update, self.ctx)
        return flow["token"], q

    async def test_five_entries_prices_and_next_five_in_one_topic(self):
        nonce, q = await self.start_and_select()
        text = q.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Компании 1–5 из 12", text)
        self.assertEqual(text.count("ИНН:"), 5)
        self.assertEqual(text.count("Цена:"), 5)
        self.assertEqual(text.count("Срок:"), 5)
        self.assertNotIn("НДС:", text)
        self.assertNotIn("Назначение:", text)
        buttons = [b for row in q.edit_message_text.await_args.kwargs[
            "reply_markup"].inline_keyboard for b in row]
        self.assertIn("Ещё компании", [b.text for b in buttons])
        flow = group_forms.read(-1003794002701, 3, 42)
        with patch.object(quick, "read_offers", return_value=self.offers):
            update, q2 = self.query(f"qg:page:{nonce}:0:1",
                                    message_id=flow["prompt_id"])
            await quick.callback(update, self.ctx)
        self.assertIn("Компании 6–10 из 12",
                      q2.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(group_forms.read(-1003794002701, 3, 42)["data"]["page"], 1)

    async def test_select_company_confirms_once_and_routes_both_cards(self):
        nonce, _ = await self.start_and_select()
        flow = group_forms.read(-1003794002701, 3, 42)
        with patch.object(quick, "read_offers", return_value=self.offers):
            update, q = self.query(f"qg:pick:{nonce}:0:0",
                                   message_id=flow["prompt_id"])
            await quick.callback(update, self.ctx)
        self.assertIn("Тестовая компания 1",
                      q.edit_message_text.await_args.kwargs["text"])
        flow = group_forms.read(-1003794002701, 3, 42)
        self.assertEqual(flow["stage"], "confirm")
        with (patch.object(quick, "read_offers", return_value=self.offers),
              patch.object(quick, "save_request",
                           return_value="REQ-GROUP0001") as save):
            update, q = self.query(f"qg:send:{nonce}",
                                   message_id=flow["prompt_id"])
            await quick.callback(update, self.ctx)
            await quick.callback(update, self.ctx)
        self.assertEqual(save.call_count, 1)
        case = case_store.find("REQ-GROUP0001")
        self.assertEqual(case["origin_chat_id"], -1003794002701)
        self.assertEqual(case["origin_topic_id"], 3)
        self.assertEqual(case["client_group_id"], -1003794002701)
        self.assertTrue(case["origin_message_id"])
        self.assertTrue(case["chat_message_id"])
        self.assertIn("Цена: 10,1%", case["client_summary"])
        self.assertTrue(any(m.chat_id == -1003759930340 for m in self.messages))
        self.assertFalse(any(m.chat_id == 42 for m in self.messages))

    async def test_other_member_cannot_click_owners_form(self):
        nonce, _ = await self.start_and_select()
        flow = group_forms.read(-1003794002701, 3, 42)
        update, q = self.query(f"qg:pick:{nonce}:0:1",
                               user=self.other, message_id=flow["prompt_id"])
        await quick.callback(update, self.ctx)
        q.answer.assert_awaited()
        self.assertEqual(group_forms.read(-1003794002701, 3, 42)["stage"],
                         "quick_results")
        self.assertIsNone(group_forms.read(-1003794002701, 3, 99))

    async def test_exact_chat_and_group_membership_required(self):
        update, q = self.query("qg:start:search", chat_id=-1003794002702)
        await quick.callback(update, self.ctx)
        self.api.send_message.assert_not_awaited()
        self.api.get_chat_member.return_value = SimpleNamespace(status="left")
        update, q = self.query("qg:start:search")
        await quick.callback(update, self.ctx)
        self.api.send_message.assert_not_awaited()

    async def test_private_messages_offer_no_catalog(self):
        reply = AsyncMock()
        u = SimpleNamespace(
            effective_chat=SimpleNamespace(type="private", id=42),
            effective_user=self.user,
            effective_message=SimpleNamespace(reply_text=reply),
        )
        context = SimpleNamespace(user_data={"stage": "lead_inn"},
                                  bot=self.api, args=[])
        await bot.start(u, context)
        self.assertEqual(context.user_data, {})
        self.assertIn("только", reply.await_args.args[0])
        self.api.send_message.assert_not_awaited()
        reply.reset_mock()
        await bot.message(u, context)
        self.assertIn("только", reply.await_args.args[0])
        self.assertFalse(context.user_data)


if __name__ == "__main__":
    unittest.main()
