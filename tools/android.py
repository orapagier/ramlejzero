import httpx
from core.config_loader import get_apis

TOOL_DEFINITION = {
    "name": "android_tool",
    "description": "Control the Android phone via REST API. Can send SMS, make calls, manage files, get GPS location, read notifications, and open apps.",
    "examples": [
        "text john that I'm on my way",
        "call mom",
        "open spotify on my phone",
        "what's my phone battery",
        "read my notifications",
    ],
    "parameters": {
        "action": {
            "type": "string",
            "description": "The action to perform on the Android device"
        },
        "data": {
            "type": "object",
            "description": "Action parameters as key-value pairs (e.g., phone number, message text, file path)"
        }
    },
    "required": ["action"]
}

# Human-readable explanations for common HTTP error codes
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


async def execute(action: str, data: dict = None) -> str:
    cfg = get_apis().get("android", {})
    api_url = cfg.get("api_url", "").rstrip("/")
    api_key = cfg.get("api_key", "")

    if not api_url:
        return "Android tool error: api_url is not configured in apis.yaml."

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
            return result.get("result", str(result))

        # Return a clean, agent-readable error instead of raising
        explanation = _HTTP_ERRORS.get(
            r.status_code,
            f"Unexpected HTTP {r.status_code} response."
        )
        return (
            f"Android tool failed (HTTP {r.status_code}): {explanation} "
            f"Do not retry with different actions — report this error to the user directly."
        )

    except httpx.ConnectTimeout:
        return (
            "Android tool failed: Connection timed out. "
            "The Android REST API server is not responding. "
            "Do not retry — report this to the user directly."
        )
    except httpx.ConnectError:
        return (
            "Android tool failed: Cannot connect to the Android REST API. "
            "The server may be offline or the URL may be wrong. "
            "Do not retry — report this to the user directly."
        )
    except httpx.TimeoutException:
        return (
            "Android tool failed: Request timed out after 30 seconds. "
            "Do not retry — report this to the user directly."
        )
    except Exception as e:
        return (
            f"Android tool failed: {str(e)} "
            f"Do not retry — report this to the user directly."
        )
