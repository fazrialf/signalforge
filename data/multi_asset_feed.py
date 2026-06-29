"""
SignalForge Multi-Asset Feed Manager
Manages WebSocket connections for multiple assets simultaneously.
Routes ticks to a shared on_tick(symbol, price) callback and maintains
per-asset state. Wraps the existing WebSocketFeed and DataFetcher classes.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from data.fetcher import DataFetcher
from data.websocket_feed import WebSocketFeed

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Asset config shape expected in asset_configs list
# ---------------------------------------------------------------------------
# Each item is a dict (or object with .symbol / .timeframes / .enabled attrs).
# Minimum required keys when dict:
#   "symbol"     str  e.g. "BTC/USDT"
#   "enabled"    bool (default True if absent)
#   "timeframes" list[str] (default ["1m","5m","15m","1h","4h","1d"])
# ---------------------------------------------------------------------------


@dataclass
class AssetFeedState:
    symbol: str
    last_price: float = 0.0
    tick_count: int = 0
    connected: bool = False
    last_tick_at: float = 0.0   # unix timestamp of last received tick
    ws: Optional[object] = None  # WebSocketFeed instance
    ws_task: Optional[asyncio.Task] = None  # asyncio task running ws.start()


class MultiAssetFeed:
    """
    Manages one WebSocketFeed per asset.

    Usage:
        feed = MultiAssetFeed(db_path, on_tick=my_callback)
        await feed.start_all(asset_configs)
        ...
        await feed.stop_all()

    on_tick signature: on_tick(symbol: str, price: float) -> None
    """

    # Default timeframes used when an asset config omits the key
    DEFAULT_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
    DEFAULT_HISTORY_BARS = 300

    def __init__(self, db_path: str, on_tick: Callable[[str, float], None]):
        """
        db_path:  path to the SQLite database (passed through to DataFetcher)
        on_tick:  called as on_tick(symbol, price) for every incoming tick
        """
        self._db_path  = db_path
        self._on_tick  = on_tick
        # symbol (upper, slash-normalised) -> AssetFeedState
        self._states: dict[str, AssetFeedState] = {}

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    async def start_all(self, asset_configs: list) -> None:
        """
        Start a WebSocketFeed for every enabled asset in asset_configs.
        Configs that are already running are skipped (idempotent).
        """
        enabled = [_parse_config(c) for c in asset_configs if _is_enabled(c)]

        if not enabled:
            logger.warning("[MAF] start_all called with no enabled assets.")
            return

        tasks = []
        for cfg in enabled:
            symbol = cfg["symbol"]
            if symbol in self._states and self._states[symbol].connected:
                logger.debug("[MAF] %s already running, skipping.", symbol)
                continue
            tasks.append(self._start_one(cfg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_all(self) -> None:
        """Stop all active WebSocket feeds and clean up tasks."""
        stop_tasks = []
        for symbol, state in list(self._states.items()):
            stop_tasks.append(self._stop_one(symbol, state))

        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)

        logger.info("[MAF] All feeds stopped.")

    def get_state(self, symbol: str) -> Optional[AssetFeedState]:
        """Return the AssetFeedState for symbol, or None if not tracked."""
        return self._states.get(_normalise(symbol))

    def get_all_states(self) -> list[AssetFeedState]:
        """Return a snapshot list of all tracked AssetFeedState objects."""
        return list(self._states.values())

    def latest_price(self, symbol: str) -> float:
        """Return the last known price for symbol (0.0 if never received)."""
        state = self._states.get(_normalise(symbol))
        return state.last_price if state else 0.0

    def is_healthy(self, symbol: str, max_age_seconds: int = 120) -> bool:
        """
        True if the feed for symbol is connected and received a tick within
        max_age_seconds.  Falls back to WebSocketFeed.is_alive when available.
        """
        state = self._states.get(_normalise(symbol))
        if state is None or not state.connected:
            return False

        # Delegate to the underlying feed's own liveness check if possible
        if state.ws is not None and hasattr(state.ws, "is_alive"):
            return state.ws.is_alive

        # Fallback: check our own timestamp
        if state.last_tick_at == 0.0:
            return False
        return (time.time() - state.last_tick_at) < max_age_seconds

    async def fetch_history(
        self,
        asset_configs: list,
        db_path: str,
    ) -> dict[str, bool]:
        """
        Fetch historical OHLCV data for all enabled assets via DataFetcher.

        Returns a dict {symbol: success_bool} for every enabled asset.
        Fetches are run concurrently; individual failures do not abort others.
        """
        enabled = [_parse_config(c) for c in asset_configs if _is_enabled(c)]
        if not enabled:
            logger.warning("[MAF] fetch_history: no enabled assets.")
            return {}

        results = await asyncio.gather(
            *[self._fetch_one_history(cfg, db_path) for cfg in enabled],
            return_exceptions=True,
        )

        outcome: dict[str, bool] = {}
        for cfg, result in zip(enabled, results):
            symbol = cfg["symbol"]
            if isinstance(result, Exception):
                logger.error("[MAF] fetch_history %s: %s", symbol, result)
                outcome[symbol] = False
            else:
                outcome[symbol] = result  # True / False returned by helper

        return outcome

    # ------------------------------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------------------------------

    async def _start_one(self, cfg: dict) -> None:
        """Initialise state and launch the WebSocketFeed task for one asset."""
        symbol = cfg["symbol"]

        state = self._states.get(symbol)
        if state is None:
            state = AssetFeedState(symbol=symbol)
            self._states[symbol] = state

        # Build the WebSocketFeed with a closure that captures this symbol
        def make_on_tick(sym: str, st: AssetFeedState):
            def _on_tick(tick: dict):
                price = _extract_price(tick)
                if price is None:
                    return
                st.last_price  = price
                st.tick_count += 1
                st.last_tick_at = time.time()
                try:
                    self._on_tick(sym, price)
                except Exception as exc:
                    logger.error("[MAF] on_tick callback error for %s: %s", sym, exc)
            return _on_tick

        ws = WebSocketFeed(symbol, on_tick=make_on_tick(symbol, state))
        state.ws = ws

        # Launch the feed as a background task; it handles its own reconnect loop
        task = asyncio.create_task(
            self._run_ws_with_state(ws, state),
            name=f"ws-feed-{symbol.replace('/', '').lower()}",
        )
        state.ws_task = task
        state.connected = True
        logger.info("[MAF] Started feed for %s.", symbol)

    async def _run_ws_with_state(
        self, ws: WebSocketFeed, state: AssetFeedState
    ) -> None:
        """
        Thin wrapper around ws.start() that keeps state.connected accurate.
        WebSocketFeed.start() already handles reconnect with backoff, so we
        just mark the feed as disconnected if the coroutine ever exits.
        """
        try:
            await ws.start()
        except asyncio.CancelledError:
            logger.info("[MAF] Feed task for %s cancelled.", state.symbol)
        except Exception as exc:
            logger.error("[MAF] Feed for %s exited unexpectedly: %s", state.symbol, exc)
        finally:
            state.connected = False

    async def _stop_one(self, symbol: str, state: AssetFeedState) -> None:
        """Stop the WebSocketFeed and cancel its task for one asset."""
        # Ask the feed to stop (closes the aiohttp session)
        if state.ws is not None:
            try:
                await state.ws.stop()
            except Exception as exc:
                logger.warning("[MAF] Error stopping WS for %s: %s", symbol, exc)

        # Cancel the asyncio task if still running
        if state.ws_task is not None and not state.ws_task.done():
            state.ws_task.cancel()
            try:
                await state.ws_task
            except (asyncio.CancelledError, Exception):
                pass

        state.connected = False
        logger.info("[MAF] Stopped feed for %s.", symbol)

    async def _fetch_one_history(self, cfg: dict, db_path: str) -> bool:
        """
        Fetch historical OHLCV for a single asset using DataFetcher.
        Returns True on success, False on failure.
        """
        symbol     = cfg["symbol"]
        timeframes = cfg.get("timeframes", self.DEFAULT_TIMEFRAMES)
        bars       = cfg.get("history_bars", self.DEFAULT_HISTORY_BARS)

        fetcher = DataFetcher(symbol, timeframes, history_bars=bars)
        try:
            data = await fetcher.load_all()
            loaded = [tf for tf, df in data.items() if not df.empty]
            logger.info(
                "[MAF] History fetched for %s: %d/%d timeframes loaded.",
                symbol, len(loaded), len(timeframes),
            )
            return len(loaded) > 0
        except Exception as exc:
            logger.error("[MAF] fetch_history failed for %s: %s", symbol, exc)
            return False
        finally:
            await fetcher.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _normalise(symbol: str) -> str:
    """Uppercase and slash-normalise a symbol for use as dict key."""
    return symbol.upper()


def _parse_config(cfg) -> dict:
    """
    Accept either a plain dict or an object with attributes.
    Always returns a dict with at least 'symbol' and 'enabled'.
    """
    if isinstance(cfg, dict):
        d = dict(cfg)
    else:
        # Dataclass / object fallback
        d = {
            "symbol":     getattr(cfg, "symbol", ""),
            "enabled":    getattr(cfg, "enabled", True),
            "timeframes": getattr(cfg, "timeframes", MultiAssetFeed.DEFAULT_TIMEFRAMES),
            "history_bars": getattr(cfg, "history_bars", MultiAssetFeed.DEFAULT_HISTORY_BARS),
        }
    d["symbol"] = _normalise(d.get("symbol", ""))
    return d


def _is_enabled(cfg) -> bool:
    """Return True if the asset config is marked enabled (default True)."""
    if isinstance(cfg, dict):
        return bool(cfg.get("enabled", True))
    return bool(getattr(cfg, "enabled", True))


def _extract_price(tick: dict) -> Optional[float]:
    """
    Pull the best price out of a tick dict as emitted by WebSocketFeed.
    - kline ticks  -> close price
    - trade ticks  -> trade price
    Returns None if the tick type is unrecognised.
    """
    t = tick.get("type")
    if t == "kline":
        return tick.get("close")
    if t == "trade":
        return tick.get("price")
    return None


# ---------------------------------------------------------------------------
# Manual smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    from config.settings import DB_PATH, TIMEFRAMES, HISTORY_BARS

    ASSETS = [
        {"symbol": "BTC/USDT", "enabled": True,  "timeframes": TIMEFRAMES, "history_bars": HISTORY_BARS},
        {"symbol": "ETH/USDT", "enabled": True,  "timeframes": TIMEFRAMES, "history_bars": HISTORY_BARS},
        {"symbol": "SOL/USDT", "enabled": False, "timeframes": TIMEFRAMES, "history_bars": HISTORY_BARS},
    ]

    tick_log: dict[str, int] = {}

    def on_tick(symbol: str, price: float) -> None:
        tick_log[symbol] = tick_log.get(symbol, 0) + 1
        count = tick_log[symbol]
        if count % 10 == 1:  # print every 10th tick
            print(f"  [{symbol}]  price={price:,.4f}  ticks={count}")

    async def smoke_test():
        manager = MultiAssetFeed(db_path=DB_PATH, on_tick=on_tick)

        print("=== Fetching history ===")
        results = await manager.fetch_history(ASSETS, DB_PATH)
        for sym, ok in results.items():
            print(f"  {sym}: {'OK' if ok else 'FAILED'}")

        print("\n=== Starting live feeds (30s) ===")
        await manager.start_all(ASSETS)

        await asyncio.sleep(30)

        print("\n=== Feed states ===")
        for state in manager.get_all_states():
            healthy = manager.is_healthy(state.symbol)
            print(
                f"  {state.symbol:12s}  price={state.last_price:>12,.4f}"
                f"  ticks={state.tick_count:>4d}  healthy={healthy}"
            )

        print("\n=== Stopping all feeds ===")
        await manager.stop_all()
        print("Done.")

    asyncio.run(smoke_test())
