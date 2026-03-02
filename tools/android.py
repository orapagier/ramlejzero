import httpx
from core.config_loader import get_apis

TOOL_DEFINITION = {
    "name": "android_tool",
    "description": (
        "Control the Android phone via REST API. Can send SMS, make calls, "
        "manage files (list, read, upload, download, delete), get GPS location, "
        "check battery, read/dismiss notifications, list installed apps, and open apps."
    ),
    "examples": [
        "text john that I'm on my way",
        "call mom",
        "open spotify on my phone",
        "what's my phone battery",
        "read my notifications",
        "what apps are installed on my phone",
        "list files on my phone",
        "download that file from my phone",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": (
                "Action to perform:\n"
                "'send_sms' — send a text message. data: {phone_number, message}\n"
                "'make_call' — make a phone call. data: {phone_number}\n"
                "'get_battery' — get battery level and charging status. data: {}\n"
                "'get_location' — get current GPS coordinates. data: {}\n"
                "'read_notifications' — list current notifications. data: {}\n"
                "'dismiss_notification' — dismiss a notification. data: {notification_id}\n"
                "'list_apps' — list installed apps. data: {}\n"
                "'open_app' — open an app by package name or label. data: {app}\n"
                "'list_files' — list files in a directory. data: {path?} (default: /sdcard/)\n"
                "'read_file' — read a text file's contents. data: {path}\n"
                "'delete_file' — delete a file. data: {path}\n"
                "'upload_file' — write content to a file on the phone. data: {path, content}\n"
                "'download_file' — download a file from the phone. data: {path}\n"
                "'take_screenshot' — capture the phone screen. data: {}\n"
            ),
            "enum": [
                "send_sms", "make_call", "get_battery", "get_location",
                "read_notifications", "dismiss_notification",
                "list_apps", "open_app",
                "list_files", "read_file", "delete_file", "upload_file", "download_file",
                "take_screenshot"
            ]
        },
        "data": {
            "type": "object",
            "description": "Parameters for the action. See action description for required fields per action."
        }
    },
    "required": ["action"]
}

_HTTP_ERRORS = {
    400: "Bad request — the action or data sent was invalid.",
    401: "Unauthorized — the API key is incorrect.",
    403: "Forbidden — access denied.",
    404: "Not found — the endpoint doesn't exist. Check the api_url in apis.yaml.",
    429: "Too many requests — the Android API is rate limiting.",
    500: "Internal server error — the Android REST API crashed.",
    502: "Bad gateway — the Android server is behind a proxy and the origin is down.",
    503: "Service unavailable — the Android REST API is not running.",
    520: "Cloudflare error — the origin server returned an unexpected response.",
    521: "Cloudflare error — the origin server (your phone/device) is offline.",
    522: "Cloudflare timeout — the Android REST API took too long to respond.",
    523: "Cloudflare error — origin is unreachable. Check your phone is on and the API server is running.",
    524: "Cloudflare timeout — connection established but no response received.",
    530: "Cloudflare error — origin server is unreachable. Your phone or the Android REST API server is likely offline or not running.",
}


async def execute(action: str, data: dict = None) -> tuple:
    """Returns (text_result, file_bytes, filename)"""
    cfg = get_apis().get("android", {})
    api_url = cfg.get("api_url", "").rstrip("/")
    api_key = cfg.get("api_key", "")

    if not api_url:
        return "Android tool error: api_url is not configured in apis.yaml.", None, None

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{api_url}/execute",
                headers={"X-API-Key": api_key},
                json={"action": action, "data": data or {}},
                timeout=30
            )

        if r.status_code == 200:
            result = r.json()
            text = result.get("result", str(result))

            # If downloading a file, return bytes for agent to pass back
            if action == "download_file":
                file_bytes_b64 = result.get("file_bytes")
                filename = result.get("filename") or (
                    (data or {}).get("path", "").split("/")[-1] or "download"
                )
                if file_bytes_b64:
                    import base64
                    try:
                        file_bytes = base64.b64decode(file_bytes_b64)
                        return f"Downloaded {filename} ({len(file_bytes)} bytes).", file_bytes, filename
                    except Exception:
                        pass
            return text, None, None

        explanation = _HTTP_ERRORS.get(
            r.status_code,
            f"Unexpected HTTP {r.status_code} response."
        )
        return (
            f"Android tool failed (HTTP {r.status_code}): {explanation} "
            f"Do not retry with different actions — report this error to the user directly.",
            None, None
        )

    except httpx.ConnectTimeout:
        return (
            "Android tool failed: Connection timed out. "
            "The Android REST API server is not responding. "
            "Do not retry — report this to the user directly.",
            None, None
        )
    except httpx.ConnectError:
        return (
            "Android tool failed: Cannot connect to the Android REST API. "
            "The server may be offline or the URL may be wrong. "
            "Do not retry — report this to the user directly.",
            None, None
        )
    except httpx.TimeoutException:
        return (
            "Android tool failed: Request timed out after 30 seconds. "
            "Do not retry — report this to the user directly.",
            None, None
        )
    except Exception as e:
        return (
            f"Android tool failed: {str(e)} "
            f"Do not retry — report this to the user directly.",
            None, None
        )
