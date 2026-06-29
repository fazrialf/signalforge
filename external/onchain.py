"""Binance Futures on-chain / derivatives metrics fetcher.

Fetches funding rate, open interest, long/short ratio, and taker buy/sell
volume from the Binance Futures public API. All endpoints are free and do not
require an API key.

Results are cached for 15 minutes in `/tmp/signalforge_onchain_cache.json` to
avoid excessive API calls.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL: str = "https://fapi.binance.com"
CACHE_PATH: Path = Path("/tmp/signalforge_onchain_cache.json")

# Cache TTL: 15 minutes (on-chain metrics change frequently)
_CACHE_TTL_SECONDS: int = 900
_REQUEST_TIMEOUT: int = 5  # seconds

# Default/fallback value returned when no cache is available and fetch fails
_DEFAULT_RESULT: dict = {
    "funding_rate": 0.0,
    "funding_sentiment": "neutral",
    "open_interest": 0.0,
    "oi_change_pct": 0.0,
    "long_short_ratio": 1.0,
    "ls_sentiment": "balanced",
    "taker_buy_ratio": 0.5,
    "taker_sentiment": "balanced",
    "fetched_at": None,
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict | None:
    """Load cached result from disk.

    Returns:
        Cached dict (raw storage format) if the file exists and is valid JSON,
        ``None`` otherwise.
    """
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Could not load onchain cache: %s", exc)
        return None


def _save_cache(payload: dict) -> None:
    """Persist *payload* to the cache file.

    The payload is the full result dict as returned by :func:`fetch_onchain_metrics`
    augmented with a ``cached_at`` Unix timestamp.

    Args:
        payload: Result dict to persist.
    """
    try:
        data = dict(payload)  # shallow copy
        # Store timestamp as ISO string for human readability
        if isinstance(data.get("fetched_at"), datetime):
            data["fetched_at"] = data["fetched_at"].isoformat()
        data["cached_at"] = time.time()
        with CACHE_PATH.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
        logger.debug("Onchain cache written to %s", CACHE_PATH)
    except OSError as exc:
        logger.warning("Could not write onchain cache: %s", exc)


def _cache_age_seconds(cache: dict) -> float:
    """Return how many seconds have elapsed since *cache* was written."""
    cached_at: float = cache.get("cached_at", 0.0)
    return time.time() - cached_at


def _deserialise_cache(cache: dict) -> dict:
    """Convert a raw cache dict back into the public result format.

    Specifically, ``fetched_at`` is re-hydrated from an ISO string to a
    :class:`~datetime.datetime` object if present.

    Args:
        cache: Raw dict loaded from the JSON cache file.

    Returns:
        Result dict with ``fetched_at`` as a :class:`~datetime.datetime` (or
        ``None``) and ``cached_at`` stripped out.
    """
    result = {k: v for k, v in cache.items() if k != "cached_at"}
    ts_raw = result.get("fetched_at")
    if isinstance(ts_raw, str):
        try:
            result["fetched_at"] = datetime.fromisoformat(ts_raw)
        except ValueError:
            result["fetched_at"] = None
    return result


# ---------------------------------------------------------------------------
# Sentiment logic
# ---------------------------------------------------------------------------

def _classify_funding_sentiment(funding_rate: float) -> str:
    """Classify funding rate sentiment.

    Args:
        funding_rate: Current funding rate (e.g., 0.0001).

    Returns:
        'bearish' if rate > 0.001 (longs paying = crowded long),
        'bullish' if rate < -0.001 (shorts paying = crowded short),
        'neutral' otherwise.
    """
    if funding_rate > 0.001:
        return "bearish"
    elif funding_rate < -0.001:
        return "bullish"
    else:
        return "neutral"


def _classify_ls_sentiment(ratio: float) -> str:
    """Classify long/short ratio sentiment.

    Args:
        ratio: Long/short account ratio.

    Returns:
        'crowded_long' if ratio > 1.2,
        'crowded_short' if ratio < 0.8,
        'balanced' otherwise.
    """
    if ratio > 1.2:
        return "crowded_long"
    elif ratio < 0.8:
        return "crowded_short"
    else:
        return "balanced"


def _classify_taker_sentiment(buy_ratio: float) -> str:
    """Classify taker buy/sell volume sentiment.

    Args:
        buy_ratio: Taker buy volume ratio.

    Returns:
        'aggressive_buy' if buy_ratio > 0.55,
        'aggressive_sell' if buy_ratio < 0.45,
        'balanced' otherwise.
    """
    if buy_ratio > 0.55:
        return "aggressive_buy"
    elif buy_ratio < 0.45:
        return "aggressive_sell"
    else:
        return "balanced"


# ---------------------------------------------------------------------------
# API fetchers
# ---------------------------------------------------------------------------

def _fetch_funding_rate(symbol: str) -> tuple[float, str]:
    """Fetch the current funding rate.

    Args:
        symbol: Trading pair symbol (e.g., 'BTCUSDT').

    Returns:
        Tuple of (funding_rate, sentiment).

    Raises:
        requests.RequestException: On network or API error.
        KeyError, ValueError: On response parse error.
    """
    url = f"{BASE_URL}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    data: list = response.json()

    if not data:
        raise ValueError("Empty funding rate response")

    funding_rate: float = float(data[0]["fundingRate"])
    sentiment: str = _classify_funding_sentiment(funding_rate)

    logger.debug(
        "Fetched funding rate: %.6f (%s)",
        funding_rate,
        sentiment,
    )
    return funding_rate, sentiment


def _fetch_open_interest(symbol: str) -> tuple[float, float]:
    """Fetch the current open interest.

    Args:
        symbol: Trading pair symbol (e.g., 'BTCUSDT').

    Returns:
        Tuple of (open_interest_usd, oi_change_pct).
        oi_change_pct is always 0.0 for now (requires historical tracking).

    Raises:
        requests.RequestException: On network or API error.
        KeyError, ValueError: On response parse error.
    """
    url = f"{BASE_URL}/fapi/v1/openInterest"
    params = {"symbol": symbol}
    response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    data: dict = response.json()

    # OI is returned as a string in the base asset (BTC), not USD
    # We'll return it as-is for now; converting to USD would require price data
    open_interest: float = float(data["openInterest"])
    oi_change_pct: float = 0.0  # Placeholder for future implementation

    logger.debug("Fetched open interest: %.2f", open_interest)
    return open_interest, oi_change_pct


def _fetch_long_short_ratio(symbol: str) -> tuple[float, str]:
    """Fetch the long/short account ratio.

    Args:
        symbol: Trading pair symbol (e.g., 'BTCUSDT').

    Returns:
        Tuple of (long_short_ratio, sentiment).

    Raises:
        requests.RequestException: On network or API error.
        KeyError, ValueError: On response parse error.
    """
    url = f"{BASE_URL}/futures/data/globalLongShortAccountRatio"
    params = {"symbol": symbol, "period": "1h", "limit": 1}
    response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    data: list = response.json()

    if not data:
        raise ValueError("Empty long/short ratio response")

    ratio: float = float(data[0]["longShortRatio"])
    sentiment: str = _classify_ls_sentiment(ratio)

    logger.debug(
        "Fetched long/short ratio: %.3f (%s)",
        ratio,
        sentiment,
    )
    return ratio, sentiment


def _fetch_taker_buy_sell_ratio(symbol: str) -> tuple[float, str]:
    """Fetch the taker buy/sell volume ratio.

    Args:
        symbol: Trading pair symbol (e.g., 'BTCUSDT').

    Returns:
        Tuple of (taker_buy_ratio, sentiment).

    Raises:
        requests.RequestException: On network or API error.
        KeyError, ValueError: On response parse error.
    """
    url = f"{BASE_URL}/futures/data/takerlongshortRatio"
    params = {"symbol": symbol, "period": "1h", "limit": 1}
    response = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()
    data: list = response.json()

    if not data:
        raise ValueError("Empty taker buy/sell ratio response")

    # buySellRatio is buy_volume / sell_volume
    # Convert to buy_ratio = buy_volume / (buy_volume + sell_volume)
    buy_sell_ratio: float = float(data[0]["buySellRatio"])
    buy_ratio: float = buy_sell_ratio / (1.0 + buy_sell_ratio)
    sentiment: str = _classify_taker_sentiment(buy_ratio)

    logger.debug(
        "Fetched taker buy ratio: %.3f (%s)",
        buy_ratio,
        sentiment,
    )
    return buy_ratio, sentiment


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_onchain_metrics(symbol: str = "BTCUSDT") -> dict:
    """Fetch on-chain / derivatives metrics from Binance Futures API.

    All endpoints are free and public. Results are cached for 15 minutes.
    On fetch failure, returns a safe default dict so callers don't crash.

    Args:
        symbol: Trading pair symbol (default: 'BTCUSDT').

    Returns:
        A dict with the following keys:

        - **funding_rate** (*float*): Current funding rate (e.g., 0.0001).
        - **funding_sentiment** (*str*): 'bullish' | 'bearish' | 'neutral'.
        - **open_interest** (*float*): Open interest in base asset (BTC).
        - **oi_change_pct** (*float*): % change vs 1h ago (placeholder, always 0.0).
        - **long_short_ratio** (*float*): Long/short account ratio.
        - **ls_sentiment** (*str*): 'crowded_long' | 'crowded_short' | 'balanced'.
        - **taker_buy_ratio** (*float*): Taker buy volume ratio (0–1).
        - **taker_sentiment** (*str*): 'aggressive_buy' | 'aggressive_sell' | 'balanced'.
        - **fetched_at** (*datetime*): UTC datetime when metrics were fetched,
          or ``None`` when the default fallback is used.

    Example::

        >>> result = fetch_onchain_metrics()
        >>> result["funding_rate"]
        0.0001
        >>> result["funding_sentiment"]
        'neutral'
        >>> result["ls_sentiment"]
        'crowded_long'
    """
    # --- Serve from cache if fresh enough -----------------------------------
    cache = _load_cache()
    if cache is not None and _cache_age_seconds(cache) < _CACHE_TTL_SECONDS:
        logger.debug(
            "Returning onchain metrics from cache (age %.0fs)",
            _cache_age_seconds(cache),
        )
        return _deserialise_cache(cache)

    # --- Fetch from API ------------------------------------------------------
    try:
        funding_rate, funding_sentiment = _fetch_funding_rate(symbol)
        open_interest, oi_change_pct = _fetch_open_interest(symbol)
        long_short_ratio, ls_sentiment = _fetch_long_short_ratio(symbol)
        taker_buy_ratio, taker_sentiment = _fetch_taker_buy_sell_ratio(symbol)

        result = {
            "funding_rate": funding_rate,
            "funding_sentiment": funding_sentiment,
            "open_interest": open_interest,
            "oi_change_pct": oi_change_pct,
            "long_short_ratio": long_short_ratio,
            "ls_sentiment": ls_sentiment,
            "taker_buy_ratio": taker_buy_ratio,
            "taker_sentiment": taker_sentiment,
            "fetched_at": datetime.now(timezone.utc),
        }

        _save_cache(result)
        logger.info(
            "Onchain metrics fetched: funding=%.6f (%s), LS=%.3f (%s), taker=%.3f (%s)",
            funding_rate,
            funding_sentiment,
            long_short_ratio,
            ls_sentiment,
            taker_buy_ratio,
            taker_sentiment,
        )
        return result

    except requests.exceptions.Timeout:
        logger.warning(
            "Onchain metrics API request timed out after %ds", _REQUEST_TIMEOUT
        )
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Onchain metrics API connection error: %s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("Onchain metrics API HTTP error: %s", exc)
    except requests.exceptions.RequestException as exc:
        logger.warning("Onchain metrics API request failed: %s", exc)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        logger.warning("Onchain metrics API response parse error: %s", exc)

    # --- Fallback: default ---------------------------------------------------
    logger.warning(
        "Onchain metrics fetch failed; returning neutral defaults"
    )
    return dict(_DEFAULT_RESULT)
