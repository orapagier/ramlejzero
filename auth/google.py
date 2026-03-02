import os
import json
import secrets
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # allow http in dev if needed

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from core.config_loader import get_apis, get_settings

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]


def _base_url() -> str:
    return get_settings().get("web_ui", {}).get("base_url", "http://localhost:8000").rstrip("/")


def _redirect_uri() -> str:
    return f"{_base_url()}/auth/google/callback"


def _resolve_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join("/app", path)


def _token_path() -> str:
    cfg = get_apis().get("google", {})
    path = cfg.get("token_file", "auth/google_token.json")
    return _resolve_path(path)


def _creds_path() -> str:
    cfg = get_apis().get("google", {})
    path = cfg.get("credentials_file", "auth/google_credentials.json")
    return _resolve_path(path)


def _state_path() -> str:
    cfg = get_apis().get("google", {})
    path = cfg.get("state_file", "data/tokens/google_state.json")
    return _resolve_path(path)


def get_credentials() -> Credentials:
    token_path = _token_path()
    creds = None

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds, token_path)
            return creds
        except Exception:
            pass

    raise AuthRequiredError(
        "Google auth required. Visit /auth/google in your browser to authorize."
    )


def _save(creds: Credentials, token_path: str):
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w") as f:
        f.write(creds.to_json())


def _load_client_config() -> dict:
    creds_path = _creds_path()
    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Google credentials file not found at {creds_path}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )
    with open(creds_path) as f:
        return json.load(f)


def get_auth_url() -> str:
    """Build the auth URL manually — no PKCE, no surprises."""
    from urllib.parse import urlencode

    client_config = _load_client_config()
    # supports both "web" and "installed" credential types
    info = client_config.get("web") or client_config.get("installed")
    client_id = info["client_id"]
    auth_uri = info.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")

    state = secrets.token_urlsafe(32)

    # Save state for callback validation
    state_path = _state_path()
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        json.dump({"state": state}, f)

    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "include_granted_scopes": "true",
    }

    return f"{auth_uri}?{urlencode(params)}"


def complete_auth(code: str, returned_state: str) -> Credentials:
    """Exchange the auth code for tokens using requests directly — no PKCE."""
    import httpx

    # Validate state
    state_path = _state_path()
    if os.path.exists(state_path):
        with open(state_path) as f:
            stored_state = json.load(f).get("state")
        os.remove(state_path)
        if stored_state and stored_state != returned_state:
            raise Exception("Invalid OAuth state. Possible CSRF attack.")

    client_config = _load_client_config()
    info = client_config.get("web") or client_config.get("installed")

    token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")

    data = {
        "code": code,
        "client_id": info["client_id"],
        "client_secret": info["client_secret"],
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }

    r = httpx.post(token_uri, data=data, timeout=30)
    r.raise_for_status()
    result = r.json()

    if "access_token" not in result:
        raise Exception(f"Google token exchange failed: {result}")

    # Build a Credentials object from the response
    creds = Credentials(
        token=result["access_token"],
        refresh_token=result.get("refresh_token"),
        token_uri=token_uri,
        client_id=info["client_id"],
        client_secret=info["client_secret"],
        scopes=SCOPES,
    )

    _save(creds, _token_path())
    return creds


class AuthRequiredError(Exception):
    pass
