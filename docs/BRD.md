# Business Requirements Document — SignalForge AI Trading Signal System

| **Document ID** | SF-BRD-001 |
|---|---|
| **Version** | 1.0 |
| **Date** | June 25, 2026 |
| **Status** | Draft for Review |
| **Prepared By** | Product Engineering |
| **Approved By** | *[To be completed]* |

---

## 1. Executive Summary

SignalForge is an AI-powered trading signal generation system developed to provide institutional-grade, high-conviction trade signals for digital asset markets. The system combines Smart Money Concepts (SMC) / Inner Circle Trader (ICT) methodologies with multi-timeframe technical analysis, large language model (LLM) reasoning, and a rigorous multi-layer filter gate to produce actionable signals with quantitatively validated confidence scores.

The system operates on a Linux server hosted on Amazon Web Services (AWS), monitoring four major digital assets (BTC, ETH, BNB, SOL) in real time via WebSocket connections. SignalForge uses a sophisticated architecture spanning 59 Python source files (15,783 lines of code) encompassing asynchronous data ingestion, custom SMC pattern detection, confluence scoring across 12 distinct signal categories, and a 10-layer filter gate that enforces strict admission criteria before any signal is considered for execution. Delivery of actionable signals occurs via Telegram Bot, ensuring low-latency dissemination to end users.

This document defines the business requirements, success criteria, stakeholder map, cost-benefit analysis, risk assessment, ROI projections, and compliance framework governing the SignalForge system. It serves as the authoritative reference for all stakeholders and establishes the contractual baseline against which system performance is measured.

**System at a Glance**

| Attribute | Detail |
|---|---|
| Architecture | Asynchronous Python 3.11+, asyncio, ccxt/ccxt.pro, SQLite, pandas |
| Deployment | AWS Linux server managed via systemd, health watchdog on port 8080 |
| Assets Monitored | BTC, ETH, BNB, SOL |
| Signals Processed to Date | 279 |
| Paper Trades Opened | 0 (reflecting strict filter-gate standards) |
| Development Cadence | 9 sprints delivered over several weeks |

---

## 2. Business Objectives

SignalForge is chartered to fulfill five primary business objectives. Each objective maps directly to a measurable business outcome and is embedded in the system's design philosophy.

### 2.1 Automated 24/7/365 Market Monitoring

**Objective:** Eliminate the requirement for human screen-watching by deploying an autonomous monitoring agent that operates continuously across all global trading sessions.

- The system ingests real-time market data via WebSocket connections (ccxt.pro), processing tick-level and candlestick data across four assets concurrently.
- A systemd-managed service with an HTTP health watchdog on port 8080 ensures process continuity and self-recovery.
- Asynchronous architecture (asyncio event loop) enables concurrent monitoring without degradation.
- Expected outcome: Zero gaps in market coverage; detection of setup formations during all hours including weekends and overnight sessions.

### 2.2 High-Confidence Signal Generation

**Objective:** Produce trade signals that meet institutionally rigorous confidence thresholds through systematic multi-factor validation.

- Each signal undergoes evaluation across 12 distinct signal categories using SMC/ICT concepts such as order blocks, fair value gaps, liquidity sweeps, breaker blocks, and market structure shifts.
- An LLM reasoning layer (hermes-main via 9router to claude-opus) performs natural-language analysis of market context, supplementing quantitative indicators with qualitative assessment.
- A 10-layer filter gate imposes sequential admission criteria, permitting only signals that survive every stage of scrutiny.
- Each signal carries a confluence score derived from cross-validation across timeframes, indicators, and the LLM reasoning pathway.
- Expected outcome: Signals carry demonstrable edge; false positives are minimized at the expense of signal volume.

### 2.3 Elimination of Emotional Trading

**Objective:** Remove all human emotional and psychological factors from the signal generation and execution pipeline.

- Signal generation is fully deterministic based on the confluence of technical criteria and LLM assessment — no subjective override is permitted.
- The filter gate enforces programmatic admission rules without manual intervention.
- Paper trading proceeds automatically when signals cross the admission threshold; no discretionary approval is required.
- A strict policy of no post-hoc signal modification ensures that the historical audit trail is a truthful record of system decisions.
- Expected outcome: Consistent, rules-based signal output independent of market euphoria, fear, or fatigue.

### 2.4 Full Audit Trail

**Objective:** Maintain a complete, tamper-evident record of every signal, decision, and system event for retrospective analysis and regulatory compliance.

- SQLite database records every signal with timestamps, confluence scores, filter-gate outcomes, LLM reasoning text, and asset context.
- System health metrics and watchdog events are logged to enable post-mortem analysis of any service interruption.
- Paper trade records include entry/exit rationale, pricing snapshots, and position-level attribution.
- Expected outcome: Every signal and trade decision is fully attributable, reproducible, and reviewable.

