"""
monitors/facebook_page.py
──────────────────────────
Polls Facebook Page for new Messenger conversations and post comments.
Uses the Facebook Graph API + token stored at /auth/facebook_token.json
(populated by your existing Facebook OAuth flow).

Config fields:
  page_id        — Facebook Page ID or "me"  (default: "me")
  notify_on      — ["messages", "comments"]  (default: ["messages"])
  message_limit  — conversations to fetch per poll  (default: 10)
"""
import json
import os
from monitors.base import MonitorAdapter, MonitorEvent

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

_TOKEN_PATH = "/auth/facebook_token.json"
_GRAPH_VER  = "v19.0"


class FacebookPageMonitor(MonitorAdapter):
    monitor_type = "facebook_page"
    display_name = "Facebook Page"
    description  = "New Messenger messages or comments on your Facebook Page"

    # ── Auth ──────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        if not os.path.exists(_TOKEN_PATH):
            raise RuntimeError(
                "Facebook not authenticated. Connect via Auth tab → Facebook → Auth."
            )
        with open(_TOKEN_PATH) as f:
            data = json.load(f)
        token = data.get("access_token")
        if not token:
            raise RuntimeError("Facebook token file exists but access_token is missing.")
        return token

    # ── Main poll ─────────────────────────────────────────────────────────

    async def poll(
        self, config: dict, cursor: str | None
    ) -> tuple[list[MonitorEvent], str | None]:
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp not installed — run: pip install aiohttp")

        token     = self._get_token()
        page_id   = config.get("page_id", "me")
        notify_on = config.get("notify_on", ["messages"])
        limit     = int(config.get("message_limit", 10))

        events     = []
        new_cursor = cursor                  # will advance as we find newer items

        async with aiohttp.ClientSession() as session:

            # ── Messenger conversations ────────────────────────────────────
            if "messages" in notify_on:
                url    = f"https://graph.facebook.com/{_GRAPH_VER}/{page_id}/conversations"
                params = {
                    "fields": "id,updated_time,messages{message,from,created_time}",
                    "access_token": token,
                    "limit": limit,
                }
                async with session.get(url, params=params) as resp:
                    data = await resp.json()

                if "error" in data:
                    raise RuntimeError(
                        f"Facebook API error: {data['error'].get('message', str(data['error']))}"
                    )

                last_seen_ts = cursor        # ISO timestamp of last seen update
                new_ts       = last_seen_ts

                for conv in data.get("data", []):
                    updated = conv.get("updated_time", "")

                    # Skip already-seen conversations
                    if last_seen_ts and updated <= last_seen_ts:
                        continue

                    # Advance the high-water mark
                    if not new_ts or updated > new_ts:
                        new_ts = updated

                    messages = conv.get("messages", {}).get("data", [])
                    if not messages:
                        continue

                    # Build a compact chat transcript (last 5 messages)
                    lines = []
                    for m in reversed(messages[-5:]):
                        sender = m.get("from", {}).get("name", "Unknown")
                        text   = m.get("message", "")
                        ts     = m.get("created_time", "")[:19].replace("T", " ")
                        lines.append(f"[{ts}] {sender}: {text}")

                    events.append(MonitorEvent(
                        title    = "💬 New Facebook Message",
                        body     = "\n".join(lines),
                        source_id= conv.get("id", ""),
                        metadata = {
                            "conversation_id": conv.get("id"),
                            "updated_time": updated,
                            "platform": "facebook",
                        },
                        severity = "info",
                    ))

                new_cursor = new_ts or cursor

            # ── Page comments ──────────────────────────────────────────────
            if "comments" in notify_on:
                url    = f"https://graph.facebook.com/{_GRAPH_VER}/{page_id}/feed"
                params = {
                    "fields": "id,message,created_time,comments{message,from,created_time}",
                    "access_token": token,
                    "limit": 5,
                }
                async with session.get(url, params=params) as resp:
                    data = await resp.json()

                last_seen_ts = cursor
                for post in data.get("data", []):
                    for comment in post.get("comments", {}).get("data", []):
                        ct = comment.get("created_time", "")
                        if last_seen_ts and ct <= last_seen_ts:
                            continue
                        if not new_cursor or ct > new_cursor:
                            new_cursor = ct

                        sender = comment.get("from", {}).get("name", "Unknown")
                        text   = comment.get("message", "")

                        events.append(MonitorEvent(
                            title    = "💬 New Facebook Comment",
                            body     = f"From: {sender}\n\n{text}",
                            source_id= comment.get("id", ""),
                            metadata = {
                                "post_id":    post.get("id"),
                                "comment_id": comment.get("id"),
                                "platform":   "facebook",
                            },
                            severity = "info",
                        ))

        return events, new_cursor

    # ── Dashboard schema ──────────────────────────────────────────────────

    @classmethod
    def schema(cls) -> dict:
        return {
            "fields": [
                {
                    "key": "page_id", "label": "Page ID",
                    "type": "text", "default": "me", "required": True,
                    "help": "Your Facebook Page ID or 'me' for the authenticated user's page",
                },
                {
                    "key": "notify_on", "label": "Notify On",
                    "type": "multiselect",
                    "options": ["messages", "comments"],
                    "default": ["messages"], "required": True,
                },
                {
                    "key": "message_limit", "label": "Conversations per poll",
                    "type": "number", "default": 10, "required": False,
                    "help": "How many conversations to check each interval",
                },
            ]
        }
