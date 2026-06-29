"""Alternative.me Crypto Fear & Greed Index fetcher.

Fetches the current Fear & Greed Index value from the Alternative.me API,
with 1-hour file-based caching to avoid unnecessary polling (the index
only updates once per day).

Endpoint: https://api.alternative.me/fng/?limit=1
No API key required.
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

API_URL: str = "https://api.alternative.me/fng/?limit=1"
CACHE_PATH: Path = Path("/tmp/signalforge_fg_cache.json")

# Cache TTL used for serving stale-but-acceptable data on fetch error
_CACHE_TTL_SECONDS: int = 3600          # 1 hour — normal refresh interval
_CACHE_MAX_STALE_SECONDS: int = 86_400  # 24 hours — fall back no further

_REQUEST_TIMEOUT: int = 5  # seconds

# Default/fallback value returned when no cache is available and fetch fails
_DEFAULT_RESULT: dict = {
    "value": 50,
    "classification": "Neutral",
    "timestamp": None,
    "is_extreme": False,
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
        logger.debug("Could not load Fear & Greed cache: %s", exc)
        return None


def _save_cache(payload: dict) -> None:
    """Persist *payload* to the cache file.

    The payload is the full result dict as returned by :func:`fetch_fear_greed`
    augmented with a ``cached_at`` Unix timestamp.

    Args:
        payload: Result dict to persist.
    """
    try:
        data = dict(payload)  # shallow copy
        # Store timestamp as ISO string for human readability; keep the
        # datetime object out of the serialised form.
        if isinstance(data.get("timestamp"), datetime):
            data["timestamp"] = data["timestamp"].isoformat()
        data["cached_at"] = time.time()
        with CACHE_PATH.open("w", encoding="utf-8") as fh:
            json.dump(data, fh)
        logger.debug("Fear & Greed cache written to %s", CACHE_PATH)
    except OSError as exc:
        logger.warning("Could not write Fear & Greed cache: %s", exc)


def _cache_age_seconds(cache: dict) -> float:
    """Return how many seconds have elapsed since *cache* was written."""
    cached_at: float = cache.get("cached_at", 0.0)
    return time.time() - cached_at


def _deserialise_cache(cache: dict) -> dict:
    """Convert a raw cache dict back into the public result format.

    Specifically, ``timestamp`` is re-hydrated from an ISO string to a
    :class:`~datetime.datetime` object if present.

    Args:
        cache: Raw dict loaded from the JSON cache file.

    Returns:
        Result dict with ``timestamp`` as a :class:`~datetime.datetime` (or
        ``None``) and ``cached_at`` stripped out.
    """
    result = {k: v for k, v in cache.items() if k != "cached_at"}
    ts_raw = result.get("timestamp")
    if isinstance(ts_raw, str):
        try:
            result["timestamp"] = datetime.fromisoformat(ts_raw)
        except ValueError:
            result["timestamp"] = None
    return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_response(data: dict) -> dict:
    """Parse the raw API response dict into the public result format.

    Args:
        data: Decoded JSON response from the Alternative.me API.

    Returns:
        Parsed result dict.

    Raises:
        KeyError: If expected keys are absent.
        ValueError: If value fields cannot be converted to the expected types.
    """
    entry: dict = data["data"][0]

    value: int = int(entry["value"])
    classification: str = entry["value_classification"]
    unix_ts: int = int(entry["timestamp"])
    timestamp: datetime = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    is_extreme: bool = value < 10 or value > 90

    return {
        "value": value,
        "classification": classification,
        "timestamp": timestamp,
        "is_extreme": is_extreme,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_fear_greed() -> dict:
    """Fetch the current Crypto Fear & Greed Index.

    Results are cached for 1 hour in ``/tmp/signalforge_fg_cache.json`` because
    the index is only updated once per day.  On a fetch error the cached value
    is returned if it is less than 24 hours old; otherwise a neutral default
    of 50 is returned so downstream filters can degrade gracefully.

    Returns:
        A dict with the following keys:

        - **value** (*int*): Index value in the range 0–100.
        - **classification** (*str*): Human-readable label —
          ``'Extreme Fear'``, ``'Fear'``, ``'Neutral'``, ``'Greed'``, or
          ``'Extreme Greed'``.
        - **timestamp** (*datetime*): UTC datetime of the index reading, or
          ``None`` when the default fallback is used.
        - **is_extreme** (*bool*): ``True`` when ``value < 10`` or
          ``value > 90``; used as a filter gate by callers.

    Example::

        >>> result = fetch_fear_greed()
        >>> result["value"]
        45
        >>> result["classification"]
        'Fear'
        >>> result["is_extreme"]
        False
    """
    # --- Serve from cache if fresh enough -----------------------------------
    cache = _load_cache()
    if cache is not None and _cache_age_seconds(cache) < _CACHE_TTL_SECONDS:
        logger.debug(
            "Returning Fear & Greed from cache (age %.0fs)",
            _cache_age_seconds(cache),
        )
        return _deserialise_cache(cache)

    # --- Fetch from API ------------------------------------------------------
    try:
        response = requests.get(API_URL, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        raw: dict = response.json()
        result = _parse_response(raw)
        _save_cache(result)
        logger.debug(
            "Fear & Greed fetched: value=%d classification=%s",
            result["value"],
            result["classification"],
        )
        return result

    except requests.exceptions.Timeout:
        logger.warning(
            "Fear & Greed API request timed out after %ds", _REQUEST_TIMEOUT
        )
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Fear & Greed API connection error: %s", exc)
    except requests.exceptions.HTTPError as exc:
        logger.warning("Fear & Greed API HTTP error: %s", exc)
    except requests.exceptions.RequestException as exc:
        logger.warning("Fear & Greed API request failed: %s", exc)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        logger.warning("Fear & Greed API response parse error: %s", exc)

    # --- Fallback: stale cache or default -----------------------------------
    if cache is not None and _cache_age_seconds(cache) < _CACHE_MAX_STALE_SECONDS:
        logger.warning(
            "Using stale Fear & Greed cache (age %.0fs)",
            _cache_age_seconds(cache),
        )
        return _deserialise_cache(cache)

    logger.warning(
        "No usable Fear & Greed cache; returning neutral default (value=50)"
    )
    return dict(_DEFAULT_RESULT)
