import unittest
from healthcheck import HealthHandler


class HealthcheckTests(unittest.TestCase):
    def test_handler_is_defined(self):
        self.assertTrue(callable(HealthHandler.get))


if __name__ == "__main__":
    unittest.main()
