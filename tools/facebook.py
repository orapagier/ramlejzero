import httpx
from auth.facebook import get_user_token, get_page_token, AuthRequiredError, GRAPH_VERSION
from core.rate_limiter import check_and_record

BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

TOOL_DEFINITION = {
    "name": "facebook_tool",
    "description": (
        "Full Facebook Pages management. Supports: listing pages, getting page info, "
        "creating/updating/deleting/scheduling text posts, photo posts, video posts, "
        "reading and replying to comments, liking comments, getting reaction counts, "
        "page and post insights/analytics, reading page Messenger inbox, reading "
        "conversation messages, sending and replying to Messenger messages, "
        "and creating and listing page events."
    ),
    "examples": [
        "post to my Facebook page",
        "list my Facebook pages",
        "get recent posts on my page",
        "get comments on my page post",
        "reply to a comment on my page",
        "delete a comment on my page",
        "get messages in my page inbox",
        "reply to that message on my page",
        "send a message from my page",
        "how many reactions did my last post get",
        "get my page insights",
        "get post analytics",
        "create a page event",
        "schedule a post for tomorrow",
        "edit that post",
        "post a photo to my page",
        "get page follower count",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "enum": [
                "list_pages",
                "get_page_info",
                "get_page_posts",
                "create_post",
                "update_post",
                "schedule_post",
                "create_photo_post",
                "create_video_post",
                "delete_post",
                "get_comments",
                "reply_to_comment",
                "delete_comment",
                "like_comment",
                "get_post_reactions",
                "get_page_insights",
                "get_post_insights",
                "get_conversations",
                "get_conversation_messages",
                "send_message",
                "reply_to_message",
                "get_page_events",
                "create_page_event",
            ],
            "description": "Action to perform. See 'data' for required parameters per action."
        },
        "data": {
            "type": "object",
            "description": (
                "Parameters for the chosen action:\n"
                "list_pages: {}\n"
                "get_page_info: {page_id}\n"
                "get_page_posts: {page_id, limit?}\n"
                "create_post: {page_id, message, link?}\n"
                "update_post: {page_id, post_id, message} — edit text of an existing post.\n"
                "schedule_post: {page_id, message, scheduled_time} — scheduled_time as Unix timestamp. "
                "Use for future posts. message is required.\n"
                "create_photo_post: {page_id, photo_url, caption?}\n"
                "create_video_post: {page_id, video_url, title?, description?}\n"
                "delete_post: {page_id, post_id}\n"
                "get_comments: {page_id, post_id, limit?}\n"
                "reply_to_comment: {page_id, comment_id, message}\n"
                "delete_comment: {page_id, comment_id}\n"
                "like_comment: {page_id, comment_id}\n"
                "get_post_reactions: {page_id, post_id}\n"
                "get_page_insights: {page_id, metric?, period?}\n"
                "get_post_insights: {page_id, post_id}\n"
                "get_conversations: {page_id, limit?}\n"
                "get_conversation_messages: {page_id, conversation_id, limit?}\n"
                "send_message: {page_id, recipient_id, message}\n"
                "reply_to_message: {page_id, conversation_id, message}\n"
                "get_page_events: {page_id, limit?}\n"
                "create_page_event: {page_id, name, start_time, end_time?, description?, location?}"
            )
        }
    },
    "required": ["action"]
}


# ── Async HTTP helpers ─────────────────────────────────────────────────────────

async def _get(path: str, token: str, params: dict = None) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BASE}/{path}",
            params={"access_token": token, **(params or {})},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


async def _post(path: str, token: str, payload: dict = None) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{BASE}/{path}",
            params={"access_token": token},
            json=payload or {},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


