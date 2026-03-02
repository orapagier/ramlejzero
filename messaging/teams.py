"""
messaging/teams.py — Microsoft Teams Platform Adapter
======================================================
Uses the Bot Framework SDK to receive messages and the Teams REST API to send them.

settings.yaml config block:

messaging:
  platform: teams
  primary_user_id: "29:1AbCdEf..."   # Teams user MRI (from activity.from.id)

teams:
  app_id: YOUR_MICROSOFT_APP_ID           # Azure Bot → Configuration → Microsoft App ID
  app_password: YOUR_MICROSOFT_APP_PASSWORD  # Azure Bot → Configuration → Client Secret
  allowed_user_ids:                        # Teams user MRI strings (copy from logs on first message)
    - "29:1AbCdEfGhIjKlMnOpQrStUvWxYz"

Setup:
  1. Go to https://portal.azure.com → Create a resource → Azure Bot
  2. Fill in bot handle, choose F0 (free) tier
  3. Under Configuration → copy Microsoft App ID → teams.app_id
  4. Under Configuration → Manage Password → New client secret → teams.app_password
  5. Under Channels → Add Microsoft Teams channel
  6. Under Configuration → Messaging endpoint:
       https://yourdomain.com/teams/messages
  7. In your Teams client, search for your bot and start a chat
  8. Check your logs for the user MRI (activity.from.id) → add to allowed_user_ids

Requirements:
  pip install botframework-connector aiohttp

Notes:
  - Teams messages arrive via webhook (POST to /teams/messages)
  - Replies use the Bot Framework connector service
  - File uploads in Teams require SharePoint/Graph API integration (complex)
    so send_file() sends the filename as text with a note instead
"""

import logging
import json
import time
import aiohttp
from core.config_loader import get_settings
from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile

logger = logging.getLogger("messaging.teams")

_TOKEN_URL = "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"


class TeamsPlatform(MessagingPlatform):

    def __init__(self):
        self._message_callback = None
        self._cfg = {}
        self._token: str | None = None
        self._token_expires: float = 0

    @property
    def name(self) -> str:
        return "teams"

    async def start(self, app) -> None:
        self._cfg = get_settings().get("teams", {})
        if not self._cfg.get("app_id"):
            raise ValueError("teams.app_id missing from settings.yaml")
        if not self._cfg.get("app_password"):
            raise ValueError("teams.app_password missing from settings.yaml")
        logger.info("Microsoft Teams adapter ready (webhook mode)")

    async def stop(self) -> None:
        logger.info("Teams stopped")

    # ── OAuth token for Bot Framework ──────────────────────────────────────

    async def _get_token(self) -> str:
        """Get or refresh Bot Framework OAuth token."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token

        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": self._cfg["app_id"],
                "client_secret": self._cfg["app_password"],
                "scope": _BOT_FRAMEWORK_SCOPE,
            }) as resp:
                data = await resp.json()
                self._token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 3600)
                return self._token

    # ── Sending ────────────────────────────────────────────────────────────

    async def send_message(self, chat_id: str, text: str) -> None:
        """
        chat_id format: "service_url|conversation_id|activity_id"
        This is constructed in register_webhook from the incoming activity.
        """
        try:
            parts = chat_id.split("|")
            if len(parts) < 2:
                logger.warning(f"Teams: invalid chat_id format: {chat_id}")
                return

            service_url = parts[0]
            conversation_id = parts[1]
            reply_to_id = parts[2] if len(parts) > 2 else None

            token = await self._get_token()
            url = f"{service_url}v3/conversations/{conversation_id}/activities"
            if reply_to_id:
                url += f"/{reply_to_id}"

            # Teams has a ~28k char limit but splitting at 4000 is safe
            chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
            async with aiohttp.ClientSession() as session:
                for chunk in chunks:
                    payload = {
                        "type": "message",
                        "text": chunk,
                        "textFormat": "markdown",
                    }
                    async with session.post(
                        url,
                        json=payload,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                    ) as resp:
                        if resp.status not in (200, 201):
                            body = await resp.text()
                            logger.error(f"Teams send failed {resp.status}: {body}")
        except Exception as e:
            logger.error(f"Teams send_message error: {e}")

    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        """
        Full file upload in Teams requires SharePoint/Graph API integration.
        For now, notify the user and describe the file.
        """
        note = (
            f"📎 *File ready:* `{file.filename}`\n"
            f"_{file.caption or 'Generated file'}_\n\n"
            f"_(File upload in Teams requires SharePoint integration. "
            f"Download via the dashboard instead.)_"
        )
        await self.send_message(chat_id, note)

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator via Bot Framework."""
        try:
            parts = chat_id.split("|")
            if len(parts) < 2:
                return
            service_url, conversation_id = parts[0], parts[1]
            token = await self._get_token()
            url = f"{service_url}v3/conversations/{conversation_id}/activities"
            async with aiohttp.ClientSession() as session:
                await session.post(
                    url,
                    json={"type": "typing"},
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                )
        except Exception:
            pass

    # ── Authorization ──────────────────────────────────────────────────────

    def is_allowed(self, user_id: str) -> bool:
        allowed = self._cfg.get("allowed_user_ids", [])
        return not allowed or user_id in [str(u) for u in allowed]

    def _get_primary_user_id(self) -> str | None:
        cfg = get_settings().get("messaging", {})
        return str(cfg.get("primary_user_id")) if cfg.get("primary_user_id") else None

    # ── Webhook ────────────────────────────────────────────────────────────

    def register_webhook(self, api) -> None:
        from fastapi import Request, Response

        @api.post("/teams/messages")
        async def teams_webhook(request: Request):
            try:
                activity = await request.json()
            except Exception:
                return Response(status_code=400)

            activity_type = activity.get("type")
            if activity_type != "message":
                return {"ok": True}

            text = activity.get("text", "").strip()
            user_id = activity.get("from", {}).get("id", "")
            service_url = activity.get("serviceUrl", "").rstrip("/") + "/"
            conversation_id = activity.get("conversation", {}).get("id", "")
            activity_id = activity.get("id", "")

            # Encode routing info into chat_id
            chat_id = f"{service_url}|{conversation_id}|{activity_id}"

            if not self.is_allowed(user_id):
                logger.warning(f"Teams: unauthorized user {user_id}")
                return {"ok": True}

            if text and self._message_callback:
                msg = IncomingMessage(
                    user_id=user_id,
                    text=text,
                    chat_id=chat_id,
                    platform=self.name,
                    raw=activity,
                )
                import asyncio
                asyncio.create_task(self._message_callback(msg, self))

            return {"ok": True}

    def set_message_handler(self, callback) -> None:
        self._message_callback = callback
