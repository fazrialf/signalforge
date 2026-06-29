"""
external/correlations.py

Macro correlation fetcher: BTC dominance, ETH/BTC, DXY, SPX, Gold.

Data sources:
  - Binance REST API  → ETH/BTC ratio (no key required)
  - CoinGecko API     → BTC dominance % (no key required)
  - yfinance          → DXY, SPX, Gold (optional; skipped if not installed)

Cache: 15-minute TTL at /tmp/signalforge_corr_cache.json
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

CACHE_PATH = "/tmp/signalforge_corr_cache.json"
CACHE_TTL_SECONDS = 15 * 60  # 15 minutes

REQUEST_TIMEOUT = 10  # seconds per HTTP call

# ETH/BTC ratio thresholds for trend classification
ETH_BTC_OUTPERFORMING_THRESHOLD = 0.055
ETH_BTC_UNDERPERFORMING_THRESHOLD = 0.040


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    """Load the on-disk cache; return empty dict on any error."""
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict) -> None:
    """Persist *data* to disk; silently ignore write errors."""
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:
        logger.warning("correlations: could not write cache: %s", exc)


def _cache_is_fresh(cache: dict) -> bool:
    """Return True if the cache was written within CACHE_TTL_SECONDS."""
    ts = cache.get("cached_at")
    if ts is None:
        return False
    return (time.time() - ts) < CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Individual data-source fetchers
# ---------------------------------------------------------------------------

def _fetch_eth_btc_ratio() -> Optional[float]:
    """
    Fetch the current ETH/BTC spot price from Binance.

    Returns:
        float price or None on failure.
    """
    try:
        resp = requests.get(
            BINANCE_TICKER_URL,
            params={"symbol": "ETHBTC"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        price = float(resp.json()["price"])
        logger.debug("correlations: ETH/BTC = %.6f", price)
        return price
    except Exception as exc:  # noqa: BLE001
        logger.warning("correlations: failed to fetch ETH/BTC — %s", exc)
        return None


def _fetch_btc_dominance() -> Optional[float]:
    """
    Fetch global BTC market-cap dominance % from CoinGecko.

    Returns:
        float (e.g. 54.2 for 54.2 %) or None on failure.
    """
    try:
        resp = requests.get(COINGECKO_GLOBAL_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        dominance = float(resp.json()["data"]["market_cap_percentage"]["btc"])
        logger.debug("correlations: BTC dominance = %.2f%%", dominance)
        return dominance
    except Exception as exc:  # noqa: BLE001
        logger.warning("correlations: failed to fetch BTC dominance — %s", exc)
        return None


def _fetch_tradfi() -> dict[str, Optional[float]]:
    """
    Fetch DXY, SPX, and Gold prices via yfinance.

    Returns a dict with keys 'dxy', 'spx', 'gold'.  Any value that cannot
    be retrieved is set to None.  If yfinance is not installed the function
    returns all-None immediately.
    """
    result: dict[str, Optional[float]] = {"dxy": None, "spx": None, "gold": None}

    try:
        import yfinance as yf  # optional dependency
    except ImportError:
        logger.info(
            "correlations: yfinance not installed — skipping DXY/SPX/Gold"
        )
        return result

    symbols = {
        "dxy": "DX-Y.NYB",
        "spx": "^GSPC",
        "gold": "GC=F",
    }

    for key, ticker_sym in symbols.items():
        try:
            ticker = yf.Ticker(ticker_sym)
            price = ticker.fast_info.last_price
            if price is not None:
                result[key] = float(price)
                logger.debug("correlations: %s (%s) = %.4f", key.upper(), ticker_sym, price)
            else:
                logger.warning(
                    "correlations: %s returned None from fast_info.last_price", ticker_sym
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "correlations: failed to fetch %s (%s) — %s", key.upper(), ticker_sym, exc
            )

    return result


# ---------------------------------------------------------------------------
# Trend classifiers
# ---------------------------------------------------------------------------

def _classify_btc_dom_trend(
    current: Optional[float],
    previous: Optional[float],
) -> str:
    """
    Compare current BTC dominance to a previously cached value.

    Returns:
        'rising'  — dominance increased by more than 0.5 pp
        'falling' — dominance decreased by more than 0.5 pp
        'stable'  — change within ±0.5 pp, or no baseline available
    """
    if current is None or previous is None:
        return "stable"
    delta = current - previous
    if delta > 0.5:
        return "rising"
    if delta < -0.5:
        return "falling"
    return "stable"


def _classify_eth_btc_trend(ratio: Optional[float]) -> str:
    """
    Classify the ETH/BTC ratio into a qualitative trend label.

    Returns:
        'eth_outperforming' — ratio above ETH_BTC_OUTPERFORMING_THRESHOLD
        'btc_outperforming' — ratio below ETH_BTC_UNDERPERFORMING_THRESHOLD
        'equal'             — ratio in between, or None
    """
    if ratio is None:
        return "equal"
    if ratio > ETH_BTC_OUTPERFORMING_THRESHOLD:
        return "eth_outperforming"
    if ratio < ETH_BTC_UNDERPERFORMING_THRESHOLD:
        return "btc_outperforming"
    return "equal"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_correlations() -> dict:
    """
    Fetch macro correlation data for use in LLM signal context.

    Data is cached for 15 minutes in /tmp/signalforge_corr_cache.json.  Each
    data source is fetched independently; a failure in one never prevents the
    others from being returned.

    Returns:
        dict with keys:
            btc_dominance  (float | None)  BTC market-cap dominance %
            eth_btc_ratio  (float | None)  ETH/BTC spot price ratio
            dxy            (float | None)  DXY USD index (requires yfinance)
            spx            (float | None)  S&P 500 price  (requires yfinance)
            gold           (float | None)  Gold spot USD  (requires yfinance)
            btc_dom_trend  (str)           'rising' | 'falling' | 'stable'
            eth_btc_trend  (str)           'eth_outperforming' |
                                           'btc_outperforming' | 'equal'
            fetched_at     (datetime)      UTC timestamp of this fetch
    """
    cache = _load_cache()

    if _cache_is_fresh(cache):
        logger.debug("correlations: returning cached data")
        # Re-hydrate fetched_at from the stored ISO string
        raw = cache.get("data", {})
        if raw:
            try:
                raw["fetched_at"] = datetime.fromisoformat(raw["fetched_at"])
            except (KeyError, ValueError):
                pass
            return raw

    # ------------------------------------------------------------------ fetch
    # Retrieve from each source independently
    btc_dominance = _fetch_btc_dominance()
    eth_btc_ratio = _fetch_eth_btc_ratio()
    tradfi = _fetch_tradfi()

    # --------------------------------------------------------- trend analysis
    # BTC dominance trend: compare against the value cached ~24 h ago
    prev_btc_dom: Optional[float] = cache.get("btc_dominance_24h")
    btc_dom_trend = _classify_btc_dom_trend(btc_dominance, prev_btc_dom)

    eth_btc_trend = _classify_eth_btc_trend(eth_btc_ratio)

    now = datetime.now(timezone.utc)

    result: dict = {
        "btc_dominance": btc_dominance,
        "eth_btc_ratio": eth_btc_ratio,
        "dxy": tradfi["dxy"],
        "spx": tradfi["spx"],
        "gold": tradfi["gold"],
        "btc_dom_trend": btc_dom_trend,
        "eth_btc_trend": eth_btc_trend,
        "fetched_at": now,
    }

    # -------------------------------------------------------------- write cache
    # Rotate the 24-h baseline: if the existing cached_at is ≥ 23 h old,
    # promote the stored dominance value to the 24-h slot before overwriting.
    stored_ts = cache.get("cached_at", 0)
    age_hours = (time.time() - stored_ts) / 3600
    new_24h_baseline: Optional[float] = cache.get("btc_dominance_24h")
    if age_hours >= 23:
        new_24h_baseline = cache.get("data", {}).get("btc_dominance", btc_dominance)

    new_cache: dict = {
        "cached_at": time.time(),
        "btc_dominance_24h": new_24h_baseline,
        # Serialise datetime → ISO string for JSON
        "data": {**result, "fetched_at": now.isoformat()},
    }
    _save_cache(new_cache)

    return result


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s %(name)s: %(message)s",
    )
    data = fetch_correlations()
    print("\n=== Macro Correlations ===")
    for k, v in data.items():
        print(f"  {k:<18} {v}")