### 2.5 Continuous Improvement

**Objective:** Systematically evolve signal quality through data-driven refinement.

- All signal outcomes (whether they would have resulted in winning, losing, or breakeven trades) are logged for performance analysis.
- Win rate, signal frequency, and filter-gate pass-through rates are tracked over time and by asset.
- Parameter tuning and strategy adjustments follow a controlled change-management process informed by performance data.
- Expected outcome: Measurable month-over-month improvement in signal quality metrics.

---

## 3. Success Criteria

The following criteria define the minimum acceptable performance thresholds for SignalForge. These metrics are measured on a rolling 30-day basis and are subject to weekly review by the product team. The system must satisfy all criteria concurrently for a rolling 30-day period to be considered production-ready and meeting business requirements.

| **Criterion** | **Target** | **Measurement Method** | **Review Cadence** |
|---|---|---|---|
| **Win Rate** | ≥ 60% | (Number of winning paper trades / Total closed paper trades) × 100 | Weekly |
| **Signal Frequency** | 2–8 signals per week | Count of signals surviving all 10 filter-gate layers and entering paper trading | Weekly |
| **Mean Time Between Failures (MTBF)** | > 72 hours | Wall-clock time between unplanned service interruptions requiring manual recovery | Weekly |
| **Unrecovered Crashes** | Zero | Count of crashes resulting in permanent data loss or requiring full system rebuild | Continuous |

### 3.1 Win Rate Rationale

A 60% minimum win rate is selected as the threshold above which the system generates positive expected value after accounting for typical trading costs (spread, slippage, and maker/taker fees at major exchanges). This threshold is consistent with published performance benchmarks for systematic SMC/ICT-based strategies in digital asset markets.

### 3.2 Signal Frequency Rationale

The 2–8 signals per week range reflects the deliberate design choice to prioritize signal quality over quantity. The 10-layer filter gate is intentionally conservative. A frequency below 2 signals per week would indicate excessive filtering that may miss actionable opportunities; above 8 signals per week would suggest insufficient discrimination.

### 3.3 MTBF and Crash Recovery

The >72-hour MTBF target reflects the expectation of enterprise-grade reliability. Zero unrecovered crashes is a hard requirement — any data loss due to system failure constitutes an unacceptable breach of the audit-trail objective (Section 2.4). The system's health watchdog and systemd auto-restart capabilities are designed to support this requirement.

---

## 4. Stakeholder Analysis

The following table identifies all parties with a material interest in the SignalForge system, their roles, responsibilities, and engagement model.

| **Stakeholder** | **Role** | **Primary Interest** | **Engagement** |
|---|---|---|---|
| **Executive Sponsor** | Strategic oversight and funding approval | ROI realization, risk-adjusted returns, regulatory compliance | Monthly executive review |
| **Product Manager** | Requirements definition, prioritization, stakeholder communication | Feature completeness, delivery timeline, success criteria attainment | Weekly sprint review |
| **Engineering Team** | System architecture, development, deployment, maintenance | Code quality, system reliability, latency, throughput | Daily stand-ups, continuous |
| **Quantitative Analyst** | Strategy validation, performance analysis, parameter tuning | Win rate, statistical significance, backtest coherence | Weekly performance review |
| **Risk & Compliance Officer** | Regulatory adherence, audit oversight, data governance | Audit trail completeness, data retention, operational risk | Monthly compliance review |
| **End Users** | Signal recipients (Telegram subscribers) | Signal quality, timeliness, clarity, consistency | In-app feedback, quarterly survey |
| **Exchange Partners** | Data provision and potential execution venue | API usage compliance, fair usage policy adherence | As needed |

---

## 5. Cost-Benefit Analysis

### 5.1 Cost Breakdown

All costs are expressed in USD and reflect the current operational architecture. Costs are categorized as fixed (recurring regardless of usage level) or variable (scaling with signal volume or compute demand).

| **Cost Category** | **Component** | **Type** | **Monthly Estimate** | **Annual Estimate** |
|---|---|---|---|---|
| **AWS Compute** | Linux EC2 instance (t3.medium or equivalent), EBS storage, data transfer | Fixed | $30.00 | $360.00 |
| **Exchange Data** | Binance API (REST + WebSocket) — free tier | Fixed | $0.00 | $0.00 |
| **LLM Credits** | 9router / hermes-main → claude-opus inference | Variable | $20.00 – $60.00 | $240.00 – $720.00 |
| **Telegram Bot** | Bot API — free tier | Fixed | $0.00 | $0.00 |
| **Domain / DNS** | Optional custom domain for health dashboard | Fixed | $0.00 – $2.00 | $0.00 – $24.00 |
| **Observability** | Prometheus / Grafana basic metrics (self-hosted on existing instance) | Fixed | $0.00 | $0.00 |
| **Development Overhead** | Engineering hours for maintenance, tuning, and improvements | Fixed | Included in salary | Included in salary |

