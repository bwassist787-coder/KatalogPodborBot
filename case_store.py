"""Test-stage manager workflow state.

Uses local SQLite on Render Free. This is EPHEMERAL and MUST be migrated to a
managed database before handling real customer data or relying on reminders.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time
from pathlib import Path

DB_FILE = Path(os.getenv("DB_FILE", "test_requests.sqlite3"))
OPEN = {"Новая", "В работе"}


def _db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_cases (
        id TEXT PRIMARY KEY, client_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL, chat_message_id INTEGER,
        kind TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Новая',
        owner_id INTEGER, owner_name TEXT,
        created_at INTEGER NOT NULL, reminder_origin INTEGER NOT NULL,
        reminder_stage INTEGER NOT NULL DEFAULT 0
    )""")
    # Migrate existing test cases without discarding old local rows.
    extra_columns = {
        "origin_chat_id": "INTEGER",
        "origin_topic_id": "INTEGER",
        "origin_message_id": "INTEGER",
        "origin_chat_title": "TEXT",
        "origin_username": "TEXT",
        "client_summary": "TEXT",
    }
    existing = {row["name"] for row in db.execute("PRAGMA table_info(manager_cases)")}
    for name, datatype in extra_columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE manager_cases ADD COLUMN {name} {datatype}")
    db.execute("""CREATE TABLE IF NOT EXISTS case_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
        actor_id INTEGER, kind TEXT NOT NULL, detail TEXT, occurred_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_prompts (
        chat_message_id INTEGER PRIMARY KEY, case_id TEXT NOT NULL,
        manager_id INTEGER NOT NULL, created_at INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS manager_drafts (
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
        manager_id INTEGER NOT NULL, text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL
    )""")
    return db


def _event(db, rid, actor, kind, detail=""):
    db.execute("INSERT INTO case_events(case_id,actor_id,kind,detail,occurred_at) VALUES(?,?,?,?,?)",
               (rid, actor, kind, detail, int(time.time())))


