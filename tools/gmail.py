import base64
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from googleapiclient.discovery import build
from auth.google import get_credentials
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "gmail_tool",
    "description": (
        "Manage Gmail. Can read, send, reply, forward, search, delete, archive, "
        "create drafts, manage labels, and mark emails as read or unread."
    ),
    "examples": [
        "did anyone message me",
        "write back to him saying I'll be late",
        "forward it to my boss",
        "mark that email as read",
        "archive this email",
        "save this as a draft",
        "what labels do I have",
        "move this email to Work label",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action to perform: 'list', 'read', 'send', 'reply', 'forward', 'search', "
                "'delete', 'archive', 'mark_as_read', 'mark_as_unread', "
                "'create_draft', 'list_labels', 'add_label', 'remove_label'"
            ),
            "enum": [
                "list", "read", "send", "reply", "forward", "search",
                "delete", "archive", "mark_as_read", "mark_as_unread",
                "create_draft", "list_labels", "add_label", "remove_label"
            ]
        },
        "data": {
            "type": "object",
            "description": (
                "Action data.\n"
                "list: {query?, max?} — query defaults to 'is:unread'.\n"
                "read: {message_id}.\n"
                "send: {to, subject, body, cc?, bcc?, attachments?} — "
                "attachments: list of {filename, content (base64 string), mime_type?}.\n"
                "reply: {message_id, body, cc?, bcc?}.\n"
                "forward: {message_id, to, body?} — prepends body as note above forwarded content.\n"
                "search: {query, max?}.\n"
                "delete: {message_id} — moves to trash.\n"
                "archive: {message_id} — removes from inbox without deleting.\n"
                "mark_as_read: {message_id}.\n"
                "mark_as_unread: {message_id}.\n"
                "create_draft: {to, subject, body, cc?, bcc?}.\n"
                "list_labels: {} — returns all labels (system + user-created) with IDs.\n"
                "add_label: {message_id, label_id}.\n"
                "remove_label: {message_id, label_id}."
            )
        }
    },
    "required": ["action"]
}


def _service():
    return build("gmail", "v1", credentials=get_credentials())


