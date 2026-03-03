"""
main.py — Agent Server
=======================
Zero platform-specific code. All messaging is handled by the
adapter loaded from messaging/__init__.py based on settings.yaml.

To switch messaging platforms:
  1. Change messaging.platform in settings.yaml
  2. Add platform config block (see messaging/<platform>.py docstring)
  3. Restart — that's it. No code changes here.
"""

import asyncio
import os
import json
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

import agent
from core import scheduler
from core import logger as log_module
from core import rate_limiter
from core import model_router
from core.config_loader import get_settings, reload_configs
from tools import get_tool_definitions, reload_tools, TOOLS
from messaging import get_platform, IncomingMessage, OutgoingFile, MessagingPlatform

# ── Bootstrap ──────────────────────────────────────────────────────────────

settings = get_settings()
logger = log_module.setup()

# Conversation histories per user (keyed by platform user_id string)
_histories: dict[str, list] = {}

# Config file registry for dashboard editor
_CONFIG_FILES: dict[str, str] = {
    "settings.yaml":     "config/settings.yaml",
    "apis.yaml":         "config/apis.yaml",
    "models.yaml":       "config/models.yaml",
    "system_prompt.txt": "config/system_prompt.txt",
    "tool_router.py":    "core/tool_router.py",
}


def _max_history() -> int:
    return get_settings().get("agent", {}).get("history_max_exchanges", 6) * 2


# ── Token expiry watcher ───────────────────────────────────────────────────

WATCHED_TOKENS = {
    "Facebook": "/auth/facebook_token.json",
    # "SomeService": "/auth/someservice_token.json",
}

WARN_AT_DAYS = [14, 7, 3, 1]


async def _token_expiry_watcher(platform: MessagingPlatform):
    await asyncio.sleep(10)

    while True:
        for service, token_path in WATCHED_TOKENS.items():
            try:
                if not os.path.exists(token_path):
                    continue
                with open(token_path) as f:
                    data = json.load(f)
                expires_at = data.get("expires_at", 0)
                if not expires_at:
                    continue
                days_left = int((expires_at - time.time()) / 86400)
                if days_left in WARN_AT_DAYS:
                    msg = (
                        f"⚠️ *{service} Token Expiring Soon*\n\n"
                        f"Your {service} auth token expires in "
                        f"*{days_left} day{'s' if days_left > 1 else ''}*.\n"
                        f"Please re-auth via the dashboard → Auth tab → "
                        f"{service} → Re-auth.\n\n"
                        f"Without re-auth, your agent will lose access to "
                        f"your {service} account."
                    )
                    await platform.notify(msg)
                    logger.info(
                        f"Token expiry warning sent: {service} — {days_left} days left"
                    )
            except Exception as e:
                logger.warning(f"Token watcher error [{service}]: {e}")

        await asyncio.sleep(86400)


# ── Core message handler ───────────────────────────────────────────────────

async def handle_incoming(msg: IncomingMessage, platform: MessagingPlatform):
    if msg.text == "__clear__":
        _histories.pop(msg.user_id, None)
        return

    # ── Typing indicator + heartbeat ──────────────────────────────────────
    stop_typing = asyncio.Event()

    HEARTBEAT_MESSAGES = [
        "⏳ Still working on it, hang tight...",
        "🔄 Still processing, almost there...",
        "🧠 Still thinking, give me a moment...",
        "⚙️ Working on it, this one's taking a bit...",
        "🔍 Still digging, won't be long...",
    ]
    heartbeat_index = 0

    async def _typing_loop():
        nonlocal heartbeat_index
        elapsed = 0
        while not stop_typing.is_set():
            try:
                await platform.send_typing(msg.chat_id)
            except Exception:
                pass
            await asyncio.sleep(4)
            elapsed += 4
            # Send a heartbeat text every 30 seconds
            if elapsed % 30 == 0:
                try:
                    heartbeat_msg = HEARTBEAT_MESSAGES[heartbeat_index % len(HEARTBEAT_MESSAGES)]
                    heartbeat_index += 1
                    await platform.send_message(msg.chat_id, heartbeat_msg)
                except Exception:
                    pass

    typing_task = asyncio.create_task(_typing_loop())

    try:
        history = _histories.get(msg.user_id, [])

        response = await agent.run(
            user_message=msg.text,
            user_id=msg.user_id,
            conversation_history=history,
        )
    finally:
        stop_typing.set()
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass

    history.append({"role": "user", "content": msg.text})
    if response.text:
        history.append({"role": "assistant", "content": response.text})
    _histories[msg.user_id] = history[-_max_history():]

    logger.info(
        f"Run complete | platform={msg.platform} | user={msg.user_id} | "
        f"model={response.model_used} | iterations={response.iterations} | "
        f"tokens={response.total_input_tokens}in+{response.total_output_tokens}out | "
        f"duration={response.duration_ms:.0f}ms | "
        f"history_len={len(_histories[msg.user_id])}"
    )

    if response.file_bytes:
        await platform.send_file(
            msg.chat_id,
            OutgoingFile(
                data=response.file_bytes,
                filename=response.filename,
                caption=response.text,
            )
        )
    elif response.text:
        await platform.send_message(msg.chat_id, response.text)
    else:
        await platform.send_message(msg.chat_id, "[No response]")


