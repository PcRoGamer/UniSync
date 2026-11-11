"""Watchable Echo360 recording links for inventory rows and lecture tools.

The transcript download URL and the page a person opens to watch the recording
are different. This module resolves the watch page when a manifest row or a
lecture tool result is built.

Preference order:

1. ``syllabus`` — lesson id from the cached Echo360 section syllabus
   (``data/cache/echo360_syllabus.json``), or a lesson id the sync just read
   off that same payload. The watch URL is the classroom page the lecture
   catalog already emits: ``https://echo360.net.au/lesson/<id>/classroom?focus=Video``.
2. ``media_library`` — ``source_url`` already stored on
   ``data/media_library/<id>/metadata.json`` when it is an Echo360 watch page.
3. ``embed_catalog`` — ``player_url`` (or ``provider_launch_url``) the sync
   resolved onto ``data/libraries/echo360_embeds.json``. ``/media/<id>/public``
   is skipped; that path 404s. A ``resolved_media_id`` becomes
   ``/ui/player/<id>``, which is the player the native ingestor opens.
4. ``filename`` — last resort for ``G_`` subtitle stems. Cache files replace
   ``:`` with ``_`` because Windows filenames cannot contain colons. Restoring
   those colons rebuilds the lesson id. Embed filenames are never turned into
   a lesson URL: the UUID in ``BLAW_EMBED_<uuid>`` is often the shared Canvas
   LTI placement, not a media id.

Echo360's own URL builders in this repo (``/lesson/<id>/classroom`` and
``?focus=Video``) do not take a start-time query. Cue timestamps stay beside
the recording link; they are not appended to the URL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_HOST = "echo360.net.au"
_LESSON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,300}$")
_FILENAME_TIME = re.compile(r"T(\d{2})_(\d{2})_(\d{2})")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WATCH_PATH = re.compile(
    r"^/(?:lesson/.+/classroom|ui/player/[^/]+|lti/[^/]+)/?$",
    re.IGNORECASE,
)
_SECRET_QUERY = {
    "cloudfront-signature",
    "cloudfront-policy",
    "cloudfront-key-pair-id",
    "echo_jwt",
    "token",
    "access_token",
    "signature",
}

_json_cache: dict[str, tuple[int, int, Any]] = {}


@dataclass(frozen=True, slots=True)
class RecordingLink:
    """A validated watch page and the evidence that produced it."""

    lesson_id: str
    recording_url: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "lesson_id": self.lesson_id,
            "recording_url": self.recording_url,
            "recording_url_source": self.source,
        }


def is_echo360_https_url(url: object) -> bool:
    """True for https URLs whose host is echo360.net.au or a subdomain of it."""
    text = str(url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return host == _HOST or host.endswith("." + _HOST)


def is_watch_page(url: object) -> bool:
    """True when ``url`` is an Echo360 lesson, player, or LTI page over https.

    Signed media playlists and ``/media/<id>/public`` are not watch pages.
    """
    text = str(url or "").strip()
    if not is_echo360_https_url(text):
        return False
    if _is_public_media(text):
        return False
    parsed = urlparse(text)
    path = parsed.path or ""
    if ".." in path or path.lower().endswith((".m3u8", ".vtt", ".mp4")):
        return False
    if "/transcript" in path.lower() or "/media/download/" in path.lower():
        return False
    return bool(_WATCH_PATH.match(path))


def is_api_lesson_id(value: object) -> bool:
    """A lesson id as Echo360 returned it (ISO timestamps still contain colons)."""
    text = str(value or "").strip()
    if not text or text.startswith("BLAW_EMBED_") or ":" not in text:
        return False
    return bool(_LESSON_ID.fullmatch(text))


def classroom_url(lesson_id: object) -> str:
    """Catalog watch URL for one real Echo360 lesson id."""
    text = str(lesson_id or "").strip()
    if not text or text.startswith("BLAW_EMBED_") or not _LESSON_ID.fullmatch(text):
        return ""
    url = f"https://echo360.net.au/lesson/{text}/classroom?focus=Video"
    return url if is_watch_page(url) else ""


def lesson_id_from_filename(stem: object) -> str:
    """Restore ``:`` in a ``G_`` cache stem. Embed stems return an empty string."""
    text = str(stem or "").strip()
    if not text.startswith("G_") or not _FILENAME_TIME.search(text):
        return ""
    restored = _FILENAME_TIME.sub(r"T\1:\2:\3", text)
    return restored if classroom_url(restored) else ""


def resolve_recording_link(
    stem: str,
    *,
    data_root: Path,
    known_lesson_id: str = "",
) -> RecordingLink | None:
    """Resolve one subtitle stem to a watch page, or ``None`` when no safe URL exists."""
    root = Path(data_root)
    text = str(stem or "").strip()

    syllabus_id = _syllabus_lesson_id(text, root) if text else ""
    if syllabus_id:
        url = classroom_url(syllabus_id)
        if url:
            return RecordingLink(syllabus_id, url, "syllabus")

    if is_api_lesson_id(known_lesson_id):
        url = classroom_url(known_lesson_id)
        if url:
            return RecordingLink(known_lesson_id.strip(), url, "syllabus")

    media = _media_library_link(text, root) if text else None
    if media is not None:
        return media

    embed = _embed_link(text, root) if text else None
    if embed is not None:
        return embed

    restored = lesson_id_from_filename(text)
    if restored:
        url = classroom_url(restored)
        if url:
            return RecordingLink(restored, url, "filename")
    return None


def recording_fields(stem: str, *, data_root: Path, known_lesson_id: str = "") -> dict[str, str]:
    """JSON fields for a lecture result. Missing links are empty strings."""
    link = resolve_recording_link(stem, data_root=data_root, known_lesson_id=known_lesson_id)
    if link is None:
        return {"lesson_id": "", "recording_url": "", "recording_url_source": ""}
    return link.as_dict()


def _is_public_media(url: str) -> bool:
    lowered = url.lower()
    return "/media/" in lowered and "/public" in lowered


def _without_secret_query(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _SECRET_QUERY
    ]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _clean_watch_url(url: object) -> str:
    text = _without_secret_query(str(url or "").strip())
    return text if is_watch_page(text) else ""


def _read_json(path: Path) -> Any:
    try:
        stat = path.stat()
    except OSError:
        return None
    token = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    cached = _json_cache.get(key)
    if cached and cached[0] == token[0] and cached[1] == token[1]:
        return cached[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        data = None
    _json_cache[key] = (token[0], token[1], data)
    return data


def _syllabus_sections(data_root: Path) -> list[Mapping[str, Any]]:
    loaded = _read_json(data_root / "cache" / "echo360_syllabus.json")
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, Mapping)]
    if isinstance(loaded, dict):
        sections: list[Mapping[str, Any]] = []
        for key, value in loaded.items():
            if isinstance(value, Mapping) and "syllabus" in value:
                sections.append(value)
            elif isinstance(value, list):
                sections.append({"sectionId": key, "syllabus": value})
        return sections
    return []


def _syllabus_lesson_id(stem: str, data_root: Path) -> str:
    from unisync.services.sync.echo360_ingest import direct_lesson_stems, lesson_id_of

    if not stem:
        return ""
    for section in _syllabus_sections(data_root):
        for item in section.get("syllabus") or []:
            if not isinstance(item, Mapping):
                continue
            lesson_id = lesson_id_of(item)
            if not lesson_id or lesson_id.startswith("BLAW_EMBED_"):
                continue
            if stem in direct_lesson_stems(item):
                return lesson_id
    return ""


def _media_library_link(stem: str, data_root: Path) -> RecordingLink | None:
    from unisync.services.sync.echo360_ingest import lesson_stem

    root = data_root / "media_library"
    if not stem or not root.is_dir():
        return None
    for meta_path in root.glob("*/metadata.json"):
        meta = _read_json(meta_path)
        if not isinstance(meta, Mapping) or meta.get("source") != "echo360":
            continue
        source_id = str(meta.get("source_id") or "").strip()
        if not source_id or source_id.startswith("BLAW_EMBED_"):
            continue
        if stem not in {source_id, lesson_stem(source_id)}:
            continue
        stored = _clean_watch_url(meta.get("source_url"))
        if stored:
            return RecordingLink(source_id, stored, "media_library")
        built = classroom_url(source_id)
        if built:
            return RecordingLink(source_id, built, "media_library")
    return None


def _embed_watch_url(record: Mapping[str, Any]) -> str:
    for key in ("player_url", "provider_launch_url"):
        raw = str(record.get(key) or "").strip()
        if _is_public_media(raw):
            continue
        cleaned = _clean_watch_url(raw)
        if cleaned:
            return cleaned
    media_id = str(record.get("resolved_media_id") or "").strip()
    if _UUID.fullmatch(media_id):
        return _clean_watch_url(f"https://echo360.net.au/ui/player/{media_id}")
    return ""


def _embed_link(stem: str, data_root: Path) -> RecordingLink | None:
    from unisync.services.sync.echo360_ingest import embed_transcript_lesson_ids, safe_name

    if not stem:
        return None
    loaded = _read_json(data_root / "libraries" / "echo360_embeds.json")
    if not isinstance(loaded, Mapping):
        return None
    for items in loaded.values():
        if not isinstance(items, list):
            continue
        for record in items:
            if not isinstance(record, Mapping):
                continue
            names = {safe_name(lesson_id) for lesson_id in embed_transcript_lesson_ids(record)}
            names.update(embed_transcript_lesson_ids(record))
            if stem not in names:
                continue
            url = _embed_watch_url(record)
            if not url:
                continue
            lesson_id = next(iter(embed_transcript_lesson_ids(record)), stem)
            return RecordingLink(safe_name(lesson_id), url, "embed_catalog")
    return None
