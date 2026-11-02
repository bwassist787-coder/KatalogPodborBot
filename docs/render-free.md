# Render free deployment (TEST ONLY)

**Scope:** Telegram catalog webhook only; ZSK is not yet deployed or automated.

1. Connect the GitHub repository to Render and create a Blueprint using `render.yaml` on a tested branch.
2. Configure the secrets in the Render dashboard:
   - `BOT_TOKEN`: BotFather token.
   - `WEBHOOK_BASE_URL`: exact public HTTPS URL assigned by Render, e.g. `https://YOUR-SERVICE.onrender.com` (without a slash at the end).
   - `WEBHOOK_SECRET`: generated secret; do not copy into code or chats.
   - `ALLOWED_USER_IDS`: allowlisted Telegram IDs, comma-separated.
   - `SHEET_ID` and `CATALOG_GID`: **demonstration data only**.
3. Keep `TEST_MODE=true` until a private Google service account, isolated client-facing data, and durable request storage are set up.
4. Bot launches with webhook mode when `WEBHOOK_BASE_URL` is present; the webhook endpoint is `/telegram`. Local development without that environment variable still uses polling.
5. Send `/start` and confirm that the service wakes after an idle period. Render Free spins down after 15 minutes of no inbound traffic and cold-start delays are expected. This is not a reliable always-on worker.
6. Do not use the ephemeral local SQLite database for production requests. Its data can disappear when the service restarts.
7. Free Render does not provide free continuously-running background workers or scheduled cron jobs. ZSK weekly checks need a separate scheduler (for example, GitHub Actions under an agreed security and authentication plan).

**Security:** Repository visibility may be public, but secrets, Telegram session files, service-account JSON and company-private data must never be committed. A Google Sheets worksheet is not a permission boundary; do not publish the entire workbook to expose only the catalog tab.


## Keepalive checks (weekdays, Moscow time)

After deploying the Render test webhook app, set these secrets in **GitHub → Settings → Secrets and variables → Actions**:
- `RENDER_BASE_URL` — the Render service origin, for example `https://your-name.onrender.com` (no path).
- `HEALTHCHECK_TOKEN` — a long random string, identical to the **Render Environment** secret with the same name.

Do not paste either value in repository files or chat. `/healthz` returns HTTP 200 only when the `X-Health-Token` header matches the secret, otherwise HTTP 403.

GitHub Actions workflow `.github/workflows/render-keepalive.yml` runs Mon–Fri every 10 minutes from **09:00 to 19:00 Europe/Moscow** (including 19:00). Scheduled workflows are only run from the **default branch**, so this will remain inactive while the change is under review. After tests and approval, merge to `main` to enable. You can use workflow_dispatch for a manual check after configuring both secrets.

Limitations: GitHub scheduled runs may be delayed or occasionally dropped; Render Free may still restart or sleep, and its ephemeral SQLite data is not durable. The check only confirms the web endpoint answered and does **not** perform ZSK verification.