async def _delete(path: str, token: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.delete(
            f"{BASE}/{path}",
            params={"access_token": token},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()


def _fmt_posts(posts: list) -> str:
    if not posts:
        return "No posts found."
    return "\n".join(
        f"- [{p['created_time']}] {p.get('message') or p.get('story') or '(no text)'} | ID: {p['id']}"
        for p in posts
    )


# ── Main executor ──────────────────────────────────────────────────────────────

async def execute(action: str, data: dict = None) -> tuple:
    await check_and_record("facebook_api", wait=True)
    data = data or {}

    try:
        user_token = get_user_token()
    except AuthRequiredError as e:
        return str(e), None, None

    # ── Page info ──────────────────────────────────────────────────────────────

    if action == "list_pages":
        result = await _get("me/accounts", user_token, {
            "fields": "id,name,category,fan_count,followers_count,link"
        })
        pages = result.get("data", [])
        if not pages:
            return "No pages found.", None, None
        lines = [
            f"- {p['name']} | ID: {p['id']} | Category: {p.get('category', '?')} "
            f"| Followers: {p.get('followers_count', p.get('fan_count', '?'))} "
            f"| URL: {p.get('link', '?')}"
            for p in pages
        ]
        return "\n".join(lines), None, None

    elif action == "get_page_info":
        pt = get_page_token(data["page_id"])
        result = await _get(data["page_id"], pt, {
            "fields": (
                "id,name,about,category,fan_count,followers_count,"
                "website,phone,emails,location,link,description,cover,picture"
            )
        })
        lines = [f"{k}: {v}" for k, v in result.items() if k != "id"]
        return f"Page: {result.get('name')} (ID: {result['id']})\n" + "\n".join(lines), None, None

    # ── Posts ──────────────────────────────────────────────────────────────────

    elif action == "get_page_posts":
        pt = get_page_token(data["page_id"])
        result = await _get(f"{data['page_id']}/posts", pt, {
            "fields": "id,message,story,created_time,full_picture,permalink_url",
            "limit": data.get("limit", 10),
        })
        return _fmt_posts(result.get("data", [])), None, None

    elif action == "create_post":
        pt = get_page_token(data["page_id"])
        payload = {"message": data["message"]}
        if data.get("link"):
            payload["link"] = data["link"]
        result = await _post(f"{data['page_id']}/feed", pt, payload)
        return f"Post published. Post ID: {result['id']}", None, None

    elif action == "update_post":
        # Edit the message text of an existing post
        pt = get_page_token(data["page_id"])
        result = await _post(data["post_id"], pt, {"message": data["message"]})
        return f"Post updated. Success: {result.get('success', result)}", None, None

    elif action == "schedule_post":
        # scheduled_time must be a Unix timestamp (int) at least 10 minutes in the future
        pt = get_page_token(data["page_id"])
        payload = {
            "message": data["message"],
            "published": False,
            "scheduled_publish_time": data["scheduled_time"],
        }
        if data.get("link"):
            payload["link"] = data["link"]
        result = await _post(f"{data['page_id']}/feed", pt, payload)
        return (
            f"Post scheduled. Post ID: {result['id']} | "
            f"Will publish at Unix timestamp: {data['scheduled_time']}"
        ), None, None

    elif action == "create_photo_post":
        pt = get_page_token(data["page_id"])
        payload = {"url": data["photo_url"]}
        if data.get("caption"):
            payload["caption"] = data["caption"]
        result = await _post(f"{data['page_id']}/photos", pt, payload)
        return f"Photo post published. Photo ID: {result.get('id', '?')}", None, None

    elif action == "create_video_post":
        pt = get_page_token(data["page_id"])
        payload = {"file_url": data["video_url"]}
        if data.get("title"):
            payload["title"] = data["title"]
        if data.get("description"):
            payload["description"] = data["description"]
        result = await _post(f"{data['page_id']}/videos", pt, payload)
        return f"Video post published. Video ID: {result.get('id', '?')}", None, None

    elif action == "delete_post":
        pt = get_page_token(data["page_id"])
        result = await _delete(data["post_id"], pt)
        return f"Post deleted: {result}", None, None

    # ── Comments ───────────────────────────────────────────────────────────────

    elif action == "get_comments":
        pt = get_page_token(data["page_id"])
        result = await _get(f"{data['post_id']}/comments", pt, {
            "fields": "id,from,message,created_time,comment_count,like_count",
            "limit": data.get("limit", 25),
        })
        comments = result.get("data", [])
        if not comments:
            return "No comments found.", None, None
        lines = [
            f"- [{c['created_time']}] {c.get('from', {}).get('name', 'Unknown')}: "
            f"{c.get('message', '')} "
            f"| 👍 {c.get('like_count', 0)} "
            f"| 💬 {c.get('comment_count', 0)} replies "
            f"| ID: {c['id']}"
            for c in comments
        ]
        return "\n".join(lines), None, None

    elif action == "reply_to_comment":
        pt = get_page_token(data["page_id"])
        result = await _post(f"{data['comment_id']}/comments", pt, {
            "message": data["message"]
        })
        return f"Reply posted. Reply ID: {result['id']}", None, None

    elif action == "delete_comment":
        pt = get_page_token(data["page_id"])
        result = await _delete(data["comment_id"], pt)
        return f"Comment deleted: {result}", None, None

    elif action == "like_comment":
        pt = get_page_token(data["page_id"])
        result = await _post(f"{data['comment_id']}/likes", pt)
        return f"Comment liked: {result}", None, None

    # ── Reactions ──────────────────────────────────────────────────────────────

    elif action == "get_post_reactions":
        pt = get_page_token(data["page_id"])
        # Get grand total in one call
        res = await _get(f"{data['post_id']}/reactions", pt, {
            "summary": "true",
            "limit": "0",
        })
        grand_total = res.get("summary", {}).get("total_count", 0)
        # Get per-type counts in parallel
        reaction_types = ["LIKE", "LOVE", "HAHA", "WOW", "SAD", "ANGRY", "CARE"]
        import asyncio
        async def _get_type_count(rt):
            r = await _get(f"{data['post_id']}/reactions", pt, {
                "type": rt, "summary": "true", "limit": "0",
            })
            return rt, r.get("summary", {}).get("total_count", 0)
        counts = await asyncio.gather(*[_get_type_count(rt) for rt in reaction_types])
        totals = {rt: count for rt, count in counts}
        lines = [f"  {k}: {v}" for k, v in totals.items() if v > 0]
        return (
            f"Reactions on post {data['post_id']}:\n"
            + "\n".join(lines)
            + f"\n  TOTAL: {grand_total}"
        ), None, None

    # ── Insights ───────────────────────────────────────────────────────────────

    elif action == "get_page_insights":
        pt = get_page_token(data["page_id"])
        metric = data.get("metric", (
            "page_fans,page_follows,page_impressions,"
            "page_engaged_users,page_post_engagements,"
            "page_views_total,page_fan_adds,page_fan_removes"
        ))
        period = data.get("period", "day")
        result = await _get(f"{data['page_id']}/insights", pt, {
            "metric": metric,
            "period": period,
        })
        insights = result.get("data", [])
        if not insights:
            return "No insights data found.", None, None
        lines = []
        for ins in insights:
            latest = ins.get("values", [{}])[-1]
            lines.append(
                f"- {ins['name']} ({ins.get('period', '?')}): "
                f"{latest.get('value', '?')} @ {latest.get('end_time', '?')}"
            )
        return "\n".join(lines), None, None

    elif action == "get_post_insights":
        pt = get_page_token(data["page_id"])
        result = await _get(f"{data['post_id']}/insights", pt, {
            "metric": (
                "post_impressions,post_impressions_unique,"
                "post_engaged_users,post_clicks,"
                "post_reactions_by_type_total"
            )
        })
        insights = result.get("data", [])
        if not insights:
            return "No post insights found.", None, None
        lines = [
            f"- {ins['name']}: {ins.get('values', [{}])[-1].get('value', '?')}"
            for ins in insights
        ]
        return "\n".join(lines), None, None

    # ── Messaging ──────────────────────────────────────────────────────────────

    elif action == "get_conversations":
        pt = get_page_token(data["page_id"])
        result = await _get(f"{data['page_id']}/conversations", pt, {
            "fields": "id,participants,updated_time,snippet,unread_count",
            "limit": data.get("limit", 10),
        })
        convos = result.get("data", [])
        if not convos:
            return "No conversations found.", None, None
        lines = []
        for c in convos:
            names = ", ".join(
                p.get("name", p.get("id", "?"))
                for p in c.get("participants", {}).get("data", [])
            )
            lines.append(
                f"- [{c['updated_time']}] {names}: {c.get('snippet', '')} "
                f"| Unread: {c.get('unread_count', 0)} | ID: {c['id']}"
            )
        return "\n".join(lines), None, None

    elif action == "get_conversation_messages":
        pt = get_page_token(data["page_id"])
        result = await _get(data["conversation_id"], pt, {
            "fields": "messages{id,message,from,created_time}",
            "limit": data.get("limit", 20),
        })
        messages = result.get("messages", {}).get("data", [])
        if not messages:
            return "No messages found.", None, None
        lines = [
            f"- [{m['created_time']}] {m.get('from', {}).get('name', '?')}: "
            f"{m.get('message', '(attachment)')}"
            for m in messages
        ]
        return "\n".join(lines), None, None

    elif action == "send_message":
        pt = get_page_token(data["page_id"])
        result = await _post("me/messages", pt, {
            "recipient": {"id": data["recipient_id"]},
            "message": {"text": data["message"]},
            "messaging_type": "RESPONSE",
        })
        return f"Message sent. ID: {result.get('message_id', '?')}", None, None

    elif action == "reply_to_message":
        pt = get_page_token(data["page_id"])
        thread = await _get(data["conversation_id"], pt, {"fields": "participants"})
        page_id = data["page_id"]
        recipient = next(
            (p for p in thread.get("participants", {}).get("data", [])
             if p["id"] != page_id),
            None,
        )
        if not recipient:
            return "Could not find recipient in conversation.", None, None
        result = await _post("me/messages", pt, {
            "recipient": {"id": recipient["id"]},
            "message": {"text": data["message"]},
            "messaging_type": "RESPONSE",
        })
        return f"Reply sent. ID: {result.get('message_id', '?')}", None, None

    # ── Events ─────────────────────────────────────────────────────────────────

    elif action == "get_page_events":
        pt = get_page_token(data["page_id"])
        result = await _get(f"{data['page_id']}/events", pt, {
            "fields": "id,name,start_time,end_time,place,description,attending_count,interested_count",
            "limit": data.get("limit", 10),
        })
        events = result.get("data", [])
        if not events:
            return "No events found.", None, None
        lines = [
            f"- {e['name']} | Start: {e.get('start_time', '?')} "
            f"| Attending: {e.get('attending_count', 0)} "
            f"| Interested: {e.get('interested_count', 0)} "
            f"| ID: {e['id']}"
            for e in events
        ]
        return "\n".join(lines), None, None

    elif action == "create_page_event":
        pt = get_page_token(data["page_id"])
        payload = {
            "name": data["name"],
            "start_time": data["start_time"],
        }
        if data.get("end_time"):
            payload["end_time"] = data["end_time"]
        if data.get("description"):
            payload["description"] = data["description"]
        if data.get("location"):
            payload["location"] = data["location"]
        result = await _post(f"{data['page_id']}/events", pt, payload)
        return f"Event created. Event ID: {result.get('id', '?')}", None, None

    return f"Unknown action: {action}", None, None
