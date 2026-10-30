"""Read only the client-facing catalog tab, never supplier base rates."""
from __future__ import annotations
import csv
import io
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests

SHEET_ID = os.getenv("SHEET_ID", "1MnLci1DvDgHsahZfW4CQt_QqiN_uuaoidXaX9of-5rc")
CATALOG_GID = os.getenv("CATALOG_GID", "1884118558")
TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"
DB_FILE = Path(os.getenv("DB_FILE", "test_requests.sqlite3"))
CATEGORIES = [
    "Строительство и СМР", "Стройматериалы", "Транспорт и логистика",
    "IT и ПО", "Оборудование", "Авто и запчасти", "Товары",
    "Деловые услуги", "Аренда",
]
NEARBY = {
    "Строительство и СМР": ["Стройматериалы", "Оборудование"],
    "Стройматериалы": ["Строительство и СМР", "Оборудование"],
    "Транспорт и логистика": ["Аренда", "Деловые услуги"],
    "IT и ПО": ["Оборудование", "Деловые услуги"],
    "Оборудование": ["Аренда", "Авто и запчасти"],
    "Авто и запчасти": ["Оборудование", "Транспорт и логистика"],
    "Товары": ["Стройматериалы", "Оборудование"],
    "Деловые услуги": ["IT и ПО", "Транспорт и логистика"],
    "Аренда": ["Оборудование", "Транспорт и логистика"],
}
EXPECTED = ["ID", "Компания", "ИНН", "Направление", "Подкатегория", "Назначение",
            "НДС", "Мин. ₽", "Макс. ₽", "Цена %", "Срок, дн.", "Условия"]

@dataclass(frozen=True)
class Offer:
    id: str
    company: str
    inn: str
    category: str
    subcategory: str
    purpose: str
    vat: str
    min_sum: int | None
    max_sum: int | None
    client_percent: str
    days: str
    terms: str

def parse_amount(value: str) -> int | None:
    if not str(value).strip():
        return None
    cleaned = re.sub(r"[\s\u00a0₽]", "", str(value)).replace(",", ".")
    try:
        amount = float(cleaned)
        if amount < 0 or not amount.is_integer():
            raise ValueError("Amount must be nonnegative integer")
        return int(amount)
    except ValueError as exc:
        raise ValueError("Invalid catalog amount") from exc

def parse_offers(rows: list[list[str]]) -> list[Offer]:
    if not rows or rows[0][:len(EXPECTED)] != EXPECTED:
        raise ValueError("Unexpected 'Каталог_бот' headers")
    items = []
    ids = set()
    duplicates = set()
    for values in rows[1:]:
        v = (values + [""] * len(EXPECTED))[:len(EXPECTED)]
        if not any(str(x).strip() for x in v):
            continue
        oid, company, inn, cat, sub, purpose, vat, lo, hi, price, days, terms = [str(x).strip() for x in v]
        if (not oid or not company or not re.fullmatch(r"\d{10}|\d{12}", inn)
                or cat not in CATEGORIES or not purpose or not vat or not price or not days):
            continue
        try:
            lo_n, hi_n = parse_amount(lo), parse_amount(hi)
        except ValueError:
            continue
        if lo_n is not None and hi_n is not None and lo_n > hi_n:
            continue
        if oid in ids:
            duplicates.add(oid)
            continue
        ids.add(oid)
        items.append(Offer(oid, company, inn, cat, sub, purpose, vat, lo_n, hi_n, price, days, terms))
    return [x for x in items if x.id not in duplicates]

def _google_session():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    file_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not raw and not file_path:
        return None
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(raw) if raw else json.loads(Path(file_path).read_text(encoding="utf-8"))
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return AuthorizedSession(credentials)

def read_offers() -> list[Offer]:
    session = _google_session()
    if session:
        encoded = quote("'Каталог_бот'!A1:L601", safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{encoded}"
        response = session.get(url, params={"valueRenderOption": "FORMATTED_VALUE"}, timeout=20)
        response.raise_for_status()
        rows = response.json().get("values", [])
    elif TEST_MODE:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export"
        response = requests.get(url, params={"format": "csv", "gid": CATALOG_GID}, timeout=20)
        response.raise_for_status()
        if "text/html" in response.headers.get("Content-Type", "").lower():
            raise RuntimeError("Google returned login page instead of CSV")
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    else:
        raise RuntimeError("Private mode needs GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")
    return parse_offers(rows)

def filter_offers(offers: list[Offer], category: str | None = None,
                  vat: str | None = None, amount: int | None = None) -> list[Offer]:
    result = []
    for offer in offers:
        if category and offer.category != category:
            continue
        if vat and vat != "Любой" and offer.vat != vat:
            continue
        if amount is not None:
            if offer.min_sum is not None and amount < offer.min_sum:
                continue
            if offer.max_sum is not None and amount > offer.max_sum:
                continue
        result.append(offer)
    return result

def save_request(user_id: int, username: str, request_type: str,
                 offer: Offer | None, client_inn: str, amount: int | None,
                 comment: str) -> str:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    request_id = f"REQ-{now:%Y%m%d}-{secrets.token_hex(5).upper()}"
    row = [request_id, now.strftime("%Y-%m-%d %H:%M:%S"), str(user_id), username,
           "Личный диалог с ботом", request_type, offer.id if offer else "",
           offer.company if offer else "", client_inn, amount or "", comment,
           "Новая", "", ""]
    if not TEST_MODE:
        session = _google_session()
        if session is None:
            raise RuntimeError("Missing Google service account credentials")
        range_value = quote("'Заявки'!A:N", safe="")
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{range_value}:append"
        response = session.post(url, params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                                json={"values": [row]}, timeout=20)
        response.raise_for_status()
        if response.json().get("updates", {}).get("updatedRows") != 1:
            raise RuntimeError("Google Sheets append not verified")
    else:
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, user_id INTEGER NOT NULL,
                username TEXT, type TEXT, offer_id TEXT, company TEXT,
                client_inn TEXT, amount INTEGER, comment TEXT, status TEXT)""")
            conn.execute("""INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (request_id, row[1], user_id, username, request_type, row[6], row[7],
                          client_inn, amount, comment, "Новая"))
    return request_id
