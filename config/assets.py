"""
SignalForge — Multi-Asset Configuration
========================================
Defines every asset SignalForge monitors, its timeframes, and per-asset
trading parameters.  Import ASSETS for the full list, or use the helpers
(get_asset, get_enabled_assets, …) for filtered lookups.

Environment variables
---------------------
PAPER_TRADING   'true' (default) | 'false'
    When true the engine runs in paper mode; no real orders are placed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# Paper-trading flag — default ON so a fresh deployment never goes live
# accidentally.  Set PAPER_TRADING=false in .env to enable live signals.
PAPER_MODE: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"


@dataclass
class AssetConfig:
    """Per-asset runtime configuration."""

    # ------------------------------------------------------------------ #
    # Identity                                                             #
    # ------------------------------------------------------------------ #
    symbol: str
    """ccxt-style trading pair, e.g. 'BTC/USDT'."""

    binance_symbol: str
    """Binance REST / WebSocket symbol, e.g. 'BTCUSDT'."""

    timeframes: list[str]
    """Ordered list of timeframes to fetch and analyse, lowest → highest.
    e.g. ['1h', '4h', '1d']."""

    primary_tf: str
    """Entry / signal timeframe — must be in *timeframes*."""

    # ------------------------------------------------------------------ #
    # Feature flags                                                        #
    # ------------------------------------------------------------------ #
    enabled: bool = True
    """Set False to exclude the asset from all pipelines without removing
    it from the list."""

    use_futures: bool = False
    """Set True for Futures-only assets (e.g. HYPE/USDT:USDT on binanceusdm)."""

    # ------------------------------------------------------------------ #
    # Signal quality thresholds (override global settings.py defaults)    #
    # ------------------------------------------------------------------ #
    min_confluence_score: int = 8
    """Minimum net confluence score required before calling the LLM."""

    min_rr: float = 1.5
    """Minimum risk-to-reward ratio accepted by the filter gate."""

    # ------------------------------------------------------------------ #
    # Timing                                                               #
    # ------------------------------------------------------------------ #
    cooldown_minutes: int = 30
    """Minimum gap (minutes) between delivered signals for this asset."""

    # ------------------------------------------------------------------ #
    # Data                                                                 #
    # ------------------------------------------------------------------ #
    lookback_bars: int = 300
    """Number of OHLCV bars to load at startup per timeframe."""

    # ------------------------------------------------------------------ #
    # Meta                                                                 #
    # ------------------------------------------------------------------ #
    description: str = ""
    """Human-readable label shown in logs and Telegram snapshots."""

    # ------------------------------------------------------------------ #
    # Derived helpers                                                      #
    # ------------------------------------------------------------------ #
    def bias_tf(self) -> str:
        """Return the timeframe one step above primary_tf for bias checks.
        Falls back to primary_tf when it is already the highest."""
        try:
            idx = self.timeframes.index(self.primary_tf)
            return self.timeframes[idx + 1]
        except (ValueError, IndexError):
            return self.primary_tf

    def macro_tf(self) -> str:
        """Return the highest timeframe in the list (macro / weekly bias)."""
        return self.timeframes[-1] if self.timeframes else self.primary_tf

    def ws_stream(self) -> str:
        """Binance WebSocket stream name for 1-minute klines."""
        return f"{self.binance_symbol.lower()}@kline_1m"


# --------------------------------------------------------------------------- #
# Default asset roster                                                         #
# --------------------------------------------------------------------------- #

ASSETS: list[AssetConfig] = [
    # --- Large-cap crypto -------------------------------------------------- #
    # lookback_bars applies to primary+higher TFs (5m/15m/1h).
    # 1m is hard-capped at 120 bars in DataFetcher + SQL load path.
    AssetConfig(
        symbol="BTC/USDT",
        binance_symbol="BTCUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        description="Bitcoin — primary asset (scalping)",
    ),
    AssetConfig(
        symbol="ETH/USDT",
        binance_symbol="ETHUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        description="Ethereum (scalping)",
    ),
    AssetConfig(
        symbol="BNB/USDT",
        binance_symbol="BNBUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        description="BNB (scalping)",
    ),
    # --- Mid-cap / high-beta ----------------------------------------------- #
    AssetConfig(
        symbol="SOL/USDT",
        binance_symbol="SOLUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        min_confluence_score=7,   # slightly tighter — more volatile
        description="Solana (scalping)",
        enabled=True,
    ),
    AssetConfig(
        symbol="XRP/USDT",
        binance_symbol="XRPUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        description="XRP (scalping)",
        enabled=True,
    ),
    AssetConfig(
        symbol="TRX/USDT",
        binance_symbol="TRXUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        description="TRON (scalping)",
    ),
    AssetConfig(
        symbol="HYPE/USDT:USDT",
        binance_symbol="HYPEUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        min_confluence_score=5,
        min_rr=1.8,
        description="Hyperliquid (scalping)",
        enabled=True,
        use_futures=True,
    ),
    AssetConfig(
        symbol="SUI/USDT",
        binance_symbol="SUIUSDT",
        timeframes=["1m", "5m", "15m", "1h"],
        primary_tf="5m",
        lookback_bars=300,
        cooldown_minutes=10,
        min_confluence_score=5,
        min_rr=1.8,
        description="Sui (scalping)",
        enabled=True,
    ),
]


# --------------------------------------------------------------------------- #
# Lookup helpers                                                               #
# --------------------------------------------------------------------------- #

def get_asset(symbol: str) -> Optional[AssetConfig]:
    """Return the AssetConfig for *symbol*, or None if not found.

    Lookup is case-insensitive and tolerates both 'BTC/USDT' and 'BTCUSDT'.

    >>> get_asset('BTC/USDT').binance_symbol
    'BTCUSDT'
    """
    symbol_norm = symbol.upper().replace("-", "/")
    for asset in ASSETS:
        if (
            asset.symbol.upper() == symbol_norm
            or asset.binance_symbol.upper() == symbol_norm
        ):
            return asset
    return None


def get_enabled_assets() -> list[AssetConfig]:
    """Return only assets where ``enabled=True``."""
    return [a for a in ASSETS if a.enabled]


def get_all_symbols() -> list[str]:
    """ccxt-style symbols for all assets (enabled + disabled).

    >>> 'BTC/USDT' in get_all_symbols()
    True
    """
    return [a.symbol for a in ASSETS]


def get_all_binance_symbols() -> list[str]:
    """Binance ticker symbols for all assets (enabled + disabled).

    >>> 'BTCUSDT' in get_all_binance_symbols()
    True
    """
    return [a.binance_symbol for a in ASSETS]


def get_enabled_symbols() -> list[str]:
    """ccxt-style symbols for enabled assets only."""
    return [a.symbol for a in get_enabled_assets()]


def get_enabled_binance_symbols() -> list[str]:
    """Binance ticker symbols for enabled assets only."""
    return [a.binance_symbol for a in get_enabled_assets()]


def get_all_timeframes() -> list[str]:
    """Deduplicated, sorted union of all timeframes across enabled assets.

    Useful for bootstrapping a single shared DataFetcher.
    """
    _order = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
              "8h", "12h", "1d", "3d", "1w", "1M"]
    seen: set[str] = set()
    for asset in get_enabled_assets():
        seen.update(asset.timeframes)
    return [tf for tf in _order if tf in seen]
