"""
SignalForge Telegram Delivery
Handles all outbound messages to the user.
"""
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _html_escape(text: str) -> str:
    """Escape characters that break Telegram HTML parse_mode.

    Telegram HTML only recognises <, >, & as special — apostrophes and
    quotes are safe.  Escaping them prevents 'Bad Request: can't parse
    entities' 400 errors when LLM reasoning contains raw < > & chars.
    """
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class TelegramBot:
    def __init__(self, token: str, chat_id: str | list[str]):
        """
        token: Telegram bot token
        chat_id: primary chat id (str) OR list of chat ids for dual/multi delivery.
                 First entry is primary (backward-compatible self.chat_id).
        """
        self.token = token
        if isinstance(chat_id, (list, tuple, set)):
            seen: set[str] = set()
            self.chat_ids: list[str] = []
            for c in chat_id:
                s = str(c).strip()
                if s and s not in seen:
                    self.chat_ids.append(s)
                    seen.add(s)
        else:
            self.chat_ids = [str(chat_id).strip()] if str(chat_id).strip() else []
        # Primary chat — used by ErrorAlerter and any single-chat callers
        self.chat_id = self.chat_ids[0] if self.chat_ids else ""
        self.base = f"https://api.telegram.org/bot{token}"
        self._session: aiohttp.ClientSession | None = None
        logger.info(
            "[Telegram] delivery targets=%s (primary=%s)",
            self.chat_ids, self.chat_id,
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return the persistent session, creating it if necessary."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the persistent session on shutdown."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _send_one(
        self, chat_id: str, text: str, parse_mode: str = "HTML"
    ) -> bool:
        """Send to a single chat_id with 1x retry. Returns True on success."""
        url = f"{self.base}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        # L-2: try up to 2 times (initial + 1 retry) with 2s gap
        for attempt in range(2):
            try:
                session = await self._get_session()
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    logger.error(
                        "Telegram send failed chat=%s (attempt %d/2) status=%s body=%s | msg_preview=%s",
                        chat_id, attempt + 1, resp.status, body,
                        text[:80].replace("\n", " "),
                    )
                    if attempt == 0:
                        await asyncio.sleep(2)
            except Exception as e:
                logger.error(
                    "Telegram exception chat=%s (attempt %d/2) type=%s error=%s | msg_preview=%s",
                    chat_id, attempt + 1, type(e).__name__, repr(e),
                    text[:80].replace("\n", " "),
                )
                if attempt == 0:
                    await asyncio.sleep(2)
        return False

    async def send(
        self,
        text: str,
        parse_mode: str = "HTML",
        chat_id: str | None = None,
    ) -> bool:
        """
        Send a message.

        - chat_id set  → single-target (command replies stay in that chat)
        - chat_id None → fan-out to ALL configured chats (signals/alerts)

        Returns True if at least one destination accepted the message.
        """
        if chat_id is not None:
            targets = [str(chat_id)]
        else:
            targets = list(self.chat_ids)

        if not targets:
            logger.error("Telegram send aborted — no chat_id configured")
            return False

        results = await asyncio.gather(
            *[self._send_one(cid, text, parse_mode) for cid in targets]
        )
        ok = any(results)
        if ok and len(targets) > 1:
            logger.info(
                "Telegram fan-out: %d/%d targets ok | targets=%s",
                sum(1 for r in results if r), len(targets), targets,
            )
        return ok

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict]:
        """Long-poll for incoming messages. Returns list of updates."""
        url = f"{self.base}/getUpdates"
        params = {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        try:
            session = await self._get_session()
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=timeout + 5)
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("result", [])
        except Exception as e:
            logger.error("Telegram getUpdates exception: %s", e)
            return []

    async def start_polling(self, cmd_handler, poll_interval: float = 1.0):
        """
        Long-poll Telegram for incoming commands and dispatch them.

        Accepts commands from ANY configured chat (DM or group).
        Command replies are sent only to the originating chat (not fan-out).
        """
        last_update_id = 0
        allowed = set(self.chat_ids)
        logger.info(
            "[POLL] Starting Telegram command polling (allowed chats=%s)...",
            sorted(allowed),
        )
        while True:
            try:
                updates = await self.get_updates(offset=last_update_id + 1, timeout=30)
                for update in updates:
                    if "message" not in update:
                        continue
                    msg = update["message"]
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    # Only accept commands from configured chats
                    if chat_id not in allowed:
                        continue
                    text = msg.get("text", "").strip()
                    if not text.startswith("/"):
                        continue
                    parts = text.split()
                    command = parts[0].lstrip("/").split("@")[0].lower()
                    args = parts[1:]
                    result = await cmd_handler.handle_command(command, args)
                    # Reply only to the chat that issued the command
                    await self.send(result, chat_id=chat_id)
                    last_update_id = update["update_id"]
            except asyncio.CancelledError:
                logger.info("[POLL] Polling cancelled.")
                break
            except Exception as e:
                logger.error("[POLL] Polling error: %s", e)
                await asyncio.sleep(5)
            await asyncio.sleep(poll_interval)

    async def send_signal(self, signal: dict) -> bool:
        """Format and send a trading signal with enriched market context."""
        d = signal
        is_long = d["direction"] == "BUY"
        direction_icon = "\U0001f7e2" if is_long else "\U0001f534"  # 🟢 / 🔴
        direction_word = "LONG" if is_long else "SHORT"
        symbol = d["symbol"]
        score = d.get("confluence_score", 0)
        conf = d.get("confidence", 0)
        rr = d.get("rr_ratio", 0)

        # Signal quality badge
        if score >= 15:
            badge = "\U0001f3c6 PREMIUM"
        elif score >= 11:
            badge = "\u2b50 STRONG"
        elif score >= 8:
            badge = "\u2705 STANDARD"
        else:
            badge = "\U0001f7e1 BORDERLINE"

        # Confidence bar (5 blocks)
        filled = round(conf / 20)
        conf_bar = "\u2588" * filled + "\u2591" * (5 - filled)

        # Market context block
        onchain = d.get("onchain", {})
        fr = onchain.get("funding_rate", 0.0)
        oi_chg = onchain.get("oi_change_pct", 0.0)
        ls_ratio = onchain.get("long_short_ratio", 1.0)
        taker_ratio = onchain.get("taker_buy_ratio", 0.5)
        news_sent = d.get("news_sentiment", "neutral")
        news_icon = {"bullish": "\U0001f4c8", "bearish": "\U0001f4c9", "neutral": "\u27a1\ufe0f"}.get(news_sent, "\u27a1\ufe0f")
        fr_icon = "\U0001f7e2" if fr > 0.0001 else ("\U0001f534" if fr < -0.0001 else "\u26aa")
        oi_icon = "\u2191" if oi_chg > 1 else ("\u2193" if oi_chg < -1 else "\u2194")

        # Confluence factors summary (top 5)
        detail = d.get("confluence_detail", {})
        factor_lines = ""
        all_factors = []
        for tier_items in detail.values():
            all_factors.extend(tier_items)
        if all_factors:
            factor_lines = "\n".join(f"  \u2022 {_html_escape(f)}" for f in all_factors[:5])
        else:
            factor_lines = "  \u2022 No breakdown available"

        text = (
            f"{direction_icon} <b>SIGNAL \u2014 {direction_word} {symbol}</b>  {badge}\n"
            + "\u2500" * 22 + "\n"
            + f"\n"
            f"<b>\U0001f4cd Entry</b>   <code>${d['entry_price']:,.4f}</code>\n"
            f"<b>\U0001f7e2 TP1</b>     <code>${d['tp1']:,.4f}</code>  <i>({d.get('tp1_pct', 0):+.2f}%)</i>  \u2192 40% out\n"
            f"<b>\U0001f7e1 TP2</b>     <code>${d['tp2']:,.4f}</code>  <i>({d.get('tp2_pct', 0):+.2f}%)</i>  \u2192 30% out\n"
            f"<b>\U0001f535 TP3</b>     <code>${d['tp3']:,.4f}</code>  <i>({d.get('tp3_pct', 0):+.2f}%)</i>  \u2192 30% out\n"
            f"<b>\U0001f6d1 SL</b>      <code>${d['sl']:,.4f}</code>  <i>({d.get('sl_pct', 0):+.2f}%)</i>\n"
            f"\n"
            f"\U0001f4ca <b>R:R</b> {rr:.2f}  \u2502  <b>Conf</b> {conf}% [{conf_bar}]  \u2502  <b>Risk</b> {d.get('risk_pct', 1.0):.1f}%\n"
            f"\U0001f552 <b>TF:</b> {d.get('bias_tf', '15m')} bias \u2192 {d.get('entry_tf', '5m')} entry\n"
            f"\n"
            f"\U0001f30a <b>Market Context</b>\n"
            f"  {fr_icon} Funding: <code>{fr:+.4%}</code>   {oi_icon} OI: <code>{oi_chg:+.1f}%</code>\n"
            f"  \U0001f465 L/S: <code>{ls_ratio:.2f}</code>   \U0001f3b2 Taker Buy: <code>{taker_ratio:.0%}</code>\n"
            f"  {news_icon} News: <b>{news_sent.capitalize()}</b>\n"
            f"\n"
            f"\U0001f9e0 <b>Confluence {score} \u2014 {badge}</b>\n"
            f"{factor_lines}\n"
            f"\n"
            f"\U0001f4dd <b>Analysis:</b>\n<i>{_html_escape(d.get('reasoning', ''))}</i>\n"
            f"\n"
            f"\u26a0\ufe0f <b>Key Risk:</b> {_html_escape(d.get('primary_risk', ''))}\n"
            f"\u23f0 <i>Valid {d.get('expiry_hours', 4)}h</i>  \u2502  \U0001f4cc Reply <b>entered</b> / <b>skip</b>"
        )
        return await self.send(text)

    async def send_tp_hit(self, symbol: str, direction: str, tp_level: int,
                           entry: float, tp_price: float, pnl_pct: float) -> bool:
        text = (
            f"\U0001f3af <b>TP{tp_level} HIT \u2014 {symbol} {'LONG' if direction=='BUY' else 'SHORT'}</b>\n"
            f"Entry: ${entry:,.2f} \u2192 TP{tp_level}: ${tp_price:,.2f} \u2705\n"
            f"P&L: <b>{pnl_pct:+.2f}%</b>\n"
            f"\n"
            f"\U0001f4cc Partial close: {40 if tp_level==1 else 30}% of position\n"
            f"\U0001f4cc SL moved to breakeven: ${entry:,.2f}"
        )
        return await self.send(text)

    async def send_sl_hit(self, symbol: str, direction: str,
                           entry: float, sl_price: float, pnl_pct: float) -> bool:
        text = (
            f"\U0001f6d1 <b>SL HIT \u2014 {symbol} {'LONG' if direction=='BUY' else 'SHORT'}</b>\n"
            f"Entry: ${entry:,.2f} \u2192 SL: ${sl_price:,.2f} \u274c\n"
            f"P&L: <b>{pnl_pct:+.2f}%</b>\n"
            f"\n"
            f"\u23f8 Cooldown: 2 hours active\n"
            f"\U0001f4ca Post-mortem in weekly report"
        )
        return await self.send(text)

    async def send_health_alert(self, issue: str) -> bool:
        text = (
            f"\u26a0\ufe0f <b>SignalForge Alert</b>\n"
            f"{issue}\n"
            f"<i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>"
        )
        return await self.send(text)

    async def send_daily_ping(self, uptime_h: float, ws_ok: bool,
                               llm_ok: bool, news_ok: bool,
                               signals_today: int, delivered_today: int) -> bool:
        ws_icon   = "✅" if ws_ok  else "❌"
        llm_icon  = "✅" if llm_ok else "❌"
        news_icon = "✅" if news_ok else "❌"

        text = (
            f"✅ <b>SignalForge Status</b> — "
            f"{datetime.now(timezone.utc).strftime('%d %b %Y %H:%M')} UTC\n"
            f"────────────────────\n"
            f"Uptime: <b>{uptime_h:.1f}h</b>\n"
            f"WebSocket: {ws_icon}  LLM API: {llm_icon}  News: {news_icon}\n"
            f"\n"
            f"📊 Signals today: <b>{signals_today}</b>  |  Delivered: <b>{delivered_today}</b>\n"
        )
        return await self.send(text)

