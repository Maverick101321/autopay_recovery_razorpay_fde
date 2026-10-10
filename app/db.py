import sqlite3
from pathlib import Path
from typing import Any, Optional

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
    next_retry_at TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS call_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    failed_payment_id TEXT,
    retell_call_id TEXT UNIQUE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    outcome TEXT,
    transcript TEXT,
    notes TEXT,
    duration_ms INTEGER,
    disconnection_reason TEXT,
    call_cost_usd REAL,
    call_analysis TEXT,
    raw_events TEXT,
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

CREATE TABLE IF NOT EXISTS outbound_messages (
    id TEXT PRIMARY KEY,
    failed_payment_id TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'sms',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (failed_payment_id) REFERENCES failed_payments(id) ON DELETE CASCADE
);
"""

DROP_TABLES_SQL = """
DROP TABLE IF EXISTS outbound_messages;
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

    conn = sqlite3.connect(resolved_path, check_same_thread=False)
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
            _migrate_missing_columns(conn)
    finally:
        conn.close()


def _quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier sourced from our schema metadata."""
    return '"' + identifier.replace('"', '""') + '"'


def _column_definition(column: sqlite3.Row | tuple[Any, ...]) -> str:
    """Build an ADD COLUMN-compatible definition from PRAGMA table_info metadata."""
    name, col_type, _notnull, default, _pk = column[1:6]
    definition = f"{_quote_identifier(name)} {col_type}".rstrip()
    # Do not add NOT NULL: old rows may not have a value. Defaults remain safe and
    # preserve the schema's behavior for rows created after the migration.
    if default is not None:
        definition += f" DEFAULT {default}"
    return definition


def _migrate_missing_columns(conn: sqlite3.Connection) -> None:
    """Add schema columns absent from an existing SQLite database.

    SQLite's CREATE TABLE IF NOT EXISTS never updates an existing table. A fresh
    in-memory database is the single source of truth for the current schema, so
    this stays lightweight as tables and columns are added over time.
    """
    schema_conn = sqlite3.connect(":memory:")
    try:
        schema_conn.row_factory = sqlite3.Row
        schema_conn.executescript(SCHEMA_SQL)
        tables = schema_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
        ).fetchall()
        for table in tables:
            table_name = table["name"]
            schema_columns = schema_conn.execute(
                f"PRAGMA table_info({_quote_identifier(table_name)})"
            ).fetchall()
            existing_columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({_quote_identifier(table_name)})").fetchall()
            }
            for column in schema_columns:
                if column["name"] not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE {_quote_identifier(table_name)} ADD COLUMN {_column_definition(column)}"
                    )
    finally:
        schema_conn.close()
