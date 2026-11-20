"""Telegram alert channel for Quantum Simons.

Tokens never go to browser or version control. On free Render, settings entered via
the website are intentionally kept in RAM (lost on restart). For durable settings
set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in Render's Environment dashboard.
"""
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

TOKEN_RE = re.compile(r"^[0-9]{6,15}:[A-Za-z0-9_-]{25,160}$")
CHAT_RE = re.compile(r"^(?:-?[0-9]{1,20}|@[a-zA-Z][a-zA-Z0-9_]{4,31})$")
ALLOWED_MARKETS = ("XAUUSD", "BTCUSD")


class TelegramError(Exception):
    pass


class TelegramAlerts:
    def __init__(self):
        self._env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self._env_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._token = self._env_token if self._env_token and self._env_chat else ""
        self._chat = self._env_chat if self._token else ""
        self.enabled = bool(self._token and self._chat)
        self.markets = set(ALLOWED_MARKETS)
        self.username = ""
        self.last_sent = None
        self.last_error = None
        self.sent_count = 0
        self.failure_count = 0
        self._source = "environment" if self.enabled else "none"

    @staticmethod
    def _check_token(token):
        if not TOKEN_RE.fullmatch(token):
            raise TelegramError("صيغة Bot Token غير صحيحة. انسخه كاملًا من BotFather.")

    @staticmethod
    def _check_chat(chat_id):
        if not CHAT_RE.fullmatch(chat_id):
            raise TelegramError("Chat ID غير صالح. استخدم رقم المحادثة أو معرّف قناة عامة.")

    async def _call(self, token, method, data=None):
        self._check_token(token)
        # The host is fixed. Token is never sent to an arbitrary URL.
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
                response = await client.post(url, json=data or {})
                result = response.json()
        except (httpx.HTTPError, ValueError):
            raise TelegramError("تعذر الوصول إلى خوادم Telegram. أعد المحاولة.")
        if not isinstance(result, dict) or not result.get("ok"):
            error = (result.get("description") or "") if isinstance(result, dict) else ""
            error = error.lower()
            if "chat not found" in error:
                raise TelegramError("لم يتم العثور على المحادثة. افتح البوت في تيليجرام وأرسل /start أولاً، ثم تحقّق من Chat ID.")
            if "bot was blocked" in error or "forbidden" in error:
                raise TelegramError("تيليجرام رفض الرسالة. أزل حظر البوت وأرسل /start له.")
            if "unauthorized" in error or response.status_code in (401, 404):
                raise TelegramError("Bot Token غير صالح. أنشئ أو راجع رمز البوت عبر BotFather.")
            if "webhook" in error or response.status_code == 409:
                raise TelegramError("لا يمكن اكتشاف المحادثات لأن البوت لديه Webhook فعال في تطبيق آخر.")
            if response.status_code == 429:
                raise TelegramError("حد إرسال تيليجرام مؤقتًا؛ أعد المحاولة لاحقًا.")
            raise TelegramError("رفض Telegram الطلب. تحقق من الرمز ومعرّف المحادثة وصلاحيات البوت.")
        return result.get("result")

    async def discover(self, token=None):
        candidate = (token or "").strip() or self._token
        if not candidate:
            raise TelegramError("أدخل Bot Token أولًا.")
        bot = await self._call(candidate, "getMe")
        updates = await self._call(candidate, "getUpdates", {"timeout": 0, "limit": 100})
        chats = {}
        for entry in updates or []:
            msg = entry.get("message") or entry.get("channel_post") or entry.get("edited_message") or entry.get("edited_channel_post")
            if msg is None:
                callback = entry.get("callback_query") or {}
                msg = callback.get("message")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))
            if not chat_id:
                continue
            title = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username") or "محادثة"
            chats[chat_id] = {"id": chat_id, "name": title[:80], "type": chat.get("type", "")}
        return {"bot_username": bot.get("username", ""), "chats": list(chats.values())[-20:]}

    async def configure(self, token, chat_id, markets=None):
        candidate = (token or "").strip() or self._token
        chat = (chat_id or "").strip()
        if not candidate:
            raise TelegramError("أدخل Bot Token أولًا.")
        self._check_chat(chat)
        market_set = set(markets or ALLOWED_MARKETS)
        if not market_set or not market_set.issubset(set(ALLOWED_MARKETS)):
            raise TelegramError("اختر الذهب أو البيتكوين أو كليهما.")
        bot = await self._call(candidate, "getMe")
        await self._call(candidate, "sendMessage", {
            "chat_id": chat,
            "text": "✅ تم ربط Quantum Simons بنجاح.\nستصل إشارات BUY وSELL عند تحقق الشروط ووصول بيانات cTrader الحقيقية. لا توجد صفقات مضمونة.\nملاحظة: على Render المجاني قد تنقطع المراقبة عند خمول الخادم."
        })
        self._token = candidate
        self._chat = chat
        self.username = bot.get("username", "")
        self.markets = market_set
        self.enabled = True
        self._source = "environment" if candidate == self._env_token and chat == self._env_chat else "session"
        self.last_error = None
        return self.public_status()

    def public_status(self):
        chat = self._chat
        if not chat:
            masked = ""
        elif len(chat) <= 5:
            masked = "***"
        else:
            masked = chat[:3] + "•••" + chat[-3:]
        return {
            "configured": bool(self._token and self._chat),
            "enabled": self.enabled,
            "bot_username": self.username,
            "chat_masked": masked,
            "markets": sorted(self.markets),
            "source": self._source,
            "persistent": self._source == "environment",
            "last_sent": self.last_sent,
            "last_error": self.last_error,
            "sent_count": self.sent_count,
            "failure_count": self.failure_count,
        }

    async def send_test(self):
        if not self._token or not self._chat:
            raise TelegramError("اربط البوت أولًا.")
        await self._call(self._token, "sendMessage", {
            "chat_id": self._chat,
            "text": "🔔 Quantum Simons — رسالة اختبار.\n✅ البوت قادر على إرسال التنبيهات إلى هذه المحادثة.\nهذه ليست توصية شراء أو بيع."
        })
        return {"ok": True}

    def toggle(self, enabled):
        if enabled and not self._token:
            raise TelegramError("أكمل ربط البوت أولًا.")
        self.enabled = bool(enabled)
        return self.public_status()

    async def send_signal(self, record):
        if not self.enabled or not self._token or not self._chat:
            return False
        if record.get("market") not in self.markets:
            return False
        side = record.get("side")
        if side not in ("BUY", "SELL"):
            return False
        icon = "🟢" if side == "BUY" else "🔴"
        price = lambda x: f"{float(x):,.5f}" if x is not None else "—"
        timestamp = datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")
        txt = (
            f"{icon} Quantum Simons • {side}\n"
            f"السوق: {record.get('market', '—')}\n"
            f"النظام: {record.get('strategy', '—')}\n"
            f"سبب الإشارة: {record.get('reason', '—')}\n"
            f"الدخول التقديري: {price(record.get('entry'))}\n"
            f"وقف الخسارة: {price(record.get('stop'))}\n"
            f"الهدف: {price(record.get('target'))}\n"
            f"العائد/المخاطرة: {record.get('rr', '—')}:1\n"
            f"الوضع: {record.get('mode_label', '—')}\n"
            f"الوقت (السعودية): {timestamp}\n"
            "ملاحظة: إشارة إحصائية تجريبية وليست ضمان ربح."
        )
        try:
            await self._call(self._token, "sendMessage", {
                "chat_id": self._chat,
                "text": txt,
                "disable_web_page_preview": True,
            })
            self.sent_count += 1
            self.last_sent = datetime.now(timezone.utc).isoformat()
            self.last_error = None
            return True
        except TelegramError as err:
            self.failure_count += 1
            self.last_error = str(err)
            return False