def register(rid, client_id, chat_id, kind, summary, *,
             origin_chat_id=None, origin_topic_id=None, origin_chat_title="",
             origin_username="", client_summary=""):
    now = int(time.time())
    with _db() as db:
        db.execute("""INSERT INTO manager_cases
            (id,client_id,chat_id,kind,summary,created_at,reminder_origin,
             origin_chat_id,origin_topic_id,origin_chat_title,origin_username,client_summary)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, client_id, chat_id, kind, summary, now, now,
             origin_chat_id, origin_topic_id, origin_chat_title,
             origin_username, client_summary))
        _event(db, rid, client_id, "created")


def find(rid):
    with _db() as db:
        row = db.execute("SELECT * FROM manager_cases WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None


def set_group_message(rid, message_id):
    with _db() as db:
        db.execute("UPDATE manager_cases SET chat_message_id=? WHERE id=?",
                   (message_id, rid))


def set_origin_message(rid, message_id):
    with _db() as db:
        db.execute("UPDATE manager_cases SET origin_message_id=? WHERE id=?",
                   (message_id, rid))


def _change(rid, actor, action, label=""):
    now = int(time.time())
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM manager_cases WHERE id=?", (rid,)).fetchone()
        if row is None:
            return "missing", None
        old = dict(row)
        if action == "take":
            if old["status"] != "Новая":
                return "busy", old
            db.execute("""UPDATE manager_cases SET status='В работе',
                owner_id=?, owner_name=? WHERE id=?""", (actor, label, rid))
            _event(db, rid, actor, "taken", label)
        elif action in ("release", "finish", "cancel"):
            if old["status"] not in OPEN:
                return "closed", old
            if action == "cancel":
                if old["owner_id"] != actor and old["client_id"] != actor:
                    return "forbidden", old
                db.execute("UPDATE manager_cases SET status='Отменена' WHERE id=?", (rid,))
                _event(db, rid, actor, "cancelled")
            else:
                if old["owner_id"] != actor:
                    return "forbidden", old
                if action == "release":
                    db.execute("""UPDATE manager_cases SET status='Новая',
                        owner_id=NULL,owner_name=NULL,reminder_stage=0,
                        reminder_origin=? WHERE id=?""", (now, rid))
                    _event(db, rid, actor, "released")
                else:
                    db.execute("UPDATE manager_cases SET status='Завершена' WHERE id=?", (rid,))
                    _event(db, rid, actor, "finished")
        else:
            raise ValueError("Unknown action")
        updated = db.execute("SELECT * FROM manager_cases WHERE id=?", (rid,)).fetchone()
        return "ok", dict(updated)


def take(rid, manager_id, manager_name):
    return _change(rid, manager_id, "take", manager_name)


def transition(rid, actor_id, action):
    return _change(rid, actor_id, action)


def history(rid, limit=12):
    with _db() as db:
        rows = db.execute("""SELECT actor_id,kind,detail,occurred_at
            FROM case_events WHERE case_id=? ORDER BY seq DESC LIMIT ?""", (rid, limit)).fetchall()
        return [dict(x) for x in rows]


def note(rid, actor, kind, detail=""):
    with _db() as db:
        _event(db, rid, actor, kind, detail[:300])


def my_cases(manager_id, limit=10):
    with _db() as db:
        rows = db.execute("""SELECT id,summary FROM manager_cases
            WHERE status='В работе' AND owner_id=?
            ORDER BY created_at DESC LIMIT ?""", (manager_id, limit)).fetchall()
        return [dict(x) for x in rows]


def prompt_add(message_id, rid, manager_id):
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO manager_prompts VALUES(?,?,?,?)",
                   (message_id, rid, manager_id, int(time.time())))


def prompt_get(message_id):
    with _db() as db:
        row = db.execute("SELECT * FROM manager_prompts WHERE chat_message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None


def draft_add(rid, manager_id, message):
    draft_id = secrets.token_hex(5)
    with _db() as db:
        db.execute("INSERT INTO manager_drafts VALUES(?,?,?,?,?,?)",
                   (draft_id, rid, manager_id, message, "pending", int(time.time())))
    return draft_id


def draft_claim(draft_id, manager_id):
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM manager_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["manager_id"] != manager_id or row["status"] != "pending":
            return None
        case = db.execute("SELECT status,owner_id FROM manager_cases WHERE id=?",
                          (row["case_id"],)).fetchone()
        if not case or case["status"] != "В работе" or case["owner_id"] != manager_id:
            return None
        db.execute("UPDATE manager_drafts SET status='sending' WHERE id=?", (draft_id,))
        return dict(row)


def draft_result(draft_id, sent):
    with _db() as db:
        db.execute("UPDATE manager_drafts SET status=? WHERE id=? AND status='sending'",
                   ("sent" if sent else "pending", draft_id))


def _reminder_thresholds():
    raw = os.getenv("CASE_REMINDER_MINUTES", "15,25,30")
    try:
        values = [int(x.strip()) for x in raw.split(",")]
        if not values or any(x < 1 for x in values) or values != sorted(set(values)):
            raise ValueError
        return values
    except ValueError:
        return [15, 25, 30]


def due_reminders(now=None):
    """Claim at most ONE reminder per case after any downtime; no burst catch-up."""
    now = int(time.time()) if now is None else int(now)
    limits = _reminder_thresholds()
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute("SELECT * FROM manager_cases WHERE status='Новая'").fetchall()
        due = []
        for row in rows:
            elapsed = max(0, now - row["reminder_origin"])
            reached = sum(elapsed >= minutes * 60 for minutes in limits)
            if reached <= row["reminder_stage"]:
                continue
            stage = reached
            db.execute("UPDATE manager_cases SET reminder_stage=? WHERE id=? AND status='Новая'",
                       (stage, row["id"]))
            due.append((dict(row), limits[stage - 1], row["reminder_stage"]))
        return due


def reset_reminder(rid, previous_stage):
    with _db() as db:
        db.execute("""UPDATE manager_cases SET reminder_stage=?
            WHERE id=? AND status='Новая'""", (previous_stage, rid))
