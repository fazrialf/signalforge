"""signals/signal_log.py — Signal logging and statistics for SignalForge.

Logs every evaluated signal (delivered or filtered) to the SQLite ``signals``
table and provides helpers to query recent signals and compute basic win-rate
statistics.

The ``signals`` table schema is defined in ``db/schema.py``.  This module
reuses that table, mapping SignalResult fields onto the existing columns.
"""
from __future__ import annotations

import sqlite3
import logging
import time
from typing import Optional

from signals.llm_engine import SignalResult
from config.settings import DB_PATH

logger = logging.getLogger(__name__)

# Columns added by Sprint 5 that may not exist on older DBs
_MIGRATION_SQLS = [
    "ALTER TABLE signals ADD COLUMN confluence_score   INTEGER",
    "ALTER TABLE signals ADD COLUMN mtf_aligned        INTEGER",  # 0/1
    "ALTER TABLE signals ADD COLUMN filter_result      TEXT",
    "ALTER TABLE signals ADD COLUMN cooldown_remaining INTEGER DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN delivered          INTEGER DEFAULT 0",
    "ALTER TABLE signals ADD COLUMN llm_reasoning      TEXT",
    "ALTER TABLE signals ADD COLUMN primary_risk       TEXT",
    "ALTER TABLE signals ADD COLUMN prompt_version     TEXT",
    "ALTER TABLE signals ADD COLUMN timeframe          TEXT",
    "ALTER TABLE signals ADD COLUMN rr_ratio           REAL",
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add Sprint-5 columns to *signals* if they do not already exist.

    SQLite does not support ``ADD COLUMN IF NOT EXISTS`` before version 3.37,
    so we attempt each ALTER and swallow ``OperationalError`` on duplicates.

    Args:
        conn: An open SQLite connection.
    """
    for sql in _MIGRATION_SQLS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # Column already present
    conn.commit()


def _get_conn(db_path: str) -> sqlite3.Connection:
    """Return a connection with row_factory and the Sprint-5 columns ensured."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)
    return conn


def log_signal(
    signal: SignalResult,
    symbol: str,
    confluence_score: int,
    mtf_aligned: bool,
    filter_result: str,
    cooldown_remaining: int = 0,
    db_path: str = DB_PATH,
) -> int:
    """Insert a signal record into the ``signals`` table.

    Reuses the existing schema columns where names differ
    (``entry_price``, ``sl``, ``direction``, etc.) and populates the
    Sprint-5 columns added by :func:`_ensure_columns`.

    Args:
        signal: Parsed ``SignalResult`` from the LLM engine.
        symbol: Asset ticker, e.g. ``'BTC/USDT'``.
        confluence_score: Integer confluence score from the confluence module.
        mtf_aligned: Whether the multi-timeframe bias was aligned.
        filter_result: ``'delivered'`` or ``'filtered: <reason>'``.
        cooldown_remaining: Minutes left in cooldown (0 if not in cooldown).
        db_path: Path to the SQLite database file.

    Returns:
        The ``rowid`` (auto-increment ``id``) of the newly inserted row.
    """
    delivered = 1 if filter_result == "delivered" else 0
    # Map filter_result -> filter_result column; keep legacy filter_result col too.
    filter_col = filter_result  # 'delivered' or 'filtered: <reason>'

    sql = """
        INSERT INTO signals (
            created_at,
            symbol, direction, timeframe,
            entry_price, sl, tp1, tp2, tp3,
            rr_ratio, confidence,
            llm_reasoning, primary_risk,
            prompt_version,
            confluence_score, mtf_aligned,
            filter_result, delivered,
            cooldown_remaining
        ) VALUES (
            :created_at,
            :symbol, :direction, :timeframe,
            :entry_price, :sl, :tp1, :tp2, :tp3,
            :rr_ratio, :confidence,
            :llm_reasoning, :primary_risk,
            :prompt_version,
            :confluence_score, :mtf_aligned,
            :filter_result, :delivered,
            :cooldown_remaining
        )
    """
    params = {
        "created_at": int(time.time() * 1000),
        "symbol": symbol,
        "direction": signal.signal,       # BUY / SELL / PASS
        "timeframe": signal.timeframe,
        "entry_price": signal.entry,
        "sl": signal.stop_loss,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "tp3": signal.tp3,
        "rr_ratio": signal.rr_ratio,
        "confidence": signal.confidence,
        "llm_reasoning": signal.reasoning,
        "primary_risk": signal.key_risk,
        "prompt_version": signal.prompt_version,
        "confluence_score": confluence_score,
        "mtf_aligned": 1 if mtf_aligned else 0,
        "filter_result": filter_col,
        "delivered": delivered,
        "cooldown_remaining": cooldown_remaining,
    }

    with _get_conn(db_path) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        row_id: int = cur.lastrowid  # type: ignore[assignment]

    logger.debug(
        "[SignalLog] Logged signal id=%d  %s %s  filter=%s",
        row_id, symbol, signal.signal, filter_result,
    )
    return row_id


def get_recent_signals(
    symbol: Optional[str] = None,
    limit: int = 20,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return the most recent signal records from the database.

    Args:
        symbol: Filter to a specific asset ticker.  ``None`` returns signals
            for all assets.
        limit: Maximum number of rows to return (default 20).
        db_path: Path to the SQLite database file.

    Returns:
        List of dicts (one per row), ordered newest-first.
    """
    if symbol:
        sql = """
            SELECT * FROM signals
            WHERE symbol = ?
            ORDER BY id DESC
            LIMIT ?
        """
        args = (symbol, limit)
    else:
        sql = """
            SELECT * FROM signals
            ORDER BY id DESC
            LIMIT ?
        """
        args = (limit,)  # type: ignore[assignment]

    with _get_conn(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()

    return [dict(row) for row in rows]


def get_signal_stats(
    symbol: Optional[str] = None,
    days: int = 7,
    db_path: str = DB_PATH,
) -> dict:
    """Compute basic delivery and filter statistics over the last *days* days.

    Counts are broken down by ``filter_result`` / ``direction`` so callers can
    build win-rate dashboards without extra queries.

    Args:
        symbol: Limit stats to a specific asset.  ``None`` aggregates all.
        days: Look-back window in calendar days (default 7).
        db_path: Path to the SQLite database file.

    Returns:
        A dict with keys:
        - ``total_signals``   — all rows in the window
        - ``delivered_count`` — rows where ``delivered = 1``
        - ``filtered_count``  — rows where ``delivered = 0`` and not PASS
        - ``pass_count``      — rows where ``direction = 'PASS'``
        - ``symbol``          — echoed back (``None`` means “all”)
        - ``days``            — echoed back
    """
    # SQLite: use created_at (unix ms) for date filtering
    cutoff_ms = int((time.time() - days * 86400) * 1000)

    base_where = "created_at >= :cutoff_ms"
    if symbol:
        base_where += " AND symbol = :symbol"

    params: dict = {"cutoff_ms": cutoff_ms}
    if symbol:
        params["symbol"] = symbol

    with _get_conn(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM signals WHERE {base_where}", params
        ).fetchone()[0]

        delivered = conn.execute(
            f"SELECT COUNT(*) FROM signals WHERE {base_where} AND delivered = 1",
            params,
        ).fetchone()[0]

        pass_count = conn.execute(
            f"SELECT COUNT(*) FROM signals WHERE {base_where} AND direction = 'PASS'",
            params,
        ).fetchone()[0]

        filtered = conn.execute(
            f"""
            SELECT COUNT(*) FROM signals
            WHERE {base_where}
              AND delivered = 0
              AND direction != 'PASS'
            """,
            params,
        ).fetchone()[0]

    return {
        "total_signals": total,
        "delivered_count": delivered,
        "filtered_count": filtered,
        "pass_count": pass_count,
        "symbol": symbol,
        "days": days,
    }
