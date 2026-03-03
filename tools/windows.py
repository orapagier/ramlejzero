import httpx
import json
import asyncio
import re
from core.config_loader import get_apis

# ── Module-level caches ──
_system_cache: dict | None = None
_api_config_cache: tuple[str, str] | None = None

# ── Tool definition holder (atomic replacement, avoids race conditions) ──
_tool_definition_holder: dict = {}


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

def _get_api_config() -> tuple[str, str]:
    """Return (api_url, token), cached after first call."""
    global _api_config_cache
    if _api_config_cache is not None:
        return _api_config_cache
    cfg = get_apis().get("windows", {})
    _api_config_cache = (cfg["api_url"].rstrip("/") + "/agent", cfg["api_key"])
    return _api_config_cache


# ─────────────────────────────────────────────
# Low-level HTTP
# ─────────────────────────────────────────────

async def _api_call(
    api_url: str,
    token: str,
    path: str,
    body: dict = None,
    timeout: float = 60.0,
) -> dict:
    """
    Low-level POST to the Windows API.
    Raises httpx exceptions on failure — callers handle retries/errors.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(
            method="POST",
            url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            json={"method": "POST", "path": path, "body": body or {}},
        )
        r.raise_for_status()
        return r.json()


async def _api_call_with_retry(
    api_url: str,
    token: str,
    path: str,
    body: dict = None,
    timeout: float = 60.0,
    retries: int = 2,
) -> dict:
    """
    Wraps _api_call with exponential-backoff retry on transient network errors.
    Does NOT retry on HTTP 4xx (client errors).
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _api_call(api_url, token, path, body, timeout)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(1.5 ** attempt)
        except httpx.HTTPStatusError as e:
            # Don't retry 4xx — these are caller mistakes
            if e.response.status_code < 500:
                raise
            last_exc = e
            if attempt < retries:
                await asyncio.sleep(1.5 ** attempt)
    raise last_exc


# ─────────────────────────────────────────────
# Structured error helper
# ─────────────────────────────────────────────

def _error(code: str, message: str, detail: str = "") -> str:
    """Return a consistent JSON error string the AI model can detect."""
    payload: dict = {"success": False, "error": code, "message": message}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload)


# ─────────────────────────────────────────────
# System cache (username + OneDrive paths)
# ─────────────────────────────────────────────

async def _ensure_system_cache() -> dict:
    """
    Fetch and cache system info (username, OneDrive paths) on first call.
    On failure, stores {"_error": <msg>} so callers can surface the problem
    instead of silently degrading.
    """
    global _system_cache
    if _system_cache is not None:
        return _system_cache

    api_url, token = _get_api_config()
    try:
        info_task = _api_call_with_retry(api_url, token, "/system/info")
        env_task  = _api_call_with_retry(api_url, token, "/system/env")
        info, env = await asyncio.gather(info_task, env_task)

        username = info.get("username", "")
        home     = f"C:\\Users\\{username}"

        # Priority: Commercial > Consumer > generic OneDrive
        onedrive_path = (
            env.get("OneDriveCommercial")
            or env.get("OneDriveConsumer")
            or env.get("OneDrive")
        )

        if onedrive_path:
            _system_cache = {
                "username":  username,
                "home":      home,
                "onedrive":  onedrive_path,
                "desktop":   onedrive_path + "\\Desktop",
                "documents": onedrive_path + "\\Documents",
                "downloads": home + "\\Downloads",
            }
        else:
            _system_cache = {
                "username":  username,
                "home":      home,
                "onedrive":  home,
                "desktop":   home + "\\Desktop",
                "documents": home + "\\Documents",
                "downloads": home + "\\Downloads",
            }

    except Exception as e:
        # Cache the error so we surface it rather than silently use empty paths
        _system_cache = {"_error": str(e)}

    return _system_cache


# ─────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────

