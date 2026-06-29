"""SignalForge — LLM Prompt Builder
Builds structured system + user prompts for GPT-4o signal reasoning.
"""
from __future__ import annotations

import json
from typing import Optional

from signals.pipeline import SMCAnalysisResult
from signals.confluence import ConfluenceScore
from signals.mtf_bias import MTFBias
from config.settings import LLM_PROMPT_VERSION


SYSTEM_PROMPT = """\
You are an expert cryptocurrency trader specializing in Smart Money Concepts (SMC/ICT), \
multi-timeframe analysis, and institutional order flow. You analyze market data objectively \
and provide trading signals based on confluence of multiple factors.

You MUST respond with valid JSON in this exact format:
{
  "signal": "BUY" | "SELL" | "PASS",
  "confidence": 0-100,
  "entry": <float>,
  "stop_loss": <float>,
  "tp1": <float>,
  "tp2": <float>,
  "tp3": <float>,
  "reasoning": "<string explaining the trade thesis>",
  "key_risk": "<string describing the main risk>",
  "timeframe": "<recommended hold timeframe e.g. '4h', '1d'>",
  "rr_ratio": <float>
}

Rules:
- If conditions are NOT favorable, return signal=PASS with reasoning.
- Never fabricate data. Base your analysis ONLY on the provided context.
- stop_loss must be a real structural level, not arbitrary pips away.
- tp1/tp2/tp3 should align with the next S/R levels or FVG boundaries.
- rr_ratio = (tp1 - entry) / (entry - stop_loss) for BUY (or inverse for SELL).
- confidence is your conviction 0-100 — be honest, not optimistic.
"""


def format_ohlcv_table(df, n: int = 10) -> str:
    """Return last N candles as a compact markdown table."""
    if df is None or len(df) == 0:
        return "_No candle data available_"
    recent = df.tail(n).copy()
    lines = ["| Time | Open | High | Low | Close | Volume |"]
    lines.append("|------|------|------|-----|-------|--------|")
    for ts, row in recent.iterrows():
        t = str(ts)[:16] if hasattr(ts, '__str__') else str(ts)
        lines.append(
            f"| {t} | {row.get('open', 0):,.0f} | {row.get('high', 0):,.0f} "
            f"| {row.get('low', 0):,.0f} | {row.get('close', 0):,.0f} "
            f"| {row.get('volume', 0):,.1f} |"
        )
    return "\n".join(lines)


def _fmt_tf_smc(tf: str, r: SMCAnalysisResult) -> str:
    """Format a single timeframe's SMC snapshot."""
    lines = [f"\n### {tf.upper()} Timeframe"]
    if r.structure:
        s = r.structure
        bias = getattr(s.bias, 'value', str(s.bias)) if s.bias else 'UNKNOWN'
        lines.append(f"- Bias: {bias}")
        if s.last_hh:
            hh_price = getattr(s.last_hh, 'price', s.last_hh)
            lines.append(f"- Last HH: {float(hh_price):,.2f}")
        if s.last_ll:
            ll_price = getattr(s.last_ll, 'price', s.last_ll)
            lines.append(f"- Last LL: {float(ll_price):,.2f}")
        if s.structure_breaks:
            recent_breaks = s.structure_breaks[-3:]
            lines.append(f"- Recent breaks: {', '.join(str(b) for b in recent_breaks)}")
    if r.nearest_resistance:
        res_price = getattr(r.nearest_resistance, 'price', None)
        res_strength = getattr(r.nearest_resistance, 'strength', 0)
        if res_price is not None:
            lines.append(f"- Nearest Resistance: {res_price:,.2f} (strength={res_strength:.1f})")
    if r.nearest_support:
        sup_price = getattr(r.nearest_support, 'price', None)
        sup_strength = getattr(r.nearest_support, 'strength', 0)
        if sup_price is not None:
            lines.append(f"- Nearest Support: {sup_price:,.2f} (strength={sup_strength:.1f})")
    active_fvgs = [f for f in r.fvgs if not getattr(f, 'filled', False)][:3]
    if active_fvgs:
        fvg_strs = [f"{getattr(f, 'direction', '?')} {getattr(f, 'low', 0):,.0f}-{getattr(f, 'high', 0):,.0f}" for f in active_fvgs]
        lines.append(f"- Active FVGs: {', '.join(fvg_strs)}")
    active_obs = [o for o in r.order_blocks if getattr(o, 'active', True)][:3]
    if active_obs:
        ob_strs = [f"{getattr(o, 'direction', '?')} OB @ {getattr(o, 'low', 0):,.0f}-{getattr(o, 'high', 0):,.0f}" for o in active_obs]
        lines.append(f"- Order Blocks: {', '.join(ob_strs)}")
    if r.recent_grab:
        lines.append(f"- Liquidity Grab: YES (recent sweep @ ${getattr(r.recent_grab, 'price', '?'):,.2f})")
    if r.premium_discount:
        pd = r.premium_discount
        lines.append(f"- P/D Zone: {getattr(pd, 'zone', '?').upper()} (EQ={getattr(pd, 'equilibrium', 0):,.2f})")
    return "\n".join(lines)


