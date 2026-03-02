"""
messaging/__init__.py — Platform Factory
=========================================
Reads messaging.platform from settings.yaml and returns the correct adapter.
main.py imports only this — never imports platform-specific code directly.

To add a new platform:
  1. Create messaging/yourplatform.py implementing MessagingPlatform
  2. Import and register it in _PLATFORMS below
  3. Set messaging.platform: yourplatform in settings.yaml
  main.py needs zero changes.
"""

from messaging.base import MessagingPlatform, IncomingMessage, OutgoingFile
from core.config_loader import get_settings

# ── Platform registry ──────────────────────────────────────────────────────
# Add new platforms here. Import is deferred inside the factory
# so unused platforms don't require their dependencies to be installed.

_PLATFORMS = {
    "telegram",
    "discord",
    "slack",
    "teams",
    "whatsapp",
}


def get_platform() -> MessagingPlatform:
    """
    Factory: reads messaging.platform from settings.yaml,
    instantiates and returns the correct adapter.
    Raises ValueError if platform is unknown or not configured.
    """
    cfg = get_settings().get("messaging", {})
    platform_name = cfg.get("platform", "telegram").lower().strip()

    if platform_name not in _PLATFORMS:
        raise ValueError(
            f"Unknown messaging platform: '{platform_name}'. "
            f"Valid options: {sorted(_PLATFORMS)}"
        )

    if platform_name == "telegram":
        from messaging.telegram import TelegramPlatform
        return TelegramPlatform()

    elif platform_name == "discord":
        from messaging.discord import DiscordPlatform
        return DiscordPlatform()

    elif platform_name == "slack":
        from messaging.slack import SlackPlatform
        return SlackPlatform()

    elif platform_name == "teams":
        from messaging.teams import TeamsPlatform
        return TeamsPlatform()

    elif platform_name == "whatsapp":
        from messaging.whatsapp import WhatsAppPlatform
        return WhatsAppPlatform()

    raise ValueError(f"Platform '{platform_name}' registered but not implemented")


__all__ = ["get_platform", "MessagingPlatform", "IncomingMessage", "OutgoingFile"]