def _resolve_path(path: str, cache: dict) -> str:
    """
    Resolve path shortcuts (Desktop, Documents, Downloads, OneDrive, ~)
    using cached system info.
    """
    if not cache or not cache.get("home"):
        return path

    # Normalise separators
    p = path.replace("/", "\\")

    # ~ expansion
    if p.startswith("~\\") or p == "~":
        return cache["home"] + "\\" + p.lstrip("~").lstrip("\\")

    shortcuts = {
        "desktop":   cache["desktop"],
        "documents": cache["documents"],
        "downloads": cache["downloads"],
        "onedrive":  cache["onedrive"],
    }

    p_lower = p.lower()
    for name, full_path in shortcuts.items():
        if p_lower.startswith(name + "\\") or p_lower == name:
            # Slice uses len(name) which is positionally correct regardless of case
            suffix = p[len(name):]
            return full_path + suffix

    return path


# Keys that may contain file paths in request bodies
_PATH_KEYS = {"path", "destination", "source", "target", "from", "to"}

# Endpoints where path resolution should be applied
_FILE_ENDPOINTS = frozenset([
    "/files/read", "/files/write", "/files/list", "/files/search",
    "/files/delete", "/files/exists", "/files/link", "/files/copy",
    "/files/move",
])


def _resolve_body_paths(endpoint: str, body_dict: dict, cache: dict) -> dict:
    """
    Auto-resolve shortcut paths in *all* path-like keys of the body
    for file-related endpoints.
    Handles: path, destination, source, target, from, to.
    """
    if not cache or cache.get("_error"):
        return body_dict

    if not any(endpoint.startswith(ep) for ep in _FILE_ENDPOINTS):
        return body_dict

    for key in _PATH_KEYS:
        if key not in body_dict:
            continue
        val = body_dict[key]
        if not isinstance(val, str):
            continue
        # Skip already-absolute Windows paths (e.g. "C:\...")
        if len(val) > 2 and val[1] == ":" and val[2] == "\\":
            continue
        body_dict[key] = _resolve_path(val, cache)

    return body_dict


# ─────────────────────────────────────────────
# Headless-safe shell command patching
# ─────────────────────────────────────────────

# Commands that require a Win32 window handle and fail in headless/agent contexts,
# mapped to their safe headless equivalents.
_HEADLESS_SUBSTITUTIONS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"Clear-RecycleBin\b[^;|&\n]*", re.IGNORECASE),
        "Remove-Item 'C:\\$Recycle.Bin\\*' -Recurse -Force -ErrorAction SilentlyContinue",
    ),
]


def _make_headless_safe(command: str) -> str:
    """
    Substitute known Win32-interactive commands with headless-safe equivalents,
    and inject -NonInteractive -NoProfile into bare powershell invocations.
    """
    for pattern, replacement in _HEADLESS_SUBSTITUTIONS:
        command = pattern.sub(replacement, command)

    # Inject -NonInteractive -NoProfile for powershell calls that don't already have them
    def _patch_ps(m: re.Match) -> str:
        flags = m.group(0)
        if "-NonInteractive" not in flags:
            flags += " -NonInteractive"
        if "-NoProfile" not in flags:
            flags += " -NoProfile"
        return flags

    command = re.sub(
        r"(?i)(powershell(?:\.exe)?)\s*(-[^\s]+\s*)*",
        lambda m: m.group(0) if "-NonInteractive" in m.group(0) else _patch_ps(m),
        command,
        count=1,
    )

    return command


# ─────────────────────────────────────────────
# Tool definition
# ─────────────────────────────────────────────

_DESCRIPTION_TEMPLATE = """Control this Windows machine via REST API.
Supports: shell commands, screenshots, file management, process control, and window management.

Usage:
1. Use method: "POST".
2. Set path to the endpoint (e.g. /shell, /screenshot/save, /files/list).
3. Set body to a JSON string of parameters (use "{}" if none).

__PATHS_BLOCK__

RULES:
- Path shortcuts resolve automatically: "Desktop/file.txt", "Documents/x.txt", etc.
- Resolved keys: path, destination, source, target, from, to.
- Use PowerShell commands for most shell tasks; cmd.exe for legacy batch ops.
- Avoid Win32-interactive cmdlets (e.g. Clear-RecycleBin) — use filesystem equivalents.
- Errors are returned as JSON: {{"success": false, "error": "...", "message": "..."}}
"""


