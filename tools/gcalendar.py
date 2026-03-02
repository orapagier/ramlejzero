import pytz
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
        "Can list calendars, check free/busy, list, get, create (with attendees), update, "
        "delete events, and add events via natural language using quick_add."
    ),
    "examples": [
        "am I free tomorrow afternoon",
        "what are my schedules tomorrow",
        "what do I have today",
        "show my agenda for this week",
        "do I have any meetings on Friday",
        "block off friday morning",
        "move my 3pm to 4pm",
        "add a meeting tomorrow at 9am with john@example.com",
        "delete my 3pm event",
        "what calendars do I have",
        "am I busy between 2pm and 4pm today",
        "add dentist appointment next tuesday at 10am",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action: 'list_calendars', 'list', 'get', 'create', 'quick_add', "
                "'update', 'delete', 'free_busy'"
            ),
            "enum": ["list_calendars", "list", "get", "create", "quick_add", "update", "delete", "free_busy"]
        },
        "data": {
            "type": "object",
            "description": (
                "Action data.\n"
                "list_calendars: {} — returns all calendars the user has access to.\n"
                "list: {days_ahead?, max?, date?, calendar_id?} — calendar_id defaults to 'primary'. "
                "Use date (YYYY-MM-DD) for a specific day, or days_ahead for upcoming.\n"
                "get: {event_id, calendar_id?} — full event details: attendees, location, video link.\n"
                "create: {title, start, end, description?, location?, attendees?, calendar_id?} — "
                "start/end as ISO8601. attendees: list of email strings.\n"
                "quick_add: {text, calendar_id?} — natural language e.g. 'Dentist Tuesday at 2pm'. "
                "Google parses the time automatically.\n"
                "update: {event_id, title?, start?, end?, description?, location?, attendees?, calendar_id?}.\n"
                "delete: {event_id, calendar_id?}.\n"
                "free_busy: {time_min, time_max, calendars?} — time_min/time_max as ISO8601. "
                "calendars: list of calendar IDs (defaults to ['primary'])."
            )
        }
    },
    "required": ["action"]
}


def _get_service():
    return build("calendar", "v3", credentials=get_credentials())


def _tz():
    return get_settings().get("agent", {}).get("timezone", "Asia/Manila")


def _format_event(e: dict) -> str:
    """Format a full event dict into a readable string."""
    start = e["start"].get("dateTime", e["start"].get("date"))
    end = e["end"].get("dateTime", e["end"].get("date"))
    lines = [
        f"Title: {e.get('summary', '(No title)')}",
        f"Start: {start}",
        f"End: {end}",
    ]
    if e.get("description"):
        lines.append(f"Description: {e['description']}")
    if e.get("location"):
        lines.append(f"Location: {e['location']}")
    if e.get("attendees"):
        attendees = ", ".join(
            f"{a.get('displayName', a['email'])} ({a.get('responseStatus', '?')})"
            for a in e["attendees"]
        )
        lines.append(f"Attendees: {attendees}")
    conf = e.get("conferenceData", {})
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            lines.append(f"Video call: {ep.get('uri', '')}")
            break
    lines.append(f"ID: {e['id']}")
    return "\n".join(lines)


async def execute(action: str, data: dict = None) -> str:
    await check_and_record("google_apis", wait=True)
    service = _get_service()
    data = data or {}
    cal_id = data.get("calendar_id", "primary")

    # ── List calendars ────────────────────────────────────────────────────────
    if action == "list_calendars":
        result = service.calendarList().list().execute()
        items = result.get("items", [])
        if not items:
            return "No calendars found."
        lines = []
        for c in items:
            primary = " (PRIMARY)" if c.get("primary") else ""
            lines.append(
                f"- {c.get('summary', '(No name)')}{primary} "
                f"| ID: {c['id']} "
                f"| Timezone: {c.get('timeZone', '?')}"
            )
        return "\n".join(lines)

    # ── List events ───────────────────────────────────────────────────────────
    elif action == "list":
        if data.get("date"):
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
            calendarId=cal_id,
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

    # ── Get event detail ──────────────────────────────────────────────────────
    elif action == "get":
        e = service.events().get(calendarId=cal_id, eventId=data["event_id"]).execute()
        return _format_event(e)

    # ── Create event (with attendees, location) ───────────────────────────────
    elif action == "create":
        event = {
            "summary": data["title"],
            "description": data.get("description", ""),
            "start": {"dateTime": data["start"], "timeZone": _tz()},
            "end": {"dateTime": data["end"], "timeZone": _tz()},
        }
        if data.get("location"):
            event["location"] = data["location"]
        if data.get("attendees"):
            event["attendees"] = [{"email": e} for e in data["attendees"]]
        result = service.events().insert(
            calendarId=cal_id, body=event, sendUpdates="all"
        ).execute()
        msg = f"Event created: {result['summary']} | ID: {result['id']}"
        if data.get("attendees"):
            msg += f" | Invites sent to: {', '.join(data['attendees'])}"
        return msg

    # ── Quick add (natural language) ──────────────────────────────────────────
    elif action == "quick_add":
        # Google Calendar parses natural language strings like "Dentist Tuesday at 2pm"
        result = service.events().quickAdd(
            calendarId=cal_id,
            text=data["text"]
        ).execute()
        start = result["start"].get("dateTime", result["start"].get("date"))
        return f"Event added: {result.get('summary', '?')} at {start} | ID: {result['id']}"

    # ── Update event ──────────────────────────────────────────────────────────
    elif action == "update":
        event = service.events().get(calendarId=cal_id, eventId=data["event_id"]).execute()
        if "title" in data:
            event["summary"] = data["title"]
        if "start" in data:
            event["start"] = {"dateTime": data["start"], "timeZone": _tz()}
        if "end" in data:
            event["end"] = {"dateTime": data["end"], "timeZone": _tz()}
        if "description" in data:
            event["description"] = data["description"]
        if "location" in data:
            event["location"] = data["location"]
        if "attendees" in data:
            event["attendees"] = [{"email": e} for e in data["attendees"]]
        result = service.events().update(
            calendarId=cal_id, eventId=data["event_id"],
            body=event, sendUpdates="all"
        ).execute()
        return f"Event updated: {result['summary']}"

    # ── Delete event ──────────────────────────────────────────────────────────
    elif action == "delete":
        service.events().delete(
            calendarId=cal_id, eventId=data["event_id"], sendUpdates="all"
        ).execute()
        return "Event deleted."

    # ── Free/busy check ───────────────────────────────────────────────────────
    elif action == "free_busy":
        calendars = data.get("calendars", ["primary"])
        body = {
            "timeMin": data["time_min"],
            "timeMax": data["time_max"],
            "timeZone": _tz(),
            "items": [{"id": c} for c in calendars],
        }
        result = service.freebusy().query(body=body).execute()
        calendars_result = result.get("calendars", {})
        lines = []
        for cal_key, cal_data in calendars_result.items():
            busy = cal_data.get("busy", [])
            if not busy:
                lines.append(f"✅ {cal_key}: FREE during this period.")
            else:
                lines.append(f"🔴 {cal_key}: BUSY during:")
                for slot in busy:
                    lines.append(f"   - {slot['start']} → {slot['end']}")
        return "\n".join(lines) if lines else "No calendar data returned."

    return f"Unknown action: {action}"
