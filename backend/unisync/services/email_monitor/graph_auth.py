"""Microsoft Graph device-code OAuth for the email monitor.

Uses Microsoft's well-known multi-tenant **public client** with the
``organizations`` tenant. Because it is a *public client*, it supports the
**device-code flow** with delegated permissions and requires **no app
registration, no client secret, and no redirect URI** — the user signs in
with their own organisational identity (e.g. a University of Melbourne M365
account), and UniSync receives a delegated ``Mail.Read`` access token.

Some organisations restrict certain public clients via Conditional Access
policies (e.g. blocking "Microsoft Graph Command Line Tools"). The client
ID is configurable via the ``MS_GRAPH_CLIENT_ID`` env var; if unset, the
default is **Microsoft Office** (``d3590ed6-…``), which is the most
universally pre-approved first-party public client.

Flow
----
1. ``start_device_flow()`` → POST to ``/devicecode``; Microsoft returns a
   ``user_code`` + ``verification_uri`` shown to the user, plus a
   ``device_code`` (kept server-side only).
2. The user visits the verification URI, enters the code, and consents.
3. ``poll_device_flow()`` polls the ``/token`` endpoint until sign-in completes
   (``authorization_pending`` → keep polling; ``slow_down`` → back off;
   ``expired_token`` / ``authorization_declined`` → terminal error). On success
   the tokens + the resolved account email are persisted.
4. ``get_valid_access_token()`` returns a live token, refreshing via the
   stored ``refresh_token`` when within 60s of expiry.

Tokens are stored as a sibling of ``cursor.json`` / ``processed.jsonl`` under
the email-monitor workspace dir, mirroring how the IMAP password lives in
``email_monitor.json``. The file is written atomically with mode ``0600``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from unisync.core.path_service import get_path_service

# ── Microsoft Entra / Graph constants ────────────────────────────────────────

# Public-client IDs for well-known Microsoft first-party applications.
# The default is "Microsoft Office" — the most universally pre-approved
# public client across organisations. Override with MS_GRAPH_CLIENT_ID.
_KNOWN_CLIENTS = {
    "microsoft_office":    "d3590ed6-52b3-4102-aeff-aad2292ab01c",
    "outlook_mobile":      "27922004-5251-4030-b22d-91ecd9a37ea4",
    "microsoft_teams":     "1fec8e78-bce4-4aaf-ab1b-5451cc387264",
    "azure_cli":           "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
    "graph_powershell":    "14d82eec-204b-4c2f-b7e8-296a70dab67e",
}

def _resolve_client_id() -> str:
    """Return the Microsoft public client ID to use.

    Checks the ``MS_GRAPH_CLIENT_ID`` env var first. If set, it can be:
    - A raw client ID (GUID) — used as-is.
    - A key from ``_KNOWN_CLIENTS`` (e.g. ``outlook_mobile``).
    If unset, defaults to the "Microsoft Office" public client.
    """
    env_val = os.environ.get("MS_GRAPH_CLIENT_ID", "").strip()
    if env_val:
        if env_val in _KNOWN_CLIENTS:
            return _KNOWN_CLIENTS[env_val]
        return env_val  # assume it's a raw GUID
    return _KNOWN_CLIENTS["outlook_mobile"]

MS_PUBLIC_CLIENT_ID = _resolve_client_id()

# ``organizations`` restricts sign-in to work/school accounts (excludes
# personal @outlook.com/@live.com), which is what a university tenant uses.
MS_TENANT = "organizations"

MS_DEVICECODE_URL = (
    f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/devicecode"
)
MS_TOKEN_URL = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/token"
MS_AUTHORIZE_URL = (
    f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0/authorize"
)

# Delegated, Mail.Read-only + openid so we can resolve the account email.
# offline_access grants a refresh_token (public clients can't use client
# credentials, so refresh tokens are how we avoid re-prompting every poll).
GRAPH_SCOPES = "https://graph.microsoft.com/Mail.Read offline_access openid"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Refresh slightly before the real expiry to avoid edge races.
_REFRESH_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 20.0


# ── Errors ───────────────────────────────────────────────────────────────────


class GraphAuthError(Exception):
    """Raised for device-code / token errors. ``code`` is safe to expose."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Token store ──────────────────────────────────────────────────────────────


