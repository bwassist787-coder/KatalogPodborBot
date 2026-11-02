# ZSK verification module — not enabled

Planned workflow:
1. Read unique normalized 10/12 digit INN values from the protected Google Sheet.
2. Group valid INNs in batches of no more than five.
3. Query an authorized ZSK source; never bypass bot rate limits or anti-abuse controls.
4. Validate a result is traceable to the exact INN; ambiguous results are flagged **Нужна проверка**.
5. Update only the intended company's ZSK status, checked-at timestamp and audit history.
6. Red/Yellow/Green are valid verified statuses; network errors and missing data are distinct states, not Green.

Before enabling: choose credentials, Telegram bot interaction pattern, private sheet permissions, scheduling and audit log storage. Keep Telegram session files and Google credentials out of GitHub.