def build_prompt(
    results: dict[str, SMCAnalysisResult],
    confluence: ConfluenceScore,
    mtf_bias: MTFBias,
    symbol: str = "BTC/USDT",
    current_price: float = 0.0,
    open_positions: Optional[list[dict]] = None,
    primary_tf: str = "1h",
    candle_df=None,
    external_data: Optional[dict] = None,
) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) for the LLM signal call.

    Args:
        results:        Per-timeframe SMCAnalysisResult dict.
        confluence:     ConfluenceScore from the scoring engine.
        mtf_bias:       MTFBias alignment result.
        symbol:         Trading pair e.g. 'BTC/USDT'.
        current_price:  Latest price.
        open_positions: Currently open trades (list of dicts).
        primary_tf:     Which timeframe is the trigger TF ('1h' default).
        candle_df:      DataFrame of primary TF candles for OHLCV table.
        external_data:  Optional dict with keys: fear_greed, onchain, correlations, news.
    """
    open_positions = open_positions or []
    external_data = external_data or {}
    sections: list[str] = []

    # 1. Header
    sections.append(f"=== SIGNALFORGE ANALYSIS {LLM_PROMPT_VERSION} ===")

    # 2. Asset + price
    sections.append(f"\n## Asset\nSymbol: {symbol}\nCurrent Price: ${current_price:,.2f}")

    # 3. MTF bias
    sections.append(f"\n## Multi-Timeframe Bias\n{mtf_bias.summary}")
    for tf_label, bias_val in [
        ("1D", mtf_bias.daily_bias),
        ("4H", mtf_bias.h4_bias),
        ("1H", mtf_bias.h1_bias),
    ]:
        val = getattr(bias_val, 'value', str(bias_val)) if bias_val else 'UNKNOWN'
        sections.append(f"- {tf_label}: {val}")
    sections.append(f"- Strength: {mtf_bias.strength:.0%}")

    # 4. OHLCV table for primary TF
    if candle_df is not None and len(candle_df) > 0:
        sections.append(f"\n## Last 10 Candles ({primary_tf.upper()})")
        sections.append(format_ohlcv_table(candle_df, n=10))

    # 5. SMC features per timeframe
    sections.append("\n## SMC Structure & Features")
    for tf in ["1d", "4h", "1h", "15m", "5m"]:
        if tf in results:
            sections.append(_fmt_tf_smc(tf, results[tf]))

    # 6. Candlestick + chart patterns (primary TF)
    primary = results.get(primary_tf)
    if primary:
        cp = getattr(primary, 'candlestick_patterns', [])
        if cp:
            recent_cp = cp[-5:]
            pat_strs = [f"{p.pattern} ({p.direction}, strength={p.strength:.1f})" for p in recent_cp]
            sections.append(f"\n## Candlestick Patterns ({primary_tf.upper()})\n" + "\n".join(f"- {s}" for s in pat_strs))
        chp = getattr(primary, 'chart_patterns', [])
        if chp:
            chp_strs = [f"{p.pattern} ({p.direction})" for p in chp[-3:]]
            sections.append(f"\n## Chart Patterns\n" + "\n".join(f"- {s}" for s in chp_strs))

    # 7. Confluence score breakdown
    sections.append(f"\n## Confluence Score\nDirection: {confluence.direction.upper()}")
    sections.append(f"Net Score: {confluence.net_score} (Bullish={confluence.bullish_score}, Bearish={confluence.bearish_score})")
    sections.append(f"Meets Threshold (≥8): {'YES' if confluence.meets_threshold else 'NO'}")
    if confluence.factors:
        sections.append("\nFactors:")
        for f in sorted(confluence.factors, key=lambda x: -x.weight):
            sections.append(f"  [{f.direction.upper():7s} T{f.tier} +{f.weight}] {f.name}: {f.description}")

    # 8. Divergences
    if primary:
        divs = getattr(primary, 'divergences', [])
        if divs:
            div_strs = [f"{d.type} on {getattr(d, 'indicator', '?')}" for d in divs[-3:]]
            sections.append(f"\n## Divergences\n" + "\n".join(f"- {s}" for s in div_strs))

    # 9. Squeeze
    if primary:
        sq = getattr(primary, 'squeeze', None)
        sq_fire = getattr(primary, 'squeeze_firing', False)
        if sq:
            in_sq = getattr(sq, 'in_squeeze', False)
            momentum = getattr(sq, 'momentum_direction', 'flat')
            sections.append(f"\n## Squeeze\nIn Squeeze: {'YES' if in_sq else 'NO'} | Firing: {'YES' if sq_fire else 'NO'} | Momentum: {momentum.upper()}")

    # 10. Volume signals
    if primary:
        vol_sigs = getattr(primary, 'volume_signals', [])
        if vol_sigs:
            vol_strs = [f"{getattr(v, 'type', getattr(v, 'signal_type', '?'))} ({v.direction})" for v in vol_sigs[-4:]]
            sections.append(f"\n## Volume Signals\n" + "\n".join(f"- {s}" for s in vol_strs))

    # 11. Open positions
    if open_positions:
        sections.append("\n## Open Positions")
        for pos in open_positions:
            sections.append(f"- {pos.get('symbol')} {pos.get('side')} @ {pos.get('entry')} | SL={pos.get('sl')} | Size={pos.get('size')}")
    else:
        sections.append("\n## Open Positions\n_None_")

    # 12. External data (Sprint 6)
    if external_data:
        # Fear & Greed
        fg = external_data.get('fear_greed')
        if fg:
            val = fg.get('value', 50)
            cls = fg.get('classification', 'Neutral')
            is_ext = fg.get('is_extreme', False)
            ext_marker = " ⚠️ EXTREME" if is_ext else ""
            sections.append(f"\n## Fear & Greed Index\n{val}/100 ({cls}){ext_marker}")
        
        # On-chain metrics
        onchain = external_data.get('onchain')
        if onchain:
            sections.append("\n## On-Chain / Derivatives")
            sections.append(f"- Funding Rate: {onchain.get('funding_rate', 0):.4f} ({onchain.get('funding_sentiment', 'neutral')})")
            sections.append(f"- Open Interest: ${onchain.get('open_interest', 0):,.0f}")
            sections.append(f"- Long/Short Ratio: {onchain.get('long_short_ratio', 1.0):.2f} ({onchain.get('ls_sentiment', 'balanced')})")
            sections.append(f"- Taker Buy Ratio: {onchain.get('taker_buy_ratio', 0.5):.2f} ({onchain.get('taker_sentiment', 'balanced')})")
        
        # Correlations
        corr = external_data.get('correlations')
        if corr:
            sections.append("\n## Macro Correlations")
            sections.append(f"- BTC Dominance: {corr.get('btc_dominance', 0):.1f}% ({corr.get('btc_dom_trend', 'stable')})")
            sections.append(f"- ETH/BTC: {corr.get('eth_btc_ratio', 0):.4f} ({corr.get('eth_btc_trend', 'equal')})")
            if corr.get('dxy'):
                sections.append(f"- DXY: {corr['dxy']:.2f}")
            if corr.get('spx'):
                sections.append(f"- S&P 500: {corr['spx']:,.0f}")
        
        # Recent news
        news = external_data.get('news', [])
        if news:
            sections.append("\n## Recent High-Impact News (last 2 hours)")
            for article in news[:5]:
                sections.append(f"- [{article.get('source', '?')}] {article.get('title', '?')}")

    # 13. Final instruction
    sections.append("\n---\nBased on the above analysis, provide your trading signal as JSON.")

    user_prompt = "\n".join(sections)
    return SYSTEM_PROMPT, user_prompt
