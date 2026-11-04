"""Single HTTP server for Telegram webhook and authenticated Render health checks."""
import asyncio
import hmac
import logging
import os

from aiohttp import web
from telegram import Update

log = logging.getLogger(__name__)


def authorized(expected: str, supplied: str) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def make_http_app(telegram_app, secret: str):
    routes = web.Application(client_max_size=1024 * 1024)

    async def telegram_hook(request):
        if not authorized(secret, request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")):
            raise web.HTTPForbidden()
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        if update is None:
            raise web.HTTPBadRequest()
        await telegram_app.update_queue.put(update)
        return web.Response(text="ok")

    async def health(request):
        expected = os.getenv("HEALTHCHECK_TOKEN", "")
        if not authorized(expected, request.headers.get("X-Health-Token", "")):
            raise web.HTTPForbidden()
        return web.Response(text="ok", headers={"Cache-Control": "no-store"})

    routes.router.add_post("/telegram", telegram_hook)
    routes.router.add_get("/healthz", health)
    return routes


async def serve(telegram_app, port: int, webhook_url: str, secret: str, on_ready=None):
    await telegram_app.initialize()
    runner = None
    reminder_task = None
    try:
        # Bind the HTTP listener before registering the webhook.
        runner = web.AppRunner(make_http_app(telegram_app, secret))
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        await telegram_app.start()
        if on_ready is not None:
            reminder_task = asyncio.create_task(on_ready(telegram_app))
        await telegram_app.bot.set_webhook(
            url=webhook_url, secret_token=secret, drop_pending_updates=True,
        )
        log.info("Webhook listening on port %s", port)
        await asyncio.Event().wait()
    finally:
        if reminder_task is not None:
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass
        if telegram_app.running:
            await telegram_app.stop()
        if runner is not None:
            await runner.cleanup()
        await telegram_app.shutdown()


def run_server(telegram_app, port: int, webhook_url: str, secret: str, on_ready=None):
    asyncio.run(serve(telegram_app, port, webhook_url, secret, on_ready=on_ready))
