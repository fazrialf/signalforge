"""
SignalForge Database Schema
Initialises all SQLite tables on first run.
"""
import sqlite3
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "db" / "signalforge.db"


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ================================================================
-- CANDLES: cached OHLCV data per timeframe
-- ================================================================
CREATE TABLE IF NOT EXISTS candles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    ts          INTEGER NOT NULL,  -- unix ms
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    UNIQUE(symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_sym_tf_ts ON candles(symbol, timeframe, ts);

-- ================================================================
-- SIGNALS: every signal evaluated (delivered or suppressed)
-- ================================================================
CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    symbol              TEXT    NOT NULL,
    direction           TEXT    NOT NULL,  -- BUY / SELL
    timeframe           TEXT    NOT NULL,
    entry_price         REAL,
    tp1                 REAL,
    tp2                 REAL,
    tp3                 REAL,
    sl                  REAL,
    rr_ratio            REAL,
    confidence          REAL,
    risk_pct            REAL,
    position_size       REAL,
    confluence_score    INTEGER,
    confluence_detail   TEXT,   -- JSON breakdown
    llm_reasoning       TEXT,
    primary_risk        TEXT,
    invalidation_level  REAL,
    expiry_hours        INTEGER,
    prompt_version      TEXT,
    mtf_bias            TEXT,   -- JSON {daily, 4h, 1h}
    indicators_snapshot TEXT,   -- JSON snapshot at signal time
    filter_result       TEXT,   -- PASS / FAIL:<filter_name>
    delivered           INTEGER DEFAULT 0,  -- 0/1
    user_action         TEXT,   -- entered / skipped / expired / NULL
    skip_reason         TEXT,
    outcome             TEXT,   -- TP1/TP2/TP3/SL/BE/OPEN/EXPIRED
    outcome_price       REAL,
    outcome_pnl_pct     REAL,
    outcome_r           REAL,   -- R multiple achieved
    outcome_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_sym ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_signals_delivered ON signals(delivered);

-- ================================================================
-- POSITIONS: currently open / recently closed positions
-- ================================================================
CREATE TABLE IF NOT EXISTS positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    tp1         REAL,
    tp2         REAL,
    tp3         REAL,
    sl          REAL,
    risk_pct    REAL,
    size        REAL,
    opened_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT,
    status      TEXT    NOT NULL DEFAULT 'open',  -- open / closed
    outcome     TEXT,   -- TP1/TP2/TP3/SL/BE
    pnl_pct     REAL,
    r_achieved  REAL
);

-- ================================================================
-- HEALTH_LOG: watchdog check results
-- ================================================================
CREATE TABLE IF NOT EXISTS health_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    ws_alive    INTEGER,  -- 0/1
    llm_alive   INTEGER,
    db_alive    INTEGER,
    disk_gb     REAL,
    ram_pct     REAL,
    notes       TEXT
);

-- ================================================================
-- BUILD_LOG: development progress tracking
-- ================================================================
CREATE TABLE IF NOT EXISTS build_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    sprint      INTEGER,
    task_id     TEXT,
    task_name   TEXT,
    event       TEXT,   -- started / completed
    note        TEXT
);
"""


def init_db():
    """Create all tables. Safe to run multiple times (IF NOT EXISTS)."""
    os.makedirs(DB_PATH.parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"[DB] Initialised at {DB_PATH}")
    return str(DB_PATH)


def get_conn() -> sqlite3.Connection:
    """Return a connection with row_factory set."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    init_db()
