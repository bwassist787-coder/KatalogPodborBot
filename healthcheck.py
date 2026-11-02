"""Authenticated endpoint used only by external uptime checks."""
import hmac
import os
from tornado.web import RequestHandler


class HealthHandler(RequestHandler):
    def get(self):
        expected = os.getenv("HEALTHCHECK_TOKEN", "")
        supplied = self.request.headers.get("X-Health-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            self.set_status(403)
            self.finish("Forbidden")
            return
        self.set_header("Cache-Control", "no-store")
        self.finish("ok")
