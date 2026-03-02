import httpx
import json
import asyncio
from core.config_loader import get_apis

# ── Cached system info (resolved on first call) ──
_system_cache: dict | None = None
_cache_lock = asyncio.Lock()


def _get_api_config():
    cfg = get_apis().get("windows", {})
    return cfg["api_url"].rstrip("/") + "/agent", cfg["api_key"]


async def _api_call(api_url: str, token: str, path: str, body: dict = None) -> dict:
    """Low-level POST to the Windows API."""
    async with httpx.AsyncClient() as client:
        r = await client.request(
            method="POST",
            url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            json={"method": "POST", "path": path, "body": body or {}},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


async def _ensure_system_cache() -> dict:
    """Fetch and cache system info (username, OneDrive paths) on first call.
    Lock prevents concurrent requests from firing duplicate /system/info calls."""
    global _system_cache

    # Fast path — already populated, no lock needed
    if _system_cache is not None:
        return _system_cache

    async with _cache_lock:
        # Re-check inside lock in case another coroutine populated it while we waited
        if _system_cache is not None:
            return _system_cache

        api_url, token = _get_api_config()
        try:
            # Fetch both system info AND environment variables for accurate OneDrive detection
            info_task = _api_call(api_url, token, "/system/info")
            env_task = _api_call(api_url, token, "/system/env")
            info, env = await asyncio.gather(info_task, env_task)

            username = info.get("username", "")
            home = f"C:\\Users\\{username}"

            # Detect active OneDrive path from environment
            onedrive_path = env.get("OneDriveCommercial") or env.get("OneDriveConsumer") or env.get("OneDrive")

            if onedrive_path:
                _system_cache = {
                    "username": username,
                    "home": home,
                    "onedrive": onedrive_path,
                    "desktop": onedrive_path + "\\Desktop",
                    "documents": onedrive_path + "\\Documents",
                    "downloads": home + "\\Downloads",
                }
            else:
                _system_cache = {
                    "username": username,
                    "home": home,
                    "onedrive": home,
                    "desktop": home + "\\Desktop",
                    "documents": home + "\\Documents",
                    "downloads": home + "\\Downloads",
                }
        except Exception:
            _system_cache = {}

    return _system_cache


def _resolve_path(path: str, cache: dict) -> str:
    """Resolve path shortcuts using cached system info."""
    if not cache or not cache.get("home"):
        return path

    p = path.replace("/", "\\")

    if p.startswith("~\\") or p.startswith("~"):
        return cache["home"] + "\\" + p.lstrip("~").lstrip("\\")

    shortcuts = {
        "desktop": cache["desktop"],
        "documents": cache["documents"],
        "downloads": cache["downloads"],
        "onedrive": cache["onedrive"],
    }
    p_lower = p.lower()
    for name, full_path in shortcuts.items():
        if p_lower.startswith(name + "\\") or p_lower == name:
            return full_path + p[len(name):]

    return path


def _resolve_body_paths(path: str, body_dict: dict, cache: dict) -> dict:
    """Auto-resolve shortcut paths in body for file-related endpoints."""
    if not cache:
        return body_dict

    file_endpoints = [
        "/files/read", "/files/write", "/files/list", "/files/search",
        "/files/delete", "/files/exists", "/files/link",
    ]
    if any(path.startswith(ep) for ep in file_endpoints):
        if "path" in body_dict and not body_dict["path"].startswith("C:"):
            body_dict["path"] = _resolve_path(body_dict["path"], cache)

    return body_dict


_DESCRIPTION_TEMPLATE = '''Control this Windows machine (local machine) via REST API. 
Supports: shell commands, screenshots, file management, process control, and window management.

Usage: 
1. Use method: "POST".
2. Set path to the endpoint (e.g. /shell, /screenshot/save, /files/list).
3. Set body to a JSON string of parameters (use "{}" if none).

__PATHS_BLOCK__

RULES:
- Resolve shortcuts: "Desktop/file.txt" works automatically.
- Path resolution is handled server-side.
- Technical details are provided in the system directive when this tool is active.
'''


def _build_tool_definition(cache: dict | None = None) -> dict:
    """Build TOOL_DEFINITION, injecting cached system info if available."""
    if cache and cache.get("username"):
        u = cache["username"]
        paths_block = (
            "WINDOWS USERNAME: " + u + "\n"
            "ACTIVE DESKTOP: " + cache["desktop"] + "\n"
            "COMMON PATHS:\n"
            "- Desktop: " + cache["desktop"] + "\n"
            "- Documents: " + cache["documents"] + "\n"
            "- Downloads: " + cache["downloads"]
        )
    else:
        paths_block = (
            'Shortcuts work: "Desktop/file.txt", "OneDrive/file.txt", etc. No /system/info needed.'
        )

    description = _DESCRIPTION_TEMPLATE.replace("__PATHS_BLOCK__", paths_block)

    return {
        "name": "windows_tool",
        "description": description,
        "examples": [
            "what apps are open",
            "close that window",
            "paste this into the form",
            "take a picture of my screen",
        ],
        "parameters": {
            "method": {
                "type": "string",
                "description": "Always POST",
                "enum": ["POST"],
            },
            "path": {
                "type": "string",
                "description": "API endpoint path, e.g. /shell or /screenshot/save?screen=0&format=png",
            },
            "body": {
                "type": "string",
                "description": 'JSON body string. Use "{}" for endpoints with no required fields.',
            },
        },
        "required": ["method", "path", "body"],
    }


TOOL_DEFINITION = _build_tool_definition()

# Lock to make the one-time TOOL_DEFINITION update atomic
_tool_def_lock = asyncio.Lock()
_tool_def_updated = False


async def execute(method: str, path: str, body: str = "{}") -> str:
    global TOOL_DEFINITION, _tool_def_updated

    api_url, token = _get_api_config()

    # Ensure system cache is populated (first call fetches /system/info and /system/env)
    cache = await _ensure_system_cache()

    # Update tool definition with resolved paths — once, atomically
    if not _tool_def_updated and cache and cache.get("username"):
        async with _tool_def_lock:
            if not _tool_def_updated:
                new_def = _build_tool_definition(cache)
                TOOL_DEFINITION.clear()
                TOOL_DEFINITION.update(new_def)
                _tool_def_updated = True

    try:
        body_dict = json.loads(body) if body and body != "{}" else {}
    except json.JSONDecodeError:
        return "Invalid JSON body: " + body

    # Auto-resolve path shortcuts in body
    body_dict = _resolve_body_paths(path, body_dict, cache)

    try:
        result = await _api_call(api_url, token, path, body_dict)
        return json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)
    except httpx.HTTPStatusError as e:
        return "HTTP " + str(e.response.status_code) + ": " + e.response.text[:500]
    except Exception as e:
        return "Windows API error: " + str(e)
