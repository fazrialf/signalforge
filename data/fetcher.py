"""
SignalForge Data Fetcher
Fetches OHLCV data from Binance and stores in SQLite.
Handles both historical fetch on startup and periodic updates.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd

from db.schema import get_conn

logger = logging.getLogger(__name__)


class DataFetcher:
    def __init__(self, symbol: str, timeframes: list[str],
                 history_bars: int = 300):
        self.symbol      = symbol
        self.timeframes  = timeframes
        self.history_bars = history_bars
        self.exchange    = ccxt.binance({"enableRateLimit": True})
        # In-memory cache: {tf: pd.DataFrame}
        self._cache: dict[str, pd.DataFrame] = {}

    async def close(self):
        await self.exchange.close()

    # ----------------------------------------------------------
    # FETCH
    # ----------------------------------------------------------
    async def fetch_ohlcv(self, timeframe: str,
                           limit: int = 300) -> pd.DataFrame:
        """Fetch OHLCV from Binance and return as DataFrame."""
        try:
            raw = await self.exchange.fetch_ohlcv(
                self.symbol, timeframe=timeframe, limit=limit
            )
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            df = df.astype(float)
            self._cache[timeframe] = df
            await self._store_ohlcv(timeframe, df)
            return df
        except Exception as e:
            logger.error("fetch_ohlcv %s %s: %s", self.symbol, timeframe, e)
            # Return cached if available
            return self._cache.get(timeframe, pd.DataFrame())

    async def _store_ohlcv(self, timeframe: str, df: pd.DataFrame):
        """Upsert candles to SQLite."""
        rows = []
        for ts, row in df.iterrows():
            rows.append((
                self.symbol, timeframe,
                int(ts.timestamp() * 1000),
                row["open"], row["high"], row["low"],
                row["close"], row["volume"]
            ))
        conn = get_conn()
        conn.executemany(
            """
            INSERT OR REPLACE INTO candles
            (symbol, timeframe, ts, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows
        )
        conn.commit()
        conn.close()

    # ----------------------------------------------------------
    # STARTUP LOAD
    # ----------------------------------------------------------
    async def load_all(self) -> dict[str, pd.DataFrame]:
        """Fetch history for all configured timeframes."""
        tasks = [
            self.fetch_ohlcv(tf, limit=self.history_bars)
            for tf in self.timeframes
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        data = {}
        for tf, result in zip(self.timeframes, results):
            if isinstance(result, Exception):
                logger.error("load_all %s: %s", tf, result)
            else:
                data[tf] = result
                logger.info("Loaded %d bars for %s %s", len(result), self.symbol, tf)
        return data

    # ----------------------------------------------------------
    # PERIODIC UPDATE
    # ----------------------------------------------------------
    async def update_loop(self, interval_seconds: int = 60):
        """Periodically refresh the 1m and 5m candles."""
        while True:
            await asyncio.sleep(interval_seconds)
            for tf in ["1m", "5m", "15m"]:
                if tf in self.timeframes:
                    await self.fetch_ohlcv(tf, limit=10)
            # Less frequent updates for higher TFs
            now_min = datetime.now(timezone.utc).minute
            if now_min % 60 == 0:  # hourly
                for tf in ["1h"]:
                    if tf in self.timeframes:
                        await self.fetch_ohlcv(tf, limit=10)
            if now_min % 240 == 0:  # every 4h
                for tf in ["4h"]:
                    if tf in self.timeframes:
                        await self.fetch_ohlcv(tf, limit=10)

    # ----------------------------------------------------------
    # ACCESSORS
    # ----------------------------------------------------------
    def get(self, timeframe: str) -> Optional[pd.DataFrame]:
        """Return cached DataFrame for timeframe."""
        return self._cache.get(timeframe)

    def latest_price(self) -> Optional[float]:
        """Return latest close price from 1m or 5m candles."""
        for tf in ["1m", "5m", "15m", "1h"]:
            df = self._cache.get(tf)
            if df is not None and not df.empty:
                return float(df["close"].iloc[-1])
        return None


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from db.schema import init_db
    init_db()

    async def test():
        from config.settings import SYMBOL, TIMEFRAMES, HISTORY_BARS
        fetcher = DataFetcher(SYMBOL, TIMEFRAMES, HISTORY_BARS)
        print("Fetching data...")
        data = await fetcher.load_all()
        for tf, df in data.items():
            print(f"  {tf}: {len(df)} bars, last close = {df['close'].iloc[-1]:,.2f}")
        await fetcher.close()
        print("Done.")

    asyncio.run(test())
