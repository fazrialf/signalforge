"""strategies/ — Scalping strategy modules for SignalForge.

Each strategy is an independent signal generator that feeds into the
main pipeline as an alternative entry trigger alongside the BOS retest model.

Strategies:
    1. vwap_reversion   — Fade at ±2σ VWAP bands (ranging markets)
    2. sweep_reclaim    — Liquidity sweep + price reclaim (highest win rate)
    3. delta_divergence — Order flow divergence at key zones
    4. session_breakout — Asia range sweep at London/NY open
    5. micro_fvg        — 1m FVG refinement within 5m zones
"""
