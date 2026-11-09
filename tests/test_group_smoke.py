import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import case_store
import group_dialog
import group_forms
from catalog import Offer


class FullGroupFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true", "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2", "TEST_CLIENT_TOPIC_OUT_ID": "3",
            "MANAGER_CHAT_ID": "-1003759930340",
            "ALLOWED_USER_IDS": "67",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = case_store.DB_FILE
        case_store.DB_FILE = Path(self.tmp.name) / "case.sqlite3"
        self.user = SimpleNamespace(id=42, username="buyer", full_name="Buyer")
        self.sent = []
        async def send_message(**kwargs):
            result = SimpleNamespace(message_id=200+len(self.sent), **kwargs)
            self.sent.append(result)
            return result
        self.bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=send_message),
            get_chat=AsyncMock(return_value=SimpleNamespace(title="ТЕСТ Бот")),
        )
        self.context = SimpleNamespace(bot=self.bot)
        self.offer = Offer(
            id="DEMO-001", company="Тестовая логистика",
            inn="0000000001", category="Транспорт и логистика",
            subcategory="Грузоперевозки", purpose="Перевозки",
            vat="С НДС", min_sum=100000, max_sum=1000000,
            client_percent="11,5%", days="3", terms="Тест",
        )

    def tearDown(self):
        case_store.DB_FILE = self.previous
        self.tmp.cleanup()
        self.env.stop()

    async def click(self, data, message_id=100):
        query = SimpleNamespace(
            data=data, from_user=self.user, answer=AsyncMock(),
            message=SimpleNamespace(
                message_id=message_id,
                chat=SimpleNamespace(id=-1003794002701)))
        await group_dialog.callback(
            SimpleNamespace(callback_query=query), self.context)
        return query

    async def reply(self, stage, text, topic):
        flow = group_forms.read(-1003794002701, topic, 42)
        self.assertEqual(flow["stage"], stage)
        result = await group_dialog.text_input(
            SimpleNamespace(
                effective_chat=SimpleNamespace(id=-1003794002701),
                effective_user=self.user,
                effective_message=SimpleNamespace(
                    text=text,
                    reply_to_message=SimpleNamespace(
                        message_id=flow["prompt_id"],
                        from_user=SimpleNamespace(is_bot=True)),
                ),
            ), self.context)
        self.assertTrue(result)

    def flow(self, topic):
        return group_forms.read(-1003794002701, topic, 42)

    async def test_search_stays_in_topic_and_reaches_manager_group(self):
        with (patch.object(group_dialog, "read_offers", return_value=[self.offer]),
              patch.object(group_dialog, "save_request", return_value="REQ-SMOKE01")):
            await self.click("gf:start:search")
            f = self.flow(3)
            await self.click(f"gf:search_cat:{f['token']}:2", f["prompt_id"])
            f = self.flow(3)
            await self.click(f"gf:search_vat:{f['token']}:1", f["prompt_id"])
            await self.reply("search_amount", "500000", 3)
            f = self.flow(3)
            self.assertEqual(f["stage"], "results")
            await self.click(f"gf:choose:{f['token']}:0", f["prompt_id"])
            await self.reply("search_comment", "-", 3)
            f = self.flow(3)
            self.assertEqual(f["stage"], "confirm")
            await self.click(f"gf:submit:{f['token']}", f["prompt_id"])
            self.assertIsNone(self.flow(3))
        case = case_store.find("REQ-SMOKE01")
        self.assertIsNotNone(case)
        self.assertEqual(case["origin_topic_id"], 3)
        self.assertTrue(case["origin_message_id"])
        self.assertTrue(case["chat_message_id"])
        self.assertEqual(case["kind"], "Заявка")
        self.assertTrue(any(x.chat_id == -1003759930340 for x in self.sent))
        self.assertTrue(all(x.chat_id in (-1003794002701, -1003759930340)
                            for x in self.sent))
        self.assertTrue(all(x.chat_id != 42 for x in self.sent))

    async def test_customer_company_offer_stays_in_own_topic(self):
        with patch.object(group_dialog, "save_request",
                          return_value="REQ-SMOKE02"):
            await self.click("gf:start:offer")
            await self.reply("offer_name", "Тестовая компания", 2)
            await self.reply("offer_inn", "0000000001", 2)
            f = self.flow(2)
            await self.click(f"gf:offer_category:{f['token']}:2", f["prompt_id"])
            f = self.flow(2)
            await self.click(f"gf:offer_vat:{f['token']}:0", f["prompt_id"])
            await self.reply("offer_amount", "500000", 2)
            await self.reply("offer_comment", "Тестовое предложение", 2)
            f = self.flow(2)
            await self.click(f"gf:submit:{f['token']}", f["prompt_id"])
            self.assertIsNone(self.flow(2))
        case = case_store.find("REQ-SMOKE02")
        self.assertEqual(case["origin_topic_id"], 2)
        self.assertEqual(case["kind"], "Предложение компании")
        self.assertIn("Тестовая компания", case["client_summary"])
        self.assertEqual(case["origin_chat_id"], -1003794002701)
        self.assertNotIn("ЗСК проверено", case["client_summary"])


if __name__ == "__main__":
    unittest.main()
