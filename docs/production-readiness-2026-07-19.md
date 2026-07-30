# SignalForge — Production Readiness Checklist
> Date: 2026-07-19  
> Goal: Scalping signals across all timeframes (low TF and high TF)  
> Verdict: **NOT PRODUCTION READY** — see blockers below

---

## 🔴 Blockers (must fix before going live)

- [ ] **Zero signals being delivered** — LLM R:R < 1.2 blocks every BUY/SELL at filter_gate
- [ ] **Liquidation tracker 401 Unauthorized** — Tier 2 confluence data missing every cycle
- [ ] **OI change always 0.0%** — Tier 3 confluence factor permanently dead
- [ ] **No signals table readable in DB** — WAL not checkpointed, zero delivery history verifiable

---

## 🟡 Required Before Stable Production

- [ ] **Memory at 992MB / 1.1GB** — OOM risk during high-volatility cycles (Fix 5: bump to 1.4G)
- [ ] **HYPE/USDT:USDT symbol malformed** — potential key mismatch in cooldown + DB (Fix 6)
- [ ] **No HTF (daily/weekly) bias in signal** — counter-trend scalps not filtered (Fix 7)
- [ ] **PASS_COOLDOWN suppression** — reduces scan frequency in ranging markets (review logic)
- [ ] **LLM sets TP/SL levels** — not anchored to real liquidity zones; needs fallback to structure levels

---

## 🟢 Production-Ready Components

- [x] Service runs via systemd, auto-restarts on failure, enabled on boot
- [x] BOS + FVG retest state machine (ARMED → TRIGGERED) — correctly implemented
- [x] Dual Telegram delivery (DM + group) — fan-out confirmed 2/2 every cycle
- [x] Confluence scoring pipeline — Tier 1 (technical) + Tier 2 (onchain) + Tier 3 (ext) structure is sound
- [x] MTF bias (4H/1H/15m) — correct hierarchy for scalping
- [x] Session filter (blocks alts 22:00–07:00 UTC) — prevents Asian session noise
- [x] Cooldown tracker — prevents signal spam per asset
- [x] ATR spike filter (F8) — protects against entering during volatility explosions
- [x] EMA9 momentum filter (F12) — prevents entries against momentum
- [x] LLM async semaphore — prevents 429 burst on Binance API
- [x] Filter gate architecture — clean short-circuit chain with full logging
- [x] MIN_CONFLUENCE_SCORE=5, MIN_LLM_CONFIDENCE=60 — correctly set in .env

---

## Timeframe Coverage Assessment

| Timeframe | Status | Notes |
|---|---|---|
| 1m | ⚠️ Partial | Scan runs but R:R rarely achievable at 1m volatility |
| 5m | ⚠️ Blocked | Primary FVG/BOS tf — blocked by R:R filter |
| 15m | ⚠️ Blocked | MTF entry tf — blocked by R:R filter |
| 1H | ⚠️ Blocked | Structure tf — blocked by R:R filter |
| 4H | ✅ Bias only | Used for MTF macro bias, not entry |
| Daily | ❌ Missing | No daily context in prompt (Fix 7) |
| Weekly | ❌ Missing | No weekly context at all |

**All timeframes are blocked by the same root cause: R:R filter.**  
Fix 1+2 unblocks all timeframes simultaneously.

---

## Signal Flow Health Check

```
Data fetch         ✅ OHLCV fetching normally
WS feed            ✅ BTC sentinel alive
SMC detection      ✅ BOS/FVG zones arming correctly
Confluence score   ⚠️  OI dead, liquidation dead — scores artificially low
BOS retest arm     ✅ ARMED log entries confirmed (ETH, BNB, SUI)
LLM call           ✅ Calling and returning (65% BUY/SELL, 35% PASS)
R:R check          ❌ BLOCKING ALL — 73/100 blocked here
FilterGate PASSED  ❌ ZERO events in full log
Telegram delivery  ❌ No signals delivered (fan-out infra is healthy)
DB logging         ⚠️  WAL not checkpointed
```

---

## Estimated Time to Production-Ready

| Phase | Fixes | Est. Time | Expected Outcome |
|---|---|---|---|
| **Phase 1** — Get signals flowing | Fix 1 (MIN_RR_RATIO=1.0) + Fix 3B (silence 401) | 15 min | First signals delivered today |
| **Phase 2** — Core quality | Fix 2 (R:R recompute) + Fix 4 (OI delta) + Fix 5 (memory) | 1–2 hours | Stable signal flow with correct R:R |
| **Phase 3** — Full production | Fix 6 (symbol normalize) + Fix 7 (HTF context) + Fix 8 (WAL) | Half day | Full production quality |

---

## Overall Verdict

> The strategy logic is sound. The infrastructure is mostly solid.  
> The system is being killed by a single compounding issue: the LLM  
> generates R:R values the filter won't accept, and two supporting  
> data sources (liquidation + OI) are silently broken.  
>  
> **Phase 1 fix takes 15 minutes and will restore signal delivery immediately.**  
> Recommend starting there before any further development.

---

*See `fixes-2026-07-19.md` for full fix instructions with code snippets.*  
*See `review-2026-07-19.md` for the full strategy + code review.*
