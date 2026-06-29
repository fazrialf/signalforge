"""signals/position_tracker.py — Position tracking with P&L calculation for SignalForge.

Tracks open and closed positions in SQLite, calculating profit/loss metrics
and monitoring TP/SL hit status. Provides methods to open, update, close, and
query positions for performance analysis.

The ``positions`` table is automatically created on first use and stores:
- Entry and exit prices
- Position size and direction (LONG/SHORT)
- TP1/TP2/TP3 hit status
- Realized P&L in USD and percentage
- Risk:Reward ratio achieved
"""
from __future__ import annotations

import sqlite3
import logging
import time
from typing import Optional
from dataclasses import dataclass

from config.settings import DB_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

POSITIONS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    position_size REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    tp1_hit INTEGER DEFAULT 0,
    tp2_hit INTEGER DEFAULT 0,
    tp3_hit INTEGER DEFAULT 0,
    sl_hit INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'OPEN',
    entry_time INTEGER NOT NULL,
    close_time INTEGER,
    close_price REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    rr_realized REAL,
    notes TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_signal_id ON positions(signal_id);
"""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Encapsulates a trading position with P&L metrics.

    Attributes:
        id: Database row ID (None for unsaved positions).
        signal_id: Foreign key to the signals table.
        symbol: Asset ticker, e.g. ``'BTC/USDT'``.
        direction: ``'LONG'`` or ``'SHORT'``.
        entry_price: Price at which the position was opened.
        position_size: Number of base-asset units.
        stop_loss: Stop-loss price.
        tp1: First take-profit target (optional).
        tp2: Second take-profit target (optional).
        tp3: Third take-profit target (optional).
        tp1_hit: Boolean flag (0/1) indicating TP1 was hit.
        tp2_hit: Boolean flag (0/1) indicating TP2 was hit.
        tp3_hit: Boolean flag (0/1) indicating TP3 was hit.
        sl_hit: Boolean flag (0/1) indicating stop-loss was hit.
        status: Position state — ``'OPEN'``, ``'CLOSED_WIN'``, ``'CLOSED_LOSS'``, ``'CLOSED_BE'``.
        entry_time: Unix timestamp in milliseconds when position opened.
        close_time: Unix timestamp in milliseconds when position closed (None if open).
        close_price: Final exit price (None if open).
        pnl_usd: Realized profit/loss in USD (None if open).
        pnl_pct: P&L as percentage of initial risk (None if open).
        rr_realized: Risk:Reward ratio achieved (None if open).
        notes: Optional text notes about the position.
    """

    id: Optional[int] = None
    signal_id: Optional[int] = None
    symbol: str = ""
    direction: str = ""
    entry_price: float = 0.0
    position_size: float = 0.0
    stop_loss: float = 0.0
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp1_hit: int = 0
    tp2_hit: int = 0
    tp3_hit: int = 0
    sl_hit: int = 0
    status: str = "OPEN"
    entry_time: int = 0
    close_time: Optional[int] = None
    close_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    rr_realized: Optional[float] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# PositionTracker
# ---------------------------------------------------------------------------