**Total Monthly Cost Estimate: $50.00 – $92.00**

**Total Annual Cost Estimate: $600.00 – $1,104.00**

### 5.2 Benefit Projections

Benefits are modeled conservatively and assume the 60% minimum win rate target is achieved. Parameterization is based on the observed signal frequency range (2–8 signals per week) and typical risk management assumptions for systematic digital asset trading.

| **Benefit Component** | **Conservative Scenario** | **Expected Scenario** | **Optimistic Scenario** |
|---|---|---|---|
| Signals per Week | 2 | 4 | 8 |
| Win Rate | 60% | 65% | 70% |
| Average Risk per Signal (assumed) | 1.0% of capital | 1.5% of capital | 2.0% of capital |
| Risk-to-Reward Ratio | 1:2 | 1:2.5 | 1:3 |
| Monthly Return on Capital (per 10 BTC notional) | 1.2% | 3.9% | 12.6% |
| **Net Annual Return (10 BTC notional ≈ $600,000)** | **$86,400** | **$280,800** | **$907,200** |

*Note: Benefit projections are illustrative only and depend on capital deployment parameters, market conditions, and slippage. These projections do not constitute a promise of financial returns.*

### 5.3 Cost-Benefit Summary

| **Metric** | **Value** |
|---|---|
| Annual Operating Cost | $600 – $1,104 |
| Conservative Annual Benefit (at 60% win rate) | $86,400 |
| Expected Annual Benefit (at 65% win rate) | $280,800 |
| Optimistic Annual Benefit (at 70% win rate) | $907,200 |
| **Expected Net Annual Value** | **$279,696 – $280,200** |
| **Expected ROI (annualized)** | **~25,400% – 46,700%** |

---

## 6. Risk Assessment

### 6.1 Technical Risks

| **Risk** | **Description** | **Likelihood** | **Impact** | **Mitigation Strategy** |
|---|---|---|---|---|
| **API Downtime** | Binance API or WebSocket endpoint becomes unavailable, interrupting data flow | Medium | High — signal generation halts | Redundant fallback data source (secondary exchange or REST polling); system enters safe mode with auto-recovery when API returns |
| **Data Quality Degradation** | Stale, delayed, or corrupted market data affects signal accuracy | Low | High — false signals possible | Data validation layer on ingestion; checksum verification; cross-referencing with secondary feed; stale-data threshold triggers alert |
| **LLM Service Outage** | 9router or claude-opus inference endpoint unavailable | Medium | Medium — reduces confidence in confluence scoring | Fallback to quantitative-only scoring; cached LLM context for repeated patterns; degraded-mode operation |
| **WebSocket Disconnection** | Recurring disconnections cause data gaps | Medium | Medium — missed setups | Automated reconnection with exponential backoff; buffer gap detection and catch-up logic |
| **Database Corruption** | SQLite file corruption leads to data loss | Low | Critical — audit trail compromise | Regular SQLite backup (hourly); WAL mode for crash safety; off-site backup to S3/cloud storage |
| **Resource Exhaustion** | Memory or CPU saturation from excessive concurrent processing | Low | Medium — service degradation | Resource monitoring via health watchdog; autoscaling triggers; process-level memory limits; alert at 80% utilization |

### 6.2 Business Risks

| **Risk** | **Description** | **Likelihood** | **Impact** | **Mitigation Strategy** |
|---|---|---|---|---|
| **Strategy Performance Variance** | SMC/ICT strategy performs differently in varying market regimes (trending vs. ranging) | Medium | High — win rate may fall below 60% threshold | Regime detection module; strategy parameter adjustment by market condition; rolling performance window for early detection of degradation |
| **Over-Optimization** | Parameters tuned excessively to historical data, reducing forward performance | Medium | High | Walk-forward validation during development; out-of-sample testing; parameter constraints; weekly performance review against expectations |
| **Signal Starvation** | Extended periods with zero signals due to conservative filtering | Medium | Low — preferable to false signals | Periodic filter-gate calibration review; user expectation management regarding quiet periods |
| **Execution Slippage** | Real-world execution differs materially from paper trade prices | High (for illiquid pairs) | Medium | Slippage modeling in paper trading; focus on liquid pairs; execution simulation for validation |
| **Regulatory Uncertainty** | Changing digital asset regulations affect legality of signal provision | Low | Critical | Legal review of signal classification; jurisdiction-specific disclaimers; compliance advisory board |

### 6.3 Risk Response Plan

All risks classified as High or Critical impact shall have a documented response plan that identifies:

- **Trigger condition** — the specific event or metric threshold that activates the response.
- **Response owner** — the individual responsible for executing the response.
- **Response action** — the specific steps to be taken.
- **Escalation path** — the chain of communication if the response does not resolve the issue within a defined timeframe.

