"""SignalForge — LLM Engine
Calls GPT-4o (via 9router OpenAI-compatible endpoint) with the structured prompt,
parses the JSON response, and returns a validated SignalResult.
Falls back to gpt-4o-mini on failure.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

from config.settings import (
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_FALLBACK,
    LLM_TIMEOUT, LLM_MAX_RETRIES, LLM_PROMPT_VERSION, LLM_MAX_CONCURRENT,
    MIN_LLM_CONFIDENCE,
)

logger = logging.getLogger(__name__)

# C-4: singleton client — created once, never leaked
_openai_client: AsyncOpenAI | None = None

# L-1: per-provider concurrency semaphore — prevents 429 bursts when all
# symbols hit confluence in the same cycle. Provider limit is 3 concurrent;
# we cap at LLM_MAX_CONCURRENT=2 to leave headroom for retries.
_llm_semaphore: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENT)
    return _llm_semaphore

def _get_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY,
            base_url=OPENAI_BASE_URL,
            timeout=LLM_TIMEOUT,
        )
    return _openai_client

# Required keys in the LLM JSON response
_REQUIRED_KEYS = {"signal", "confidence", "entry", "stop_loss", "tp1", "tp2", "tp3", "reasoning", "key_risk", "rr_ratio"}
_VALID_SIGNALS = {"BUY", "SELL", "PASS"}


@dataclass
class SignalResult:
    """Parsed + validated output from the LLM."""
    signal: str                       # BUY | SELL | PASS
    confidence: float                 # 0-100
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    reasoning: str
    key_risk: str
    timeframe: str = "1h"
    rr_ratio: float = 0.0
    model_used: str = ""
    prompt_version: str = LLM_PROMPT_VERSION
    latency_ms: int = 0
    raw_response: str = ""
    error: Optional[str] = None       # set if fallback or parse issue

    @property
    def is_actionable(self) -> bool:
        """True if the signal passed validation and is not PASS."""
        return self.signal in ("BUY", "SELL") and self.error is None

    def to_dict(self) -> dict:
        return {
            "signal": self.signal,
            "confidence": self.confidence,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "reasoning": self.reasoning,
            "key_risk": self.key_risk,
            "timeframe": self.timeframe,
            "rr_ratio": self.rr_ratio,
            "model_used": self.model_used,
            "prompt_version": self.prompt_version,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }

    def to_telegram_message(self, symbol: str = "BTC/USDT") -> str:
        """Format as a Telegram signal message."""
        if self.signal == "PASS":
            return (
                f"🔵 <b>PASS</b> | {symbol}\n"
                f"Confidence: {self.confidence:.0f}%\n"
                f"Reason: {self.reasoning[:200]}"
            )
        emoji = "🟢" if self.signal == "BUY" else "🔴"
        rr = f"{self.rr_ratio:.1f}" if self.rr_ratio else "N/A"
        return (
            f"{emoji} <b>{self.signal}</b> | {symbol} | <b>{self.confidence:.0f}%</b> confidence\n"
            f"\n📍 Entry:  <code>${self.entry:,.2f}</code>"
            f"\n🚧 SL:     <code>${self.stop_loss:,.2f}</code>"
            f"\n🎯 TP1:    <code>${self.tp1:,.2f}</code>"
            f"\n🎯 TP2:    <code>${self.tp2:,.2f}</code>"
            f"\n🎯 TP3:    <code>${self.tp3:,.2f}</code>"
            f"\n⚖️ R:R:    {rr}"
            f"\n⏱ TF:     {self.timeframe}"
            f"\n\n🧠 <b>Reasoning:</b>\n{self.reasoning[:400]}"
            f"\n\n⚠️ <b>Key Risk:</b> {self.key_risk[:200]}"
            f"\n\n<i>Model: {self.model_used} | Prompt {self.prompt_version}</i>"
        )


def _parse_llm_response(raw: str, model: str, latency_ms: int) -> SignalResult:
    """Extract and validate JSON from the LLM's raw response text."""
    import re as _re

    text = raw.strip()

    # Strip markdown code fences: ```json ... ``` or ``` ... ```
    fence_match = _re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Find the outermost JSON object boundaries
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        logger.warning(f"[LLM] No JSON object found in response. Raw (first 300 chars): {raw[:300]}")
        return _pass_result("No JSON in response", model, latency_ms, raw)

    json_str = text[start:end + 1]

    # Attempt 1: strict parse (fastest path)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Attempt 2: non-strict — tolerates literal control chars in string values
        try:
            data = json.loads(json_str, strict=False)
        except json.JSONDecodeError as e:
            # Attempt 3: json_repair — handles dropped delimiters/quotes from
            # streaming proxy token-loss (e.g. "tp2130.91 or "key_risk "value)
            try:
                from json_repair import repair_json
                repaired = repair_json(json_str)
                data = json.loads(repaired)
                logger.info(f"[LLM] JSON repaired successfully (was: {e})")
            except Exception as e2:
                logger.warning(f"[LLM] JSON parse error: {e} | context: {repr(json_str[max(0,e.pos-40):e.pos+40])}")
                return _pass_result(f"JSON parse error: {e}", model, latency_ms, raw)

    # Auto-fill missing keys where possible before hard-failing
    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        logger.warning(f"[LLM] Missing keys in response: {missing}")
        # Compute rr_ratio from entry/stop_loss/tp1/tp2/tp3 if available.
        # Uses a blended weighted average across all three TP levels (30/40/30)
        # to reflect the full trade plan — TP1-only R:R systematically
        # undervalues multi-TP scalping setups and causes false filter blocks.
        if "rr_ratio" in missing:
            try:
                entry = float(data.get("entry", 0))
                sl = float(data.get("stop_loss", 0))
                tp1 = float(data.get("tp1", 0))
                tp2 = float(data.get("tp2", 0))
                tp3 = float(data.get("tp3", 0))
                if entry and sl and entry != sl:
                    sig = str(data.get("signal", "BUY")).upper()
                    risk = abs(entry - sl)
                    # Collect valid TP levels and their weights (30/40/30)
                    tp_levels = [(tp1, 0.3), (tp2, 0.4), (tp3, 0.3)]
                    valid = [(tp, w) for tp, w in tp_levels if tp and tp != entry]
                    if valid:
                        total_w = sum(w for _, w in valid)
                        if sig == "BUY":
                            blended = sum(((tp - entry) / risk) * (w / total_w) for tp, w in valid)
                        else:
                            blended = sum(((entry - tp) / risk) * (w / total_w) for tp, w in valid)
                        data["rr_ratio"] = max(blended, 0.0)
                        logger.info(
                            "[LLM] rr_ratio computed from blended TP1/TP2/TP3 "
                            "(weights 30/40/30): %.2f", data["rr_ratio"]
                        )
                    missing.discard("rr_ratio")
            except Exception:
                data["rr_ratio"] = 0.0
                missing.discard("rr_ratio")
        # Default tp3 to tp2 if only tp3 is missing
        if "tp3" in missing and "tp2" in data:
            data["tp3"] = data["tp2"]
            missing.discard("tp3")
            logger.info("[LLM] tp3 defaulted to tp2")
        # Default tp2/tp3 from tp1 if both are missing (model only returned tp1)
        if "tp2" in missing and "tp1" in data:
            tp1 = float(data.get("tp1", 0))
            entry = float(data.get("entry", 0))
            sig = str(data.get("signal", "BUY")).upper()
            if tp1 and entry:
                diff = abs(tp1 - entry)
                data["tp2"] = tp1 + diff if sig == "BUY" else tp1 - diff
                missing.discard("tp2")
                logger.info("[LLM] tp2 projected from tp1 distance")
        if "tp3" in missing and "tp2" in data:
            tp2 = float(data.get("tp2", 0))
            tp1 = float(data.get("tp1", 0))
            if tp2 and tp1:
                diff = abs(tp2 - tp1)
                sig = str(data.get("signal", "BUY")).upper()
                data["tp3"] = tp2 + diff if sig == "BUY" else tp2 - diff
                missing.discard("tp3")
                logger.info("[LLM] tp3 projected from tp2 distance")
        # Default text fields to empty string
        for key in ("reasoning", "key_risk"):
            if key in missing:
                data[key] = ""
                missing.discard(key)
                logger.info(f"[LLM] {key} defaulted to empty string")
        # Still missing something non-recoverable
        if missing:
            logger.warning(f"[LLM] Unrecoverable missing keys: {missing}")
            return _pass_result(f"Missing keys: {missing}", model, latency_ms, raw)

    signal = str(data.get("signal", "PASS")).upper()
    if signal not in _VALID_SIGNALS:
        signal = "PASS"

    def _f(val, default=0.0) -> float:
        """Coerce LLM numeric field — handles None/null gracefully."""
        return float(val) if val is not None else default

    try:
        entry   = _f(data.get("entry"), 0)
        sl      = _f(data.get("stop_loss"), 0)
        tp1     = _f(data.get("tp1"), 0)
        sig     = str(data.get("signal", "PASS")).upper()
        llm_rr  = _f(data.get("rr_ratio"), 0)

        # Recompute R:R from price levels whenever prices are available.
        # Always recompute — LLM-provided rr_ratio is unreliable (wrong sign,
        # wrong formula, or zero). Uses blended TP1/TP2/TP3 (weights 30/40/30)
        # to reflect the full multi-TP trade plan, not just the TP1 distance.
        tp2 = _f(data.get("tp2"), 0)
        tp3 = _f(data.get("tp3"), 0)
        if entry and sl and entry != sl:
            risk = abs(entry - sl)
            tp_levels = [(tp1, 0.3), (tp2, 0.4), (tp3, 0.3)]
            valid = [(tp, w) for tp, w in tp_levels if tp and tp != entry]
            if valid:
                total_w = sum(w for _, w in valid)
                llm_rr = max(
                    sum((abs(tp - entry) / risk) * (w / total_w) for tp, w in valid),
                    0.0
                )
                logger.info("[LLM] rr_ratio recomputed from blended TP1/TP2/TP3 (30/40/30): %.2f", llm_rr)

            # Fix 2: If R:R is still below minimum after recompute, rebuild TP levels
            # from entry+SL to guarantee MIN_RR_RATIO. Only applies to BUY/SELL, not PASS.
            from config.settings import MIN_RR_RATIO as _MIN_RR
            if sig in ("BUY", "SELL") and llm_rr < _MIN_RR:
                old_rr = llm_rr
                multiplier = 1.0 if _MIN_RR <= 1.0 else _MIN_RR
                if sig == "BUY":
                    tp1 = entry + risk * multiplier * 0.8   # TP1 at 0.8R
                    tp2 = entry + risk * multiplier * 1.3   # TP2 at 1.3R
                    tp3 = entry + risk * multiplier * 1.8   # TP3 at 1.8R
                else:
                    tp1 = entry - risk * multiplier * 0.8
                    tp2 = entry - risk * multiplier * 1.3
                    tp3 = entry - risk * multiplier * 1.8
                # Recompute blended R:R with new TPs
                llm_rr = (abs(tp1 - entry) * 0.3 + abs(tp2 - entry) * 0.4 + abs(tp3 - entry) * 0.3) / risk
                logger.info(
                    "[LLM] R:R %.2f below minimum %.2f — TPs rebuilt: TP1=%.4f TP2=%.4f TP3=%.4f → R:R=%.2f",
                    old_rr, _MIN_RR, tp1, tp2, tp3, llm_rr
                )

        return SignalResult(
            signal=signal,
            confidence=_f(data.get("confidence"), 0),
            entry=entry,
            stop_loss=sl,
            tp1=tp1,
            tp2=tp2,   # use local var — may have been rebuilt by TP-rebuild path
            tp3=tp3,   # use local var — may have been rebuilt by TP-rebuild path
            reasoning=str(data.get("reasoning") or ""),
            key_risk=str(data.get("key_risk") or ""),
            timeframe=str(data.get("timeframe") or "1h"),
            rr_ratio=llm_rr,
            model_used=model,
            prompt_version=LLM_PROMPT_VERSION,
            latency_ms=latency_ms,
            raw_response=raw,
        )
    except (TypeError, ValueError) as e:
        logger.warning(f"[LLM] Value coercion error: {e}")
        return _pass_result(f"Value error: {e}", model, latency_ms, raw)


