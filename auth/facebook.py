import os
import json
import time
import httpx
import secrets
from urllib.parse import urlencode
from core.config_loader import get_apis, get_settings

GRAPH_VERSION = "v24.0"
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "pages_manage_metadata",
    "pages_messaging",
    "business_management",
    "read_insights",
]


class AuthRequiredError(Exception):
    pass


def _cfg() -> dict:
    return get_apis().get("facebook", {})


def _resolve_path(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join("/app", path)


def _token_path() -> str:
    return _resolve_path(_cfg().get("token_file", "/auth/facebook_token.json"))


def _state_path() -> str:
    return _resolve_path(_cfg().get("state_file", "/auth/facebook_state.json"))


def _redirect_uri() -> str:
    base = get_settings().get("web_ui", {}).get("base_url", "http://localhost:8000").rstrip("/")
    return f"{base}/auth/facebook/callback"


# ── Token management ──────────────────────────────────────────────────────────

def get_user_token() -> str:
    token_path = _token_path()
    if not os.path.exists(token_path):
        raise AuthRequiredError(
            "Facebook auth required. Visit /auth/facebook to authorize."
        )
    with open(token_path) as f:
        data = json.load(f)

    expires_at = data.get("expires_at", 0)
    if expires_at and time.time() > expires_at - 3600:
        raise AuthRequiredError(
            "Facebook token expired. Visit /auth/facebook to re-authorize."
        )
    return data["access_token"]


def get_page_token(page_id: str) -> str:
    """Exchange user token for a never-expiring Page access token."""
    user_token = get_user_token()
    r = httpx.get(
        f"{BASE}/{page_id}",
        params={"fields": "access_token", "access_token": user_token},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "access_token" not in data:
        raise AuthRequiredError(
            f"Could not get page token for page {page_id}. "
            "Ensure you are an admin of this page and have granted all permissions."
        )
    return data["access_token"]


# ── OAuth flow ────────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    cfg = _cfg()
    state = secrets.token_urlsafe(32)

    state_path = _state_path()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump({"state": state}, f)

    params = {
        "client_id": cfg["app_id"],
        "redirect_uri": _redirect_uri(),
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "state": state,
    }
    return f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{urlencode(params)}"


def complete_auth(code: str, returned_state: str) -> str:
    """Exchange auth code → short-lived token → long-lived token (~60 days)."""
    state_path = _state_path()
    with open(state_path) as f:
        stored = json.load(f).get("state")
    os.remove(state_path)
    if stored != returned_state:
        raise Exception("Invalid OAuth state. Possible CSRF attack.")

    cfg = _cfg()

    # Step 1: short-lived token
    r = httpx.get(f"{BASE}/oauth/access_token", params={
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "redirect_uri": _redirect_uri(),
        "code": code,
    }, timeout=30)
    r.raise_for_status()
    short_token = r.json()["access_token"]

    # Step 2: exchange for long-lived token (~60 days)
    r2 = httpx.get(f"{BASE}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": cfg["app_id"],
        "client_secret": cfg["app_secret"],
        "fb_exchange_token": short_token,
    }, timeout=30)
    r2.raise_for_status()
    result = r2.json()

    token_path = _token_path()
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w") as f:
        json.dump({
            "access_token": result["access_token"],
            "expires_at": time.time() + result.get("expires_in", 5_184_000),
        }, f, indent=2)

    return result["access_token"]
