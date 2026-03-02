import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
from auth.google import get_credentials
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "gmail_tool",
    "description": "Manage Gmail. Can read, send, reply, search, and delete emails.",
    "examples": [
        "did anyone message me",
        "write back to him saying I'll be late",
        "forward it to my boss",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "Action: 'list', 'read', 'send', 'reply', 'search', 'delete'",
            "enum": ["list", "read", "send", "reply", "search", "delete"]
        },
        "data": {
            "type": "object",
            "description": "Action data. list: {query, max}. read/delete: {message_id}. send: {to, subject, body}. reply: {message_id, body}. search: {query}."
        }
    },
    "required": ["action"]
}


def _service():
    return build("gmail", "v1", credentials=get_credentials())


def _decode_body(payload):
    if "body" in payload and payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
    return ""


async def execute(action: str, data: dict = None) -> str:
    await check_and_record("google_apis", wait=True)
    svc = _service()
    data = data or {}

    if action == "list":
        res = svc.users().messages().list(
            userId="me", maxResults=data.get("max", 10),
            labelIds=["INBOX"], q=data.get("query", "is:unread")
        ).execute()
        msgs = res.get("messages", [])
        output = []
        for m in msgs[:10]:
            d = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            output.append(f"ID: {m['id']} | From: {h.get('From','')} | Subject: {h.get('Subject','')} | {h.get('Date','')}")
        return "\n".join(output) if output else "No messages found."

    elif action == "read":
        m = svc.users().messages().get(userId="me", id=data["message_id"], format="full").execute()
        h = {x["name"]: x["value"] for x in m["payload"]["headers"]}
        body = _decode_body(m["payload"])
        return f"From: {h.get('From')}\nSubject: {h.get('Subject')}\nDate: {h.get('Date')}\n\n{body[:3000]}"

    elif action == "send":
        msg = MIMEMultipart()
        msg["to"] = data["to"]
        msg["subject"] = data["subject"]
        msg.attach(MIMEText(data["body"], "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Email sent to {data['to']}"

    elif action == "reply":
        orig = svc.users().messages().get(userId="me", id=data["message_id"], format="metadata",
            metadataHeaders=["Subject", "From", "Message-ID", "References"]).execute()
        h = {x["name"]: x["value"] for x in orig["payload"]["headers"]}
        msg = MIMEMultipart()
        msg["to"] = h.get("From")
        msg["subject"] = f"Re: {h.get('Subject', '')}"
        msg["In-Reply-To"] = h.get("Message-ID", "")
        msg["References"] = h.get("References", "") + " " + h.get("Message-ID", "")
        msg.attach(MIMEText(data["body"], "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw, "threadId": orig["threadId"]}).execute()
        return "Reply sent."

    elif action == "search":
        res = svc.users().messages().list(userId="me", q=data["query"], maxResults=10).execute()
        msgs = res.get("messages", [])
        output = []
        for m in msgs:
            d = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]).execute()
            h = {x["name"]: x["value"] for x in d["payload"]["headers"]}
            output.append(f"ID: {m['id']} | {h.get('From','')} | {h.get('Subject','')} | {h.get('Date','')}")
        return "\n".join(output) if output else "No results."

    elif action == "delete":
        svc.users().messages().trash(userId="me", id=data["message_id"]).execute()
        return "Email moved to trash."

    return f"Unknown action: {action}"
