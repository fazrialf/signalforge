"""SignalForge — LLM Prompt Builder
Builds structured system + user prompts for GPT-4o signal reasoning.
"""
from __future__ import annotations

import json
from typing import Optional

from signals.pipeline import SMCAnalysisResult
from signals.confluence import ConfluenceScore
from signals.mtf_bias import MTFBias
from config.settings import LLM_PROMPT_VERSION, MIN_CONFLUENCE_SCORE


SYSTEM_PROMPT = """\
You are an expert cryptocurrency scalp trader specializing in Smart Money Concepts (SMC/ICT), \
multi-timeframe analysis, and institutional order flow on the 5-minute timeframe. You analyze \
market data objectively and provide short-term trading signals based on confluence of multiple factors.

SCALPING CONTEXT:
- You are operating on a 5m PRIMARY timeframe with 15m BIAS and 1h MACRO context.
- Entries should target 15–60 minute holds, NOT multi-hour swings.
- The PRIMARY TF structure takes priority for entries — higher TFs provide directional bias only.
- If the 5m structure is clean (BOS + FVG retest + reversal candle) but 1H is neutral, that is VALID for a scalp.
- Tight stops are expected: SL should be at the nearest 5m structure level (FVG edge, OB boundary, swing low/high).
- TP1 targets the next 5m liquidity pool or swing point; TP2/TP3 extend to 15m levels.
- R:R minimum is 1.8 — entries with R:R below this are negative EV after fees (0.2% round-trip).
- Be decisive: if confluence is strong on 5m, don't let neutral 1H bias reduce confidence excessively.
- Confidence should reflect the 5m setup quality, not macro uncertainty.

CRITICAL: You MUST respond with ONLY valid JSON — no markdown, no code fences, no explanation text.
Your entire response must be a single JSON object with ALL of the following fields.
Every field is REQUIRED regardless of signal type.

Required JSON schema (all fields mandatory, no exceptions):
{
  "signal": "BUY" or "SELL" or "PASS",
  "confidence": <integer 0-100>,
  "entry": <float — use current price for PASS>,
  "stop_loss": <float — use nearest structure level, use current price for PASS>,
  "tp1": <float — first take profit target, use current price for PASS>,
  "tp2": <float — second take profit target, use current price for PASS>,
  "tp3": <float — third take profit target, use current price for PASS>,
  "reasoning": "<string — explain your analysis and decision, minimum 20 words>",
  "key_risk": "<string — describe the main risk or reason for PASS, minimum 10 words>",
  "timeframe": "<string — recommended hold timeframe e.g. '5m', '15m', '1h'>",
  "rr_ratio": <float — abs(tp1-entry)/abs(entry-stop_loss), 0.0 for PASS>
}

Rules for BUY/SELL signals:
- stop_loss MUST be a real structural level (swing low/high, OB boundary, FVG edge) — never arbitrary.
- tp1/tp2/tp3 MUST align with the next S/R levels, FVG boundaries, or swing highs/lows.
- rr_ratio MUST be calculated: abs(tp1 - entry) / abs(entry - stop_loss).
- confidence reflects your conviction 0-100 — be decisive on clean 5m setups, not overly conservative.

Rules for PASS signal:
- Set entry, stop_loss, tp1, tp2, tp3 all to the current price (do not leave as 0).
- Set rr_ratio to 0.0.
- Set confidence to your certainty that PASS is correct (e.g. 85 if very sure conditions are unfavorable).
- reasoning MUST explain WHY you are passing (conflicting structure, no clean entry zone, chop, etc).
- key_risk MUST describe what would need to change for a valid signal to exist.

Example PASS response:
{"signal":"PASS","confidence":82,"entry":64500.0,"stop_loss":64500.0,"tp1":64500.0,"tp2":64500.0,"tp3":64500.0,"reasoning":"5m structure is ranging with no clean BOS — equal highs/lows forming. No directional conviction until a sweep and reclaim occurs.","key_risk":"Need a 5m BOS with displacement followed by a retest of the FVG left behind before entry is valid.","timeframe":"5m","rr_ratio":0.0}

Never omit any field. Never return partial JSON. Never add text outside the JSON object.\
"""

def _price_fmt(price: float) -> str:
    """Return a format spec appropriate for *price*.

    BTC-range (>= 1000): 0 decimal places  → e.g. 65,432
    Mid-range  (>= 1):   2 decimal places  → e.g. 3,421.56
    Altcoin    (< 1):    4–6 sig-figs       → e.g. 0.001234
    """
    if price >= 1_000:
        return f"{price:,.0f}"
    if price >= 1:
        return f"{price:,.2f}"
    # Find first significant digit position
    import math
    if price <= 0:
        return f"{price}"
    sig_places = max(4, -int(math.floor(math.log10(abs(price)))) + 3)
    return f"{price:.{sig_places}f}"


def format_ohlcv_table(df, n: int = 10) -> str:
    """Return last N candles as a compact markdown table.

    Uses adaptive price formatting so sub-$1 alts (XRP/TRX) are not rounded
    to 0 or 1 — that previously caused the LLM to PASS on "zeroed candles".
    """
    if df is None or len(df) == 0:
        return "_No candle data available_"
    recent = df.tail(n).copy()
    lines = ["| Time | Open | High | Low | Close | Volume |"]
    lines.append("|------|------|------|-----|-------|--------|")
    for ts, row in recent.iterrows():
        t = str(ts)[:16] if hasattr(ts, '__str__') else str(ts)
        o = float(row.get('open', 0) or 0)
        h = float(row.get('high', 0) or 0)
        l = float(row.get('low', 0) or 0)
        c = float(row.get('close', 0) or 0)
        v = float(row.get('volume', 0) or 0)
        lines.append(
            f"| {t} | {_price_fmt(o)} | {_price_fmt(h)} "
            f"| {_price_fmt(l)} | {_price_fmt(c)} "
            f"| {v:,.1f} |"
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
    sections.append(f"Meets Threshold (≥{MIN_CONFLUENCE_SCORE}): {'YES' if confluence.meets_threshold else 'NO'}")
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

        # Sprint 13: Active strategy signals (entry refinement context for LLM)
        # main.py injects a pre-formatted string under 'strategy_signals'.
        # Without this block the multi-strategy layer was invisible to the model.
        strat_ctx = external_data.get('strategy_signals')
        if strat_ctx:
            sections.append(f"\n## Active Strategy Signals\n{strat_ctx}")
            sections.append(
                "Use strategy signals as entry refinement context. "
                "Prefer the strategy's entry/SL/TP when structure agrees; "
                "PASS if strategy direction conflicts with 5m structure or confluence is weak."
            )

    # 13. Final instruction — re-state schema requirements immediately before
    # the model generates its response. This is the highest-weight instruction
    # for models that front-load the system prompt and lose it mid-context.
    sections.append(
        "\n---\n"
        "Respond with ONLY a single JSON object. ALL fields are required:\n"
        "signal, confidence, entry, stop_loss, tp1, tp2, tp3, "
        "reasoning, key_risk, timeframe, rr_ratio.\n"
        "For PASS: set entry/stop_loss/tp1/tp2/tp3 to current price, rr_ratio=0.0.\n"
        "Do not omit any field. Do not add text outside the JSON."
    )

    user_prompt = "\n".join(sections)
    return SYSTEM_PROMPT, user_prompt
