"""SQLite storage. Swap for PostgreSQL later by changing this one file."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT UNIQUE,               -- gmail message id + attachment name (dedupe)
    message_id TEXT, sender TEXT, subject TEXT, received_at TEXT,
    filename TEXT, source_format TEXT, extraction_method TEXT,
    doc_type TEXT, doc_number TEXT, doc_date TEXT, due_date TEXT, reference_number TEXT,
    currency TEXT, payment_terms TEXT,
    vendor_name TEXT, buyer_name TEXT,
    subtotal REAL, tax_total REAL, grand_total REAL,
    confidence REAL, status TEXT, issues TEXT,
    data TEXT,                            -- full standard JSON
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, title TEXT, body TEXT, document_id INTEGER,
    is_read INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, source TEXT,
    emails_seen INTEGER, docs_added INTEGER, docs_failed INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS skipped (source_key TEXT PRIMARY KEY, created_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ignored_emails (
    message_id TEXT PRIMARY KEY, sender TEXT, sender_email TEXT, subject TEXT,
    received_at TEXT, reason TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_doc_number ON documents(doc_number);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(config.DB_PATH)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def seen(source_key: str) -> bool:
    with conn() as c:
        return (c.execute("SELECT 1 FROM documents WHERE source_key=?", (source_key,)).fetchone() is not None
                or c.execute("SELECT 1 FROM skipped WHERE source_key=?", (source_key,)).fetchone() is not None)


def mark_skipped(source_key: str) -> None:
    """Remember items that were checked and are not documents, so they are not re-processed."""
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO skipped (source_key, created_at) VALUES (?,?)", (source_key, now()))


def insert_document(meta: dict, ex, filename: str, fmt: str, status: str) -> int:
    d = ex.document
    with conn() as c:
        cur = c.execute(
            """INSERT INTO documents (source_key, message_id, sender, subject, received_at, filename,
               source_format, extraction_method, doc_type, doc_number, doc_date, due_date, reference_number,
               currency, payment_terms, vendor_name, buyer_name, subtotal, tax_total, grand_total,
               confidence, status, issues, data, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (meta["source_key"], meta.get("message_id"), meta.get("sender"), meta.get("subject"),
             meta.get("received_at"), filename, fmt, ex.method, d.doc_type, d.doc_number, d.doc_date,
             d.valid_until_or_delivery_date, d.reference_number, d.currency, d.payment_terms,
             d.vendor.name, d.buyer.name, d.subtotal, d.tax_total, d.grand_total,
             ex.confidence, status, json.dumps(ex.issues), d.model_dump_json(), now()),
        )
        return cur.lastrowid


def add_notification(kind: str, title: str, body: str = "", document_id: int | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO notifications (kind,title,body,document_id,created_at) VALUES (?,?,?,?,?)",
                  (kind, title, body, document_id, now()))


def log_sync(source: str, started: str, seen_n: int, added: int, failed: int, error: str | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO sync_runs (started_at,finished_at,source,emails_seen,docs_added,docs_failed,error) "
                  "VALUES (?,?,?,?,?,?,?)", (started, now(), source, seen_n, added, failed, error))


# ---------------- settings / filters ----------------

def get_setting(key: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def add_ignored(meta: dict, reason: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO ignored_emails (message_id,sender,sender_email,subject,received_at,reason,created_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (meta["message_id"], meta.get("sender"), meta.get("sender_email"), meta.get("subject"),
                   meta.get("received_at"), reason, now()))


def list_ignored(limit: int = 300) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ignored_emails ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                           (limit,)).fetchall()]


def get_ignored(message_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM ignored_emails WHERE message_id=?", (message_id,)).fetchone()
        return dict(r) if r else None


def delete_ignored(message_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM ignored_emails WHERE message_id=?", (message_id,))


def count_ignored() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) n FROM ignored_emails").fetchone()["n"]


def clear_ignored() -> None:
    with conn() as c:
        c.execute("DELETE FROM ignored_emails")


def delete_ignored_by_sender(sender_email: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM ignored_emails WHERE lower(sender_email)=lower(?)", (sender_email,))


def message_known(message_id: str) -> bool:
    """True if this Gmail message was already turned into documents, skipped, or ignored by the filters.
    Lets a sync skip it without even fetching it again."""
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM documents WHERE message_id=? UNION ALL SELECT 1 FROM ignored_emails WHERE message_id=? "
            "UNION ALL SELECT 1 FROM skipped WHERE source_key LIKE ? LIMIT 1",
            (message_id, message_id, f"gmail:{message_id}:%")).fetchone() is not None
