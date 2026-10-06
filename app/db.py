"""SQLite storage. One connection per call keeps things thread-safe for the
background detection jobs."""

import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    start_lat REAL, start_lng REAL,
    end_lat REAL, end_lng REAL,
    origin_lat REAL, origin_lng REAL,
    reached_start INTEGER NOT NULL DEFAULT 0,
    max_progress REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'idle',
    started_at REAL
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY,
    lat REAL NOT NULL, lng REAL NOT NULL,
    accuracy REAL, heading REAL, speed REAL,
    user_id INTEGER,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY,
    lat REAL NOT NULL, lng REAL NOT NULL,
    label TEXT NOT NULL,
    score REAL,
    hits INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    crop TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    created_by INTEGER
);
CREATE INDEX IF NOT EXISTS markers_expiry ON markers(expires_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    detections INTEGER NOT NULL DEFAULT 0,
    markers INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    created_by INTEGER
);
"""


# Mine-laying parameters chosen from the density-frontage chart, and the
# time the next canister fires while laying.
MISSION_COLUMNS = {
    "mine_type": "TEXT", "atm": "REAL", "apm": "REAL", "time_gap": "INTEGER",
    "fire_next_at": "REAL", "fire_stopped": "TEXT",
}

# Canister stock per type (handout p. 9: GBL-240 and GBL-243 are the two types in use).
CANISTER_TYPES = ("GBL-240", "GBL-243")
AMMO_KEYS = [f"ammo_{k}_{t[4:]}" for t in CANISTER_TYPES for k in ("total", "loaded", "fired")]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    config.ensure_dirs()
    with tx() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO mission (id) VALUES (1)")
        c.execute("INSERT OR IGNORE INTO settings VALUES ('sd_hours', ?)", (str(config.DEFAULT_SD_HOURS),))
        for key in AMMO_KEYS:
            c.execute("INSERT OR IGNORE INTO settings VALUES (?, '0')", (key,))
        c.execute("INSERT OR IGNORE INTO settings VALUES ('speed_tol_pct', '10')")
        c.execute("DELETE FROM settings WHERE key IN ('grid_gzd', 'grid_square', 'total_canisters', 'fired_canisters')")
        # Columns added after the first release.
        have = {r["name"] for r in c.execute("PRAGMA table_info(mission)")}
        for col, decl in MISSION_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE mission ADD COLUMN {col} {decl}")


def get_setting(key: str, default: str | None = None) -> str | None:
    with tx() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