def _pass_result(
    reason: str,
    model: str,
    latency_ms: int,
    raw: str = "",
) -> SignalResult:
    """Return a safe PASS result with error context."""
    return SignalResult(
        signal="PASS",
        confidence=0,
        entry=0, stop_loss=0, tp1=0, tp2=0, tp3=0,
        reasoning=f"LLM fallback: {reason}",
        key_risk="LLM response invalid",
        model_used=model,
        latency_ms=latency_ms,
        raw_response=raw,
        error=reason,
    )


async def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    temperature: float = 0.2,
) -> SignalResult:
    """Call the LLM with retry + fallback logic.

    Tries OPENAI_MODEL first, falls back to OPENAI_FALLBACK.
    Returns a SignalResult — never raises.

    L-1: Acquires the per-provider semaphore before each API call to prevent
    concurrent-limit 429 errors when all symbols hit confluence simultaneously.
    """
    model = model or OPENAI_MODEL
    client = _get_client()  # C-4: reuse singleton, no leak

    for attempt in range(LLM_MAX_RETRIES + 1):
        # M-6: was `attempt < LLM_MAX_RETRIES` — gave primary only 2 of 3 attempts.
        # Fallback fires on the last attempt only (attempt == LLM_MAX_RETRIES).
        current_model = OPENAI_FALLBACK if attempt == LLM_MAX_RETRIES else model
        t0 = time.monotonic()
        try:
            logger.info(f"[LLM] Calling {current_model} (attempt {attempt + 1})...")
            # L-1: semaphore limits simultaneous API calls to LLM_MAX_CONCURRENT
            async with _get_semaphore():
                response = await client.chat.completions.create(
                    model=current_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    max_tokens=4096,
                )
            latency_ms = int((time.monotonic() - t0) * 1000)
            raw = response.choices[0].message.content or ""
            logger.info(f"[LLM] Response in {latency_ms}ms from {current_model}")
            logger.debug(f"[LLM] Raw response: {repr(raw[:800])}")
            result = _parse_llm_response(raw, current_model, latency_ms)
            if result.error and attempt < LLM_MAX_RETRIES:
                logger.warning(f"[LLM] Attempt {attempt + 1} failed ({result.error}), retrying...")
                await asyncio.sleep(1)
                continue
            return result
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.error(f"[LLM] API error on attempt {attempt + 1}: {e}")
            if attempt < LLM_MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
                continue
            return _pass_result(f"API error: {e}", current_model, latency_ms)

    return _pass_result("All retries exhausted", OPENAI_FALLBACK, 0)


async def get_signal(
    system_prompt: str,
    user_prompt: str,
    min_confidence: float = MIN_LLM_CONFIDENCE,
) -> SignalResult:
    """High-level entry: call LLM and apply minimum confidence gate.

    Returns PASS if confidence < min_confidence.
    """
    result = await call_llm(system_prompt, user_prompt)
    if result.signal != "PASS" and result.confidence < min_confidence:
        logger.info(
            f"[LLM] Signal {result.signal} below confidence threshold "
            f"({result.confidence:.0f}% < {min_confidence:.0f}%) → PASS"
        )
        result.signal = "PASS"
        result.error = f"Confidence {result.confidence:.0f}% below threshold {min_confidence:.0f}%"
    return result