---

## 7. ROI Projections

### 7.1 Development Investment

SignalForge was developed over 9 sprints with a total engineering investment estimated as follows:

| **Cost Component** | **Estimate** |
|---|---|
| Engineering Hours (dedicated) | ~540 hours (9 sprints × 3 weeks × 20 hours/week) |
| Engineering Cost at blended rate ($150/hr) | $81,000 |
| AWS Development Infrastructure | ~$270 (9 sprints × $30/month for partial months) |
| LLM Credits (development and testing) | ~$180 (9 sprints × $20/month average) |
| **Total Development Investment** | **~$81,450** |

### 7.2 Break-Even Analysis

Using the expected scenario annual benefit of $280,800:

| **Scenario** | **Break-Even Period** |
|---|---|
| Expected Scenario ($280,800/yr) | ~3.5 months |
| Conservative Scenario ($86,400/yr) | ~11.3 months |
| Optimistic Scenario ($907,200/yr) | ~1.1 months |

### 7.3 Three-Year ROI Projection

Assuming the expected scenario and a 10% annual improvement in signal quality due to continuous improvement (Section 2.5):

| **Year** | **Annual Operating Cost** | **Annual Benefit** | **Net Annual Value** | **Cumulative Net Value** |
|---|---|---|---|---|
| Year 1 | $900 (including development amortization) | $280,800 | $279,900 | $279,900 |
| Year 2 | $960 | $308,880 (10% improvement) | $307,920 | $587,820 |
| Year 3 | $1,200 | $339,768 (10% improvement) | $338,568 | $926,388 |
| **Total** | **$3,060** | **$929,448** | **$926,388** | **$926,388** |

**Three-Year ROI: ~30,200%** (based on total investment of ~$3,060 operating costs against $929,448 in benefits).

---

## 8. Compliance & Security

### 8.1 Data Governance

- All signal data, trade records, and system logs are stored in SQLite with WAL (Write-Ahead Logging) mode enabled for crash consistency.
- Hourly automated backups are performed to secondary storage.
- No personally identifiable information (PII) is collected, stored, or transmitted by the SignalForge system.
- Data retention policy: A minimum of 3 years of signal and performance data shall be retained for audit and analysis purposes.

### 8.2 API Security

- All exchange API keys (if used for paper or live trading) are stored with environment-variable injection and are never committed to source code.
- API requests are rate-limited in accordance with exchange terms of service to avoid abuse or account flagging.
- WebSocket connections are authenticated via API keys where required; unauthenticated public data streams are used where sufficient.

### 8.3 Infrastructure Security

- The AWS EC2 instance is configured with a restrictive security group allowing only necessary inbound access (SSH from authorized IPs, HTTP health check on port 8080).
- SSH key-based authentication is enforced; password authentication is disabled.
- The system runs as a non-root service user with minimal required permissions.
- System packages are updated on a regular cadence to address security vulnerabilities.
- The health watchdog on port 8080 exposes only minimal system status information; no signal data or trading logic is exposed via the health endpoint.

### 8.4 Operational Compliance

- SignalForge is a signal generation system only; it does not execute trades or manage funds directly. Paper trading occurs in a simulated environment with no real capital at risk.
- All signals include a disclaimer indicating that they are for informational and educational purposes only and do not constitute financial or investment advice.
- The system complies with Binance API terms of service, including fair usage policies and data attribution requirements where applicable.
- Telegram message delivery complies with Telegram Bot API terms of service.

### 8.5 Audit & Transparency

- A complete, immutable audit trail is maintained for every signal generated, including the full chain of filter-gate decisions, LLM reasoning output, and confluence score calculation.
- System health events (restarts, disconnections, errors) are logged with timestamps to enable full post-mortem analysis.
- Any modification to signal generation logic or parameters is tracked via version control and requires documented approval.

---

## 9. Approval

This Business Requirements Document is submitted for formal review and approval by the designated stakeholders. Signatures below indicate that the undersigned have reviewed the document, understand the requirements, costs, risks, and success criteria, and authorize proceeding with the defined scope.

| **Role** | **Name** | **Signature** | **Date** |
|---|---|---|---|
| Executive Sponsor | | | |
| Product Manager | | | |
| Engineering Lead | | | |
| Risk & Compliance Officer | | | |

---

### Document Revision History

| **Version** | **Date** | **Author** | **Description of Changes** |
|---|---|---|---|
| 1.0 | June 25, 2026 | Product Engineering | Initial document creation for stakeholder review |

---

*This document contains proprietary information. Distribution outside of authorized stakeholders requires written approval from the Executive Sponsor.*

---

**END OF DOCUMENT — SF-BRD-001 REV 1.0**
