from datetime import datetime, timezone, timedelta
from googleapiclient.discovery import build
from auth.google import get_credentials
from core.config_loader import get_settings
from core.rate_limiter import check_and_record

TOOL_DEFINITION = {
    "name": "google_calendar_tool",
    "description": (
        "Manage Google Calendar. Use this tool whenever the user asks about their schedule, "
        "agenda, appointments, events, meetings, or availability — for any time period including "
        "today, tomorrow, this week, next week, or a specific date. "
        "Can list, create, update, and delete events, and check availability."
    ),
    "examples": [
        "am I free tomorrow afternoon",
        "what are my schedules tomorrow",
        "what do I have today",
        "show my agenda for this week",
        "what are my appointments next week",
        "do I have any meetings on Friday",
        "block off friday morning",
        "move my 3pm to 4pm",
        "add a meeting tomorrow at 9am",
        "delete my 3pm event",
        "what events do I have coming up",
        "am I busy on Saturday",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "Action: 'list', 'create', 'update', 'delete'",
            "enum": ["list", "create", "update", "delete"]
        },
        "data": {
            "type": "object",
            "description": (
                "Action data. "
                "list: {days_ahead?, max?, date?} — use date (YYYY-MM-DD) to list events for a specific day, "
                "or days_ahead to list upcoming events. "
                "create: {title, start, end, description?} — start/end as ISO8601 datetime. "
                "update: {event_id, title?, start?, end?, description?}. "
                "delete: {event_id}."
            )
        }
    },
    "required": ["action"]
}


def _get_service():
    return build("calendar", "v3", credentials=get_credentials())


def _tz():
    return get_settings().get("agent", {}).get("timezone", "Asia/Manila")


async def execute(action: str, data: dict = None) -> str:
    await check_and_record("google_apis", wait=True)
    service = _get_service()
    data = data or {}

    if action == "list":
        # If a specific date is given, list just that day
        if data.get("date"):
            from datetime import timezone as tz_module
            import pytz
            tz = pytz.timezone(_tz())
            day = datetime.strptime(data["date"], "%Y-%m-%d")
            time_min = tz.localize(day.replace(hour=0, minute=0, second=0)).isoformat()
            time_max = tz.localize(day.replace(hour=23, minute=59, second=59)).isoformat()
        else:
            days = data.get("days_ahead", 7)
            now = datetime.now(timezone.utc)
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days)).isoformat()

        results = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=data.get("max", 20),
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = results.get("items", [])
        if not events:
            return "No events found for that period."
        output = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            title = e.get("summary", "(No title)")
            output.append(f"- {title} | {start} | ID: {e['id']}")
        return "\n".join(output)

    elif action == "create":
        event = {
            "summary": data["title"],
            "description": data.get("description", ""),
            "start": {"dateTime": data["start"], "timeZone": _tz()},
            "end": {"dateTime": data["end"], "timeZone": _tz()},
        }
        result = service.events().insert(calendarId="primary", body=event).execute()
        return f"Event created: {result['summary']} | ID: {result['id']}"

    elif action == "delete":
        service.events().delete(calendarId="primary", eventId=data["event_id"]).execute()
        return "Event deleted."

    elif action == "update":
        event = service.events().get(calendarId="primary", eventId=data["event_id"]).execute()
        if "title" in data:
            event["summary"] = data["title"]
        if "start" in data:
            event["start"] = {"dateTime": data["start"], "timeZone": _tz()}
        if "end" in data:
            event["end"] = {"dateTime": data["end"], "timeZone": _tz()}
        if "description" in data:
            event["description"] = data["description"]
        result = service.events().update(calendarId="primary", eventId=data["event_id"], body=event).execute()
        return f"Event updated: {result['summary']}"

    return f"Unknown action: {action}"