"""
monitoring/error_alerter.py — Telegram error alerts for SignalForge.

Monitors system health and sends alerts via Telegram when something goes wrong.
Supports multiple alert rules, recovery notifications, per-component cooldowns,
rate limiting, and a daily summary sent at 00:00 UTC.

Usage:
    alerter = ErrorAlerter(bot, chat_id="12345", log_path="/path/to/log")
    asyncio.create_task(alerter.start_monitoring())
    # ... later ...
    await alerter.stop()
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# WIB offset from UTC
_WIB_OFFSET = timedelta(hours=7)

# ---------------------------------------------------------------------------
# In-memory error tracking structures
# ---------------------------------------------------------------------------
_error_counts: list[tuple[float, str, str, str]] = (
    []
)  # (timestamp, component, message, error_type)
_component_states: dict[str, str] = {}  # component -> "healthy" | "degraded" | "down"
_component_cooldowns: dict[str, float] = {}  # component -> timestamp until which muted
_component_consecutive_failures: dict[str, int] = defaultdict(int)
_last_llm_tick: dict[str, float] = {}  # symbol -> last WebSocket tick timestamp
_alert_history: list[dict] = []  # all alert events (in-memory)


def _wib_now() -> str:
    """Return current time formatted as WIB (UTC+7)."""
    return (datetime.now(timezone.utc) + _WIB_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def _format_alert(
    component: str, message: str, error_type: str, timestamp_wib: str
) -> str:
    """Format a Telegram alert message."""
    return (
        f"❌ *SignalForge Alert* — {component}\n"
        f"Type: {error_type}\n"
        f"Message: {message}\n"
        f"Time: {timestamp_wib}"
    )


def _format_recovery(component: str, timestamp_wib: str) -> str:
    """Format a Telegram recovery notification."""
    return (
        f"✅ *SignalForge Recovery* — {component}\n"
        f"Component has recovered.\n"
        f"Time: {timestamp_wib}"
    )


class ErrorAlerter:
    """Monitors system health and sends Telegram alerts on failures.

    Parameters
    ----------
    bot : TelegramBot
        A bot instance with an async ``.send(message: str) -> bool`` method.
    chat_id : str
        Telegram chat identifier (stored for context, though the bot already
        knows its target chat).
    log_path : str
        Filesystem path for writing alert logs (appended, not rotated).
    check_interval : int
        Seconds between health-check cycles (default 60).
    """

    def __init__(
        self,
        bot,
        chat_id: str,
        log_path: str,
        check_interval: int = 60,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.log_path = log_path
        self.check_interval = check_interval

        self._running = False
        self._task: asyncio.Task | None = None

        # Daily summary tracking
        self._last_summary_date: int | None = None  # UTC date integer (YYYYMMDD)

        # Rate tracking window (5 minutes, >10 errors = rate alert)
        self._rate_window_seconds = 300
        self._rate_threshold = 10

        logger.info(
            "ErrorAlerter initialised (chat=%s, log=%s, interval=%ds)",
            chat_id,
            log_path,
            check_interval,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_monitoring(self) -> None:
        """Start the background monitoring loop as an asyncio task.

        Runs every *check_interval* seconds.  Checks for:
        - High error rate (>10 errors in 5 minutes)
        - WebSocket disconnects (>120s without a tick)
        - Daily summary at 00:00 UTC
        """
        if self._running:
            logger.warning("ErrorAlerter already running — ignoring duplicate start")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("ErrorAlerter monitoring started (interval=%ds)", self.check_interval)

    async def stop(self) -> None:
        """Gracefully stop the monitoring loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ErrorAlerter stopped")

    def report_error(
        self, component: str, message: str, error_type: str = "error"
    ) -> None:
        """Immediately send an alert for a component failure.

        Parameters
        ----------
        component : str
            Name of the failing component (e.g. ``"pipeline"``, ``"llm:gpt-4"``).
        message : str
            Human-readable description of the error.
        error_type : str
            Category label (``"error"``, ``"crash"``, ``"timeout"``, etc.).
        """
        now = time.time()
        timestamp = _wib_now()

        # Record the error globally for rate tracking
        _error_counts.append((now, component, message, error_type))
        # Trim old entries beyond 10 minutes
        self._trim_error_counts(now)

        # Track consecutive failures per component
        _component_consecutive_failures[component] += 1

        # Update component state
        _component_states[component] = "down"

        # Check cooldown — don't re-alert the same component within 30 minutes
        if component in _component_cooldowns and now < _component_cooldowns[component]:
            logger.debug(
                "Suppressing alert for %s (cooldown active until %.0f)",
                component,
                _component_cooldowns[component],
            )
            return

        # Set 30-minute cooldown
        _component_cooldowns[component] = now + 1800

        # Build and send alert
        alert_text = _format_alert(component, message, error_type, timestamp)
        self._send_alert(alert_text)

        # Log locally
        self._write_log_line(
            timestamp, component, error_type, message, status="alert"
        )

        # Record in history
        _alert_history.append(
            {
                "time": now,
                "component": component,
                "type": error_type,
                "message": message,
            }
        )
        # Keep history trim (last 1000 entries)
        while len(_alert_history) > 1000:
            _alert_history.pop(0)

    def report_recovery(self, component: str) -> None:
        """Report that a previously-failing component has recovered.

        Sends a ✅ recovery notification unless the component was already
        healthy or no alert had been sent for it.

        Parameters
        ----------
        component : str
            Name of the component that recovered.
        """
        old_state = _component_states.get(component, "healthy")
        if old_state == "healthy":
            return  # no alert was active, skip

        timestamp = _wib_now()
        _component_states[component] = "healthy"
        _component_consecutive_failures[component] = 0

        # Clear cooldown so future alerts are not suppressed
        _component_cooldowns.pop(component, None)

        recovery_text = _format_recovery(component, timestamp)
        self._send_alert(recovery_text)

        self._write_log_line(
            timestamp, component, "recovery", "Component recovered", status="recovery"
        )

        logger.info("Recovery reported for component: %s", component)

    def get_recent_errors(self, minutes: int = 30) -> list[dict]:
        """Return errors recorded in the last *minutes*.

        Parameters
        ----------
        minutes : int
            Look-back window in minutes (default 30).

        Returns
        -------
        list[dict]
            Each entry: ``{"time", "component", "message", "error_type"}``.
        """
        cutoff = time.time() - minutes * 60
        recent = []
        for ts, comp, msg, err_type in reversed(_error_counts):
            if ts >= cutoff:
                recent.append(
                    {
                        "time": ts,
                        "component": comp,
                        "message": msg,
                        "error_type": err_type,
                    }
                )
        return recent

    def get_health_status(self) -> dict:
        """Return the current health status of all known components.

        Returns
        -------
        dict
            Structure: ``{"components": {name: state}, "total_errors": N}``
            where *state* is ``"healthy"``, ``"degraded"``, or ``"down"``.
        """
        return {
            "components": dict(_component_states),
            "total_errors": len(_error_counts),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Main asyncio task — runs periodic health checks."""
        logger.info("Monitor loop started")

        while self._running:
            try:
                now = time.time()
                self._trim_error_counts(now)

                # --- Rate alert check (>10 errors in 5 min) ---
                window_start = now - self._rate_window_seconds
                recent_count = sum(
                    1 for ts, _, _, _ in _error_counts if ts >= window_start
                )
                if recent_count > self._rate_threshold:
                    self.report_error(
                        component="system",
                        message=(
                            f"High error rate: {recent_count} errors in "
                            f"the last {self._rate_window_seconds // 60} minutes"
                        ),
                        error_type="rate_alert",
                    )

                # --- WebSocket disconnect check ---
                for symbol, last_tick in list(_last_llm_tick.items()):
                    if now - last_tick > 120:
                        self.report_error(
                            component=f"websocket:{symbol}",
                            message=(
                                f"No tick for {now - last_tick:.0f}s "
                                f"(threshold: 120s)"
                            ),
                            error_type="websocket_timeout",
                        )
                        # Reset so we don't re-alert every cycle
                        _last_llm_tick[symbol] = now
                        _component_states[f"websocket:{symbol}"] = "down"

                # --- Daily summary at 00:00 UTC ---
                utc_today = datetime.now(timezone.utc).date()
                today_int = utc_today.year * 10000 + utc_today.month * 100 + utc_today.day

                if (
                    self._last_summary_date is None
                    or today_int != self._last_summary_date
                ):
                    # Check if it's close to midnight UTC
                    utc_now = datetime.now(timezone.utc)
                    if utc_now.hour == 0 and utc_now.minute < 5:
                        await self._send_daily_summary()
                        self._last_summary_date = today_int

            except asyncio.CancelledError:
                logger.info("Monitor loop cancelled")
                raise
            except Exception:
                logger.exception("Monitor loop error")
                # Don't let an exception kill the loop

            await asyncio.sleep(self.check_interval)

    def _send_alert(self, text: str) -> None:
        """Fire-and-forget send via the bot (runs in the event loop).

        Creates a task in the current running loop, or queues one if no
        loop is active yet (safe for both sync and async callers).
        """
        try:
            if not asyncio.iscoroutinefunction(self.bot.send):
                self.bot.send(text)
                return

            loop = asyncio.get_running_loop()
            task = loop.create_task(self._send_inner(text))
            task.add_done_callback(self._on_send_done)
        except RuntimeError:
            # No running event loop — schedule for the next available one
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    task = loop.create_task(self._send_inner(text))
                    task.add_done_callback(self._on_send_done)
                else:
                    loop.run_until_complete(self._send_inner(text))
            except Exception as e:
                logger.error("Failed to dispatch alert (no loop): %s", e)
        except Exception as e:
            logger.error("Failed to dispatch alert: %s", e)

    async def _send_inner(self, text: str) -> bool:
        """Actually perform the send (separate coroutine for task creation)."""
        return await self.bot.send(text)

    @staticmethod
    def _on_send_done(fut: asyncio.Future) -> None:
        """Callback that logs send success/failure."""
        try:
            success = fut.result()
            if not success:
                logger.warning("Alert send returned False")
        except Exception as e:
            logger.error("Alert send exception: %s", e)

    def _trim_error_counts(self, now: float) -> None:
        """Remove error entries older than 10 minutes."""
        cutoff = now - 600  # 10 minutes
        while _error_counts and _error_counts[0][0] < cutoff:
            _error_counts.pop(0)

    def _write_log_line(
        self,
        timestamp: str,
        component: str,
        error_type: str,
        message: str,
        status: str = "alert",
    ) -> None:
        """Append a structured log line to the alert log file."""
        try:
            with open(self.log_path, "a") as f:
                f.write(
                    f"[{timestamp}] [{status.upper()}] [{component}] "
                    f"[{error_type}] {message}\n"
                )
        except OSError as e:
            logger.error("Failed to write alert log: %s", e)

    async def _send_daily_summary(self) -> None:
        """Compile and send a 24h summary to Telegram."""
        cutoff = time.time() - 86400  # last 24 hours
        window_start = cutoff

        # Count alerts by component
        alerts_by_component: dict[str, int] = defaultdict(int)
        recovery_count = 0
        error_count = 0
        for ts, comp, msg, err_type in _error_counts:
            if ts >= window_start:
                if err_type == "recovery":
                    recovery_count += 1
                else:
                    error_count += 1
                alerts_by_component[comp] += 1

        # Count alerts from history too
        for entry in _alert_history:
            if entry["time"] >= window_start:
                comp = entry["component"]
                alerts_by_component[comp] += 1

        # Build component status lines
        component_lines = []
        for comp, state in sorted(_component_states.items()):
            if state == "healthy":
                icon = "✅"
            elif state == "degraded":
                icon = "⚠️"
            else:
                icon = "❌"
            component_lines.append(f"{icon} {comp}: {state}")

        summary_lines = [
            "📊 *SignalForge Daily Summary*",
            f"Period: last 24 hours",
            "",
            f"Total alerts: {error_count + recovery_count}",
            f"  ❌ Errors: {error_count}",
            f"  ✅ Recoveries: {recovery_count}",
        ]

        if alerts_by_component:
            summary_lines.append("")
            summary_lines.append("*Breakdown by component:*")
            for comp, count in sorted(
                alerts_by_component.items(), key=lambda x: -x[1]
            ):
                summary_lines.append(f"  • {comp}: {count}")

        if component_lines:
            summary_lines.append("")
            summary_lines.append("*Current state:*")
            summary_lines.extend(component_lines)

        summary_lines.append("")
        summary_lines.append(f"Time: {_wib_now()} WIB")

        text = "\n".join(summary_lines)
        self._send_alert(text)
        logger.info("Daily summary sent")

    # ------------------------------------------------------------------
    # Static methods for external integration
    # ------------------------------------------------------------------

    @staticmethod
    def record_websocket_tick(symbol: str) -> None:
        """Record a WebSocket tick time so the monitor can detect disconnects.

        Parameters
        ----------
        symbol : str
            Trading symbol (e.g. ``"BTC/USDT"``).
        """
        _last_llm_tick[symbol] = time.time()
        # Mark component as healthy on tick
        comp = f"websocket:{symbol}"
        if _component_states.get(comp) in ("down", "degraded"):
            _component_states[comp] = "healthy"
            _component_consecutive_failures[comp] = 0

    @staticmethod
    def record_llm_success(model_name: str) -> None:
        """Reset the consecutive-failure counter for an LLM model.

        Parameters
        ----------
        model_name : str
            Model identifier (e.g. ``"gpt-4"``, ``"deepseek-chat"``).
        """
        comp = f"llm:{model_name}"
        _component_consecutive_failures[comp] = 0
        if _component_states.get(comp) == "down":
            _component_states[comp] = "healthy"

    @staticmethod
    def record_llm_failure(model_name: str, alerter: "ErrorAlerter | None" = None) -> None:
        """Increment the consecutive-failure counter for an LLM model.

        If there are 3+ consecutive failures, an alert is triggered.

        Parameters
        ----------
        model_name : str
            Model identifier (e.g. ``"gpt-4"``, ``"deepseek-chat"``).
        alerter : ErrorAlerter, optional
            If provided, ``report_error`` is called automatically on 3 failures.
        """
        comp = f"llm:{model_name}"
        _component_consecutive_failures[comp] += 1
        count = _component_consecutive_failures[comp]

        if count >= 3:
            _component_states[comp] = "down"
            if alerter is not None:
                alerter.report_error(
                    component=comp,
                    message=(
                        f"LLM failure: {count} consecutive errors for {model_name}"
                    ),
                    error_type="llm_failure",
                )

    @staticmethod
    def record_pipeline_crash(alerter: "ErrorAlerter", exception: Exception) -> None:
        """Immediately alert on a pipeline crash.

        Parameters
        ----------
        alerter : ErrorAlerter
            Instance to send the alert through.
        exception : Exception
            The exception that caused the crash.
        """
        alerter.report_error(
            component="pipeline",
            message=f"Pipeline crashed: {type(exception).__name__}: {exception}",
            error_type="crash",
        )
        _component_states["pipeline"] = "down"