# ── FastAPI lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    platform = get_platform()
    platform.set_message_handler(handle_incoming)
    platform.register_webhook(api)
    await platform.start(app)
    logger.info(f"Messaging platform started: {platform.name}")
    app.state.platform = platform

    # ── Proactive scheduler ──
    _engine, _cron = await scheduler.start(platform)
    app.state.monitor_engine = _engine
    app.state.cron_manager   = _cron
    # ─────────────────────────

    watcher = asyncio.create_task(_token_expiry_watcher(platform))
    logger.info("Token expiry watcher started")
    yield
    watcher.cancel()
    await scheduler.stop(_engine, _cron)
    await platform.stop()


# ── FastAPI app ────────────────────────────────────────────────────────────

api = FastAPI(title="Agent", lifespan=lifespan)

security = HTTPBasic()


def verify(credentials: HTTPBasicCredentials = Depends(security)):
    secret = get_settings().get("web_ui", {}).get("secret", "changeme")
    if credentials.password != secret:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return credentials


# ── Core routes ────────────────────────────────────────────────────────────

@api.get("/health")
async def health():
    return {
        "status": "ok",
        "platform": get_settings().get("messaging", {}).get("platform", "telegram"),
        "tools": len(TOOLS),
        "models": len(model_router.get_all_model_status()),
    }


@api.get("/api/stats")
async def api_stats():
    return log_module.get_total_stats()


# ── Web UI ──────────────────────────────────────────────────────────────────

@api.get("/", response_class=HTMLResponse)
async def web_ui(creds=Depends(verify)):
    with open("web_ui/index.html", encoding="utf-8") as f:
        return f.read()


# ── Tools ───────────────────────────────────────────────────────────────────

@api.get("/api/tools")
async def api_tools(creds=Depends(verify)):
    return [
        {
            "name": t["definition"]["name"],
            "description": t["definition"]["description"],
            "module": t["module"],
            "parameters": t["definition"].get("parameters", {}),
        }
        for t in TOOLS.values()
    ]


@api.post("/api/tools/upload")
async def api_upload_tool(file: UploadFile = File(...), creds=Depends(verify)):
    if not file.filename.endswith(".py"):
        raise HTTPException(400, "Only .py files allowed")
    content = await file.read()
    with open(f"tools/{file.filename}", "wb") as f:
        f.write(content)
    reload_tools()
    return {"status": "loaded", "total_tools": len(TOOLS)}


@api.delete("/api/tools/{module_name}")
async def api_disable_tool(module_name: str, creds=Depends(verify)):
    src, dst = f"tools/{module_name}.py", f"tools/{module_name}.disabled"
    if not os.path.exists(src):
        raise HTTPException(404, "Tool not found")
    os.rename(src, dst)
    reload_tools()
    return {"status": "disabled"}


@api.post("/api/tools/{module_name}/enable")
async def api_enable_tool(module_name: str, creds=Depends(verify)):
    src, dst = f"tools/{module_name}.disabled", f"tools/{module_name}.py"
    if not os.path.exists(src):
        raise HTTPException(404, "Disabled tool not found")
    os.rename(src, dst)
    reload_tools()
    return {"status": "enabled"}


