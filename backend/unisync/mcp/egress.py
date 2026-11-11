"""Scrub MCP payloads before they leave the process.

Drops credential-like fields and query parameters. Marker comments and scores
are kept. This does not remove academic feedback.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit, urlencode

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
