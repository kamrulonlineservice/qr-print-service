"""Database compatibility layer for QR Print.

Uses Neon/PostgreSQL when DATABASE_URL is configured, otherwise local SQLite.
The wrapper keeps the sqlite-style API currently used by app.py (question-mark
parameters, Row access by column name or index, and cursor.lastrowid).
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://", "postgresql+psycopg://"))


class Row:
    """A row that supports both row['column'] and row[0] access."""
    def __init__(self, values, columns):
        self._values = tuple(values)
        self._columns = tuple(columns)
        self._index = {name: i for i, name in enumerate(self._columns)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._columns


class _Cursor:
    def __init__(self, cursor, postgres=False):
        self._cursor = cursor
        self._postgres = postgres
        self._buffer = None
        self.lastrowid = None

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql, params=()):
        sql = _translate_sql(sql, self._postgres)
        insert_match = re.match(r'\s*INSERT\s+INTO\s+([\w"`]+)', sql, re.I)
        # Most application tables use an integer `id` primary key. The agent
        # token table is keyed by shop_id and has no `id` column.
        returns_id = bool(
            self._postgres
            and insert_match
            and insert_match.group(1).strip('"`').lower() in {
                "shops", "print_jobs", "recharge_history"
            }
            and not re.search(r"\bRETURNING\b", sql, re.I)
        )
        if returns_id:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            self._cursor.execute(sql, params)
            returned = self._cursor.fetchone()
            self.lastrowid = returned[0] if returned else None
            self._buffer = returned
        else:
            self._cursor.execute(sql, params)
            self.lastrowid = getattr(self._cursor, "lastrowid", None)
            self._buffer = None
        return self

    def fetchone(self):
        if self._buffer is not None:
            row, self._buffer = self._buffer, None
            return _make_row(row, self._cursor, self._postgres)
        result = self._cursor.fetchone()
        return _make_row(result, self._cursor, self._postgres) if result is not None else None

    def fetchall(self):
        rows = []
        if self._buffer is not None:
            rows.append(self._buffer)
            self._buffer = None
        rows.extend(self._cursor.fetchall())
        return [_make_row(row, self._cursor, self._postgres) for row in rows]


class _Connection:
    def __init__(self, raw, postgres=False):
        self._raw = raw
        self._postgres = postgres
        self.row_factory = None

    def execute(self, sql, params=()):
        if self._postgres and re.match(r"\s*PRAGMA\s+table_info\(([^)]+)\)", sql, re.I):
            table = re.match(r"\s*PRAGMA\s+table_info\(([^)]+)\)", sql, re.I).group(1).strip('"`[]')
            cur = self._raw.cursor()
            cur.execute("SELECT ordinal_position - 1 AS cid, column_name AS name, data_type AS type, (is_nullable = 'NO') AS notnull, column_default AS dflt_value, (CASE WHEN column_name = 'id' THEN 1 ELSE 0 END) AS pk FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position", (table,))
            return _Cursor(cur, True)
        cur = self._raw.cursor()
        return _Cursor(cur, self._postgres).execute(sql, params)

    def cursor(self):
        return _Cursor(self._raw.cursor(), self._postgres)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()



def _make_row(row, cursor, postgres):
    if row is None:
        return None
    if isinstance(row, Row):
        return row
    if postgres:
        columns = [item.name for item in cursor.description]
        values = tuple(row)
    else:
        columns = [item[0] for item in cursor.description]
        values = tuple(row)
    return Row(values, columns)


def _translate_sql(sql, postgres):
    if not postgres:
        return sql
    sql = sql.replace("?", "%s")
    sql = re.sub(r"datetime\('now',\s*'localtime'\)", "CURRENT_TIMESTAMP", sql, flags=re.I)
    sql = re.sub(r"datetime\('now'\)", "CURRENT_TIMESTAMP", sql, flags=re.I)
    sql = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", sql, flags=re.I)
    return sql


def connect_db(path="database.db", timeout=15):
    """Open a DB connection; path is used only by the local SQLite fallback."""
    if USE_POSTGRES:
        import psycopg
        from psycopg.rows import tuple_row
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        raw = psycopg.connect(url, connect_timeout=timeout, row_factory=tuple_row)
        return _Connection(raw, postgres=True)

    db_path = Path(path)
    if not db_path.is_absolute():
        db_path = Path(__file__).resolve().parent / db_path
    raw = sqlite3.connect(str(db_path), timeout=timeout)
    raw.row_factory = sqlite3.Row
    return _Connection(raw, postgres=False)


def init_db():
    """Create the core tables and add payment/agent columns if missing."""
    conn = connect_db("database.db")
    try:
        if USE_POSTGRES:
            statements = [
                """CREATE TABLE IF NOT EXISTS shops (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    shop_name TEXT NOT NULL, owner_name TEXT NOT NULL, phone TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, country TEXT, state TEXT,
                    district TEXT, village TEXT, pincode TEXT, upi_id TEXT,
                    bw_rate REAL DEFAULT 2.0, color_rate REAL DEFAULT 10.0,
                    registration_date TIMESTAMP, trial_end_date TIMESTAMP,
                    subscription_end_date TIMESTAMP, recharge_amount REAL DEFAULT 0.0,
                    amount REAL DEFAULT 0.0, payment_status TEXT DEFAULT 'Unpaid',
                    status TEXT DEFAULT 'Active', created_at TIMESTAMP, plan_name TEXT, utr_number TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS print_jobs (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    shop_id INTEGER REFERENCES shops(id), customer_phone TEXT, file_name TEXT,
                    copies INTEGER, print_type TEXT, amount REAL, status TEXT, timestamp TIMESTAMP,
                    pages_input TEXT, pay_mode TEXT, payment_id TEXT, order_id TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS recharge_history (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    shop_id INTEGER REFERENCES shops(id), plan_name TEXT, amount REAL,
                    utr_number TEXT, status TEXT, timestamp TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS shop_agent_tokens (
                    shop_id INTEGER PRIMARY KEY REFERENCES shops(id), token_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP
                )""",
            ]
        else:
            # Keep the original SQLite schema for local development.
            statements = [
                """CREATE TABLE IF NOT EXISTS shops (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_name TEXT NOT NULL, owner_name TEXT NOT NULL, phone TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, country TEXT, state TEXT,
                    district TEXT, village TEXT, pincode TEXT, upi_id TEXT,
                    bw_rate REAL DEFAULT 2.0, color_rate REAL DEFAULT 10.0,
                    registration_date TIMESTAMP, trial_end_date TIMESTAMP,
                    subscription_end_date TIMESTAMP, recharge_amount REAL DEFAULT 0.0,
                    amount REAL DEFAULT 0.0, payment_status TEXT DEFAULT 'Unpaid',
                    status TEXT DEFAULT 'Active', created_at TIMESTAMP, plan_name TEXT, utr_number TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS print_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER REFERENCES shops(id), customer_phone TEXT, file_name TEXT,
                    copies INTEGER, print_type TEXT, amount REAL, status TEXT, timestamp TIMESTAMP,
                    pages_input TEXT, pay_mode TEXT, payment_id TEXT, order_id TEXT
                )""",
                """CREATE TABLE IF NOT EXISTS recharge_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shop_id INTEGER REFERENCES shops(id), plan_name TEXT, amount REAL,
                    utr_number TEXT, status TEXT, timestamp TIMESTAMP
                )""",
                """CREATE TABLE IF NOT EXISTS shop_agent_tokens (
                    shop_id INTEGER PRIMARY KEY REFERENCES shops(id), token_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP
                )""",
            ]
        for statement in statements:
            conn.execute(statement)

        columns = {
            "online_payment_enabled": "INTEGER NOT NULL DEFAULT 1",
            "cash_payment_enabled": "INTEGER NOT NULL DEFAULT 1",
            "payment_gateway": "TEXT NOT NULL DEFAULT 'upi'",
            "razorpay_key_id": "TEXT", "razorpay_key_secret": "TEXT",
            "phonepe_merchant_id": "TEXT", "phonepe_salt_key": "TEXT",
            "payment_merchant_name": "TEXT",
            "payment_method": "TEXT NOT NULL DEFAULT 'upi_id'",
            "payment_qr_data": "TEXT",
        }
        if USE_POSTGRES:
            existing = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'shops'"
            ).fetchall()}
        else:
            existing = {r[1] for r in conn.execute("PRAGMA table_info(shops)").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE shops ADD COLUMN {name} {definition}")
        conn.commit()
    finally:
        conn.close()
