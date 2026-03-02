"""
messaging/slack.py — Slack Platform Adapter
============================================
settings.yaml config block:

messaging:
  platform: slack
  primary_user_id: "U1234567890"   # Slack member ID (starts with U)

slack:
  bot_token: xoxb-YOUR-BOT-TOKEN          # Bot User OAuth Token (xoxb-...)
  signing_secret: YOUR_SIGNING_SECRET     # From Slack App → Basic Information
  allowed_user_ids:                        # Slack member IDs (starts with U)
    - "U1234567890"
  default_channel: "C1234567890"          # Default channel ID for notifications

Setup:
  1. Go to https://api.slack.com/apps → Create New App → From Scratch
  2. Under OAuth & Permissions → Bot Token Scopes, add:
       chat:write, im:read, im:write, im:history,
       channels:history, files:write, users:read
  3. Install app to workspace → copy Bot User OAuth Token → slack.bot_token
  4. Under Basic Information → App Credentials → copy Signing Secret → slack.signing_secret
  5. Under Event Subscriptions → Enable Events:
       Request URL: https://yourdomain.com/slack/events
       Subscribe to bot events: message.im, message.channels
  6. Under App Home → enable Messages Tab
  7. Reinstall app if prompted after permission changes

To find user/channel IDs:
  - Right-click any user or channel in Slack → Copy link → ID is in the URL

Requirements:
  pip install slack-sdk
"""

import logging
import hmac
import hashlib
import time
from core.config_loader import get_settings
from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile

logger = logging.getLogger("messaging.slack")

_MAX_LEN = 3000  # Slack has a 3001 char limit per block


class SlackPlatform(MessagingPlatform):

    def __init__(self):
        self._client = None
        self._message_callback = None
        self._cfg = {}

    @property
    def name(self) -> str:
        return "slack"

    async def start(self, app) -> None:
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            raise ImportError("slack-sdk not installed. Run: pip install slack-sdk")

        self._cfg = get_settings().get("slack", {})
        bot_token = self._cfg.get("bot_token")
        if not bot_token:
            raise ValueError("slack.bot_token missing from settings.yaml")

        self._client = AsyncWebClient(token=bot_token)

        # Verify connection
        try:
            auth = await self._client.auth_test()
            logger.info(f"Slack connected: {auth['user']} in {auth['team']}")
        except Exception as e:
            raise ValueError(f"Slack auth failed: {e}")

        app.state.slack_client = self._client

    async def stop(self) -> None:
        logger.info("Slack stopped")

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self._client:
            return
        for i in range(0, len(text), _MAX_LEN):
            await self._client.chat_postMessage(
                channel=chat_id,
                text=text[i:i + _MAX_LEN],
                mrkdwn=True,
            )

    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        if not self._client:
            return
        await self._client.files_upload_v2(
            channel=chat_id,
            content=file.data,
            filename=file.filename,
            initial_comment=file.caption or "",
        )

    async def send_typing(self, chat_id: str) -> None:
        # Slack doesn't support typing indicators via bot API
        pass

    def is_allowed(self, user_id: str) -> bool:
        allowed = self._cfg.get("allowed_user_ids", [])
        return not allowed or user_id in [str(u) for u in allowed]

    def _get_primary_user_id(self) -> str | None:
        cfg = get_settings().get("messaging", {})
        return str(cfg.get("primary_user_id")) if cfg.get("primary_user_id") else None

    async def notify(self, text: str) -> None:
        """Send DM to primary user or default channel."""
        primary = self._get_primary_user_id()
        channel = primary or self._cfg.get("default_channel")
        if channel:
            await self.send_message(channel, text)

    def _verify_signature(self, body: bytes, timestamp: str, signature: str) -> bool:
        """Verify Slack request signature to prevent spoofing."""
        signing_secret = self._cfg.get("signing_secret", "")
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False  # Request too old
        base = f"v0:{timestamp}:{body.decode()}"
        expected = "v0=" + hmac.new(
            signing_secret.encode(), base.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def register_webhook(self, api) -> None:
        from fastapi import Request, Response

        @api.post("/slack/events")
        async def slack_events(request: Request):
            body = await request.body()
            data = await request.json()

            # Handle Slack URL verification challenge
            if data.get("type") == "url_verification":
                return {"challenge": data["challenge"]}

            # Verify signature
            timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
            signature = request.headers.get("X-Slack-Signature", "")
            if not self._verify_signature(body, timestamp, signature):
                return Response(status_code=403)

            event = data.get("event", {})
            event_type = event.get("type")

            # Only handle actual messages (not bot messages or edits)
            if event_type in ("message", "app_mention"):
                if event.get("bot_id") or event.get("subtype"):
                    return {"ok": True}

                user_id = event.get("user", "")
                text = event.get("text", "").strip()
                channel = event.get("channel", "")

                if not self.is_allowed(user_id):
                    return {"ok": True}

                if text and self._message_callback:
                    msg = IncomingMessage(
                        user_id=user_id,
                        text=text,
                        chat_id=channel,
                        platform=self.name,
                        raw=event,
                    )
                    import asyncio
                    asyncio.create_task(self._message_callback(msg, self))

            return {"ok": True}

    def set_message_handler(self, callback) -> None:
        self._message_callback = callback
