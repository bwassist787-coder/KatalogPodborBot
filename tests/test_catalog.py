import unittest
from catalog import EXPECTED, parse_offers, filter_offers

class CatalogTests(unittest.TestCase):
    def demo(self):
        return [EXPECTED, ["DEMO-001", "ТЕСТОВАЯ КОМПАНИЯ", "0000000000",
                "Транспорт и логистика", "Грузоперевозки", "Демонстрационный груз",
                "С НДС", "100 000", "1 000 000", "11,5%", "3", "Тест"]]

    def test_valid_offer(self):
        item = parse_offers(self.demo())[0]
        self.assertEqual((item.min_sum, item.max_sum), (100000, 1000000))
        self.assertEqual(item.client_percent, "11,5%")

    def test_filter(self):
        offers = parse_offers(self.demo())
        self.assertEqual(len(filter_offers(offers, vat="С НДС", amount=500000)), 1)
        self.assertEqual(filter_offers(offers, vat="Без НДС"), [])
        self.assertEqual(filter_offers(offers, amount=1), [])
        self.assertEqual(filter_offers(offers, amount=2000000), [])

    def test_duplicate_ids_rejected(self):
        rows = self.demo()
        rows.append(rows[1][:])
        self.assertEqual(parse_offers(rows), [])

    def test_incomplete_not_released(self):
        rows = self.demo()
        rows[1][0] = ""
        self.assertEqual(parse_offers(rows), [])

    def test_unexpected_headers_fail(self):
        with self.assertRaises(ValueError):
            parse_offers([["Название", "Цена"]])

    def test_invalid_amount_fail(self):
        rows = self.demo()
        rows[1][7] = "не число"
        self.assertEqual(parse_offers(rows), [])

if __name__ == "__main__":
    unittest.main()