def _build_tool_definition(cache: dict | None = None) -> dict:
    """Build the tool definition dict, injecting resolved paths when available."""
    if cache and cache.get("username") and not cache.get("_error"):
        u = cache["username"]
        paths_block = (
            f"WINDOWS USERNAME: {u}\n"
            f"ACTIVE DESKTOP: {cache['desktop']}\n"
            "COMMON PATHS:\n"
            f"  Desktop   → {cache['desktop']}\n"
            f"  Documents → {cache['documents']}\n"
            f"  Downloads → {cache['downloads']}\n"
            f"  OneDrive  → {cache['onedrive']}"
        )
    else:
        paths_block = (
            'Shortcuts resolve automatically: "Desktop/file.txt", "OneDrive/file.txt", etc.'
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
            "empty the recycle bin",
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


# Initial definition (no cache yet)
_tool_definition_holder["def"] = _build_tool_definition()


def get_tool_definition() -> dict:
    """Live accessor — always returns the most up-to-date tool definition."""
    return _tool_definition_holder["def"]


# Keep TOOL_DEFINITION as a convenience alias pointing to the holder
# (external imports should prefer get_tool_definition() for live updates)
TOOL_DEFINITION = _tool_definition_holder["def"]


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────

async def execute(method: str, path: str, body: str = "{}") -> str:
    """
    Execute a windows_tool call.

    Returns:
        JSON string — either the API response or a structured error object.
    """
    api_url, token = _get_api_config()

    # ── 1. Populate system cache (once) ──
    cache = await _ensure_system_cache()

    # ── 2. Surface cache errors as warnings (don't hard-fail) ──
    cache_warning = ""
    if cache.get("_error"):
        cache_warning = f"[warn] System cache unavailable: {cache['_error']}. Path shortcuts disabled.\n"

    # ── 3. Update tool definition atomically (once cache is ready) ──
    current_def = _tool_definition_holder["def"]
    if cache.get("username") and "USERNAME:" not in current_def.get("description", ""):
        _tool_definition_holder["def"] = _build_tool_definition(cache)

    # ── 4. Parse and validate JSON body ──
    raw_body = (body or "{}").strip()
    if raw_body in ("", "null", "{}"):
        body_dict: dict = {}
    else:
        try:
            parsed = json.loads(raw_body)
            if not isinstance(parsed, dict):
                return _error("invalid_body", "Body must be a JSON object, not a scalar or array", raw_body)
            body_dict = parsed
        except json.JSONDecodeError as e:
            return _error("invalid_json", f"Could not parse body as JSON: {e}", raw_body)

    # ── 5. Apply headless-safe patching for shell commands ──
    if path == "/shell" and "command" in body_dict:
        body_dict["command"] = _make_headless_safe(body_dict["command"])

    # ── 6. Auto-resolve path shortcuts in body ──
    body_dict = _resolve_body_paths(path, body_dict, cache)

    # ── 7. Choose timeout based on endpoint ──
    if path in ("/shell", "/shell/run"):
        timeout = 120.0   # shell commands can be slow
    elif path.startswith("/screenshot"):
        timeout = 20.0
    else:
        timeout = 60.0

    # ── 8. Execute with retry ──
    try:
        result = await _api_call_with_retry(api_url, token, path, body_dict, timeout=timeout)
        output = json.dumps(result, indent=2) if isinstance(result, (dict, list)) else str(result)
        return cache_warning + output if cache_warning else output

    except httpx.TimeoutException:
        return _error(
            "timeout",
            f"Request to {path} timed out after {timeout:.0f}s.",
            "For long-running shell commands, consider splitting the task or using background execution.",
        )
    except httpx.HTTPStatusError as e:
        return _error(
            "http_error",
            f"HTTP {e.response.status_code} from Windows API",
            e.response.text[:500],
        )
    except Exception as e:
        return _error("api_error", str(e))
