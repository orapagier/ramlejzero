"""
messaging/discord.py — Discord Platform Adapter
================================================
settings.yaml config block:

messaging:
  platform: discord
  primary_user_id: "123456789012345678"   # Discord user ID (18-digit snowflake)

discord:
  bot_token: YOUR_DISCORD_BOT_TOKEN       # From Discord Developer Portal → Bot → Token
  allowed_user_ids:                        # Discord user IDs (snowflakes as strings)
    - "123456789012345678"
  allowed_guild_ids:                       # Optional: restrict to specific servers
    - "987654321098765432"
  command_prefix: "!"                      # Prefix for !clear, !status commands

Setup:
  1. Go to https://discord.com/developers/applications
  2. Create a new application → Bot → Add Bot
  3. Copy the bot token → discord.bot_token
  4. Under OAuth2 → URL Generator:
     Scopes: bot
     Bot permissions: Send Messages, Read Message History, Attach Files
  5. Use generated URL to invite bot to your server
  6. Enable Developer Mode in Discord settings to copy user/guild IDs

Requirements:
  pip install discord.py
"""

import logging
import asyncio
from core.config_loader import get_settings
from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile

logger = logging.getLogger("messaging.discord")

_MAX_LEN = 2000  # Discord character limit


class DiscordPlatform(MessagingPlatform):

    def __init__(self):
        self._client = None
        self._message_callback = None
        self._cfg = {}

    @property
    def name(self) -> str:
        return "discord"

    async def start(self, app) -> None:
        try:
            import discord
        except ImportError:
            raise ImportError("discord.py not installed. Run: pip install discord.py")

        self._cfg = get_settings().get("discord", {})
        bot_token = self._cfg.get("bot_token")
        if not bot_token:
            raise ValueError("discord.bot_token missing from settings.yaml")

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True

        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready():
            logger.info(f"Discord bot ready: {self._client.user}")

        @self._client.event
        async def on_message(message):
            if message.author == self._client.user:
                return
            user_id = str(message.author.id)
            if not self.is_allowed(user_id):
                return

            text = message.content.strip()
            if not text:
                return

            if self._message_callback:
                msg = IncomingMessage(
                    user_id=user_id,
                    text=text,
                    chat_id=str(message.channel.id),
                    platform=self.name,
                    raw=message,
                )
                await self._message_callback(msg, self)

        # Start Discord client in background task
        asyncio.create_task(self._client.start(bot_token))
        app.state.discord_client = self._client
        logger.info("Discord client starting...")

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
            logger.info("Discord stopped")

    async def send_message(self, chat_id: str, text: str) -> None:
        if not self._client:
            return
        channel = self._client.get_channel(int(chat_id))
        if not channel:
            # Try fetching if not in cache
            channel = await self._client.fetch_channel(int(chat_id))
        if channel:
            for i in range(0, len(text), _MAX_LEN):
                await channel.send(text[i:i + _MAX_LEN])

    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        if not self._client:
            return
        import discord
        import io
        channel = self._client.get_channel(int(chat_id))
        if not channel:
            channel = await self._client.fetch_channel(int(chat_id))
        if channel:
            await channel.send(
                content=file.caption or "",
                file=discord.File(io.BytesIO(file.data), filename=file.filename),
            )

    async def send_typing(self, chat_id: str) -> None:
        if not self._client:
            return
        channel = self._client.get_channel(int(chat_id))
        if channel:
            async with channel.typing():
                pass

    def is_allowed(self, user_id: str) -> bool:
        allowed = self._cfg.get("allowed_user_ids", [])
        return not allowed or user_id in [str(u) for u in allowed]

    def _get_primary_user_id(self) -> str | None:
        # For Discord notifications, send to a DM channel
        # primary_user_id should be a Discord user ID
        cfg = get_settings().get("messaging", {})
        return str(cfg.get("primary_user_id")) if cfg.get("primary_user_id") else None

    async def notify(self, text: str) -> None:
        """Send DM to primary user for system notifications."""
        if not self._client:
            return
        primary_id = self._get_primary_user_id()
        if not primary_id:
            return
        try:
            user = await self._client.fetch_user(int(primary_id))
            dm = await user.create_dm()
            await self.send_message(str(dm.id), text)
        except Exception as e:
            logger.warning(f"Discord notify failed: {e}")

    def register_webhook(self, api) -> None:
        # Discord uses gateway (WebSocket polling), not webhooks for receiving messages
        pass

    def set_message_handler(self, callback) -> None:
        self._message_callback = callback
