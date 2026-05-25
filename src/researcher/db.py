from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    depart_date  TEXT NOT NULL,
    cabin        TEXT NOT NULL,
    seats_remaining INTEGER NOT NULL,
    miles        INTEGER NOT NULL,
    fees_cents   INTEGER NOT NULL,
    stops        INTEGER NOT NULL,
    direct       INTEGER NOT NULL,
    duration_min INTEGER,
    flight_numbers TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_snapshot_json TEXT,
    UNIQUE(source, origin, destination, depart_date, cabin)
);

CREATE INDEX IF NOT EXISTS idx_legs_search
    ON legs (origin, destination, depart_date, cabin, seats_remaining);

CREATE TABLE IF NOT EXISTS pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    out_leg_id  INTEGER NOT NULL REFERENCES legs(id) ON DELETE CASCADE,
    ret_leg_id  INTEGER NOT NULL REFERENCES legs(id) ON DELETE CASCADE,
    nights      INTEGER NOT NULL,
    total_miles INTEGER NOT NULL,
    total_fees_cents INTEGER NOT NULL,
    bookable_from TEXT,
    state       TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    last_alerted_at TEXT,
    UNIQUE(out_leg_id, ret_leg_id)
);

CREATE INDEX IF NOT EXISTS idx_pairs_state ON pairs (state, last_seen_at);

CREATE TABLE IF NOT EXISTS pair_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id   INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    old_state TEXT,
    new_state TEXT NOT NULL,
    ts        TEXT NOT NULL,
    note      TEXT
);

CREATE INDEX IF NOT EXISTS idx_pair_events_pair ON pair_events (pair_id, ts);

CREATE TABLE IF NOT EXISTS poll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result_count INTEGER,
    error TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
