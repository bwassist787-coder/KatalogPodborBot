import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import case_store
import forum_route
import manager_chat


class ForumRoutingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true",
            "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2",
            "TEST_CLIENT_TOPIC_OUT_ID": "3",
        })
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_original = case_store.DB_FILE
        case_store.DB_FILE = Path(self.tmp.name) / "test.sqlite3"

    def tearDown(self):
        case_store.DB_FILE = self.db_original
        self.tmp.cleanup()
        self.env.stop()

    def test_exact_three_topic_routes(self):
        self.assertTrue(forum_route.configured())
        self.assertEqual(forum_route.origin("offer"), {
            "chat_id": -1003794002701, "thread_id": 2, "kind": "offer"
        })
        self.assertEqual(forum_route.origin("search")["thread_id"], 3)
        self.assertFalse(forum_route.group_topic_matches(-1003794002702, 2, "offer"))
        self.assertFalse(forum_route.group_topic_matches(-1003794002701, 3, "offer"))
        self.assertTrue(forum_route.group_topic_matches(-1003794002701, 2, "offer"))

    def test_missing_environment_blocks_all_routes(self):
        with patch.dict(os.environ, {
            "TEST_CLIENT_CHAT_ID": "",
            "TEST_CLIENT_TOPIC_IN_ID": "2",
            "TEST_CLIENT_TOPIC_OUT_ID": "3",
        }):
            self.assertFalse(forum_route.configured())
            with self.assertRaises(ValueError):
                forum_route.origin("search")

    def test_manager_and_client_cards_share_status(self):
        rid = "REQ-TEST001"
        case_store.register(
            rid, 42, -1003759930340, "Заявка", "Служебная информация",
            origin_chat_id=-1003794002701, origin_topic_id=3,
            origin_chat_title="ТЕСТ Бот", origin_username="@client",
            client_summary="Направление: Транспорт\nСумма: 500 000 ₽")
        case_store.set_origin_message(rid, 57)
        a = case_store.find(rid)
        self.assertIn("Статус: Новая", forum_route.client_card(a))
        self.assertIn("Взять в работу", str(forum_route.client_keyboard(a).to_dict()))
        self.assertIn("ТЕСТ Бот", manager_chat.card(a))
        self.assertIn("https://t.me/c/3794002701/57", manager_chat.card(a))
        status, _ = case_store.take(rid, 20, "@manager")
        self.assertEqual(status, "ok")
        updated = case_store.find(rid)
        self.assertIn("@manager", forum_route.client_card(updated))
        self.assertNotIn("Взять в работу", str(forum_route.client_keyboard(updated).to_dict()))

    def test_proposed_company_is_not_published_to_catalog(self):
        rid = "REQ-SUPPLY"
        case_store.register(
            rid, 42, -1003759930340, "Предложение компании", "INTERNAL",
            origin_chat_id=-1003794002701, origin_topic_id=2,
            origin_chat_title="ТЕСТ Бот", client_summary="Компания: Тестовая")
        case = case_store.find(rid)
        self.assertEqual(case["kind"], "Предложение компании")
        self.assertEqual(case["origin_topic_id"], 2)
        self.assertIn("Предложение клиента", forum_route.client_card(case))


if __name__ == "__main__":
    unittest.main()
