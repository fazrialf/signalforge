"""
SignalForge — Liquidation Level Tracker
========================================
Tracks recent forced liquidation orders from Binance Futures and builds
a rolling map of liquidation price clusters per symbol.

Liquidation clusters near current price = high-probability sweep targets.
Price approaching a dense liquidation cluster adds confluence for a move
toward that level (stop-hunt / liquidity grab setup).

Data source: Binance Futures public REST API — no API key required.
Endpoint: GET https://fapi.binance.com/fapi/v1/forceOrders
Cache TTL: 5 minutes per symbol.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://fapi.binance.com"
_REQUEST_TIMEOUT = 8
_CACHE_TTL = 300  # 5 minutes

# In-memory cache: symbol -> {"levels": [...], "fetched_at": float}
_cache: dict[str, dict] = {}

def _binance_symbol(symbol: str) -> str:
    """Convert 'BTC/USDT' -> 'BTCUSDT'."""
    return symbol.replace("/", "").upper()

def fetch_liquidation_levels(
    symbol: str,
    current_price: float,
    lookback_hours: int = 4,
    cluster_pct: float = 0.5,
) -> dict:
    """
    Fetch and cluster recent liquidation orders for a symbol.

    Args:
        symbol: ccxt-style symbol e.g. 'BTC/USDT'
        current_price: current market price (used for proximity scoring)
        lookback_hours: how many hours back to scan (max 24h, API limit)
        cluster_pct: price range % to group liquidations into one cluster

    Returns dict with keys:
        symbol: str
        current_price: float
        clusters: list of dicts, each with:
            price: float  (cluster centroid price)
            total_qty: float  (total liquidated qty in cluster)
            count: int  (number of liquidation events)
            side: 'long' | 'short' | 'mixed'  (which side was liquidated)
            distance_pct: float  (% distance from current_price)
        nearest_long_liq: float | None  (nearest long liquidation level above/below price)
        nearest_short_liq: float | None  (nearest short liquidation level)
        dense_cluster_nearby: bool  (True if any cluster within 1.5% of current price)
        sweep_direction: 'bullish' | 'bearish' | None  (likely sweep direction)
        fetched_at: str (ISO timestamp)
    """
    bin_sym = _binance_symbol(symbol)
    now = time.time()

    # Check cache
    cached = _cache.get(bin_sym)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL:
        logger.debug("[LIQ] %s using cached liquidation data", symbol)
        return cached["data"]

    default = {
        "symbol": symbol,
        "current_price": current_price,
        "clusters": [],
        "nearest_long_liq": None,
        "nearest_short_liq": None,
        "dense_cluster_nearby": False,
        "sweep_direction": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        start_ms = int((now - lookback_hours * 3600) * 1000)
        params = {
            "symbol": bin_sym,
            "startTime": start_ms,
            "limit": 100,
        }
        resp = requests.get(
            f"{BASE_URL}/fapi/v1/forceOrders",
            params=params,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        orders = resp.json()

        if not orders:
            logger.info("[LIQ] %s no recent liquidations found", symbol)
            _cache[bin_sym] = {"fetched_at": now, "data": default}
            return default

        # Group into price clusters
        # Sort by price, then group nearby prices within cluster_pct%
        orders_sorted = sorted(orders, key=lambda o: float(o["price"]))
        clusters = []
        current_cluster: list[dict] = []
        cluster_base_price: float | None = None

        for order in orders_sorted:
            price = float(order["price"])
            qty = float(order.get("origQty", order.get("executedQty", 0)))
            side = order.get("side", "").lower()  # BUY=short_liq, SELL=long_liq

            if cluster_base_price is None:
                cluster_base_price = price
                current_cluster = [{"price": price, "qty": qty, "side": side}]
            elif abs(price - cluster_base_price) / cluster_base_price * 100 <= cluster_pct:
                current_cluster.append({"price": price, "qty": qty, "side": side})
            else:
                # Save current cluster
                if current_cluster:
                    clusters.append(_build_cluster(current_cluster, current_price))
                cluster_base_price = price
                current_cluster = [{"price": price, "qty": qty, "side": side}]

        if current_cluster:
            clusters.append(_build_cluster(current_cluster, current_price))

        # Sort by total_qty descending (most significant first)
        clusters.sort(key=lambda c: c["total_qty"], reverse=True)

        # Find nearest long/short liq levels
        nearest_long = None
        nearest_short = None
        dense_nearby = False
        sweep_dir = None

        long_liq_clusters = [c for c in clusters if c["side"] in ("long", "mixed")]
        short_liq_clusters = [c for c in clusters if c["side"] in ("short", "mixed")]

        if long_liq_clusters:
            nearest_long = min(long_liq_clusters, key=lambda c: abs(c["distance_pct"]))["price"]
        if short_liq_clusters:
            nearest_short = min(short_liq_clusters, key=lambda c: abs(c["distance_pct"]))["price"]

        # Check for dense cluster within 1.5% of current price
        nearby = [c for c in clusters if abs(c["distance_pct"]) <= 1.5]
        if nearby:
            dense_nearby = True
            # Sweep direction: dense long liq below price = bearish sweep target
            # dense short liq above price = bullish sweep target
            below = [c for c in nearby if c["price"] < current_price]
            above = [c for c in nearby if c["price"] > current_price]
            below_longs = [c for c in below if c["side"] in ("long", "mixed")]
            above_shorts = [c for c in above if c["side"] in ("short", "mixed")]
            if below_longs and not above_shorts:
                sweep_dir = "bearish"  # long liq pool below = magnet for price to sweep down
            elif above_shorts and not below_longs:
                sweep_dir = "bullish"  # short liq pool above = magnet for price to sweep up
            elif above_shorts and below_longs:
                sweep_dir = None  # liq on both sides = neutral

        result = {
            **default,
            "clusters": clusters[:10],  # top 10 by volume
            "nearest_long_liq": nearest_long,
            "nearest_short_liq": nearest_short,
            "dense_cluster_nearby": dense_nearby,
            "sweep_direction": sweep_dir,
        }

        _cache[bin_sym] = {"fetched_at": now, "data": result}
        logger.info(
            "[LIQ] %s clusters=%d dense_nearby=%s sweep=%s",
            symbol, len(clusters), dense_nearby, sweep_dir,
        )
        return result

    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            # forceOrders returns 400 if no liquidations exist in window — treat as empty
            logger.info("[LIQ] %s no liquidations in window (400 response)", symbol)
            _cache[bin_sym] = {"fetched_at": now, "data": default}
            return default
        logger.warning("[LIQ] %s HTTP error: %s", symbol, e)
    except requests.exceptions.Timeout:
        logger.warning("[LIQ] %s request timed out", symbol)
    except Exception as e:
        logger.warning("[LIQ] %s unexpected error: %s", symbol, e)

    return default

def _build_cluster(orders: list[dict], current_price: float) -> dict:
    """Build a cluster dict from a list of order dicts."""
    total_qty = sum(o["qty"] for o in orders)
    centroid = sum(o["price"] * o["qty"] for o in orders) / total_qty if total_qty else orders[0]["price"]
    sides = set(o["side"] for o in orders)
    # BUY side = short liquidation (shorts got liquidated, buying to close)
    # SELL side = long liquidation (longs got liquidated, selling to close)
    if sides == {"buy"}:
        side_label = "short"  # short positions liquidated
    elif sides == {"sell"}:
        side_label = "long"   # long positions liquidated
    else:
        side_label = "mixed"
    distance_pct = (centroid - current_price) / current_price * 100
    return {
        "price": round(centroid, 6),
        "total_qty": round(total_qty, 4),
        "count": len(orders),
        "side": side_label,
        "distance_pct": round(distance_pct, 3),
    }

def get_liq_confluence_signal(
    symbol: str,
    current_price: float,
    direction: str,
) -> tuple[bool, str]:
    """
    Returns (should_add_confluence: bool, description: str).

    direction: 'bullish' or 'bearish' — the current trade direction being evaluated.

    Logic:
    - Short liq pool ABOVE price + bullish direction = confluence (price will sweep up to grab liq)
    - Long liq pool BELOW price + bearish direction = confluence (price will sweep down to grab liq)
    - Dense cluster within 1.5% and sweep_direction matches trade direction = strong confluence
    """
    try:
        data = fetch_liquidation_levels(symbol, current_price)
        sweep = data.get("sweep_direction")
        dense = data.get("dense_cluster_nearby", False)
        clusters = data.get("clusters", [])

        if not clusters:
            return False, ""

        if dense and sweep == direction:
            nearest = min(clusters, key=lambda c: abs(c["distance_pct"]))
            return True, (
                f"Dense liq cluster at ${nearest['price']:,.4f} "
                f"({nearest['distance_pct']:+.1f}%) — {direction} sweep target"
            )

        # Weaker signal: any significant cluster in trade direction
        if direction == "bullish":
            targets = [c for c in clusters if c["side"] == "short" and c["price"] > current_price and abs(c["distance_pct"]) <= 3.0]
        else:
            targets = [c for c in clusters if c["side"] == "long" and c["price"] < current_price and abs(c["distance_pct"]) <= 3.0]

        if targets:
            best = max(targets, key=lambda c: c["total_qty"])
            return True, (
                f"Liq pool at ${best['price']:,.4f} "
                f"({best['distance_pct']:+.1f}%) — institutional sweep target"
            )

        return False, ""
    except Exception as e:
        logger.warning("[LIQ] get_liq_confluence_signal error: %s", e)
        return False, ""
