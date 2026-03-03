"""
monitors/gmail_monitor.py
──────────────────────────
Polls Gmail for new messages matching a search query.
Uses the Google OAuth token from your existing Google auth flow
(token at /auth/google_token.json).

Config fields:
  query        — Gmail search syntax  (default: "is:unread label:INBOX")
  max_results  — messages to fetch per poll  (default: 5)

Cursor: comma-separated set of seen message IDs (last 200 max).
New messages = those whose ID isn't in the set.
"""
import json
import os
from monitors.base import MonitorAdapter, MonitorEvent

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

_TOKEN_PATH  = "/auth/google_token.json"
_GMAIL_BASE  = "https://gmail.googleapis.com/gmail/v1/users/me"
_MAX_CURSOR  = 300                          # cap stored IDs to prevent cursor bloat


class GmailMonitor(MonitorAdapter):
    monitor_type = "gmail"
    display_name = "Gmail"
    description  = "New emails matching a Gmail search filter"

    # ── Auth ──────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        if not os.path.exists(_TOKEN_PATH):
            raise RuntimeError(
                "Google not authenticated. Connect via Auth tab → Google → Auth."
            )
        with open(_TOKEN_PATH) as f:
            data = json.load(f)
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Google token file exists but access_token is missing.")
        return token

    # ── Main poll ─────────────────────────────────────────────────────────

    async def poll(
        self, config: dict, cursor: str | None
    ) -> tuple[list[MonitorEvent], str | None]:
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp not installed — run: pip install aiohttp")

        token       = self._get_token()
        query       = config.get("query", "is:unread label:INBOX")
        max_results = int(config.get("max_results", 5))

        # Cursor = pipe-separated seen message IDs
        seen_ids: set[str] = set(cursor.split("|")) if cursor else set()

        headers = {"Authorization": f"Bearer {token}"}
        events  = []
        new_ids = set(seen_ids)

        async with aiohttp.ClientSession(headers=headers) as session:

            # 1. List messages
            async with session.get(
                f"{_GMAIL_BASE}/messages",
                params={"q": query, "maxResults": max_results},
            ) as resp:
                data = await resp.json()

            if "error" in data:
                raise RuntimeError(
                    f"Gmail API error: {data['error'].get('message', str(data['error']))}"
                )

            messages = data.get("messages", [])
            if not messages:
                return [], cursor           # nothing new — keep old cursor

            for msg_ref in messages:
                msg_id = msg_ref["id"]
                if msg_id in seen_ids:
                    continue                # already processed

                new_ids.add(msg_id)

                # 2. Fetch message metadata (headers only — no body download)
                async with session.get(
                    f"{_GMAIL_BASE}/messages/{msg_id}",
                    params={
                        "format": "metadata",
                        "metadataHeaders": ["Subject", "From", "Date"],
                    },
                ) as resp:
                    msg = await resp.json()

                hdrs    = msg.get("payload", {}).get("headers", [])
                subject = _header(hdrs, "Subject") or "(no subject)"
                sender  = _header(hdrs, "From")    or "Unknown"
                date    = _header(hdrs, "Date")    or ""
                snippet = msg.get("snippet", "")

                events.append(MonitorEvent(
                    title    = f"📧 {subject[:70]}",
                    body     = f"From: {sender}\nDate: {date}\n\n{snippet}",
                    source_id= msg_id,
                    metadata = {
                        "message_id": msg_id,
                        "subject":    subject,
                        "sender":     sender,
                        "platform":   "gmail",
                    },
                    severity = "info",
                ))

        # Cap cursor size — keep the most-recent IDs
        trimmed = list(new_ids)[-_MAX_CURSOR:]
        new_cursor = "|".join(trimmed) if trimmed else None

        return events, new_cursor

    # ── Dashboard schema ──────────────────────────────────────────────────

    @classmethod
    def schema(cls) -> dict:
        return {
            "fields": [
                {
                    "key": "query", "label": "Gmail Search Query",
                    "type": "text",
                    "default": "is:unread label:INBOX", "required": True,
                    "help": (
                        "Standard Gmail search syntax. "
                        "Examples: 'is:unread', 'from:boss@company.com is:unread', "
                        "'subject:invoice is:unread'"
                    ),
                },
                {
                    "key": "max_results", "label": "Max messages per poll",
                    "type": "number", "default": 5, "required": False,
                    "help": "How many matching messages to inspect each interval",
                },
            ]
        }


def _header(headers: list, name: str) -> str | None:
    """Extract a header value by name from Gmail metadata headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None
