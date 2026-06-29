"""
SignalForge Health Watchdog
Monitors system health every 5 minutes and alerts via Telegram on issues.
Also sends the daily status ping at 07:00 Jakarta (00:00 UTC).
"""
import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class HealthWatchdog:
    def __init__(self, bot, ws_feed, config: dict):
        """
        bot:       TelegramBot instance
        ws_feed:   WebSocketFeed instance (has .is_alive and .last_tick_age)
        config:    dict with watchdog settings
        """
        self.bot           = bot
        self.ws            = ws_feed
        self.config        = config
        self.start_time    = time.time()
        self._last_ping_day = None   # track which day we last sent the daily ping
        self._status_path  = Path(config.get("status_path", "config/build_status.json"))

    # ------------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------------
    async def run(self):
        """Runs forever. Checks health every WATCHDOG_INTERVAL seconds."""
        interval = self.config.get("watchdog_interval", 300)
        while True:
            await asyncio.sleep(interval)
            await self._check()
            await self._maybe_daily_ping()

    # ------------------------------------------------------------------
    # HEALTH CHECK
    # ------------------------------------------------------------------
    async def _check(self):
        issues = []

        # WebSocket alive?
        if not self.ws.is_alive:
            age = self.ws.last_tick_age
            issues.append(
                f"WebSocket stale: no tick for {age:.0f}s" if age else
                "WebSocket: never received a tick"
            )

        # Disk space
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024 ** 3)
        if free_gb < 1.0:
            issues.append(f"Low disk space: {free_gb:.1f} GB remaining")

        # RAM
        try:
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        mem[parts[0].rstrip(":")] = int(parts[1])
            total_ram = mem.get("MemTotal", 1)
            avail_ram = mem.get("MemAvailable", total_ram)
            ram_used_pct = (1 - avail_ram / total_ram) * 100
            if ram_used_pct > 85:
                issues.append(f"High RAM usage: {ram_used_pct:.0f}%")
        except Exception:
            pass

        # Send alert if any issues
        if issues:
            alert = "\u26a0\ufe0f <b>SignalForge Alert</b>\n" + "\n".join(f"\u2022 {i}" for i in issues)
            await self.bot.send_health_alert("\n".join(issues))
            logger.warning("Health issues: %s", issues)
        else:
            logger.debug("[Watchdog] All checks passed.")

    # ------------------------------------------------------------------
    # DAILY PING
    # ------------------------------------------------------------------
    async def _maybe_daily_ping(self):
        """Send the daily status ping once at 00:00 UTC (07:00 Jakarta)."""
        now = datetime.now(timezone.utc)
        today = now.date()
        ping_hour = self.config.get("daily_ping_hour_utc", 0)

        if now.hour == ping_hour and self._last_ping_day != today:
            self._last_ping_day = today
            await self._send_daily_ping()

    async def _send_daily_ping(self):
        uptime_h = (time.time() - self.start_time) / 3600

        ws_ok = self.ws.is_alive

        await self.bot.send_daily_ping(
            uptime_h      = uptime_h,
            ws_ok         = ws_ok,
            llm_ok        = True,
            news_ok       = True,
            signals_today = 0,
            delivered_today = 0,
        )
        logger.info("[Watchdog] Daily ping sent.")


if __name__ == "__main__":
    """Quick test: send one health message."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    from delivery.telegram_bot import TelegramBot

    class FakeWS:
        is_alive = True
        last_tick_age = 5.0

    async def test():
        bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        watchdog = HealthWatchdog(
            bot=bot, ws_feed=FakeWS(),
            config={"watchdog_interval": 300, "daily_ping_hour_utc": 0,
                    "status_path": "/home/ssm-user/signalforge/config/build_status.json"}
        )
        # Force a daily ping now
        await watchdog._send_daily_ping()
        print("Daily ping sent.")

    asyncio.run(test())