@api.get("/api/tools/{module_name}/source")
async def api_get_tool_source(module_name: str, creds=Depends(verify)):
    path = f"tools/{module_name}.py"
    if not os.path.exists(path):
        raise HTTPException(404, "Tool not found")
    with open(path, "r", encoding="utf-8") as f:
        return {"module": module_name, "content": f.read()}


@api.post("/api/tools/{module_name}/source")
async def api_save_tool_source(module_name: str, request: Request, creds=Depends(verify)):
    data = await request.json()
    content = data.get("content")
    if content is None:
        raise HTTPException(400, "Missing content")
    with open(f"tools/{module_name}.py", "w", encoding="utf-8") as f:
        f.write(content)
    reload_tools()
    return {"status": "saved", "total_tools": len(TOOLS)}


# ── Models ──────────────────────────────────────────────────────────────────

@api.get("/api/models")
async def api_models(creds=Depends(verify)):
    return model_router.get_all_model_status()


@api.post("/api/models/{model_name}/reset")
async def api_reset_model(model_name: str, creds=Depends(verify)):
    if model_router.reset_model(model_name):
        return {"status": "reset"}
    raise HTTPException(404, "Model not found")


# ── Rate limits ──────────────────────────────────────────────────────────────

@api.get("/api/rate-limits")
async def api_rate_limits(creds=Depends(verify)):
    return rate_limiter.get_all_states()


@api.get("/api/rate-limits/models")
async def api_model_rate_limits(creds=Depends(verify)):
    """
    Return per-model rate limit status including:
    - Live header snapshot from last API call
    - Provider free / paid tier baselines
    Used by the Rate Limits dashboard tab.
    """
    return model_router.get_model_rate_limit_status()


# ── Logs ─────────────────────────────────────────────────────────────────────

@api.get("/api/logs/runs")
async def api_runs(limit: int = 20, creds=Depends(verify)):
    return log_module.get_recent_runs(limit)


@api.get("/api/logs/runs/{run_id}/tools")
async def api_run_tools(run_id: int, creds=Depends(verify)):
    return log_module.get_run_tools(run_id)


@api.get("/api/logs/models")
async def api_model_events(creds=Depends(verify)):
    return log_module.get_model_events()


# ── Config ────────────────────────────────────────────────────────────────────

@api.post("/api/config/reload")
async def api_reload_config(creds=Depends(verify)):
    reload_configs()
    reload_tools()
    model_router.reload_models()
    return {"status": "reloaded"}


@api.get("/api/config/{filename}")
async def api_get_config(filename: str, creds=Depends(verify)):
    if filename not in _CONFIG_FILES:
        raise HTTPException(400, "Invalid config file")
    path = _CONFIG_FILES[filename]
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    with open(path, "r", encoding="utf-8") as f:
        return {"content": f.read()}


@api.post("/api/config/{filename}")
async def api_save_config(filename: str, request: Request, creds=Depends(verify)):
    if filename not in _CONFIG_FILES:
        raise HTTPException(400, "Invalid config file")
    data = await request.json()
    content = data.get("content")
    if content is None:
        raise HTTPException(400, "Missing content")
    path = _CONFIG_FILES[filename]
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if filename == "tool_router.py":
        import importlib, sys
        if "core.tool_router" in sys.modules:
            importlib.reload(sys.modules["core.tool_router"])
            logger.info("tool_router reloaded after dashboard edit")
    return {"status": "saved"}


# ════════════════════════════════════════════════════════════════════════════
# AUTH / INTEGRATIONS
# ════════════════════════════════════════════════════════════════════════════

def _get_token_registry() -> dict:
    from core.config_loader import get_apis
    apis = get_apis()
    return {
        service: cfg["token_file"]
        for service, cfg in apis.items()
        if isinstance(cfg, dict) and "token_file" in cfg
    }