def _token_file_path() -> Path:
    """Resolve the Graph token file under the email-monitor workspace dir."""
    base = get_path_service().get_workspace_feature_dir("email_monitor")
    base.mkdir(parents=True, exist_ok=True)
    return Path(base) / "graph_tokens.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic, 0600 JSON write (temp file + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_tokens() -> dict[str, Any] | None:
    """Load persisted Graph tokens, or ``None`` if not connected."""
    path = _token_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_tokens(tokens: dict[str, Any]) -> None:
    """Persist the Graph token blob (overwrites)."""
    _atomic_write_json(_token_file_path(), tokens)


def clear_tokens() -> None:
    """Remove persisted Graph tokens (logout / disconnect)."""
    path = _token_file_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Decode an Entra ID ``id_token`` claims *without* verifying the signature.

    This mirrors the trust assumption the existing Codex OAuth flow makes
    (``decode_codex_jwt``): the token arrived over TLS from a confidential
    token exchange, so we read the claims directly. For email we only use it
    to display the user's address — never as an auth decision on its own.
    """
    try:
        _, payload_b64, _ = id_token.split(".")
        # Add padding for base64 decode.
        padding = "=" * (-len(payload_b64) % 4)
        import base64

        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        return json.loads(decoded)
    except Exception:
        return {}


def _resolve_account(id_token: str | None, access_token: str) -> str:
    """Best-effort resolve the connected account email from tokens/claims."""
    if id_token:
        claims = _decode_id_token_claims(id_token)
        for key in ("preferred_username", "email", "upn"):
            val = claims.get(key)
            if isinstance(val, str) and "@" in val:
                return val
    # Fall back to a /me call against Graph (uses the live access token).
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "userPrincipalName,mail"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return (
                    data.get("userPrincipalName")
                    or data.get("mail")
                    or ""
                )
    except Exception:
        pass
    return ""


# ── PKCE Browser Flow ─────────────────────────────────────────────────────────

_pending_pkce: dict[str, dict[str, Any]] = {}
_pkce_lock = threading.Lock()


def _generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_pkce_auth_url(redirect_uri: str) -> tuple[str, str]:
    """Build Microsoft OAuth 2.0 PKCE authorization URL.

    Returns (auth_url, state). The caller should preserve state for verification.
    """
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    with _pkce_lock:
        _pending_pkce[state] = {
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    pkce_client_id = _KNOWN_CLIENTS["azure_cli"]
    params = {
        "client_id": pkce_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": GRAPH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode
    auth_url = f"{MS_AUTHORIZE_URL}?{urlencode(params)}"
    return auth_url, state


def exchange_pkce_code(code: str, state: str, redirect_uri: str | None = None) -> dict[str, Any]:
    """Exchange PKCE authorization code for access & refresh tokens."""
    with _pkce_lock:
        pending = _pending_pkce.pop(state, None)
    if not pending:
        # Fallback if state expired/restarted — verifier won't match if unprovided
        verifier = ""
        used_redirect_uri = redirect_uri or ""
    else:
        verifier = pending["verifier"]
        used_redirect_uri = redirect_uri or pending["redirect_uri"]

    payload = {
        "client_id": _KNOWN_CLIENTS["azure_cli"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": used_redirect_uri,
        "code_verifier": verifier,
        "scope": GRAPH_SCOPES,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(MS_TOKEN_URL, data=payload)
    except httpx.HTTPError as exc:
        raise GraphAuthError("network_error", f"Could not reach Microsoft token endpoint: {exc}") from exc

    if resp.status_code != 200:
        err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = err_data.get("error_description") or f"Token exchange failed ({resp.status_code})."
        raise GraphAuthError(err_data.get("error", "token_exchange_failed"), msg)

    token_data = resp.json()
    account = _resolve_account(token_data.get("id_token"), token_data["access_token"])
    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_at": time.time() + float(token_data.get("expires_in", 3600)),
        "scope": token_data.get("scope", GRAPH_SCOPES),
        "account": account,
        "created_at": time.time(),
    }
    save_tokens(tokens)
    return tokens


# ── Device-code flow ─────────────────────────────────────────────────────────
#
# A single in-process session dict holds the device_code Microsoft returned,
# keyed by an opaque session id we hand back to the frontend. The frontend
# polls ``poll_device_flow`` with that id until it resolves. This is identical
# in shape to how ``Connect-MgGraph -UseDeviceAuthentication`` works.

_pending_flows: dict[str, dict[str, Any]] = {}
_flows_lock = threading.Lock()


def start_device_flow() -> dict[str, Any]:
    """Start a device-code flow. Returns the data to show the user.

    The returned dict has: ``session_id``, ``user_code``, ``verification_uri``,
    ``expires_in``, ``interval``. ``device_code`` is kept server-side only.
    """
    data = {
        "client_id": MS_PUBLIC_CLIENT_ID,
        "scope": GRAPH_SCOPES,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(MS_DEVICECODE_URL, data=data)
    except httpx.HTTPError as exc:
        raise GraphAuthError(
            "network_error", f"Could not reach Microsoft login: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise GraphAuthError(
            "devicecode_failed",
            f"Microsoft rejected the device-code request ({resp.status_code}).",
        )

    body = resp.json()
    session_id = secrets.token_urlsafe(24)
    with _flows_lock:
        _pending_flows[session_id] = {
            "device_code": body.get("device_code"),
            "interval": int(body.get("interval", 5)),
            "expires_at": time.time() + int(body.get("expires_in", 900)),
        }

    return {
        "session_id": session_id,
        "user_code": body.get("user_code", ""),
        "verification_uri": body.get("verification_uri", "https://microsoft.com/link"),
        "verification_uri_complete": body.get("verification_uri_complete"),
        "expires_in": int(body.get("expires_in", 900)),
        "interval": int(body.get("interval", 5)),
    }


def poll_device_flow(session_id: str) -> dict[str, Any]:
    """Poll the token endpoint for an in-progress device-code flow.

    Returns ``{"connected": bool, ...}``. While the user has not yet
    completed sign-in, returns ``{"connected": False, "pending": True}``.
    Terminal errors raise ``GraphAuthError``.
    """
    with _flows_lock:
        flow = _pending_flows.get(session_id)
    if not flow:
        raise GraphAuthError("no_flow", "No device sign-in in progress.")

    if time.time() > flow.get("expires_at", 0):
        with _flows_lock:
            _pending_flows.pop(session_id, None)
        raise GraphAuthError(
            "expired_token", "The sign-in code expired. Please try again."
        )

    data = {
        "client_id": MS_PUBLIC_CLIENT_ID,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": flow["device_code"],
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(MS_TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise GraphAuthError(
            "network_error", f"Could not reach Microsoft login: {exc}"
        ) from exc

    body = resp.json()
    error = body.get("error")

    if error == "authorization_pending":
        return {"connected": False, "pending": True}
    if error == "slow_down":
        # Bump the poll interval and tell the caller to slow down.
        with _flows_lock:
            if session_id in _pending_flows:
                _pending_flows[session_id]["interval"] = (
                    int(_pending_flows[session_id].get("interval", 5)) + 5
                )
        return {"connected": False, "pending": True, "slow_down": True}
    if error == "expired_token":
        with _flows_lock:
            _pending_flows.pop(session_id, None)
        raise GraphAuthError(
            "expired_token", "The sign-in code expired. Please try again."
        )
    if error == "authorization_declined":
        with _flows_lock:
            _pending_flows.pop(session_id, None)
        raise GraphAuthError(
            "authorization_declined", "Sign-in was declined."
        )
    if error:
        # Unexpected OAuth error (e.g. Conditional Access block).
        description = body.get("error_description") or error
        # Many Entra policy errors are surfaced as invalid_grant with a
        # description mentioning "Conditional Access" — surface a friendly note.
        if "conditional access" in description.lower():
            raise GraphAuthError(
                "conditional_access",
                "Your organisation's Conditional Access policy blocked this "
                "sign-in. Try IMAP with an app password, or contact your IT admin.",
            )
        raise GraphAuthError("auth_failed", description)

    # Success — persist tokens.
    access_token = body.get("access_token", "")
    refresh_token = body.get("refresh_token", "")
    id_token = body.get("id_token")
    expires_in = int(body.get("expires_in", 3600))
    account = _resolve_account(id_token, access_token)

    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "expires_at": time.time() + expires_in,
        "account": account,
        "obtained_at": time.time(),
    }
    save_tokens(tokens)

    with _flows_lock:
        _pending_flows.pop(session_id, None)

    return {
        "connected": True,
        "account": account,
        "expires_in": expires_in,
    }


def cancel_device_flow(session_id: str) -> None:
    """Discard an in-progress device-code flow (user cancelled)."""
    with _flows_lock:
        _pending_flows.pop(session_id, None)


# ── Token consumption ────────────────────────────────────────────────────────


def _refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Exchange a refresh token for a new access token. Persists the result."""
    data = {
        "client_id": MS_PUBLIC_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": GRAPH_SCOPES,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(MS_TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise GraphAuthError(
            "network_error", f"Could not refresh Microsoft token: {exc}"
        ) from exc

    body = resp.json()
    if body.get("error"):
        # Refresh failed (revoked, expired, CA) — clear stored tokens so the
        # UI re-prompts for a fresh device-code sign-in.
        clear_tokens()
        raise GraphAuthError(
            "refresh_failed",
            body.get("error_description") or body.get("error"),
        )

    access_token = body.get("access_token", "")
    new_refresh = body.get("refresh_token", refresh_token)
    id_token = body.get("id_token")
    expires_in = int(body.get("expires_in", 3600))
    current = load_tokens() or {}
    account = current.get("account") or _resolve_account(id_token, access_token)

    tokens = {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "id_token": id_token,
        "expires_at": time.time() + expires_in,
        "account": account,
        "obtained_at": current.get("obtained_at", time.time()),
    }
    save_tokens(tokens)
    return tokens