class PositionTracker:
    """SQLite-backed position tracker with automatic P&L calculation.

    Manages the lifecycle of trading positions: opening, updating based on
    current price, and closing with calculated profit/loss metrics.

    Args:
        db_path: Path to the SQLite database file (default from settings).
    """

    def __init__(self, db_path: str = DB_PATH):
        """Initialize the tracker and ensure the positions table exists."""
        self.db_path = db_path
        self._ensure_table()

    def _ensure_table(self) -> None:
        """Create the positions table if it does not already exist."""
        try:
            with self._get_conn() as conn:
                conn.executescript(POSITIONS_TABLE_SCHEMA)
                conn.commit()
            logger.debug("[PositionTracker] Positions table initialized")
        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to create positions table: %s", e)
            raise

    def _get_conn(self) -> sqlite3.Connection:
        """Return a connection with row_factory set to Row."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def open_position(
        self,
        signal_id: Optional[int],
        symbol: str,
        direction: str,
        entry_price: float,
        position_size: float,
        stop_loss: float,
        tp1: Optional[float] = None,
        tp2: Optional[float] = None,
        tp3: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Open a new position and insert it into the database.

        Args:
            signal_id: Foreign key to the signals table (None if manual entry).
            symbol: Asset ticker, e.g. ``'BTC/USDT'``.
            direction: ``'LONG'`` or ``'SHORT'``.
            entry_price: Price at which the position is opened.
            position_size: Number of base-asset units to trade.
            stop_loss: Stop-loss price.
            tp1: First take-profit target (optional).
            tp2: Second take-profit target (optional).
            tp3: Third take-profit target (optional).
            notes: Optional text notes.

        Returns:
            The database row ID of the newly created position.

        Raises:
            sqlite3.Error: If database insertion fails.
        """
        entry_time = int(time.time() * 1000)

        sql = """
            INSERT INTO positions (
                signal_id, symbol, direction,
                entry_price, position_size, stop_loss,
                tp1, tp2, tp3,
                status, entry_time, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """
        params = (
            signal_id, symbol, direction,
            entry_price, position_size, stop_loss,
            tp1, tp2, tp3,
            entry_time, notes,
        )

        try:
            with self._get_conn() as conn:
                cur = conn.execute(sql, params)
                conn.commit()
                position_id: int = cur.lastrowid  # type: ignore[assignment]

            logger.info(
                "[PositionTracker] Opened position id=%d %s %s @ %.2f (size=%.6f, SL=%.2f)",
                position_id, direction, symbol, entry_price, position_size, stop_loss,
            )
            return position_id

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to open position: %s", e)
            raise

    def update_position(
        self,
        position_id: int,
        current_price: float,
    ) -> dict:
        """Update a position based on current price, checking for TP/SL hits.

        If the stop-loss or any take-profit level is hit, the position is
        automatically closed and P&L metrics are calculated.

        Logic:
        - If price hits SL → close as CLOSED_LOSS.
        - If price hits TP1/TP2/TP3 → mark the TP as hit.
        - Optionally auto-close on first TP hit (currently TP3 closes the position).

        Args:
            position_id: Database ID of the position to update.
            current_price: Current market price.

        Returns:
            A dict representation of the updated position with all fields.

        Raises:
            ValueError: If the position does not exist or is already closed.
            sqlite3.Error: If database operations fail.
        """
        try:
            # Fetch the position
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                ).fetchone()

            if not row:
                raise ValueError(f"Position {position_id} does not exist")

            pos = dict(row)

            if pos["status"] != "OPEN":
                raise ValueError(
                    f"Position {position_id} is already closed with status {pos['status']}"
                )

            direction = pos["direction"]
            entry_price = pos["entry_price"]
            stop_loss = pos["stop_loss"]
            tp1 = pos["tp1"]
            tp2 = pos["tp2"]
            tp3 = pos["tp3"]

            # Check for SL hit
            sl_hit = False
            if direction == "LONG" and current_price <= stop_loss:
                sl_hit = True
            elif direction == "SHORT" and current_price >= stop_loss:
                sl_hit = True

            if sl_hit:
                logger.info(
                    "[PositionTracker] SL hit for position %d at %.2f",
                    position_id, current_price,
                )
                return self.close_position(
                    position_id, close_price=stop_loss, reason="SL_HIT"
                )

            # Check for TP hits
            tp1_hit = pos["tp1_hit"]
            tp2_hit = pos["tp2_hit"]
            tp3_hit = pos["tp3_hit"]

            if tp1 is not None and not tp1_hit:
                if (direction == "LONG" and current_price >= tp1) or \
                   (direction == "SHORT" and current_price <= tp1):
                    tp1_hit = 1
                    logger.info(
                        "[PositionTracker] TP1 hit for position %d at %.2f",
                        position_id, current_price,
                    )

            if tp2 is not None and not tp2_hit:
                if (direction == "LONG" and current_price >= tp2) or \
                   (direction == "SHORT" and current_price <= tp2):
                    tp2_hit = 1
                    logger.info(
                        "[PositionTracker] TP2 hit for position %d at %.2f",
                        position_id, current_price,
                    )

            if tp3 is not None and not tp3_hit:
                if (direction == "LONG" and current_price >= tp3) or \
                   (direction == "SHORT" and current_price <= tp3):
                    tp3_hit = 1
                    logger.info(
                        "[PositionTracker] TP3 hit for position %d at %.2f",
                        position_id, current_price,
                    )
                    # Auto-close on TP3 hit
                    return self.close_position(
                        position_id, close_price=tp3, reason="TP3_HIT"
                    )

            # Update TP hit flags
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE positions
                    SET tp1_hit = ?, tp2_hit = ?, tp3_hit = ?
                    WHERE id = ?
                    """,
                    (tp1_hit, tp2_hit, tp3_hit, position_id),
                )
                conn.commit()

                # Fetch updated position
                updated_row = conn.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                ).fetchone()

            return dict(updated_row)

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to update position %d: %s", position_id, e)
            raise

    def close_position(
        self,
        position_id: int,
        close_price: float,
        reason: str = "manual",
    ) -> dict:
        """Manually close a position and calculate P&L metrics.

        P&L calculation:
        - For LONG: ``pnl_usd = (close_price - entry_price) * position_size``
        - For SHORT: ``pnl_usd = (entry_price - close_price) * position_size``
        - ``pnl_pct = (pnl_usd / risk_usd) * 100``
        - ``rr_realized = pnl_usd / risk_usd`` (in R multiples)

        Status assignment:
        - ``pnl_pct < -5`` → CLOSED_LOSS
        - ``pnl_pct > 5`` → CLOSED_WIN
        - ``-5 <= pnl_pct <= 5`` → CLOSED_BE (break-even)

        Args:
            position_id: Database ID of the position to close.
            close_price: Final exit price.
            reason: Reason for closing (e.g., ``'manual'``, ``'SL_HIT'``, ``'TP3_HIT'``).

        Returns:
            A dict representation of the closed position with all P&L fields populated.

        Raises:
            ValueError: If the position does not exist or is already closed.
            sqlite3.Error: If database operations fail.
        """
        close_time = int(time.time() * 1000)

        try:
            # Fetch the position
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                ).fetchone()

            if not row:
                raise ValueError(f"Position {position_id} does not exist")

            pos = dict(row)

            if pos["status"] != "OPEN":
                logger.warning(
                    "[PositionTracker] Position %d already closed with status %s",
                    position_id, pos["status"],
                )
                return pos

            direction = pos["direction"]
            entry_price = pos["entry_price"]
            position_size = pos["position_size"]
            stop_loss = pos["stop_loss"]

            # Calculate P&L
            if direction == "LONG":
                pnl_usd = (close_price - entry_price) * position_size
            elif direction == "SHORT":
                pnl_usd = (entry_price - close_price) * position_size
            else:
                raise ValueError(f"Invalid direction: {direction}")

            # Calculate risk in USD (distance to SL * position size)
            risk_usd = abs(entry_price - stop_loss) * position_size

            if risk_usd == 0:
                logger.warning(
                    "[PositionTracker] Zero risk_usd for position %d — cannot calculate pnl_pct",
                    position_id,
                )
                pnl_pct = 0.0
                rr_realized = 0.0
            else:
                pnl_pct = (pnl_usd / risk_usd) * 100.0
                rr_realized = pnl_usd / risk_usd

            # Determine status
            if pnl_pct < -5:
                status = "CLOSED_LOSS"
            elif pnl_pct > 5:
                status = "CLOSED_WIN"
            else:
                status = "CLOSED_BE"

            # Mark SL hit if reason is SL_HIT
            sl_hit = 1 if reason == "SL_HIT" else pos["sl_hit"]

            # Update notes
            existing_notes = pos["notes"] or ""
            new_notes = f"{existing_notes}\nClosed: {reason}".strip()

            # Update the database
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE positions
                    SET status = ?, close_time = ?, close_price = ?,
                        pnl_usd = ?, pnl_pct = ?, rr_realized = ?,
                        sl_hit = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        status, close_time, close_price,
                        pnl_usd, pnl_pct, rr_realized,
                        sl_hit, new_notes,
                        position_id,
                    ),
                )
                conn.commit()

                # Fetch updated position
                updated_row = conn.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                ).fetchone()

            result = dict(updated_row)

            logger.info(
                "[PositionTracker] Closed position id=%d %s @ %.2f | "
                "P&L: $%.2f (%.1f%%) | RR: %.2fR | Status: %s",
                position_id, pos["symbol"], close_price,
                pnl_usd, pnl_pct, rr_realized, status,
            )

            return result

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to close position %d: %s", position_id, e)
            raise

    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Return all open positions, optionally filtered by symbol.

        Args:
            symbol: Filter to a specific asset ticker (None returns all).

        Returns:
            List of position dicts, ordered by entry time (oldest first).
        """
        try:
            with self._get_conn() as conn:
                if symbol:
                    sql = """
                        SELECT * FROM positions
                        WHERE status = 'OPEN' AND symbol = ?
                        ORDER BY entry_time ASC
                    """
                    rows = conn.execute(sql, (symbol,)).fetchall()
                else:
                    sql = """
                        SELECT * FROM positions
                        WHERE status = 'OPEN'
                        ORDER BY entry_time ASC
                    """
                    rows = conn.execute(sql).fetchall()

            return [dict(row) for row in rows]

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to fetch open positions: %s", e)
            return []

    def get_closed_positions(
        self,
        days: int = 7,
        symbol: Optional[str] = None,
    ) -> list[dict]:
        """Return positions closed in the last N days, optionally filtered by symbol.

        Args:
            days: Look-back window in calendar days (default 7).
            symbol: Filter to a specific asset ticker (None returns all).

        Returns:
            List of position dicts, ordered by close time (newest first).
        """
        cutoff_ms = int((time.time() - days * 86400) * 1000)

        try:
            with self._get_conn() as conn:
                if symbol:
                    sql = """
                        SELECT * FROM positions
                        WHERE status != 'OPEN' AND close_time >= ? AND symbol = ?
                        ORDER BY close_time DESC
                    """
                    rows = conn.execute(sql, (cutoff_ms, symbol)).fetchall()
                else:
                    sql = """
                        SELECT * FROM positions
                        WHERE status != 'OPEN' AND close_time >= ?
                        ORDER BY close_time DESC
                    """
                    rows = conn.execute(sql, (cutoff_ms,)).fetchall()

            return [dict(row) for row in rows]

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to fetch closed positions: %s", e)
            return []

    def get_position(self, position_id: int) -> Optional[dict]:
        """Fetch a single position by ID.

        Args:
            position_id: Database ID of the position.

        Returns:
            Position dict, or None if not found.
        """
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                ).fetchone()

            return dict(row) if row else None

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to fetch position %d: %s", position_id, e)
            return None

    def get_statistics(
        self,
        days: int = 7,
        symbol: Optional[str] = None,
    ) -> dict:
        """Calculate performance statistics over the last N days.

        Args:
            days: Look-back window in calendar days (default 7).
            symbol: Filter to a specific asset ticker (None aggregates all).

        Returns:
            A dict with keys:
            - ``total_closed``: Total number of closed positions.
            - ``wins``: Number of CLOSED_WIN positions.
            - ``losses``: Number of CLOSED_LOSS positions.
            - ``breakeven``: Number of CLOSED_BE positions.
            - ``win_rate``: Percentage of wins (0–100).
            - ``total_pnl_usd``: Sum of all realized P&L in USD.
            - ``avg_rr_realized``: Average risk:reward ratio achieved.
            - ``symbol``: Echoed back (None means "all").
            - ``days``: Echoed back.
        """
        cutoff_ms = int((time.time() - days * 86400) * 1000)
        base_where = "status != 'OPEN' AND close_time >= ?"
        params: list = [cutoff_ms]

        if symbol:
            base_where += " AND symbol = ?"
            params.append(symbol)

        try:
            with self._get_conn() as conn:
                # Total closed
                total_closed = conn.execute(
                    f"SELECT COUNT(*) FROM positions WHERE {base_where}", params
                ).fetchone()[0]

                # Wins, losses, breakeven
                wins = conn.execute(
                    f"SELECT COUNT(*) FROM positions WHERE {base_where} AND status = 'CLOSED_WIN'",
                    params,
                ).fetchone()[0]

                losses = conn.execute(
                    f"SELECT COUNT(*) FROM positions WHERE {base_where} AND status = 'CLOSED_LOSS'",
                    params,
                ).fetchone()[0]

                breakeven = conn.execute(
                    f"SELECT COUNT(*) FROM positions WHERE {base_where} AND status = 'CLOSED_BE'",
                    params,
                ).fetchone()[0]

                # Total P&L
                total_pnl_row = conn.execute(
                    f"SELECT SUM(pnl_usd) FROM positions WHERE {base_where}", params
                ).fetchone()
                total_pnl_usd = total_pnl_row[0] if total_pnl_row[0] is not None else 0.0

                # Average RR
                avg_rr_row = conn.execute(
                    f"SELECT AVG(rr_realized) FROM positions WHERE {base_where}", params
                ).fetchone()
                avg_rr_realized = avg_rr_row[0] if avg_rr_row[0] is not None else 0.0

            win_rate = (wins / total_closed * 100) if total_closed > 0 else 0.0

            return {
                "total_closed": total_closed,
                "wins": wins,
                "losses": losses,
                "breakeven": breakeven,
                "win_rate": win_rate,
                "total_pnl_usd": total_pnl_usd,
                "avg_rr_realized": avg_rr_realized,
                "symbol": symbol,
                "days": days,
            }

        except sqlite3.Error as e:
            logger.error("[PositionTracker] Failed to calculate statistics: %s", e)
            return {
                "total_closed": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
                "win_rate": 0.0,
                "total_pnl_usd": 0.0,
                "avg_rr_realized": 0.0,
                "symbol": symbol,
                "days": days,
            }
