"""
messaging/telegram.py — Telegram Platform Adapter
===================================================
settings.yaml config block:

messaging:
  platform: telegram
  primary_user_id: 6967671873   # Telegram user ID to receive system notifications

telegram:
  bot_token: YOUR_BOT_TOKEN
  webhook_url: https://yourdomain.com   # omit or leave empty to use polling instead
  allowed_user_ids:
    - 6967671873
"""

import logging
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler, filters, ContextTypes
)
from core.config_loader import get_settings
from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile

logger = logging.getLogger("messaging.telegram")

# Character limit per Telegram message
_MAX_LEN = 4096


class TelegramPlatform(MessagingPlatform):

    def __init__(self):
        self._app: Application | None = None
        self._message_callback = None   # set by main.py via set_message_handler()
        self._cfg = {}

    @property
    def name(self) -> str:
        return "telegram"

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self, app) -> None:
        self._cfg = get_settings().get("telegram", {})
        bot_token = self._cfg.get("bot_token")
        if not bot_token:
            raise ValueError("telegram.bot_token missing from settings.yaml")

        self._app = (
            Application.builder()
            .token(bot_token)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .build()
        )

        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message)
        )
        self._app.add_handler(
            MessageHandler(filters.Document.ALL, self._handle_file)
        )
        self._app.add_handler(CommandHandler("clear", self._cmd_clear))
        self._app.add_handler(CommandHandler("status", self._cmd_status))

        await self._app.initialize()
        await self._app.start()

        webhook_url = self._cfg.get("webhook_url", "").rstrip("/")
        if webhook_url:
            await self._app.bot.set_webhook(f"{webhook_url}/telegram")
            logger.info(f"Telegram webhook set: {webhook_url}/telegram")
        else:
            await self._app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            logger.info("Telegram polling started")

        # Store on FastAPI app state so webhook route can access it
        app.state.telegram_app = self._app

    async def stop(self) -> None:
        if self._app:
            await self._app.stop()
            logger.info("Telegram stopped")

    # ── Message sending ────────────────────────────────────────────────────

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self._app:
            return
        chunks = self._split_text(text, _MAX_LEN)
        for chunk in chunks:
            sent = False
            for parse_mode in ("MarkdownV2", "Markdown", "HTML", None):
                try:
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=chunk,
                        parse_mode=parse_mode,
                    )
                    if parse_mode != "MarkdownV2":
                        logger.info(f"Message sent with parse_mode={parse_mode}")
                    sent = True
                    break
                except Exception as e:
                    logger.warning(f"send_message failed with parse_mode={parse_mode}: {e}")
            if not sent:
                logger.error(f"All parse modes failed for chat_id={chat_id}, dropped. Preview: {chunk[:100]}")

    def _split_text(self, text: str, max_len: int) -> list[str]:
        if len(text) <= max_len:
            return [text]
        chunks = []
        while len(text) > max_len:
            split_at = text.rfind('\n', 0, max_len)
            if split_at == -1:
                split_at = text.rfind(' ', 0, max_len)
            if split_at == -1:
                split_at = max_len
            chunks.append(text[:split_at].strip())
            text = text[split_at:].strip()
        if text:
            chunks.append(text)
        return chunks

    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        if not self._app:
            return
        await self._app.bot.send_document(
            chat_id=int(chat_id),
            document=file.data,
            filename=file.filename,
            caption=file.caption[:1024] if file.caption else None,
        )

    async def send_typing(self, chat_id: str) -> None:
        if not self._app:
            return
        await self._app.bot.send_chat_action(
            chat_id=int(chat_id), action="typing"
        )

    # ── Authorization ──────────────────────────────────────────────────────

    def is_allowed(self, user_id: str) -> bool:
        allowed = self._cfg.get("allowed_user_ids", [])
        return not allowed or int(user_id) in allowed

    def _get_primary_user_id(self) -> str | None:
        allowed = self._cfg.get("allowed_user_ids", [])
        return str(allowed[0]) if allowed else None

    # ── Webhook registration ───────────────────────────────────────────────

    def register_webhook(self, api) -> None:
        from fastapi import Request

        @api.post("/telegram")
        async def telegram_webhook(request: Request):
            data = await request.json()
            update = Update.de_json(data, request.app.state.telegram_app.bot)
            await request.app.state.telegram_app.process_update(update)
            return {"ok": True}

    # ── Internal handlers ──────────────────────────────────────────────────

    def set_message_handler(self, callback) -> None:
        """Register the main message handler from main.py."""
        self._message_callback = callback

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = str(update.effective_user.id)
        if not self.is_allowed(user_id):
            await update.message.reply_text("Unauthorized.")
            return

        if self._message_callback:
            msg = IncomingMessage(
                user_id=user_id,
                text=update.message.text,
                chat_id=str(update.effective_chat.id),
                platform=self.name,
                raw=update,
            )
            await self._message_callback(msg, self)

    async def _handle_file(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = str(update.effective_user.id)
        if not self.is_allowed(user_id):
            return

        doc = update.message.document
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()
        caption = update.message.caption or f"Handle this file: {doc.file_name}"
        text = f"{caption}\n[File received: {doc.file_name}, {len(file_bytes)} bytes]"

        if self._message_callback:
            msg = IncomingMessage(
                user_id=user_id,
                text=text,
                chat_id=str(update.effective_chat.id),
                platform=self.name,
                raw=update,
            )
            await self._message_callback(msg, self)

    async def _cmd_clear(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        user_id = str(update.effective_user.id)
        if self._message_callback:
            msg = IncomingMessage(
                user_id=user_id,
                text="__clear__",
                chat_id=str(update.effective_chat.id),
                platform=self.name,
                raw=update,
            )
            await self._message_callback(msg, self)
        await update.message.reply_text("Conversation history cleared.")

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not self.is_allowed(str(update.effective_user.id)):
            return
        from core import model_router
        models = model_router.get_all_model_status()
        lines = ["*Model Status*"]
        for m in models:
            if m.get("is_fallback"):
                icon, label = "🔐", "fallback"
            elif m["status"] == "available":
                icon, label = "✅", m["status"]
            elif m["status"] == "rate_limited":
                icon, label = "⏳", m["status"]
            else:
                icon, label = "❌", m["status"]
            reset = f" (resets: {m['rate_limit_reset_at']})" if m["rate_limit_reset_at"] else ""
            lines.append(f"{icon} `{m['name']}` — {label}{reset}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
