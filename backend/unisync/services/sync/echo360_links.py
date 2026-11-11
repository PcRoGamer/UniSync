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
3. ``embed_catalog`` — a stored public page
   (``/media/<id>/public`` or ``/public/media/<id>``) from the embed catalog.
   ``/lti/`` and ``/ui/player/`` are not directly watchable. When no public
   page is recorded, the Canvas page, module item, or Lecture Capture tool
   URL already on the catalog is used instead. Nothing is invented.
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
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

_HOST = "echo360.net.au"
_CANVAS_HOST = "canvas.lms.unimelb.edu.au"
_LESSON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,300}$")
_FILENAME_TIME = re.compile(r"T(\d{2})_(\d{2})_(\d{2})")
_WATCH_PATH = re.compile(
    r"^/(?:lesson/.+/classroom|media/[0-9a-fA-F-]{36}/public|public/media/[0-9a-fA-F-]{36})/?$",
    re.IGNORECASE,
)
_UNPLAYABLE_PATH = re.compile(r"^/(?:ui/player/[^/]+|lti/[^/]+)/?$", re.IGNORECASE)
_CANVAS_PATH = re.compile(
    r"^/courses/\d+/(?:pages(?:/[^/]+)?|modules(?:/items/\d+)?|external_tools/(?:\d+|retrieve))/?$",
    re.IGNORECASE,
)
# Query keys that identify a page without granting access. Everything else,
# including sessionId and secureLinkAccessDataId, is dropped.
_ALLOWED_QUERY = {
    "focus",
    "linkid",
    "autoplay",
    "automute",
    "display",
    "currentmediaid",
    "url",
}
_ECHO_URL_FIELDS = ("player_url", "provider_launch_url", "canvas_launch_url")
_CANVAS_URL_FIELDS = (
    "canvas_launch_url",
    "canvas_page_url",
    "page_url",
    "html_url",
    "source_page_url",
    "module_item_url",
    "module_url",
)

_json_cache: dict[str, tuple[int, int, Any]] = {}


@dataclass(frozen=True, slots=True)
class RecordingLink:
    """A validated watch page and the evidence that produced it."""

    lesson_id: str
    recording_url: str
    source: str
    kind: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "lesson_id": self.lesson_id,
            "recording_url": self.recording_url,
            "recording_url_source": self.source,
            "recording_url_kind": self.kind,
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
    """True for an Echo360 lesson classroom or public media page over https.

    ``/lti/`` and ``/ui/player/`` are not directly watchable. The shared Canvas
    placement UUID is not a media id. Signed playlists are not watch pages.
    """
    text = str(url or "").strip()
    if not is_echo360_https_url(text):
        return False
    parsed = urlparse(text)
    path = parsed.path or ""
    if _is_public_media(text) and _public_media_id(text) in _PLACEMENT_IDS:
        return False
    if ".." in path or path.lower().endswith((".m3u8", ".vtt", ".mp4")):
        return False
    if "/transcript" in path.lower() or "/media/download/" in path.lower():
        return False
    return bool(_WATCH_PATH.match(path))


def is_canvas_recording_url(url: object) -> bool:
    """True for a Canvas page, module item, or Lecture Capture tool URL.

    Only ``canvas.lms.unimelb.edu.au``. This is the host egress may keep on
    ``recording_url`` and nowhere else.
    """
    text = str(url or "").strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host != _CANVAS_HOST or parsed.username or parsed.password:
        return False
    path = parsed.path or ""
    return ".." not in path and bool(_CANVAS_PATH.match(path))


def recording_kind(url: str) -> str:
    """``echo360_lesson``, ``echo360_public``, or ``canvas_embed_page``."""
    if is_canvas_recording_url(url):
        return "canvas_embed_page"
    if not is_watch_page(url):
        return ""
    path = (urlparse(url).path or "").lower()
    if "/lesson/" in path and path.rstrip("/").endswith("/classroom"):
        return "echo360_lesson"
    if _is_public_media(url):
        return "echo360_public"
    return ""


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
            return RecordingLink(syllabus_id, url, "syllabus", "echo360_lesson")

    if is_api_lesson_id(known_lesson_id):
        url = classroom_url(known_lesson_id)
        if url:
            return RecordingLink(known_lesson_id.strip(), url, "syllabus", "echo360_lesson")

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
            return RecordingLink(restored, url, "filename", "echo360_lesson")
    return None


def recording_fields(stem: str, *, data_root: Path, known_lesson_id: str = "") -> dict[str, str]:
    """JSON fields for a lecture result. Missing links are empty strings."""
    link = resolve_recording_link(stem, data_root=data_root, known_lesson_id=known_lesson_id)
    if link is None:
        return {
            "lesson_id": "",
            "recording_url": "",
            "recording_url_source": "",
            "recording_url_kind": "",
        }
    return link.as_dict()