@api.get("/api/auth/status")
async def api_auth_status(creds=Depends(verify)):
    registry = _get_token_registry()
    result = {}

    for service, token_path in registry.items():
        if not os.path.isabs(token_path):
            token_path = os.path.join("/app", token_path)
        if not os.path.exists(token_path):
            result[service] = {"connected": False}
            continue
        try:
            with open(token_path) as f:
                data = json.load(f)

            expires_at = data.get("expires_at", 0)
            now = time.time()

            if expires_at and now > expires_at:
                if data.get("refresh_token"):
                    expires_in = "token expired (will auto-refresh)"
                    connected = True
                else:
                    result[service] = {"connected": False, "error": "Token expired, re-auth needed"}
                    continue
            else:
                secs = int(expires_at - now) if expires_at else None
                if secs and secs < 300:
                    expires_in = f"{secs}s (refreshing soon)"
                elif secs:
                    hrs = secs // 3600
                    mins = (secs % 3600) // 60
                    expires_in = f"{hrs}h {mins}m" if hrs else f"{mins}m"
                else:
                    expires_in = None
                connected = True

            result[service] = {
                "connected": connected,
                "account": data.get("account") or data.get("email") or data.get("upn") or None,
                "expires_in": expires_in if connected else None,
                "scope": data.get("scope", ""),
            }
        except Exception as e:
            result[service] = {"connected": False, "error": str(e)}

    return result


