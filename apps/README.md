# Two modules, one repository

- `apps/catalog/`: Telegram client catalog. The tested implementation currently remains in the repository root (`bot.py`, `catalog.py`) for compatibility; move only after automated test coverage and deployment validation.
- `apps/zsk/`: independent ZSK verification module. NOT YET CONNECTED to Benefitsar or Google Sheets.
- One Google spreadsheet can hold separate protected internal and public-facing *data views*, but Google Sheets does not provide access isolation per tab. Never publish the internal workbook or export it as a public CSV.
- ZSK updates must match a validated INN, preserve prior result and timestamp, and fail closed on parsing/network errors.
- No automation may present an unverified ZSK state as green; do not send a company to the customer catalog solely from an unchecked flag.
- One hosting deployment for the catalog webhook; separate scheduled ZSK process after its authorization, storage and test strategy are validated.