# UniMelb's shared Canvas Echo360 placement UUID (see AGENTS.md); never a media id.
_PLACEMENT_IDS = {"9381284d-5253-4e5b-b173-fb47a63576cb"}


def _is_public_media(url: str) -> bool:
    path = (urlparse(url).path or "").lower().rstrip("/")
    return bool(re.fullmatch(r"/(?:media/[0-9a-f-]{36}/public|public/media/[0-9a-f-]{36})", path))


def _public_media_id(url: str) -> str:
    match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", (urlparse(url).path or "").lower())
    return match.group(0) if match else ""


def _without_secret_query(url: str, *, _depth: int = 0) -> str:
    """Keep only harmless query keys. Nested ``url`` values are cleaned too."""
    parsed = urlparse(url)
    if not parsed.query or _depth > 2:
        return url
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() not in _ALLOWED_QUERY:
            continue
        if key.lower() == "url" and _depth < 2:
            nested = unquote(value).strip()
            if nested.startswith(("https://", "http://")):
                value = _without_secret_query(nested, _depth=_depth + 1)
        kept.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _clean_watch_url(url: object) -> str:
    text = _without_secret_query(str(url or "").strip())
    return text if is_watch_page(text) else ""


def _clean_canvas_url(url: object) -> str:
    text = _without_secret_query(str(url or "").strip())
    return text if is_canvas_recording_url(text) else ""


def _is_unplayable_echo(url: object) -> bool:
    text = str(url or "").strip()
    if not is_echo360_https_url(text):
        return False
    path = urlparse(text).path or ""
    return bool(_UNPLAYABLE_PATH.match(path))


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
            return RecordingLink(source_id, stored, "media_library", recording_kind(stored))
        if source_id.startswith("G_") or is_api_lesson_id(source_id):
            built = classroom_url(source_id)
            if built:
                return RecordingLink(source_id, built, "media_library", "echo360_lesson")
    return None


def _course_lecture_capture(data_root: Path, course_id: object) -> str:
    """Lecture Capture URL already stored for this Canvas course, if any."""
    loaded = _read_json(data_root / "libraries" / "libraries.json")
    if not isinstance(loaded, Mapping):
        return ""
    wanted = str(course_id or "")
    for lib in loaded.values():
        if not isinstance(lib, Mapping):
            continue
        if str(lib.get("canvas_course_id") or "") != wanted:
            continue
        return _clean_canvas_url(lib.get("lecture_capture_url"))
    return ""


def _embed_choice(record: Mapping[str, Any], *, canvas_fallback: str = "") -> tuple[str, str] | None:
    """Best link that actually opens, plus its kind. Never synthesizes a URL."""
    public = ""
    lesson = ""
    canvas = ""
    saw_unplayable = False
    for key in _ECHO_URL_FIELDS:
        raw = record.get(key)
        cleaned = _clean_watch_url(raw)
        kind = recording_kind(cleaned) if cleaned else ""
        if kind == "echo360_public" and not public:
            public = cleaned
        elif kind == "echo360_lesson" and not lesson:
            lesson = cleaned
        elif _is_unplayable_echo(raw):
            saw_unplayable = True
    for key in _CANVAS_URL_FIELDS:
        cleaned = _clean_canvas_url(record.get(key))
        if cleaned and not canvas:
            canvas = cleaned
    if not canvas:
        canvas = canvas_fallback
    if public:
        return public, "echo360_public"
    if lesson:
        return lesson, "echo360_lesson"
    if canvas:
        return canvas, "canvas_embed_page"
    if saw_unplayable:
        return "", "unplayable_lti"
    return None


def _embed_link(stem: str, data_root: Path) -> RecordingLink | None:
    from unisync.services.sync.echo360_ingest import embed_transcript_lesson_ids, safe_name

    if not stem:
        return None
    loaded = _read_json(data_root / "libraries" / "echo360_embeds.json")
    if not isinstance(loaded, Mapping):
        return None
    for course_id, items in loaded.items():
        if not isinstance(items, list):
            continue
        canvas_fallback = _course_lecture_capture(data_root, course_id)
        for record in items:
            if not isinstance(record, Mapping):
                continue
            names = {safe_name(lesson_id) for lesson_id in embed_transcript_lesson_ids(record)}
            names.update(embed_transcript_lesson_ids(record))
            if stem not in names:
                continue
            choice = _embed_choice(record, canvas_fallback=canvas_fallback)
            if choice is None:
                continue
            url, kind = choice
            lesson_id = next(iter(embed_transcript_lesson_ids(record)), stem)
            return RecordingLink(safe_name(lesson_id), url, "embed_catalog", kind)
    return None
