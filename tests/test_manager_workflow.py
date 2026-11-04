import os
import tempfile
import unittest
from unittest.mock import patch

import case_store as s


class ManagerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = s.DB_FILE
        s.DB_FILE = __import__("pathlib").Path(self.temp.name) / "cases.sqlite3"

    def tearDown(self):
        s.DB_FILE = self.old_db
        self.temp.cleanup()

    def add(self):
        s.register("REQ-TEST", 1001, -1003759930340, "Заявка", "Тестовая компания")
        s.set_group_message("REQ-TEST", 123)
        return s.find("REQ-TEST")

    def test_only_one_manager_can_take_and_release(self):
        self.add()
        status, case = s.take("REQ-TEST", 201, "@first")
        self.assertEqual(status, "ok")
        self.assertEqual(case["owner_name"], "@first")
        self.assertEqual(s.take("REQ-TEST", 202, "@second")[0], "busy")
        self.assertEqual(s.transition("REQ-TEST", 202, "release")[0], "forbidden")
        self.assertEqual(s.transition("REQ-TEST", 201, "release")[0], "ok")
        self.assertEqual(s.take("REQ-TEST", 202, "@second")[0], "ok")

    def test_cancel_customer_and_close(self):
        self.add()
        self.assertEqual(s.transition("REQ-TEST", 2222, "cancel")[0], "forbidden")
        self.assertEqual(s.transition("REQ-TEST", 1001, "cancel")[0], "ok")
        self.assertEqual(s.take("REQ-TEST", 201, "@manager")[0], "busy")
        self.assertEqual(s.find("REQ-TEST")["status"], "Отменена")

    def test_manager_must_confirm_draft(self):
        self.add()
        s.take("REQ-TEST", 201, "@first")
        did = s.draft_add("REQ-TEST", 201, "Пример предложения")
        self.assertIsNone(s.draft_claim(did, 202))
        self.assertIsNotNone(s.draft_claim(did, 201))
        self.assertIsNone(s.draft_claim(did, 201))
        s.draft_result(did, True)
        self.assertIsNone(s.draft_claim(did, 201))

    def test_reminders_at_15_25_30_minutes_only_while_new(self):
        case = self.add()
        start = case["created_at"]
        self.assertEqual(s.due_reminders(now=start + 14 * 60), [])
        self.assertEqual(s.due_reminders(now=start + 15 * 60)[0][1], 15)
        self.assertEqual(s.due_reminders(now=start + 20 * 60), [])
        self.assertEqual(s.due_reminders(now=start + 25 * 60)[0][1], 25)
        self.assertEqual(s.due_reminders(now=start + 30 * 60)[0][1], 30)
        self.assertEqual(s.due_reminders(now=start + 60 * 60), [])
        s.take("REQ-TEST", 201, "@manager")
        self.assertEqual(s.due_reminders(now=start + 100 * 60), [])

    def test_no_burst_after_service_sleep(self):
        case = self.add()
        start = case["created_at"]
        r = s.due_reminders(now=start + 40 * 60)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0][1], 30)
        self.assertEqual(s.due_reminders(now=start + 41 * 60), [])

    def test_new_offers_are_not_published_to_catalog(self):
        s.register("REQ-OFFER", 1002, -1003759930340,
                   "Предложение компании", "Не проверена ЗСК")
        self.assertEqual(s.find("REQ-OFFER")["kind"], "Предложение компании")
        self.assertEqual(s.find("REQ-OFFER")["status"], "Новая")
        self.assertEqual([x["kind"] for x in s.history("REQ-OFFER")], ["created"])


if __name__ == "__main__":
    unittest.main()
