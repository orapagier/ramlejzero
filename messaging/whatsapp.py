"""
messaging/whatsapp.py — WhatsApp Platform Adapter (via Twilio)
==============================================================
Uses Twilio's WhatsApp Business API. Twilio is the most reliable
hosted option — no need to run your own WhatsApp Business Platform server.

settings.yaml config block:

messaging:
  platform: whatsapp
  primary_user_id: "+639171234567"   # WhatsApp number with country code

whatsapp:
  account_sid: ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX   # Twilio Console → Account SID
  auth_token: your_auth_token                       # Twilio Console → Auth Token
  from_number: "whatsapp:+14155238886"              # Twilio sandbox or approved number
  allowed_user_ids:                                  # WhatsApp numbers with country code
    - "+639171234567"

Setup (Twilio Sandbox for testing):
  1. Sign up at https://www.twilio.com/console
  2. Under Messaging → Try it out → Send a WhatsApp message
  3. Follow sandbox join instructions (send "join <word>" to sandbox number)
  4. Copy Account SID and Auth Token from Console Dashboard
  5. Under Messaging → Settings → WhatsApp Sandbox Settings:
       When a message comes in: https://yourdomain.com/whatsapp/webhook
  6. The sandbox number is whatsapp:+14155238886

Setup (Production WhatsApp Business):
  1. Apply for WhatsApp Business API at https://www.twilio.com/whatsapp/request-access
  2. Once approved, a dedicated WhatsApp number is assigned
  3. Update from_number to your approved number: "whatsapp:+1XXXXXXXXXX"

Requirements:
  pip install twilio
"""

import logging
from core.config_loader import get_settings
from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile

logger = logging.getLogger("messaging.whatsapp")

_MAX_LEN = 1600  # WhatsApp/Twilio message character limit


class WhatsAppPlatform(MessagingPlatform):

    def __init__(self):
        self._client = None
        self._message_callback = None
        self._cfg = {}

    @property
    def name(self) -> str:
        return "whatsapp"

    async def start(self, app) -> None:
        try:
            from twilio.rest import Client
        except ImportError:
            raise ImportError("twilio not installed. Run: pip install twilio")

        self._cfg = get_settings().get("whatsapp", {})
        account_sid = self._cfg.get("account_sid")
        auth_token = self._cfg.get("auth_token")

        if not account_sid or not auth_token:
            raise ValueError(
                "whatsapp.account_sid and whatsapp.auth_token required in settings.yaml"
            )

        self._client = Client(account_sid, auth_token)
        logger.info("WhatsApp (Twilio) adapter ready (webhook mode)")

    async def stop(self) -> None:
        logger.info("WhatsApp stopped")

    async def send_message(self, chat_id: str, text: str) -> None:
        """chat_id = WhatsApp number like '+639171234567'"""
        if not self._client:
            return
        from_number = self._cfg.get("from_number", "")
        to = f"whatsapp:{chat_id}" if not chat_id.startswith("whatsapp:") else chat_id

        # Twilio is synchronous — run in thread to avoid blocking
        import asyncio
        for i in range(0, len(text), _MAX_LEN):
            chunk = text[i:i + _MAX_LEN]
            await asyncio.to_thread(
                self._client.messages.create,
                body=chunk,
                from_=from_number,
                to=to,
            )

    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        """
        WhatsApp via Twilio supports media URLs but not direct binary uploads.
        Notify user the file is available via dashboard instead.
        """
        note = (
            f"📎 *File ready:* `{file.filename}`\n"
            f"_{file.caption or 'Generated file'}_\n\n"
            f"_(Download via the dashboard.)_"
        )
        await self.send_message(chat_id, note)

    async def send_typing(self, chat_id: str) -> None:
        # WhatsApp via Twilio doesn't support typing indicators
        pass

    def is_allowed(self, user_id: str) -> bool:
        allowed = self._cfg.get("allowed_user_ids", [])
        # Normalize: strip whatsapp: prefix for comparison
        normalized = [str(u).replace("whatsapp:", "") for u in allowed]
        user_normalized = user_id.replace("whatsapp:", "")
        return not normalized or user_normalized in normalized

    def _get_primary_user_id(self) -> str | None:
        cfg = get_settings().get("messaging", {})
        return str(cfg.get("primary_user_id")) if cfg.get("primary_user_id") else None

    def register_webhook(self, api) -> None:
        from fastapi import Request, Response

        @api.post("/whatsapp/webhook")
        async def whatsapp_webhook(request: Request):
            # Twilio sends form-encoded data
            form = await request.form()
            text = form.get("Body", "").strip()
            from_number = form.get("From", "").replace("whatsapp:", "")

            if not self.is_allowed(from_number):
                logger.warning(f"WhatsApp: unauthorized number {from_number}")
                # Return empty TwiML to avoid error
                return Response(
                    content='<?xml version="1.0"?><Response></Response>',
                    media_type="application/xml"
                )

            if text and self._message_callback:
                msg = IncomingMessage(
                    user_id=from_number,
                    text=text,
                    chat_id=from_number,
                    platform=self.name,
                    raw=dict(form),
                )
                import asyncio
                asyncio.create_task(self._message_callback(msg, self))

            # Return empty TwiML — we reply via REST API, not TwiML
            return Response(
                content='<?xml version="1.0"?><Response></Response>',
                media_type="application/xml"
            )

    def set_message_handler(self, callback) -> None:
        self._message_callback = callback
