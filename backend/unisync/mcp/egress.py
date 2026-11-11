"""Allowlist for recording links on the way out of MCP lecture tools.

Lecture payloads carry ``recording_url`` as its own field, next to the
transcript text. The academic boilerplate scrubber only sees that text.
This check keeps an Echo360 lesson or public page, and — on ``recording_url``
only — a Canvas page or Lecture Capture URL. ``/lti/`` and ``/ui/player/``
are cleared. Access-grant query parameters are stripped. Transcript
download links (``direct_link``) are left alone.
"""

from __future__ import annotations

from typing import Any

from unisync.services.sync.echo360_links import (
    _without_secret_query,
    is_canvas_recording_url,
    is_watch_page,
)

_RECORDING_KEYS = {"recording_url", "video_url", "player_url", "classroom_url"}


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
    """Walk a tool payload and keep only allowlisted recording links."""
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