def get_valid_access_token() -> str:
    """Return a non-expired access token, refreshing if needed.

    Raises ``GraphAuthError("not_connected")`` if no tokens are stored.
    """
    tokens = load_tokens()
    if not tokens:
        raise GraphAuthError(
            "not_connected",
            "Microsoft 365 account is not connected. Use device sign-in first.",
        )
    expires_at = float(tokens.get("expires_at", 0))
    if time.time() < expires_at - _REFRESH_SKEW_SECONDS:
        return tokens["access_token"]

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        clear_tokens()
        raise GraphAuthError(
            "not_connected",
            "Microsoft token expired and no refresh token is stored.",
        )
    refreshed = _refresh_tokens(refresh_token)
    return refreshed["access_token"]


def force_refresh_after_401() -> str:
    """Force a refresh after a Graph 401 (token revoked server-side).

    Mirrors the Codex provider's ``recover_after_unauthorized`` pattern.
    """
    tokens = load_tokens()
    if not tokens or not tokens.get("refresh_token"):
        clear_tokens()
        raise GraphAuthError(
            "not_connected", "Microsoft 365 access was revoked. Please sign in again."
        )
    return _refresh_tokens(tokens["refresh_token"])["access_token"]


# ── Status / logout ──────────────────────────────────────────────────────────


def connection_status() -> dict[str, Any]:
    """Return ``{connected, account, expires_at, has_refresh}`` for the UI."""
    tokens = load_tokens()
    if not tokens:
        return {"connected": False, "account": "", "expires_at": None}
    expires_at = tokens.get("expires_at")
    return {
        "connected": True,
        "account": tokens.get("account", ""),
        "expires_at": float(expires_at) if expires_at else None,
        "has_refresh": bool(tokens.get("refresh_token")),
    }


def logout() -> None:
    """Clear stored Graph tokens (disconnect)."""
    clear_tokens()


def is_connected() -> bool:
    """Cheap connectivity check used to gate the connector."""
    return load_tokens() is not None


def graph_now_iso() -> str:
    """UTC now as an ISO-8601 string Graph's ``receivedDateTime`` accepts."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
