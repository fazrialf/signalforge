"""
SignalForge WebSocket Feed
Maintains a live Binance WebSocket for real-time price ticks.
Auto-reconnects with exponential backoff on disconnect.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"


class WebSocketFeed:
    def __init__(self, symbol: str, on_tick: Callable[[dict], None]):
        """
        symbol:   e.g. 'BTC/USDT' (converted to 'btcusdt' for WS)
        on_tick:  callback called with each kline/trade event
        """
        self.symbol    = symbol.replace("/", "").lower()  # btcusdt
        self.on_tick   = on_tick
        self._running  = False
        self._last_tick: Optional[float] = None  # unix timestamp of last received tick
        self._ws_session: Optional[aiohttp.ClientSession] = None

    @property
    def last_tick_age(self) -> Optional[float]:
        """Seconds since last tick received. None if never received."""
        if self._last_tick is None:
            return None
        return time.time() - self._last_tick

    @property
    def is_alive(self) -> bool:
        """True if last tick was within 120 seconds."""
        age = self.last_tick_age
        return age is not None and age < 120

    async def start(self):
        """Start the WebSocket feed with auto-reconnect."""
        self._running = True
        backoff = 1  # seconds

        while self._running:
            try:
                await self._connect()
                backoff = 1  # reset on success
            except asyncio.CancelledError:
                logger.info("[WS] Cancelled.")
                break
            except Exception as e:
                logger.warning("[WS] Disconnected: %s. Reconnecting in %ds...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)  # max 60s backoff

    async def stop(self):
        self._running = False
        if self._ws_session:
            await self._ws_session.close()

    async def _connect(self):
        """Connect to Binance WebSocket and stream kline + aggTrade data."""
        # Subscribe to 1m klines + aggTrade stream.
        # aggTrade bundles fills at the same price/time into one message,
        # which is what OrderFlowAccumulator.on_trade() expects for
        # accurate delta / CVD calculation.  Raw @trade sends each fill
        # individually — noisier and higher bandwidth for no benefit.
        stream = f"{self.symbol}@kline_1m/{self.symbol}@aggTrade"
        url    = f"{BINANCE_WS_BASE}/{stream}"

        logger.info("[WS] Connecting to %s", url)
        async with aiohttp.ClientSession() as session:
            self._ws_session = session
            async with session.ws_connect(
                url,
                heartbeat=20,
                receive_timeout=60
            ) as ws:
                logger.info("[WS] Connected to %s stream", self.symbol.upper())
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error("[WS] Error: %s", msg.data)
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        logger.warning("[WS] Connection closed.")
                        break

    async def _handle_message(self, raw: str):
        """Parse and dispatch incoming WebSocket message."""
        try:
            data = json.loads(raw)
            event_type = data.get("e")

            if event_type == "kline":
                kline = data["k"]
                tick = {
                    "type":      "kline",
                    "symbol":    self.symbol.upper(),
                    "ts":        data["E"],  # event time ms
                    "open":      float(kline["o"]),
                    "high":      float(kline["h"]),
                    "low":       float(kline["l"]),
                    "close":     float(kline["c"]),
                    "volume":    float(kline["v"]),
                    "is_closed": kline["x"],  # True = candle closed
                    "interval":  kline["i"],
                }
            elif event_type == "aggTrade":
                # aggTrade bundles fills at the same price/time/side.
                # Fields: p=price, q=aggregate_qty, T=trade_time, m=is_buyer_maker
                tick = {
                    "type":   "trade",  # downstream consumers key on "trade"
                    "symbol": self.symbol.upper(),
                    "ts":     data["T"],  # trade time ms
                    "price":  float(data["p"]),
                    "qty":    float(data["q"]),
                    "side":   "sell" if data["m"] else "buy",  # m=True → taker sold
                }
            else:
                return  # unknown event type, skip

            self._last_tick = time.time()
            self.on_tick(tick)

        except Exception as e:
            logger.warning("[WS] Failed to parse message: %s — %s", e, raw[:200])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    tick_count = 0
    last_price = None

    def handle_tick(tick: dict):
        global tick_count, last_price
        tick_count += 1
        if tick["type"] == "kline":
            last_price = tick["close"]
            if tick_count % 5 == 0:  # print every 5 ticks
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{ts}] BTC/USDT = ${last_price:,.2f}  (tick #{tick_count})")
        if tick_count >= 20:
            print("\nTest complete: received 20 ticks.")
            asyncio.get_event_loop().stop()

    async def test():
        feed = WebSocketFeed("BTC/USDT", handle_tick)
        await asyncio.wait_for(feed.start(), timeout=60)

    try:
        asyncio.run(test())
    except (asyncio.TimeoutError, RuntimeError):
        print(f"Final: received {tick_count} ticks, last price = ${last_price:,.2f}")
