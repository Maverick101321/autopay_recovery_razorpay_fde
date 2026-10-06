import sqlite3
from pathlib import Path
from typing import Optional

from app.config import get_resolved_db_path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    language TEXT NOT NULL CHECK(language IN ('en', 'hi')),
    consent_to_call INTEGER NOT NULL DEFAULT 1 CHECK(consent_to_call IN (0, 1)),
    call_window_start TEXT NOT NULL,
    call_window_end TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata'
);

CREATE TABLE IF NOT EXISTS failed_payments (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    merchant_name TEXT NOT NULL,
    plan_name TEXT NOT NULL,
    amount_inr INTEGER NOT NULL,
    failure_reason TEXT NOT NULL CHECK(failure_reason IN (
        'insufficient_funds',
        'card_expired',
        'mandate_expired',
        'bank_declined',
        'otp_not_completed',
        'daily_limit_exceeded'
    )),
    failed_at TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
        'pending',
        'recovered',
        'promised',
        'escalated',
        'no_answer',
        'do_not_call',
        'failed'
    )),
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS call_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    failed_payment_id TEXT NOT NULL,
    retell_call_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    transcript TEXT,
    notes TEXT,
    FOREIGN KEY (failed_payment_id) REFERENCES failed_payments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payment_links (
    id TEXT PRIMARY KEY,
    failed_payment_id TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    FOREIGN KEY (failed_payment_id) REFERENCES failed_payments(id) ON DELETE CASCADE
);
"""

DROP_TABLES_SQL = """
DROP TABLE IF EXISTS payment_links;
DROP TABLE IF EXISTS call_attempts;
DROP TABLE IF EXISTS failed_payments;
DROP TABLE IF EXISTS customers;
"""


def get_db_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Return an active SQLite database connection with row factory and foreign keys enabled."""
    resolved_path = get_resolved_db_path(db_path)
    if resolved_path != ":memory:":
        Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(resolved_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str | Path] = None, reset: bool = False) -> None:
    """Initialize database tables. If reset is True, drops existing tables first."""
    conn = get_db_connection(db_path)
    try:
        with conn:
            if reset:
                conn.executescript(DROP_TABLES_SQL)
            conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()