@api.get("/api/auth/token-files")
async def api_token_files(creds=Depends(verify)):
    import datetime
    registry = _get_token_registry()
    files = []
    for service, path in registry.items():
        abs_path = path if os.path.isabs(path) else os.path.join("/app", path)
        if os.path.exists(abs_path):
            stat = os.stat(abs_path)
            files.append({
                "service": service,
                "path": path,
                "size_bytes": stat.st_size,
                "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return files


@api.delete("/api/auth/token-files/{service}")
async def api_delete_token_file(service: str, creds=Depends(verify)):
    registry = _get_token_registry()
    if service not in registry:
        raise HTTPException(404, f"Unknown service: {service}")
    path = registry[service]
    if not os.path.isabs(path):
        path = os.path.join("/app", path)
    if not os.path.exists(path):
        raise HTTPException(404, "Token file not found")
    os.remove(path)
    logger.info(f"Token file deleted for service={service} by dashboard")
    return {"status": "deleted", "service": service}


# ── Shared OAuth HTML helpers ────────────────────────────────────────────────

def _oauth_success_html(service: str) -> str:
    return f"""
        <html><head><style>
            body {{ font-family: -apple-system, sans-serif; background:#0f1117; color:#e2e8f0;
                   display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
            .box {{ text-align:center; background:#1a1d2e; border:1px solid #276749;
                   border-radius:14px; padding:40px 48px; }}
            h2 {{ color:#9ae6b4; margin-bottom:8px; }}
            p  {{ color:#718096; font-size:14px; }}
        </style></head>
        <body><div class="box">
            <h2>✅ {service} Connected</h2>
            <p>Authentication successful. You can close this tab.</p>
            <script>setTimeout(() => window.close(), 2000);</script>
        </div></body></html>"""


def _oauth_error_html(service: str, error: str) -> str:
    return f"""
        <html><head><style>
            body {{ font-family: -apple-system, sans-serif; background:#0f1117; color:#e2e8f0;
                   display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
            .box {{ text-align:center; background:#1a1d2e; border:1px solid #742a2a;
                   border-radius:14px; padding:40px 48px; }}
            h2 {{ color:#feb2b2; margin-bottom:8px; }}
            p  {{ color:#718096; font-size:14px; }}
        </style></head>
        <body><div class="box">
            <h2>❌ {service} Authentication Failed</h2>
            <p>{error}</p>
        </div></body></html>"""


# ── Microsoft OAuth ──────────────────────────────────────────────────────────

@api.get("/api/auth/microsoft/url")
async def api_ms_auth_url(creds=Depends(verify)):
    from auth.microsoft import get_auth_url
    return {"auth_url": get_auth_url()}


@api.get("/auth/microsoft/callback")
async def auth_microsoft_callback(code: str, state: str):
    from auth.microsoft import complete_auth
    try:
        complete_auth(code, state)
        logger.info("Microsoft OAuth completed")
        return HTMLResponse(_oauth_success_html("Microsoft"))
    except Exception as e:
        logger.error(f"Microsoft OAuth failed: {e}")
        return HTMLResponse(_oauth_error_html("Microsoft", str(e)), status_code=400)


@api.delete("/api/auth/microsoft/disconnect")
async def api_ms_disconnect(creds=Depends(verify)):
    from auth.microsoft import _token_path
    path = _token_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info("Microsoft disconnected")
        return {"status": "disconnected"}
    return {"status": "already_disconnected"}


# ── Google OAuth ──────────────────────────────────────────────────────────────

@api.get("/api/auth/google/url")
async def api_google_auth_url(creds=Depends(verify)):
    from auth.google import get_auth_url
    return {"auth_url": get_auth_url()}


@api.get("/auth/google/callback")
async def auth_google_callback(code: str, state: str):
    from auth.google import complete_auth
    try:
        complete_auth(code, state)
        logger.info("Google OAuth completed")
        return HTMLResponse(_oauth_success_html("Google"))
    except Exception as e:
        logger.error(f"Google OAuth failed: {e}")
        return HTMLResponse(_oauth_error_html("Google", str(e)), status_code=400)


@api.delete("/api/auth/google/disconnect")
async def api_google_disconnect(creds=Depends(verify)):
    from auth.google import _token_path
    path = _token_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info("Google disconnected")
        return {"status": "disconnected"}
    return {"status": "already_disconnected"}


# ── Facebook OAuth ────────────────────────────────────────────────────────────

@api.get("/api/auth/facebook/url")
async def api_facebook_auth_url(creds=Depends(verify)):
    from auth.facebook import get_auth_url
    return {"auth_url": get_auth_url()}


@api.get("/auth/facebook/callback")
async def auth_facebook_callback(code: str, state: str):
    from auth.facebook import complete_auth
    try:
        complete_auth(code, state)
        logger.info("Facebook OAuth completed")
        return HTMLResponse(_oauth_success_html("Facebook"))
    except Exception as e:
        logger.error(f"Facebook OAuth failed: {e}")
        return HTMLResponse(_oauth_error_html("Facebook", str(e)), status_code=400)


@api.delete("/api/auth/facebook/disconnect")
async def api_facebook_disconnect(creds=Depends(verify)):
    from auth.facebook import _token_path
    path = _token_path()
    if os.path.exists(path):
        os.remove(path)
        logger.info("Facebook disconnected")
        return {"status": "disconnected"}
    return {"status": "already_disconnected"}


# ════════════════════════════════════════════════════════════════════════════
# MESSAGING PLATFORM MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════

_MESSAGING_DIR = "messaging"


@api.get("/api/messaging/adapters")
async def api_messaging_adapters(creds=Depends(verify)):
    skip = {"__init__", "base"}
    adapters = []
    for filename in sorted(os.listdir(_MESSAGING_DIR)):
        if not filename.endswith(".py"):
            continue
        module = filename[:-3]
        if module in skip:
            continue
        adapters.append({
            "module": module,
            "filename": filename,
            "active": module == get_settings().get("messaging", {}).get("platform", "telegram"),
        })
    return adapters


@api.get("/api/messaging/adapters/{module}/source")
async def api_get_adapter_source(module: str, creds=Depends(verify)):
    path = os.path.join(_MESSAGING_DIR, f"{module}.py")
    if not os.path.exists(path):
        raise HTTPException(404, "Adapter not found")
    with open(path, "r", encoding="utf-8") as f:
        return {"module": module, "content": f.read()}


@api.post("/api/messaging/adapters/{module}/source")
async def api_save_adapter_source(module: str, request: Request, creds=Depends(verify)):
    data = await request.json()
    content = data.get("content")
    if content is None:
        raise HTTPException(400, "Missing content")
    path = os.path.join(_MESSAGING_DIR, f"{module}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"Messaging adapter saved: {module}.py")
    return {"status": "saved", "note": "Restart required to apply platform changes"}


@api.post("/api/messaging/adapters/upload")
async def api_upload_adapter(file: UploadFile = File(...), creds=Depends(verify)):
    if not file.filename.endswith(".py"):
        raise HTTPException(400, "Only .py files allowed")
    content = await file.read()
    path = os.path.join(_MESSAGING_DIR, file.filename)
    with open(path, "wb") as f:
        f.write(content)
    logger.info(f"Messaging adapter uploaded: {file.filename}")
    return {"status": "uploaded", "filename": file.filename}


@api.get("/api/messaging/current")
async def api_messaging_current(creds=Depends(verify)):
    cfg = get_settings().get("messaging", {})
    return {
        "platform": cfg.get("platform", "telegram"),
        "primary_user_id": cfg.get("primary_user_id"),
    }

# ════════════════════════════════════════════════════════════════════════════
# MONITOR ROUTES
# ════════════════════════════════════════════════════════════════════════════

from fastapi import Request, HTTPException, Depends
from core import monitor_db
from monitors import get_all_types, get_adapter
import json


@api.get("/api/monitors/types")
async def api_monitor_types(creds=Depends(verify)):
    """All registered adapter types + their config schemas (for dashboard form builder)."""
    return get_all_types()


@api.get("/api/monitors")
async def api_list_monitors(creds=Depends(verify)):
    return monitor_db.get_all_monitors()


@api.get("/api/monitors/stats")
async def api_monitor_stats(creds=Depends(verify)):
    return monitor_db.get_monitor_stats()


@api.post("/api/monitors")
async def api_create_monitor(request: Request, creds=Depends(verify)):
    data = await request.json()
    required = ("name", "type", "interval_s")
    for f in required:
        if f not in data:
            raise HTTPException(400, f"Missing field: {f}")

    monitor_id = monitor_db.create_monitor(
        name       = data["name"],
        type_      = data["type"],
        config     = data.get("config", {}),
        actions    = data.get("actions", []),
        interval_s = int(data["interval_s"]),
    )

    # Hot-reload engine
    request.app.state.monitor_engine.reload()
    return {"id": monitor_id, "status": "created"}


@api.put("/api/monitors/{monitor_id}")
async def api_update_monitor(monitor_id: int, request: Request, creds=Depends(verify)):
    data = await request.json()
    if not monitor_db.get_monitor(monitor_id):
        raise HTTPException(404, "Monitor not found")

    # Normalise JSON fields coming as strings or dicts
    for field in ("config", "actions"):
        if field in data and isinstance(data[field], str):
            try:
                data[field] = json.loads(data[field])
            except Exception:
                pass

    monitor_db.update_monitor(monitor_id, **data)
    request.app.state.monitor_engine.reload()
    return {"status": "updated"}


@api.delete("/api/monitors/{monitor_id}")
async def api_delete_monitor(monitor_id: int, request: Request, creds=Depends(verify)):
    if not monitor_db.get_monitor(monitor_id):
        raise HTTPException(404, "Monitor not found")
    monitor_db.delete_monitor(monitor_id)
    request.app.state.monitor_engine.reload()
    return {"status": "deleted"}


@api.post("/api/monitors/{monitor_id}/toggle")
async def api_toggle_monitor(monitor_id: int, request: Request, creds=Depends(verify)):
    m = monitor_db.get_monitor(monitor_id)
    if not m:
        raise HTTPException(404, "Monitor not found")
    new_state = 0 if m["enabled"] else 1
    monitor_db.update_monitor(monitor_id, enabled=new_state)
    request.app.state.monitor_engine.reload()
    return {"enabled": bool(new_state)}


@api.post("/api/monitors/{monitor_id}/run-now")
async def api_run_monitor_now(monitor_id: int, request: Request, creds=Depends(verify)):
    if not monitor_db.get_monitor(monitor_id):
        raise HTTPException(404, "Monitor not found")
    import asyncio
    asyncio.create_task(
        request.app.state.monitor_engine.run_monitor_now(monitor_id),
        name=f"manual-poll-{monitor_id}",
    )
    return {"status": "triggered"}


@api.post("/api/monitors/{monitor_id}/reset-cursor")
async def api_reset_monitor_cursor(monitor_id: int, creds=Depends(verify)):
    """Clear cursor so next poll re-scans from scratch."""
    if not monitor_db.get_monitor(monitor_id):
        raise HTTPException(404, "Monitor not found")
    monitor_db.reset_monitor_state(monitor_id)
    return {"status": "cursor_cleared"}


@api.get("/api/monitors/{monitor_id}/runs")
async def api_monitor_runs(monitor_id: int, limit: int = 30, creds=Depends(verify)):
    return monitor_db.get_monitor_runs(monitor_id, limit)


@api.post("/api/monitors/test")
async def api_test_monitor(request: Request, creds=Depends(verify)):
    """Test a monitor config without saving it."""
    data    = await request.json()
    type_   = data.get("type")
    config  = data.get("config", {})
    adapter = get_adapter(type_)
    if not adapter:
        raise HTTPException(400, f"Unknown monitor type: {type_}")
    result = await adapter.test(config)
    return result


# ════════════════════════════════════════════════════════════════════════════
# CRON JOB ROUTES
# ════════════════════════════════════════════════════════════════════════════

@api.get("/api/cron")
async def api_list_cron_jobs(request: Request, creds=Depends(verify)):
    return request.app.state.cron_manager.get_all_statuses()


@api.get("/api/cron/stats")
async def api_cron_stats(creds=Depends(verify)):
    return monitor_db.get_cron_stats()


@api.post("/api/cron")
async def api_create_cron_job(request: Request, creds=Depends(verify)):
    data = await request.json()
    for f in ("name", "cron_expr", "prompt"):
        if not data.get(f):
            raise HTTPException(400, f"Missing field: {f}")

    # Basic cron validation
    from core.cron_manager import _HAS_APSCHEDULER
    if _HAS_APSCHEDULER:
        from apscheduler.triggers.cron import CronTrigger
        try:
            CronTrigger.from_crontab(data["cron_expr"], timezone="UTC")
        except Exception as e:
            raise HTTPException(400, f"Invalid cron expression: {e}")

    job_id = monitor_db.create_cron_job(
        name      = data["name"],
        cron_expr = data["cron_expr"],
        prompt    = data["prompt"],
        mode      = data.get("mode", "autonomous"),
        chat_id   = data.get("chat_id") or None,
    )
    request.app.state.cron_manager.reload()
    return {"id": job_id, "status": "created"}


@api.put("/api/cron/{job_id}")
async def api_update_cron_job(job_id: int, request: Request, creds=Depends(verify)):
    data = await request.json()
    if not monitor_db.get_cron_job(job_id):
        raise HTTPException(404, "Cron job not found")

    if "cron_expr" in data:
        from core.cron_manager import _HAS_APSCHEDULER
        if _HAS_APSCHEDULER:
            from apscheduler.triggers.cron import CronTrigger
            try:
                CronTrigger.from_crontab(data["cron_expr"], timezone="UTC")
            except Exception as e:
                raise HTTPException(400, f"Invalid cron expression: {e}")

    monitor_db.update_cron_job(job_id, **data)
    request.app.state.cron_manager.reload()
    return {"status": "updated"}


@api.delete("/api/cron/{job_id}")
async def api_delete_cron_job(job_id: int, request: Request, creds=Depends(verify)):
    if not monitor_db.get_cron_job(job_id):
        raise HTTPException(404, "Cron job not found")
    monitor_db.delete_cron_job(job_id)
    request.app.state.cron_manager.reload()
    return {"status": "deleted"}


@api.post("/api/cron/{job_id}/toggle")
async def api_toggle_cron_job(job_id: int, request: Request, creds=Depends(verify)):
    job = monitor_db.get_cron_job(job_id)
    if not job:
        raise HTTPException(404, "Cron job not found")
    new_state = 0 if job["enabled"] else 1
    monitor_db.update_cron_job(job_id, enabled=new_state)
    request.app.state.cron_manager.reload()
    return {"enabled": bool(new_state)}


@api.post("/api/cron/{job_id}/run-now")
async def api_run_cron_now(job_id: int, request: Request, creds=Depends(verify)):
    if not monitor_db.get_cron_job(job_id):
        raise HTTPException(404, "Cron job not found")
    import asyncio
    asyncio.create_task(
        request.app.state.cron_manager.run_job_now(job_id),
        name=f"manual-cron-{job_id}",
    )
    return {"status": "triggered"}


@api.get("/api/cron/{job_id}/runs")
async def api_cron_runs(job_id: int, limit: int = 20, creds=Depends(verify)):
    return monitor_db.get_cron_runs(job_id, limit)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = get_settings().get("web_ui", {}).get("port", 8000)
    uvicorn.run("main:api", host="0.0.0.0", port=port, reload=False)
