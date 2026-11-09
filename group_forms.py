"""Per-user and per-topic test flows for Telegram forum dialogs.

Local SQLite is temporary on Render Free. Move these tables to durable
PostgreSQL before accepting real client information.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time

import case_store


def _db():
    case_store.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(case_store.DB_FILE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS group_forms (
       chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
       token TEXT NOT NULL, stage TEXT NOT NULL, data_json TEXT NOT NULL,
       prompt_id INTEGER, updated_at INTEGER NOT NULL,
       PRIMARY KEY (chat_id, topic_id, user_id)
    )""")
    return db


def read(chat: int, topic: int, user: int):
    with _db() as db:
        row = db.execute(
            "SELECT * FROM group_forms WHERE chat_id=? AND topic_id=? AND user_id=?",
            (chat, topic, user)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json"))
        return result


def begin(chat: int, topic: int, user: int, stage: str, data: dict):
    token = secrets.token_hex(5)
    with _db() as db:
        db.execute("""INSERT INTO group_forms
             (chat_id,topic_id,user_id,token,stage,data_json,prompt_id,updated_at)
             VALUES(?,?,?,?,?,?,NULL,?)
             ON CONFLICT(chat_id,topic_id,user_id) DO UPDATE SET
             token=excluded.token,stage=excluded.stage,data_json=excluded.data_json,
             prompt_id=NULL,updated_at=excluded.updated_at""",
             (chat, topic, user, token, stage, json.dumps(data, ensure_ascii=False),
              int(time.time())))
    return token


def advance(chat: int, topic: int, user: int, token: str,
            stage: str, data: dict, prompt_id: int):
    with _db() as db:
        result = db.execute("""UPDATE group_forms SET
            stage=?,data_json=?,prompt_id=?,updated_at=?
            WHERE chat_id=? AND topic_id=? AND user_id=? AND token=?
            AND stage!='submitting'""",
            (stage, json.dumps(data, ensure_ascii=False), prompt_id, int(time.time()),
             chat, topic, user, token))
        return result.rowcount == 1


def claim_submit(chat: int, topic: int, user: int, token: str, prompt_id: int) -> bool:
    with _db() as db:
        result = db.execute("""UPDATE group_forms SET stage='submitting',updated_at=?
           WHERE chat_id=? AND topic_id=? AND user_id=? AND token=?
           AND stage='confirm' AND prompt_id=?""",
           (int(time.time()), chat, topic, user, token, prompt_id))
        return result.rowcount == 1


def complete(chat: int, topic: int, user: int, token: str):
    with _db() as db:
        db.execute("""DELETE FROM group_forms WHERE
          chat_id=? AND topic_id=? AND user_id=? AND token=?""",
          (chat, topic, user, token))


def reset_after_failure(chat: int, topic: int, user: int, token: str):
    with _db() as db:
        db.execute("""UPDATE group_forms SET stage='confirm' WHERE
          chat_id=? AND topic_id=? AND user_id=? AND token=?
          AND stage='submitting'""",
          (chat, topic, user, token))
