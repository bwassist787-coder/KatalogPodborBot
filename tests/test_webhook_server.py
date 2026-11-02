import unittest
from webhook_server import authorized, make_http_app


class WebhookTests(unittest.TestCase):
    def test_authentication_denies_missing_or_wrong_tokens(self):
        self.assertFalse(authorized("", "value"))
        self.assertFalse(authorized("value", ""))
        self.assertFalse(authorized("value", "wrong"))
        self.assertTrue(authorized("value", "value"))

    def test_expected_routes(self):
        app = make_http_app(object(), "not-a-real-secret")
        paths = {route.resource.canonical for route in app.router.routes()}
        self.assertIn("/telegram", paths)
        self.assertIn("/healthz", paths)


if __name__ == "__main__":
    unittest.main()
