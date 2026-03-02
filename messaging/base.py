"""
messaging/base.py — Abstract Messaging Platform Base Class
===========================================================
All platform adapters must implement this interface.
main.py only ever interacts with this base class — never with platform specifics.

To add a new platform:
  1. Create messaging/yourplatform.py implementing MessagingPlatform
  2. Add your platform config block to settings.yaml
  3. Add your platform to messaging/__init__.py factory
  4. Set messaging.platform: yourplatform in settings.yaml
  That's it. main.py needs zero changes.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class IncomingMessage:
    """
    Normalized message from any platform.
    Platform adapters translate their native message format into this.
    agent.run() and main.py only ever see this — never raw platform objects.
    """
    user_id: str          # Platform-specific user/sender ID (string for cross-platform compat)
    text: str             # Message text content
    chat_id: str          # Where to reply (channel, thread, chat — platform-specific meaning)
    platform: str         # "telegram" | "discord" | "slack" | "teams" | "whatsapp"
    raw: object = None    # Original platform message object, if needed


@dataclass
class OutgoingFile:
    """File attachment to send alongside a text response."""
    data: bytes
    filename: str
    caption: str | None = None


class MessagingPlatform(ABC):
    """
    Abstract base for all messaging platform adapters.
    Implement all abstract methods in your platform subclass.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Platform identifier string. Must match settings.yaml messaging.platform value."""
        ...

    @abstractmethod
    async def start(self, app) -> None:
        """
        Initialize and start the platform (connect to API, set webhook, start polling, etc).
        Called during FastAPI lifespan startup.
        app = FastAPI app instance (store state on app.state if needed).
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """
        Gracefully shut down the platform connection.
        Called during FastAPI lifespan shutdown.
        """
        ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """
        Send a plain text message to the given chat/channel/user.
        Used by: agent replies, token expiry warnings, system notifications.
        Split long messages if platform has character limits.
        """
        ...

    @abstractmethod
    async def send_file(self, chat_id: str, file: OutgoingFile) -> None:
        """
        Send a file (document/attachment) to the given chat.
        Used when agent.run() returns file_bytes.
        """
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """
        Send a typing indicator / "is typing" status.
        No-op if platform doesn't support it — just implement as `pass`.
        """
        ...

    @abstractmethod
    def is_allowed(self, user_id: str) -> bool:
        """
        Check if this user is authorized to use the agent.
        Read from settings.yaml allowed_user_ids for your platform.
        Return True if no allowlist is configured (open access).
        """
        ...

    @abstractmethod
    def register_webhook(self, api) -> None:
        """
        Register any FastAPI webhook routes needed by this platform.
        Called after FastAPI app is created.
        api = FastAPI app instance.
        If your platform uses polling instead of webhooks, implement as `pass`.
        """
        ...

    async def notify(self, text: str) -> None:
        """
        Send a notification to the primary user (first allowed_user_id).
        Default implementation uses send_message — override if needed.
        Used by: token expiry watcher, system alerts.
        """
        primary = self._get_primary_user_id()
        if primary:
            await self.send_message(primary, text)

    def _get_primary_user_id(self) -> str | None:
        """Override in subclass to return the first allowed user ID."""
        return None
