# Test client forum: group-only workflow

All client actions happen inside the existing **test Telegram supergroup**, not in private messages with the bot.

## Operator setup (one time after deploying the updated bot)

1. Confirm Render still has `TEST_MODE=true`, a test group ID, its two topic IDs, and the internal `MANAGER_CHAT_ID`.
2. From an allowed operator Telegram account, send `/setup_topics` in **General** of the allowlisted test group.
3. Verify that the bot posts one button menu in each topic (General, client offers, our offers).
4. Pin **the new button menus**, and unpin/delete obsolete menus with links opening the bot's private chat.
5. Test BOTH flows only using fictitious company INNs and demo offers.

## Group UX

- **General**: buttons for "Подобрать компанию" and "Предложить компанию".
- **Предложения от Нас**: "Подобрать компанию". The bot asks for direction (buttons), VAT (buttons), amount (reply), shows allowed demo catalog offers (buttons), optional comment (reply), and confirms (button).
- **Предложения от Клиента**: "Предложить компанию". The bot asks for company name and INN (replies), direction and VAT (buttons), amount and conditions (replies), and confirmation (button).
- If initiated from General, the bot opens the dialog in the appropriate topic, never in DM.
- On confirmation, one customer-safe status card is posted in that topic and a separate manager card in the internal group.
- Managers may take the request in either group if authorized; only the internal group contains manager reply controls. Client group cards support questions, cancellation and starting a new request.
- Replies to the bot's text prompts are visible to members of that group. Do not request personal secrets, confidential internal comments or other clients' data.

## Safety

- Exact group/topic IDs are configured in Render. All other groups fail closed.
- Every active form is keyed by group, topic, Telegram user ID and a random token. Old buttons and another user's input cannot mutate it.
- Each confirmed request receives a stable ID and is associated with its original group and topic.
- Manager messages are relayed back only to the verified original client topic.
- A client-offered company does **not** become a catalog offer and is not marked ZSK-approved without separate review.
- On delivery uncertainty do **not** submit the request twice; check the manager group and logs manually.
- This is a test build: local SQLite may be lost on Render Free restart, so use dummy data and do not rely on reminders as guaranteed timed work.

## No consumer commands

The operator uses `/setup_topics` only to publish the menu. Clients then interact via inline buttons and explicit replies to the bot's prompts. No client commands or private bot DMs are required.
