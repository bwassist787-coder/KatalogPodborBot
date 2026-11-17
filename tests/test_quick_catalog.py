import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import case_store
import quick_catalog
from catalog import Offer


def offer(n, *, cat="Строительство и СМР", inn=None):
    return Offer(
        id=f"DEMO-{n:03d}",
        company=f"ТЕСТ — Компания {n}",
        inn=inn or f"{n:010d}",
        category=cat, subcategory="Общие", purpose="Тестовые услуги",
        vat="С НДС", min_sum=10000, max_sum=1000000,
        client_percent=f"{10+n/10:.1f}%".replace(".", ","),
        days=str(n), terms="Для тестирования",
    )


class QuickCatalogTests(unittest.IsolatedAsyncioTestCase):
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
        self.user = SimpleNamespace(id=42, username="client", full_name="Тест")
        self.message = SimpleNamespace(reply_text=AsyncMock())
        self.send = AsyncMock(return_value=SimpleNamespace(message_id=999))
        self.bot = SimpleNamespace(send_message=self.send)
        self.ctx = SimpleNamespace(bot=self.bot, user_data={})
        self.group = {"chat_id": -1003794002701, "title": "ТЕСТ Бот"}

    def tearDown(self):
        case_store.DB_FILE = self.old_path
        self.tmp.cleanup()
        self.env.stop()

    async def click(self, code):
        q = SimpleNamespace(
            data=code, from_user=self.user,
            edit_message_text=AsyncMock(), answer=AsyncMock(),
            message=self.message,
        )
        await quick_catalog.callback(q, self.ctx, self.group)
        return q

    async def begin_and_choose_category(self, category_index=0, offers=None):
        await quick_catalog.start(self.message, self.ctx)
        token = self.ctx.user_data["quick_catalog"]["nonce"]
        with patch.object(quick_catalog, "read_offers",
                          return_value=offers or []):
            q = await self.click(f"browse:cat:{token}:{category_index}")
        return token, q

    def test_client_catalog_selection_is_exact_and_unique_by_inn(self):
        first = offer(1)
        alt = offer(2, inn=first.inn)
        rows = [first, alt, offer(3, cat="Электроника")]
        selected = quick_catalog.selected_group(rows, "Строительство и СМР")
        self.assertEqual([x.id for x in selected], [first.id])

    async def test_category_only_and_five_card_pagination(self):
        rows = [offer(n) for n in range(1, 13)]
        token, q = await self.begin_and_choose_category(0, rows)
        text = q.edit_message_text.await_args.kwargs["text"]
        markup = q.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("Компании 1–5 из 12", text)
        self.assertEqual(text.count("Цена:"), 5)
        self.assertEqual(text.count("ИНН:"), 5)
        self.assertEqual(text.count("Срок:"), 5)
        self.assertNotIn("Назначение:", text)
        self.assertNotIn("НДС:", text)
        self.assertIn("11,5%", text)
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f"browse:page:{token}:0:1", actions)
        self.assertIn(f"browse:pick:{token}:0:4", actions)
        self.assertIn(f"browse:manager:{token}", actions)
        with patch.object(quick_catalog, "read_offers", return_value=rows):
            q2 = await self.click(f"browse:page:{token}:0:1")
        page2 = q2.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Компании 6–10 из 12", page2)
        self.assertNotIn("Компания 1\n", page2)
        with patch.object(quick_catalog, "read_offers", return_value=rows):
            q3 = await self.click(f"browse:page:{token}:1:2")
        page3 = q3.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Компании 11–12 из 12", page3)
        actions3 = [b.text for row in
                    q3.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard
                    for b in row]
        self.assertNotIn("Ещё компании", actions3)

    async def test_selected_company_generates_manager_request_once(self):
        rows = [offer(n) for n in range(1, 7)]
        token, _ = await self.begin_and_choose_category(0, rows)
        with patch.object(quick_catalog, "read_offers", return_value=rows):
            q = await self.click(f"browse:pick:{token}:0:2")
        self.assertIn("ТЕСТ — Компания 3", q.edit_message_text.await_args.kwargs["text"])
        self.assertEqual(self.ctx.user_data["quick_catalog"]["selected_id"], "DEMO-003")
        with (patch.object(quick_catalog, "read_offers", return_value=rows),
              patch.object(quick_catalog, "save_request",
                           return_value="REQ-QUICK003") as save,
              patch("bot.MANAGER_CHAT_ID", "-1003759930340")):
            await self.click(f"browse:send:{token}")
        case = case_store.find("REQ-QUICK003")
        self.assertEqual(case["kind"], "Заявка")
        self.assertEqual(case["client_group_id"], -1003794002701)
        self.assertEqual(case["client_id"], 42)
        self.assertIn("Цена:", case["client_summary"])
        self.assertIn("11", case["client_summary"])
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.args[4].id, "DEMO-003")
        self.send.assert_awaited_once()
        self.assertEqual(self.send.await_args.kwargs["chat_id"], -1003759930340)
        await self.click(f"browse:send:{token}")
        self.assertEqual(save.call_count, 1)

    async def test_no_results_suggest_manager_not_fake_five(self):
        token, q = await self.begin_and_choose_category(3, [offer(1)])
        text = q.edit_message_text.await_args.kwargs["text"]
        self.assertIn("Пока нет доступных компаний", text)
        self.assertEqual(text.count("Цена:"), 0)
        markup = q.edit_message_text.await_args.kwargs["reply_markup"]
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn(f"browse:manager:{token}", actions)

    async def test_manager_request_without_client_inn_or_amount(self):
        token, _ = await self.begin_and_choose_category(0, [offer(1)])
        await self.click(f"browse:manager:{token}")
        with (patch.object(quick_catalog, "save_request",
                           return_value="REQ-MANAGER") as save,
              patch("bot.MANAGER_CHAT_ID", "-1003759930340")):
            await self.click(f"browse:send:{token}")
        case = case_store.find("REQ-MANAGER")
        self.assertEqual(case["kind"], "Не нашёл нужное")
        self.assertIn("Категория:", case["client_summary"])
        self.assertEqual(save.call_args.args[4], None)
        self.assertEqual(save.call_args.args[5], "")
        self.assertEqual(save.call_args.args[6], None)

    async def test_old_category_buttons_and_other_sessions_cannot_pick(self):
        rows = [offer(1)]
        token, _ = await self.begin_and_choose_category(0, rows)
        original = self.ctx.user_data["quick_catalog"]["page_offer_ids"]
        q = await self.click(f"browse:pick:deadbeef:0:0")
        q.answer.assert_awaited()
        self.assertEqual(
            self.ctx.user_data["quick_catalog"]["page_offer_ids"], original)
        self.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
