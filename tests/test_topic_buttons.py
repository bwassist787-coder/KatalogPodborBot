import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import forum_route


class TopicButtonsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "TEST_MODE": "true",
            "TEST_CLIENT_CHAT_ID": "-1003794002701",
            "TEST_CLIENT_TOPIC_IN_ID": "2",
            "TEST_CLIENT_TOPIC_OUT_ID": "3",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_menu_buttons_are_group_callbacks_not_private_links(self):
        for kind in ("search", "offer"):
            message, markup = forum_route.button_menu(kind)
            self.assertTrue(message)
            self.assertEqual(markup.inline_keyboard[0][0].callback_data,
                             "gf:start:" + kind)
            self.assertIsNone(markup.inline_keyboard[0][0].url)
        _, menu = forum_route.button_menu("general")
        self.assertEqual(len(menu.inline_keyboard), 2)
        self.assertEqual(menu.inline_keyboard[0][0].callback_data,
                         "gf:start:search")

    async def test_install_routes_to_exact_two_topics_and_general(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
            SimpleNamespace(message_id=100),
            SimpleNamespace(message_id=101),
            SimpleNamespace(message_id=102),
        ]))
        result = await forum_route.install_menus(bot)
        self.assertEqual(result, {"search": 100, "offer": 101, "general": 102})
        self.assertEqual(bot.send_message.await_count, 3)
        calls = [c.kwargs for c in bot.send_message.await_args_list]
        self.assertEqual([c["chat_id"] for c in calls],
                         [-1003794002701] * 3)
        self.assertEqual([c.get("message_thread_id") for c in calls],
                         [3, 2, None])

    async def test_old_catalog_command_ignores_incoming_thread_id(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        # Telegram can omit the topic ID; the command is not the user interface,
        # but must still send the button to the configured topic.
        message = SimpleNamespace(text="/catalog", message_thread_id=None)
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1003794002701),
            effective_message=message)
        context = SimpleNamespace(bot=bot)
        await forum_route.entry(update, context)
        self.assertEqual(bot.send_message.await_args.kwargs["message_thread_id"], 3)

    async def test_never_post_to_unregistered_group(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1009990009990),
            effective_message=SimpleNamespace(text="/catalog", message_thread_id=3))
        await forum_route.entry(update, SimpleNamespace(bot=bot))
        bot.send_message.assert_not_awaited()

    async def test_disabled_in_non_test_mode(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.dict(os.environ, {"TEST_MODE": "false"}):
            with self.assertRaises(ValueError):
                await forum_route.install_menus(bot)
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