def _strip_html(html: str) -> str:
    """Lightweight HTML tag stripper for email body fallback."""
    html = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<(br|p|div|tr|li)[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<[^>]+>", "", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    html = re.sub(r"\n{3,}", "\n\n", html.strip())
    return html


def _decode_body(payload) -> str:
    """
    Extract readable text from a Gmail message payload.
    Priority: text/plain → text/html (stripped) → empty string.
    """
    if "body" in payload and payload["body"].get("data"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
        if payload.get("mimeType", "").startswith("text/html"):
            return _strip_html(raw)
        return raw

    if "parts" in payload:
        html_fallback = None
        for part in payload["parts"]:
            mime = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data")
            if not body_data:
                if mime.startswith("multipart") and "parts" in part:
                    result = _decode_body(part)
                    if result:
                        return result
                continue
            decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
            if mime == "text/plain":
                return decoded
            if mime == "text/html":
                html_fallback = decoded
        if html_fallback:
            return _strip_html(html_fallback)

    return ""


def _build_message(to: str, subject: str, body: str,
                   cc: str = None, bcc: str = None,
                   attachments: list = None) -> MIMEMultipart:
    """Build a MIMEMultipart message with optional cc, bcc, and file attachments."""
    msg = MIMEMultipart()
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if bcc:
        msg["bcc"] = bcc
    msg.attach(MIMEText(body, "plain"))

    for att in (attachments or []):
        part = MIMEBase("application", "octet-stream")
        content = att.get("content", "")
        if isinstance(content, str):
            try:
                part.set_payload(base64.b64decode(content))
            except Exception:
                part.set_payload(content.encode())
        else:
            part.set_payload(content)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename=\"{att['filename']}\"")
        if att.get("mime_type"):
            part.set_type(att["mime_type"])
        msg.attach(part)

    return msg


async def execute(action: str, data: dict = None) -> str:
    await check_and_record("google_apis", wait=True)
    svc = _service()
    data = data or {}

    # ── List ──────────────────────────────────────────────────────────────────
    if action == "list":
        res = svc.users().messages().list(
            userId="me",
            maxResults=data.get("max", 10),
            labelIds=["INBOX"],
            q=data.get("query", "is:unread")
        ).execute()
        msgs = res.get("messages", [])
        output = []
        for m in msgs[:10]:
            d = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            output.append(
                f"ID: {m['id']} | From: {h.get('From', '')} "
                f"| Subject: {h.get('Subject', '')} | {h.get('Date', '')}"
            )
        return "\n".join(output) if output else "No messages found."

    # ── Read ──────────────────────────────────────────────────────────────────
    elif action == "read":
        m = svc.users().messages().get(
            userId="me", id=data["message_id"], format="full"
        ).execute()
        h = {x["name"]: x["value"] for x in m["payload"]["headers"]}
        body = _decode_body(m["payload"])
        return (
            f"From: {h.get('From')}\n"
            f"Subject: {h.get('Subject')}\n"
            f"Date: {h.get('Date')}\n\n"
            f"{body[:3000]}"
        )

    # ── Send (with cc, bcc, attachments) ──────────────────────────────────────
    elif action == "send":
        msg = _build_message(
            to=data["to"],
            subject=data["subject"],
            body=data["body"],
            cc=data.get("cc"),
            bcc=data.get("bcc"),
            attachments=data.get("attachments"),
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        summary = data["to"]
        if data.get("cc"):
            summary += f", CC: {data['cc']}"
        if data.get("attachments"):
            names = ", ".join(a["filename"] for a in data["attachments"])
            summary += f" | Attachments: {names}"
        return f"Email sent to {summary}."

    # ── Reply (with cc, bcc) ──────────────────────────────────────────────────
    elif action == "reply":
        orig = svc.users().messages().get(
            userId="me", id=data["message_id"], format="metadata",
            metadataHeaders=["Subject", "From", "Message-ID", "References"]
        ).execute()
        h = {x["name"]: x["value"] for x in orig["payload"]["headers"]}
        msg = _build_message(
            to=h.get("From", ""),
            subject=f"Re: {h.get('Subject', '')}",
            body=data["body"],
            cc=data.get("cc"),
            bcc=data.get("bcc"),
        )
        msg["In-Reply-To"] = h.get("Message-ID", "")
        msg["References"] = h.get("References", "") + " " + h.get("Message-ID", "")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(
            userId="me", body={"raw": raw, "threadId": orig["threadId"]}
        ).execute()
        return "Reply sent."

    # ── Forward ───────────────────────────────────────────────────────────────
    elif action == "forward":
        orig_full = svc.users().messages().get(
            userId="me", id=data["message_id"], format="full"
        ).execute()
        h = {x["name"]: x["value"] for x in orig_full["payload"]["headers"]}
        orig_body = _decode_body(orig_full["payload"])
        fwd_note = data.get("body", "")
        forward_body = (
            f"{fwd_note}\n\n"
            f"---------- Forwarded message ----------\n"
            f"From: {h.get('From', '')}\n"
            f"Date: {h.get('Date', '')}\n"
            f"Subject: {h.get('Subject', '')}\n\n"
            f"{orig_body[:2000]}"
        )
        msg = _build_message(
            to=data["to"],
            subject=f"Fwd: {h.get('Subject', '')}",
            body=forward_body,
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email forwarded to {data['to']}."

    # ── Search ────────────────────────────────────────────────────────────────
    elif action == "search":
        res = svc.users().messages().list(
            userId="me", q=data["query"], maxResults=data.get("max", 10)
        ).execute()
        msgs = res.get("messages", [])
        output = []
        for m in msgs:
            d = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]
            ).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            output.append(
                f"ID: {m['id']} | {h.get('From', '')} "
                f"| {h.get('Subject', '')} | {h.get('Date', '')}"
            )
        return "\n".join(output) if output else "No results."

    # ── Delete ────────────────────────────────────────────────────────────────
    elif action == "delete":
        svc.users().messages().trash(userId="me", id=data["message_id"]).execute()
        return "Email moved to trash."

    # ── Archive ───────────────────────────────────────────────────────────────
    elif action == "archive":
        svc.users().messages().modify(
            userId="me",
            id=data["message_id"],
            body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return "Email archived (removed from inbox, kept in All Mail)."

    # ── Mark as read / unread ─────────────────────────────────────────────────
    elif action == "mark_as_read":
        svc.users().messages().modify(
            userId="me",
            id=data["message_id"],
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
        return "Email marked as read."

    elif action == "mark_as_unread":
        svc.users().messages().modify(
            userId="me",
            id=data["message_id"],
            body={"addLabelIds": ["UNREAD"]}
        ).execute()
        return "Email marked as unread."

    # ── Create draft ──────────────────────────────────────────────────────────
    elif action == "create_draft":
        msg = _build_message(
            to=data["to"],
            subject=data["subject"],
            body=data["body"],
            cc=data.get("cc"),
            bcc=data.get("bcc"),
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        return f"Draft saved. Draft ID: {result['id']}"

    # ── Labels ────────────────────────────────────────────────────────────────
    elif action == "list_labels":
        result = svc.users().labels().list(userId="me").execute()
        labels = result.get("labels", [])
        system_labels = [l for l in labels if l.get("type") == "system"]
        user_labels = [l for l in labels if l.get("type") == "user"]
        lines = ["System labels:"]
        lines += [f"  {l['name']} | ID: {l['id']}" for l in system_labels]
        lines += ["\nUser-created labels:"]
        lines += [f"  {l['name']} | ID: {l['id']}" for l in user_labels]
        return "\n".join(lines) if labels else "No labels found."

    elif action == "add_label":
        svc.users().messages().modify(
            userId="me",
            id=data["message_id"],
            body={"addLabelIds": [data["label_id"]]}
        ).execute()
        return f"Label '{data['label_id']}' added to email."

    elif action == "remove_label":
        svc.users().messages().modify(
            userId="me",
            id=data["message_id"],
            body={"removeLabelIds": [data["label_id"]]}
        ).execute()
        return f"Label '{data['label_id']}' removed from email."

    return f"Unknown action: {action}"
