"""Allowlist for recording links on the way out of MCP lecture tools.

Lecture payloads carry ``recording_url`` as its own field, next to the
transcript text. The academic boilerplate scrubber only sees that text.
This check runs on the structured payload: an https Echo360 lesson, player,
or LTI page is kept, and every other value in a recording-link field is
cleared. Transcript download links (``direct_link``) are left alone.
"""

from __future__ import annotations

from typing import Any

from unisync.services.sync.echo360_links import is_watch_page

_RECORDING_KEYS = {"recording_url", "video_url", "player_url", "classroom_url"}


def scrub_recording_url(url: object) -> str:
    """Return ``url`` when it is an Echo360 watch page, otherwise an empty string."""
    text = str(url or "").strip()
    return text if is_watch_page(text) else ""


def scrub_lecture_payload(payload: Any) -> Any:
    """Walk a tool payload and keep only allowlisted recording links."""
    if isinstance(payload, dict):
        cleaned: dict[Any, Any] = {}
        for key, value in payload.items():
            if key in _RECORDING_KEYS and isinstance(value, str):
                cleaned[key] = scrub_recording_url(value)
            else:
                cleaned[key] = scrub_lecture_payload(value)
        return cleaned
    if isinstance(payload, list):
        return [scrub_lecture_payload(item) for item in payload]
    return payload
