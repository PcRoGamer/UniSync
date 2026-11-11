"""Scrub MCP payloads before they leave the process.

Gradescope and assignment payloads go through ``scrub`` / ``scrub_json``, which
drop credential-like fields and query parameters. Marker comments and scores
are kept. That pass does not remove academic feedback.

Lecture payloads carry ``recording_url`` as its own field. ``scrub_lecture_payload``
keeps an Echo360 lesson or public page, and — on ``recording_url`` only — a
Canvas page or Lecture Capture URL. ``/lti/`` and ``/ui/player/`` are cleared.
Access-grant query parameters are stripped. Transcript download links
(``direct_link``) are left alone.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from unisync.services.sync.echo360_links import (
    _without_secret_query,
    is_canvas_recording_url,
    is_watch_page,
)

_SECRET_KEYS = {
    "cookie",
    "cookies",
    "authorization",
    "token",
    "idtoken",
    "accesstoken",
    "refreshtoken",
    "password",
    "secret",
    "setcookie",
    "session",
    "cloudfrontsignature",
    "cloudfrontpolicy",
    "cloudfrontkeypairid",
    "echojwt",
    "verifier",
}
_SECRET_QUERY = {
    "token",
    "id_token",
    "access_token",
    "refresh_token",
    "login_hint",
    "signature",
    "state",
    "nonce",
    "verifier",
    "cookie",
    "password",
    "code",
}
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")
_URL = re.compile(r"https?://[^\s\"'<>]+")

_RECORDING_KEYS = {"recording_url", "video_url", "player_url", "classroom_url"}


def _scrub_text(text: str) -> str:
    text = _JWT.sub("[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        parts = urlsplit(raw)
        if parts.scheme not in {"http", "https"} or not parts.query:
            return raw
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _SECRET_QUERY
        ]
        return urlunsplit(parts._replace(query=urlencode(kept)))

    return _URL.sub(replace, text)


def scrub(value):
    """Return a copy safe to send to an MCP client."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in _SECRET_KEYS:
                continue
            cleaned[str(key)] = scrub(item)
        return cleaned
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def scrub_json(payload) -> str:
    return json.dumps(scrub(payload), indent=2, ensure_ascii=False)


def scrub_recording_url(url: object, *, allow_canvas: bool = False) -> str:
    """Return a directly usable recording URL, or an empty string.

    Canvas is allowed only for ``recording_url``. Other fields stay on
    Echo360 lesson and public pages.
    """
    text = _without_secret_query(str(url or "").strip())
    if is_watch_page(text):
        return text
    if allow_canvas and is_canvas_recording_url(text):
        return text
    return ""


def scrub_lecture_payload(payload: Any) -> Any:
    """Walk a lecture payload and keep only allowlisted recording links."""
    if isinstance(payload, dict):
        cleaned: dict[Any, Any] = {}
        for key, value in payload.items():
            if key == "recording_url" and isinstance(value, str):
                cleaned[key] = scrub_recording_url(value, allow_canvas=True)
            elif key in _RECORDING_KEYS and isinstance(value, str):
                cleaned[key] = scrub_recording_url(value)
            else:
                cleaned[key] = scrub_lecture_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [scrub_lecture_payload(item) for item in payload]
    return payload
