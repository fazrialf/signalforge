"""Cron script: Send SignalForge weekly report to Telegram.
Scheduled via Hermes cron every Sunday at 00:00 UTC (07:00 WIB Monday)."""
import sys, os, asyncio

sys.path.insert(0, '/home/ssm-user/signalforge')

# Patch asyncio
try:
    asyncio.get_running_loop()
except RuntimeError:
    pass

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DB_PATH
from delivery.telegram_bot import TelegramBot
from monitoring.weekly_report import send_weekly_report

async def main():
    bot = TelegramBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    success = await send_weekly_report(db_path=str(DB_PATH), bot=bot)
    if success:
        print("Weekly report sent successfully.")
    else:
        print("ERROR: Weekly report failed to send.")
        sys.exit(1)

if __name__ == '__main__':
    asyncio.run(main())
