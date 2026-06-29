"""signals/cooldown.py — Per-asset cooldown tracker for SignalForge.

Prevents signal spam by enforcing a minimum time gap between consecutive
signals for the same asset. Supports optional SQLite persistence so cooldowns
survive process restarts.
"""
from __future__ import annotations

import sqlite3
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# SQLite table used for persistence
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS cooldowns (
    symbol  TEXT PRIMARY KEY,
    expiry  REAL NOT NULL  -- unix timestamp (seconds)
);
"""


class CooldownTracker:
    """Tracks per-asset cooldowns to prevent signal spam.

    Maintains an in-memory dict of ``symbol -> expiry_timestamp`` and
    optionally mirrors it to a SQLite table so cooldowns survive restarts.

    Args:
        default_cooldown_minutes: Minutes to block the same asset after a
            signal is delivered.  Defaults to 30.
        db_path: If given, cooldowns are persisted to this SQLite file and
            loaded back on construction.  Pass ``None`` (default) for
            purely in-memory operation.
    """

    def __init__(
        self,
        default_cooldown_minutes: int = 30,
        db_path: Optional[str] = None,
    ) -> None:
        self.default_cooldown_minutes = default_cooldown_minutes
        self.db_path = db_path
        # symbol -> expiry unix timestamp (float seconds)
        self._cooldowns: dict[str, float] = {}

        if self.db_path:
            self._init_db()
            self._load_from_db()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_in_cooldown(self, symbol: str) -> bool:
        """Return True if *symbol* is still within its cooldown period.

        Args:
            symbol: Asset ticker, e.g. ``'BTC/USDT'``.

        Returns:
            ``True`` if the cooldown has not yet expired, ``False`` otherwise.
        """
        expiry = self._cooldowns.get(symbol)
        if expiry is None:
            return False
        if time.time() < expiry:
            return True
        # Expired — clean up
        del self._cooldowns[symbol]
        if self.db_path:
            self._delete_from_db(symbol)
        return False

    def set_cooldown(self, symbol: str, minutes: Optional[int] = None) -> None:
        """Start a cooldown for *symbol*.

        Args:
            symbol: Asset ticker.
            minutes: How long to block the symbol.  Falls back to
                ``default_cooldown_minutes`` when ``None``.
        """
        duration = (minutes if minutes is not None else self.default_cooldown_minutes)
        expiry = time.time() + duration * 60.0
        self._cooldowns[symbol] = expiry
        logger.debug("[Cooldown] %s → %d min cooldown set", symbol, duration)
        if self.db_path:
            self._upsert_to_db(symbol, expiry)

    def clear_cooldown(self, symbol: str) -> None:
        """Manually remove the cooldown for *symbol* (e.g. after manual exit).

        Args:
            symbol: Asset ticker.
        """
        if symbol in self._cooldowns:
            del self._cooldowns[symbol]
            logger.debug("[Cooldown] %s cooldown cleared", symbol)
        if self.db_path:
            self._delete_from_db(symbol)

    def time_remaining(self, symbol: str) -> int:
        """Return minutes remaining in the cooldown for *symbol*.

        Args:
            symbol: Asset ticker.

        Returns:
            Minutes remaining (rounded up), or ``0`` if not in cooldown.
        """
        expiry = self._cooldowns.get(symbol)
        if expiry is None:
            return 0
        remaining = expiry - time.time()
        if remaining <= 0:
            del self._cooldowns[symbol]
            if self.db_path:
                self._delete_from_db(symbol)
            return 0
        return max(1, int(remaining / 60) + (1 if remaining % 60 > 0 else 0))

    # ------------------------------------------------------------------
    # SQLite helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create the cooldowns table if it does not already exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_TABLE_DDL)
            conn.commit()

    def _load_from_db(self) -> None:
        """Load non-expired cooldowns from SQLite into the in-memory dict."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, expiry FROM cooldowns WHERE expiry > ?", (now,)
            ).fetchall()
        for symbol, expiry in rows:
            self._cooldowns[symbol] = expiry
        if rows:
            logger.debug("[Cooldown] Loaded %d active cooldown(s) from DB", len(rows))

    def _upsert_to_db(self, symbol: str, expiry: float) -> None:
        """Insert or replace a cooldown row in SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cooldowns (symbol, expiry) VALUES (?, ?)",
                (symbol, expiry),
            )
            conn.commit()

    def _delete_from_db(self, symbol: str) -> None:
        """Remove a cooldown row from SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM cooldowns WHERE symbol = ?", (symbol,))
            conn.commit()
