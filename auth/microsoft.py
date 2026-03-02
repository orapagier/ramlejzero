import os
import json
import time
import httpx
import secrets
from urllib.parse import urlencode
from core.config_loader import get_apis, get_settings


# Delegated scopes — access the signed-in user's OneDrive and profile
SCOPES = [
    "Files.ReadWrite",
    "offline_access",   # required to get a refresh_token
    "User.Read",
]


class AuthRequiredError(Exception):
    pass


def _cfg() -> dict:
    return get_apis().get("microsoft", {})


def _redirect_uri() -> str:
    settings = get_settings()
    base = settings.get("web_ui", {}).get("base_url", "http://localhost:8000").rstrip("/")
    return f"{base}/auth/microsoft/callback"


def _resolve_path(path: str) -> str:
    """Resolve a relative path to an absolute path anchored at /app."""
    if os.path.isabs(path):
        return path
    return os.path.join("/app", path)


def _token_path() -> str:
    path = _cfg().get("token_file", "auth/microsoft_token.json")
    return _resolve_path(path)


def _state_path() -> str:
    path = _cfg().get("state_file", "auth/microsoft_state.json")
    return _resolve_path(path)


# ===============================
# TOKEN MANAGEMENT
# ===============================

def get_access_token() -> str:
    """
    Returns a valid Microsoft access token for the signed-in user.
    - If token file exists and is fresh: returns immediately
    - If expired: refreshes using the refresh_token silently
    - If no token: raises AuthRequiredError
    """
    token_path = _token_path()

    if not os.path.exists(token_path):
        raise AuthRequiredError(
            "Microsoft auth required. Visit /auth/microsoft to authorize."
        )

    with open(token_path) as f:
        token_data = json.load(f)

    expires_at = token_data.get("expires_at", 0)

    # 60-second safety buffer
    if time.time() < expires_at - 60:
        return token_data["access_token"]

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        os.remove(token_path)
        raise AuthRequiredError(
            "Microsoft token expired and no refresh token available. "
            "Visit /auth/microsoft to re-authorize."
        )

    return _refresh_token(refresh_token, token_path)


def _refresh_token(refresh_token: str, token_path: str) -> str:
    """Exchange a refresh token for a new access token silently."""
    cfg = _cfg()
    tenant_id = cfg.get("tenant_id", "common")

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    data = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(SCOPES),
    }

    r = httpx.post(url, data=data, timeout=30)
    r.raise_for_status()
    result = r.json()

    if "access_token" not in result:
        raise AuthRequiredError(
            f"Microsoft token refresh failed: {result.get('error_description', result)}. "
            "Visit /auth/microsoft to re-authorize."
        )

    _save_token(result, token_path)
    return result["access_token"]


def _save_token(result: dict, token_path: str):
    """Save token response to disk with expires_at timestamp."""
    os.makedirs(os.path.dirname(token_path), exist_ok=True)

    token_data = {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token", ""),
        "expires_at": time.time() + result.get("expires_in", 3600),
        "token_type": result.get("token_type", "Bearer"),
        "scope": result.get("scope", ""),
    }

    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)


# ===============================
# OAUTH FLOW
# ===============================

def get_auth_url() -> str:
    """Generate Microsoft OAuth authorization URL."""
    cfg = _cfg()
    tenant_id = cfg.get("tenant_id", "common")

    # Generate secure state for CSRF protection
    state = secrets.token_urlsafe(32)
    _save_state(state)

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "scope": " ".join(SCOPES),
        "response_mode": "query",
        "state": state,
    }

    query = urlencode(params)

    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?{query}"


def complete_auth(code: str, returned_state: str) -> str:
    """Exchange auth code for tokens and validate state."""
    _validate_state(returned_state)

    cfg = _cfg()
    tenant_id = cfg.get("tenant_id", "common")
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    data = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(),
        "scope": " ".join(SCOPES),
    }

    r = httpx.post(url, data=data, timeout=30)
    r.raise_for_status()
    result = r.json()

    if "access_token" not in result:
        raise Exception(f"Microsoft auth failed: {result.get('error_description', result)}")

    _save_token(result, _token_path())
    return result["access_token"]


# ===============================
# STATE MANAGEMENT (CSRF PROTECTION)
# ===============================

def _save_state(state: str):
    state_path = _state_path()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump({"state": state}, f)


def _validate_state(returned_state: str):
    state_path = _state_path()

    if not os.path.exists(state_path):
        raise Exception("OAuth state missing. Possible CSRF attack.")

    with open(state_path) as f:
        stored_state = json.load(f).get("state")

    os.remove(state_path)

    if not stored_state or stored_state != returned_state:
        raise Exception("Invalid OAuth state. Possible CSRF attack.")
