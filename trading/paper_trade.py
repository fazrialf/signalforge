"""trading/paper_trade.py — Paper trading engine for SignalForge.

Simulates trade fills without sending real orders. Tracks paper positions,
P&L, and performance statistics separately from live positions in a dedicated
``paper_trades`` SQLite table.

The engine is intentionally lightweight — stdlib + sqlite3 only. It mirrors
the schema conventions of ``signals/position_tracker.py`` so results can be
compared side-by-side with live signals.

Typical usage::

    engine = PaperTradeEngine(db_path=str(DB_PATH), initial_balance=10_000.0)

    trade_id = engine.open_trade(
        symbol="BTC/USDT",
        direction="LONG",
        entry=65_000.0,
        sl=63_500.0,
        tp1=66_500.0,
        tp2=68_000.0,
        tp3=70_000.0,
        size=0.05,
        signal_id=42,
        confidence=0.78,
    )

    closed = engine.tick("BTC/USDT", current_price=70_200.0)
    stats   = engine.get_paper_stats(days=30)
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,          -- LONG | SHORT
    entry_price     REAL    NOT NULL,
    sl              REAL    NOT NULL,
    tp1             REAL,
    tp2             REAL,
    tp3             REAL,
    position_size   REAL    NOT NULL,
    signal_id       INTEGER,
    confidence      REAL,
    status          TEXT    NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED_WIN | CLOSED_LOSS | CLOSED_BE
    open_price      REAL,                      -- fill price (same as entry_price for paper)
    close_price     REAL,
    pnl_usd         REAL,
    pnl_pct         REAL,
    rr_realized     REAL,
    tp1_hit         INTEGER NOT NULL DEFAULT 0,
    tp2_hit         INTEGER NOT NULL DEFAULT 0,
    sl_hit          INTEGER NOT NULL DEFAULT 0,
    opened_at       INTEGER NOT NULL,          -- unix ms
    closed_at       INTEGER                    -- unix ms, NULL while open
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_symbol ON paper_trades(symbol);

CREATE TABLE IF NOT EXISTS paper_balance (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- single-row sentinel
    balance         REAL    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# PaperTradeEngine
# ---------------------------------------------------------------------------

class PaperTradeEngine:
    """Simulated trading engine that processes TP/SL hits against a price stream.

    All state lives in SQLite so it survives process restarts and can be
    queried independently by reporting tools.

    Args:
        db_path: Path to the shared SignalForge SQLite database.
        initial_balance: Starting paper-account balance in USD.  Only used
            when the paper_balance row does not yet exist.
    """

    def __init__(self, db_path: str, initial_balance: float = 10_000.0) -> None:
        self.db_path = db_path
        self.initial_balance = initial_balance
        self._ensure_schema()
        self._ensure_balance(initial_balance)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return a connection with ``row_factory`` set to ``sqlite3.Row``."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        """Create paper_trades and paper_balance tables if they don't exist."""
        try:
            with self._get_conn() as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
            logger.debug("[PaperTrade] Schema initialised")
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] Schema init failed: %s", exc)
            raise

    def _ensure_balance(self, initial: float) -> None:
        """Insert the balance sentinel row if absent; leave it untouched otherwise."""
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT balance FROM paper_balance WHERE id = 1").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO paper_balance (id, balance) VALUES (1, ?)", (initial,)
                    )
                    conn.commit()
                    logger.info("[PaperTrade] Paper balance initialised: $%.2f", initial)
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] Balance init failed: %s", exc)
            raise

    def _update_balance(self, conn: sqlite3.Connection, delta: float) -> None:
        """Apply *delta* USD to the paper balance within an existing connection."""
        conn.execute(
            "UPDATE paper_balance SET balance = balance + ? WHERE id = 1", (delta,)
        )

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_trade(
        self,
        symbol: str,
        direction: str,
        entry: float,
        sl: float,
        tp1: Optional[float],
        tp2: Optional[float],
        tp3: Optional[float],
        size: float,
        signal_id: Optional[int] = None,
        confidence: Optional[float] = None,
    ) -> int:
        """Open a new paper trade and return its ``id``.

        Args:
            symbol: Asset ticker, e.g. ``'BTC/USDT'``.
            direction: ``'LONG'`` or ``'SHORT'``.
            entry: Simulated fill price.
            sl: Stop-loss price.
            tp1: First take-profit price (intermediate marker).
            tp2: Second take-profit price (intermediate marker).
            tp3: Final take-profit price — hit triggers a CLOSED_WIN.
            size: Position size in base-asset units.
            signal_id: FK to the ``signals`` table (optional).
            confidence: LLM confidence score at signal time (optional).

        Returns:
            The auto-incremented ``id`` of the new paper_trades row.

        Raises:
            ValueError: If ``direction`` is not ``'LONG'`` or ``'SHORT'``, or
                if ``size`` is not positive.
            sqlite3.Error: On database failure.
        """
        direction = direction.upper()
        if direction not in ("LONG", "SHORT"):
            raise ValueError(f"direction must be 'LONG' or 'SHORT', got {direction!r}")
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")

        now = self._now_ms()
        try:
            with self._get_conn() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO paper_trades
                        (symbol, direction, entry_price, sl, tp1, tp2, tp3,
                         position_size, signal_id, confidence,
                         status, open_price, opened_at)
                    VALUES
                        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (symbol, direction, entry, sl, tp1, tp2, tp3,
                     size, signal_id, confidence, entry, now),
                )
                conn.commit()
                trade_id = cur.lastrowid

            logger.info(
                "[PaperTrade] Opened #%d %s %s @ %.4f | SL=%.4f | size=%.6f",
                trade_id, direction, symbol, entry, sl, size,
            )
            return trade_id

        except sqlite3.Error as exc:
            logger.error("[PaperTrade] open_trade failed: %s", exc)
            raise

    def tick(self, symbol: str, current_price: float) -> list[dict]:
        """Evaluate all open trades for *symbol* against *current_price*.

        For each open trade:

        - **LONG**: ``current_price <= sl`` → ``CLOSED_LOSS``;
          ``current_price >= tp1`` → mark ``tp1_hit``; same for ``tp2``;
          ``current_price >= tp3`` → ``CLOSED_WIN``.
        - **SHORT**: inverse comparisons.
        - TP1/TP2 are intermediate markers only; the trade stays open until
          TP3 is hit or SL triggers.
        - On close, P&L is applied to the paper balance.

        Args:
            symbol: Asset ticker to evaluate.
            current_price: Latest market price.

        Returns:
            A list of ``dict`` representations of trades that were closed
            during this tick (may be empty).
        """
        closed: list[dict] = []

        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN' AND symbol = ?",
                    (symbol,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] tick() fetch failed: %s", exc)
            return closed

        for row in rows:
            trade = dict(row)
            result = self._evaluate(trade, current_price)
            if result is not None:
                closed.append(result)

        return closed

    def _evaluate(self, trade: dict, current_price: float) -> Optional[dict]:
        """Check a single open trade and close/update it if a level is hit.

        Returns the closed trade dict if the trade was closed, else ``None``.
        """
        tid        = trade["id"]
        direction  = trade["direction"]
        entry      = trade["entry_price"]
        sl         = trade["sl"]
        tp1        = trade["tp1"]
        tp2        = trade["tp2"]
        tp3        = trade["tp3"]
        size       = trade["position_size"]
        tp1_hit    = bool(trade["tp1_hit"])
        tp2_hit    = bool(trade["tp2_hit"])

        is_long = (direction == "LONG")

        # --- SL check (highest priority) ---
        sl_triggered = (
            (is_long  and current_price <= sl) or
            (not is_long and current_price >= sl)
        )
        if sl_triggered:
            return self._close_trade(tid, sl, reason="SL_HIT")

        # --- TP1 marker ---
        if tp1 is not None and not tp1_hit:
            if (is_long and current_price >= tp1) or (not is_long and current_price <= tp1):
                tp1_hit = True
                self._set_tp_flags(tid, tp1_hit=1)
                logger.info("[PaperTrade] #%d TP1 hit @ %.4f", tid, current_price)

        # --- TP2 marker ---
        if tp2 is not None and not tp2_hit:
            if (is_long and current_price >= tp2) or (not is_long and current_price <= tp2):
                tp2_hit = True
                self._set_tp_flags(tid, tp2_hit=1)
                logger.info("[PaperTrade] #%d TP2 hit @ %.4f", tid, current_price)

        # --- TP3 close ---
        if tp3 is not None:
            if (is_long and current_price >= tp3) or (not is_long and current_price <= tp3):
                return self._close_trade(tid, tp3, reason="TP3_HIT")

        return None

    def _set_tp_flags(self, trade_id: int, tp1_hit: int = -1, tp2_hit: int = -1) -> None:
        """Persist TP marker updates without closing the trade."""
        updates: list[tuple] = []
        params: list = []

        if tp1_hit != -1:
            updates.append("tp1_hit = ?")
            params.append(tp1_hit)
        if tp2_hit != -1:
            updates.append("tp2_hit = ?")
            params.append(tp2_hit)

        if not updates:
            return

        params.append(trade_id)
        sql = f"UPDATE paper_trades SET {', '.join(updates)} WHERE id = ?"
        try:
            with self._get_conn() as conn:
                conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as exc:
            logger.warning("[PaperTrade] _set_tp_flags failed for #%d: %s", trade_id, exc)

    def _close_trade(self, trade_id: int, close_price: float, reason: str) -> dict:
        """Compute P&L, update the row to closed, and adjust the paper balance.

        Args:
            trade_id: Row ID of the trade to close.
            close_price: Price at which the trade is filled on close.
            reason: ``'SL_HIT'``, ``'TP3_HIT'``, or ``'manual'``.

        Returns:
            Dict representation of the closed row.
        """
        now = self._now_ms()

        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] _close_trade fetch failed for #%d: %s", trade_id, exc)
            raise

        if not row:
            raise ValueError(f"Paper trade #{trade_id} does not exist")

        trade = dict(row)

        if trade["status"] != "OPEN":
            logger.warning("[PaperTrade] Trade #%d already closed (%s)", trade_id, trade["status"])
            return trade

        direction = trade["direction"]
        entry     = trade["entry_price"]
        sl        = trade["sl"]
        size      = trade["position_size"]

        # P&L
        if direction == "LONG":
            pnl_usd = (close_price - entry) * size
        else:
            pnl_usd = (entry - close_price) * size

        # Risk basis: distance to SL × size
        risk_usd = abs(entry - sl) * size
        if risk_usd > 0:
            pnl_pct     = (pnl_usd / risk_usd) * 100.0
            rr_realized = abs(pnl_usd) / risk_usd
        else:
            logger.warning("[PaperTrade] Zero risk_usd for #%d — pnl_pct/rr set to 0", trade_id)
            pnl_pct     = 0.0
            rr_realized = 0.0

        # Outcome status
        if pnl_pct < -5.0:
            status = "CLOSED_LOSS"
        elif pnl_pct > 5.0:
            status = "CLOSED_WIN"
        else:
            status = "CLOSED_BE"

        sl_hit_flag = 1 if reason == "SL_HIT" else 0

        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET status      = ?,
                        close_price = ?,
                        pnl_usd     = ?,
                        pnl_pct     = ?,
                        rr_realized = ?,
                        sl_hit      = ?,
                        closed_at   = ?
                    WHERE id = ?
                    """,
                    (status, close_price, pnl_usd, pnl_pct, rr_realized,
                     sl_hit_flag, now, trade_id),
                )
                self._update_balance(conn, pnl_usd)
                conn.commit()

                closed_row = conn.execute(
                    "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] _close_trade update failed for #%d: %s", trade_id, exc)
            raise

        result = dict(closed_row)

        logger.info(
            "[PaperTrade] Closed #%d %s %s @ %.4f | P&L: $%.2f (%.1f%%) | "
            "%.2fR | %s [%s]",
            trade_id, direction, trade["symbol"], close_price,
            pnl_usd, pnl_pct, rr_realized, status, reason,
        )
        return result

    def get_open_trades(self) -> list[dict]:
        """Return all open paper trades ordered by ``opened_at`` ascending.

        Returns:
            List of row dicts for every trade with ``status = 'OPEN'``.
        """
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY opened_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] get_open_trades failed: %s", exc)
            return []

    def get_closed_trades(self, days: int = 30) -> list[dict]:
        """Return closed paper trades from the past *days* days.

        Args:
            days: Lookback window. ``0`` returns all closed trades ever.

        Returns:
            List of row dicts ordered by ``closed_at`` descending.
        """
        try:
            with self._get_conn() as conn:
                if days > 0:
                    cutoff_ms = self._now_ms() - (days * 86_400 * 1000)
                    rows = conn.execute(
                        """
                        SELECT * FROM paper_trades
                        WHERE status != 'OPEN' AND closed_at >= ?
                        ORDER BY closed_at DESC
                        """,
                        (cutoff_ms,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM paper_trades WHERE status != 'OPEN' ORDER BY closed_at DESC"
                    ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] get_closed_trades failed: %s", exc)
            return []

    def get_paper_stats(self, days: int = 30) -> dict:
        """Compute performance statistics over closed trades.

        Args:
            days: Lookback window passed to ``get_closed_trades()``.

        Returns:
            A dict with the following keys:

            - ``total_trades``: int
            - ``wins``: int — trades with ``status = 'CLOSED_WIN'``
            - ``losses``: int — trades with ``status = 'CLOSED_LOSS'``
            - ``breakevens``: int — trades with ``status = 'CLOSED_BE'``
            - ``win_rate``: float — wins / (wins + losses) × 100, or 0.0
            - ``total_pnl_usd``: float — sum of all ``pnl_usd``
            - ``avg_rr``: float — mean ``rr_realized`` across closed trades
            - ``profit_factor``: float — gross_wins / gross_losses; ``999.0``
              when there are no losses
            - ``best_trade``: dict or ``None`` — row with highest ``pnl_usd``
            - ``worst_trade``: dict or ``None`` — row with lowest ``pnl_usd``
        """
        trades = self.get_closed_trades(days=days)

        wins       = [t for t in trades if t["status"] == "CLOSED_WIN"]
        losses     = [t for t in trades if t["status"] == "CLOSED_LOSS"]
        breakevens = [t for t in trades if t["status"] == "CLOSED_BE"]

        n_wins   = len(wins)
        n_losses = len(losses)
        n_total  = len(trades)

        decided = n_wins + n_losses  # break-evens excluded from win-rate denominator
        win_rate = (n_wins / decided * 100.0) if decided > 0 else 0.0

        total_pnl = sum(t["pnl_usd"] or 0.0 for t in trades)

        rr_values = [t["rr_realized"] for t in trades if t["rr_realized"] is not None]
        avg_rr    = (sum(rr_values) / len(rr_values)) if rr_values else 0.0

        gross_wins   = sum(t["pnl_usd"] for t in wins   if t["pnl_usd"] is not None)
        gross_losses = abs(sum(t["pnl_usd"] for t in losses if t["pnl_usd"] is not None))

        if gross_losses == 0.0:
            profit_factor = 999.0
        else:
            profit_factor = gross_wins / gross_losses

        best_trade = (
            max(trades, key=lambda t: t["pnl_usd"] or float("-inf"))
            if trades else None
        )
        worst_trade = (
            min(trades, key=lambda t: t["pnl_usd"] or float("inf"))
            if trades else None
        )

        return {
            "total_trades":   n_total,
            "wins":           n_wins,
            "losses":         n_losses,
            "breakevens":     len(breakevens),
            "win_rate":       round(win_rate, 2),
            "total_pnl_usd":  round(total_pnl, 4),
            "avg_rr":         round(avg_rr, 4),
            "profit_factor":  round(profit_factor, 4),
            "best_trade":     best_trade,
            "worst_trade":    worst_trade,
        }

    def get_balance(self) -> float:
        """Return the current paper-account balance in USD.

        Returns:
            Current balance, or ``initial_balance`` if the row is missing.
        """
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT balance FROM paper_balance WHERE id = 1").fetchone()
            return float(row["balance"]) if row else self.initial_balance
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] get_balance failed: %s", exc)
            return self.initial_balance

    def reset(self) -> None:
        """Delete all paper trades and restore the balance to ``initial_balance``.

        This is irreversible. Intended for test resets or starting a fresh
        paper-trading session.
        """
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM paper_trades")
                conn.execute(
                    "UPDATE paper_balance SET balance = ? WHERE id = 1",
                    (self.initial_balance,),
                )
                conn.commit()
            logger.info(
                "[PaperTrade] Reset — all trades cleared, balance restored to $%.2f",
                self.initial_balance,
            )
        except sqlite3.Error as exc:
            logger.error("[PaperTrade] reset() failed: %s", exc)
            raise

    def close_trade_manual(self, trade_id: int, close_price: float) -> dict:
        """Force-close a specific open trade at *close_price*.

        Useful for manual overrides or end-of-session cleanup.

        Args:
            trade_id: The ``id`` of the paper trade to close.
            close_price: Price at which to simulate the close fill.

        Returns:
            Dict representation of the closed row.

        Raises:
            ValueError: If the trade does not exist or is already closed.
        """
        return self._close_trade(trade_id, close_price, reason="manual")
