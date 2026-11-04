import unittest
from unittest.mock import patch

import manager_chat as chat


class ManagerButtonsTests(unittest.TestCase):
    @staticmethod
    def labels(markup):
        return [button.text for row in markup.inline_keyboard for button in row]

    def test_unclaimed_case_only_take(self):
        case = {"id": "REQ-X", "status": "Новая", "owner_name": None}
        self.assertEqual(self.labels(chat.manager_keyboard(case)), ["Взять в работу"])

    def test_working_case_has_actions(self):
        case = {"id": "REQ-X", "status": "В работе", "owner_name": "@manager"}
        self.assertEqual(self.labels(chat.manager_keyboard(case)),
                         ["Написать клиенту", "Освободить", "Завершить", "Отменить"])

    def test_closed_has_no_buttons(self):
        case = {"id": "REQ-X", "status": "Отменена", "owner_name": "@manager"}
        self.assertIsNone(chat.manager_keyboard(case))

    def test_offer_and_search_client_buttons_are_different(self):
        with patch.object(chat.store, "find", return_value={"kind": "Предложение компании"}):
            labels = self.labels(chat.customer_keyboard("REQ-X", offer=True))
            self.assertIn("Есть интерес", labels)
            self.assertIn("Предложить ещё компанию", labels)
            self.assertNotIn("Другой вариант", labels)
        with patch.object(chat.store, "find", return_value={"kind": "Заявка"}):
            labels = self.labels(chat.customer_keyboard("REQ-X", offer=True))
            self.assertIn("Подходит", labels)
            self.assertIn("Другой вариант", labels)
            self.assertIn("Новый подбор", labels)


if __name__ == "__main__":
    unittest.main()
