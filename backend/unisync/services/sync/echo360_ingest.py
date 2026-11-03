"""Native Echo360 ingestion primitives.

The legacy bridge identified transcripts with a mixture of lesson ids, media ids
and the shared Canvas LTI tool UUID.  A shared tool UUID is not a video: one
Canvas external-tool UUID can serve dozens of distinct ``linkId`` embeds.  This
module is the single place that decides what an embed is called on disk, when a
cached file is a real transcript, and what a course's coverage actually is.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote

from unisync.core.file_io import atomic_write_json, atomic_write_text

EMBED_PREFIX = "BLAW_EMBED_"

_TIMESTAMP = re.compile(
    r"^\s*(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{3}\s*-->\s*(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{3}",
    re.MULTILINE,
)
_UNSAFE = re.compile(r"[^a-zA-Z0-9_\-]")
_LINK_ID = re.compile(r"linkId=([0-9a-fA-F\-]{8,})", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_link_id(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text or None


def link_id_from_url(url: object) -> str | None:
    if not url:
        return None
    text = str(url)
    for _ in range(2):
        match = _LINK_ID.search(text)
        if match:
            return normalize_link_id(match.group(1))
        text = unquote(text)
    return None


def media_uuid_from_url(url: object) -> str | None:
    if not url:
        return None
    match = re.search(
        r"/(?:media|lti|ui/player|public/media)/([0-9a-fA-F\-]{36})",
        unquote(str(url)),
    )
    return match.group(1).lower() if match else None


def canonical_embed_key(course_id: object, uuid: object, link_id: object) -> str:
    """The identity every producer and reader must agree on."""
    return f"{str(course_id or '').strip()}::{str(uuid or '').strip().lower()}::{normalize_link_id(link_id) or ''}"


def embed_lesson_id(uuid: object, link_id: object) -> str:
    """Legacy-compatible lesson id; a link id is always part of a specific key."""
    uuid_value = str(uuid or "").strip()
    link_value = normalize_link_id(link_id)
    return f"{EMBED_PREFIX}{uuid_value}_{link_value[:8]}" if link_value else f"{EMBED_PREFIX}{uuid_value}"


def safe_name(value: object) -> str:
    return _UNSAFE.sub("_", str(value or ""))


def subtitle_path(cache_dir: Path, lesson_id: str) -> Path:
    return Path(cache_dir) / f"{safe_name(lesson_id)}.vtt"


def is_cue_valid_vtt(text: object) -> bool:
    """A transcript is present only when it is WEBVTT *and* has a timed cue."""
    if not isinstance(text, str):
        return False
    body = text.lstrip("\ufeff \t\r\n")
    return body.upper().startswith("WEBVTT") and bool(_TIMESTAMP.search(body))


_CUE_SPAN = re.compile(
    r"(?:(?P<sh>\d{1,3}):)?(?P<sm>\d{1,2}):(?P<ss>\d{2})[.,](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?:(?P<eh>\d{1,3}):)?(?P<em>\d{1,2}):(?P<es>\d{2})[.,](?P<ems>\d{3})"
)
_NOTICE_MARKERS = (
    "this material is made available",
    "university of melbourne under section 113p",
    "copyright act 1968",
    "university copyright website",
    "it may be subject to copyright",
)


def _seconds(hours, minutes, seconds, millis) -> float:
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0


def is_notice_text(text: object) -> bool:
    """True when a cue carries only the University copyright disclaimer."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _NOTICE_MARKERS)


def transcript_shape(text: object) -> tuple[float, int]:
    """(last cue end in seconds, cue count) for a WebVTT body."""
    if not isinstance(text, str):
        return 0.0, 0
    matches = list(_CUE_SPAN.finditer(text))
    if not matches:
        return 0.0, 0
    last = max(
        _seconds(m.group("eh"), m.group("em"), m.group("es"), m.group("ems"))
        for m in matches
    )
    return round(last, 3), len(matches)


def is_notice_only_vtt(text: object, *, max_seconds: float = 120.0) -> bool:
    """A 15 s disclaimer track is not a lecture transcript.

    Echo360 publishes the copyright notice as its own caption track.  It is a
    cue-valid WebVTT, so the old check counted it as full coverage and a
    lecture with no captions looked archived.
    """
    span, cues = transcript_shape(text)
    if not cues or span >= max_seconds:
        return False
    body = str(text).replace("\r\n", "\n")
    spoken = 0
    for block in re.split(r"\n\s*\n", body):
        if "-->" not in block:
            continue
        payload = " ".join(
            line.strip()
            for line in block.splitlines()
            if line.strip()
            and "-->" not in line
            and line.strip().upper() != "WEBVTT"
            and not line.strip().upper().startswith("NOTE")
        )
        if payload and not is_notice_text(payload):
            spoken += 1
    notice = sum(body.lower().count(marker) for marker in _NOTICE_MARKERS)
    if not notice:
        return False
    return spoken == 0 or (span < 20 and spoken <= 1)


def _ms_to_vtt(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def cues_to_vtt(cues: Iterable[Mapping[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    count = 0
    for cue in cues or []:
        if not isinstance(cue, Mapping):
            continue
        text = str(cue.get("content") or cue.get("text") or "").strip()
        if not text:
            continue
        start_ms = int(cue["startMs"]) if cue.get("startMs") is not None else int(float(cue.get("start") or 0) * 1000)
        end_ms = int(cue["endMs"]) if cue.get("endMs") is not None else int(float(cue.get("end") or 0) * 1000)
        if end_ms <= start_ms:
            end_ms = start_ms + 1000
        speaker = str(cue.get("speaker") or "").strip()
        lines.append(f"{_ms_to_vtt(start_ms)} --> {_ms_to_vtt(end_ms)}")
        lines.append(f"<v {speaker}>{text}" if speaker else text)
        lines.append("")
        count += 1
    return "\n".join(lines) if count else ""


def transcript_from_json(payload: Any) -> str:
    if isinstance(payload, list):
        cues = payload
    elif isinstance(payload, Mapping):
        cues = next(
            (payload[key] for key in ("transcript", "cues", "data", "captions") if isinstance(payload.get(key), list)),
            [],
        )
    else:
        cues = []
    cues = [
        cue
        for cue in cues or []
        if isinstance(cue, Mapping)
        and (cue.get("startMs") is not None or cue.get("start") is not None)
        and (cue.get("content") or cue.get("text"))
    ]
    return cues_to_vtt(cues) if cues else ""


def parse_hls_subtitle_uris(manifest: str, base_url: str) -> list[str]:
    uris: list[str] = []
    base = base_url.rsplit("/", 1)[0] + "/"
    for line in str(manifest or "").splitlines():
        if "TYPE=SUBTITLES" not in line.upper():
            continue
        match = re.search(r'URI="([^"]+)"', line, re.IGNORECASE)
        if match:
            raw = match.group(1)
            uris.append(raw if raw.startswith("http") else base + raw.lstrip("/"))
    return uris


def write_transcript(cache_dir: Path, lesson_id: str, text: object) -> bool:
    """Atomically store a validated transcript. Rejects generic/empty bodies."""
    if not is_cue_valid_vtt(text):
        return False
    path = subtitle_path(cache_dir, lesson_id)
    if is_notice_only_vtt(text) and path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if existing and not is_notice_only_vtt(existing):
            # Never downgrade a real lecture track to the copyright notice.
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, str(text).lstrip("\ufeff").strip() + "\n")
    return True


def read_transcript(
    cache_dir: Path,
    lesson_id: str,
    *,
    allow_notice_only: bool = False,
) -> str | None:
    try:
        text = subtitle_path(cache_dir, lesson_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not is_cue_valid_vtt(text):
        return None
    if not allow_notice_only and is_notice_only_vtt(text):
        return None
    return text


def embed_transcript_lesson_ids(record: Mapping[str, Any]) -> list[str]:
    """Ids allowed to satisfy this embed.

    With a ``link_id`` only the link-specific id counts; the shared tool UUID is
    never a fallback.
    """
    uuid_value = str(record.get("uuid") or record.get("media_id") or "").strip()
    if not uuid_value:
        return []
    link_id = normalize_link_id(record.get("link_id"))
    if link_id:
        return [embed_lesson_id(uuid_value, link_id)]
    ids = [embed_lesson_id(uuid_value, None)]
    media_id = str(record.get("media_id") or "").strip()
    if media_id and media_id.lower() != uuid_value.lower():
        ids.append(embed_lesson_id(media_id, None))
    return ids


def embed_transcript_path(cache_dir: Path, record: Mapping[str, Any]) -> Path | None:
    """Longest real transcript for this embed (disclaimer tracks excluded)."""
    best: tuple[float, Path] | None = None
    for lesson_id in embed_transcript_lesson_ids(record):
        text = read_transcript(cache_dir, lesson_id)
        if not text:
            continue
        span = transcript_shape(text)[0]
        if best is None or span > best[0]:
            best = (span, subtitle_path(cache_dir, lesson_id))
    return best[1] if best else None


def embed_transcript_present(cache_dir: Path, record: Mapping[str, Any]) -> bool:
    return embed_transcript_path(cache_dir, record) is not None


def thumbnail_path(cache_dir: Path, record: Mapping[str, Any]) -> Path | None:
    for lesson_id in embed_transcript_lesson_ids(record):
        path = Path(cache_dir) / f"{safe_name(lesson_id)}.jpg"
        if path.is_file() and path.stat().st_size > 64:
            return path
    return None


def write_thumbnail(cache_dir: Path, record: Mapping[str, Any], raw: bytes, *, media_id: object = None) -> bool:
    if not raw or len(raw) < 64:
        return False
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    uuid_value = str(record.get("uuid") or record.get("media_id") or "")
    link_id = normalize_link_id(record.get("link_id"))
    (cache / f"{safe_name(embed_lesson_id(uuid_value, link_id))}.jpg").write_bytes(raw)
    media_value = str(media_id or record.get("media_id") or "")
    if media_value and not link_id:
        (cache / f"{safe_name(media_value)}.jpg").write_bytes(raw)
    return True


_PLACEHOLDER_TITLES = {"embedded content", "echo360 video", "canvas page video", ""}


def merge_canvas_embeds(
    records: Sequence[Mapping[str, Any]],
    discovered: Sequence[Mapping[str, Any]],
    *,
    course_id: object,
    discovered_at: str | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Deduplicate by ``(course_id, uuid, link_id)``; never drop legacy rows."""
    stamp = discovered_at or utc_now()
    course_key = str(course_id)
    ordered: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def key_of(record: Mapping[str, Any]) -> str | None:
        uuid_value = str(record.get("uuid") or record.get("media_id") or "").strip()
        if not uuid_value:
            return None
        return canonical_embed_key(course_key, uuid_value, record.get("link_id"))

    for record in records or []:
        if not isinstance(record, Mapping):
            continue
        key = key_of(record)
        if not key or key in index:
            continue
        enriched = dict(record)
        enriched.setdefault("canvas_course_id", course_key)
        enriched["link_id"] = normalize_link_id(record.get("link_id"))
        index[key] = len(ordered)
        ordered.append(enriched)

    added = updated = 0
    for item in discovered or []:
        if not isinstance(item, Mapping):
            continue
        key = key_of(item)
        if not key:
            continue
        incoming = dict(item)
        incoming.setdefault("provider", "echo360")
        incoming["uuid"] = str(item.get("uuid") or item.get("media_id") or "").strip()
        incoming["link_id"] = normalize_link_id(item.get("link_id"))
        incoming["canvas_course_id"] = course_key
        incoming.setdefault("discovered_at", stamp)
        if key in index:
            position = index[key]
            merged = dict(ordered[position])
            for field, value in incoming.items():
                if value in (None, "", [], {}):
                    continue
                if field in {"title", "source_title"} and str(merged.get(field) or "").strip().lower() not in _PLACEHOLDER_TITLES:
                    continue
                merged[field] = value
            if merged != ordered[position]:
                updated += 1
            ordered[position] = merged
        else:
            index[key] = len(ordered)
            ordered.append(incoming)
            added += 1
    return ordered, added, updated


def merge_syllabus_snapshot(
    existing: Sequence[Mapping[str, Any]] | None,
    fetched: Mapping[str, Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]],
    *,
    fetched_at: str | None = None,
    section_names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Merge fresh syllabi into the cached snapshot without losing old lessons."""
    stamp = fetched_at or utc_now()
    merged: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []

    def absorb(section: Mapping[str, Any]) -> None:
        key = str(section.get("sectionId") or section.get("id") or section.get("name") or "").strip()
        if not key:
            return
        if key not in merged:
            merged[key] = dict(section)
            ordered.append(key)
        elif section.get("syllabus"):
            merged[key]["syllabus"] = section["syllabus"]

    for section in existing or []:
        if isinstance(section, Mapping):
            absorb(section)

    if isinstance(fetched, Mapping):
        for section_id, lessons in fetched.items():
            key = str(section_id or "").strip()
            if not key:
                continue
            document = dict(merged.get(key, {"sectionId": key}))
            if lessons:
                document["syllabus"] = list(lessons)
                document["last_syllabus_fetch"] = stamp
            else:
                document.setdefault("syllabus", [])
                document["last_syllabus_fetch_attempt"] = stamp
            if section_names and section_names.get(key):
                document["name"] = section_names[key]
            if key not in merged:
                ordered.append(key)
            merged[key] = document

    return [merged[key] for key in ordered]


def syllabus_lesson_index(sections: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index cached syllabus lessons by lesson id and normalised title."""
    index: dict[str, dict[str, Any]] = {}
    for section in sections or []:
        for item in section.get("syllabus") or []:
            if not isinstance(item, Mapping):
                continue
            lesson = item.get("lesson") or {}
            inner = lesson.get("lesson") or lesson
            lesson_id = str(inner.get("id") or "").strip()
            if lesson_id and not lesson_id.startswith(EMBED_PREFIX):
                index[lesson_id] = item
            title = str(inner.get("name") or inner.get("displayName") or "").strip().lower()
            if title:
                index.setdefault(f"title::{title}", item)
    return index


def enrich_embed_from_syllabus(record: dict[str, Any], index: Mapping[str, dict[str, Any]]) -> bool:
    """Attach a real lecture date from the syllabus when titles match exactly."""
    if record.get("timing_start"):
        return False
    source_title = str(record.get("source_title") or record.get("title") or "").strip().lower()
    if source_title in _PLACEHOLDER_TITLES:
        return False
    item = index.get(f"title::{source_title}")
    if not item:
        return False
    lesson = item.get("lesson") or {}
    inner = lesson.get("lesson") or lesson
    start = str((inner.get("timing") or {}).get("start") or "").strip()
    if not start:
        return False
    record["timing_start"] = start
    record["lecture_date"] = start
    return True


def persist_syllabus(
    path: Path,
    fetched: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    section_names: Mapping[str, str] | None = None,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    """Read/merge/write ``echo360_syllabus.json`` atomically."""
    path = Path(path)
    existing: list[dict[str, Any]] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = [item for item in loaded if isinstance(item, dict)]
        except (OSError, ValueError):
            existing = []
    merged = merge_syllabus_snapshot(existing, fetched, fetched_at=fetched_at, section_names=section_names)
    if merged:
        atomic_write_json(path, merged)
    return merged


def classify_embed(
    record: Mapping[str, Any],
    *,
    cache_dir: Path,
    thumbnail_dir: Path | None = None,
) -> dict[str, Any]:
    """Describe one embed from on-disk evidence only.

    Status is never inferred from a shared tool UUID, from the presence of a
    catalog row, or from a stale syllabus snapshot.
    """
    transcript = embed_transcript_path(cache_dir, record)
    notice_only = None
    for lesson_id in embed_transcript_lesson_ids(record):
        candidate = subtitle_path(cache_dir, lesson_id)
        try:
            raw = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if is_cue_valid_vtt(raw) and is_notice_only_vtt(raw):
            notice_only = candidate
            break
    media_url = str(record.get("media_url") or "").strip()
    downloaded = bool(record.get("downloaded"))
    thumbnail = thumbnail_path(thumbnail_dir, record) if thumbnail_dir else None
    if transcript is not None:
        status = "transcript"
        reason = ""
    elif not (record.get("link_id") or record.get("player_url") or record.get("provider_launch_url")):
        status = "unresolved"
        reason = "missing_link_identity"
    elif not media_url and not downloaded:
        status = "unresolved"
        # Prefer the last recorded probe: a player that returned 403 is a
        # permission result, not proof that the media URL is absent.
        reason = str(
            record.get("last_outcome_reason")
            or ("notice_only" if notice_only else "no_media_url")
        )
    else:
        status = "missing_transcript"
        reason = str(
            record.get("last_outcome_reason")
            or ("notice_only" if notice_only else "no_cue_valid_transcript")
        )
    return {
        "key": canonical_embed_key(record.get("canvas_course_id"), record.get("uuid") or record.get("media_id"), record.get("link_id")),
        "title": str(record.get("title") or record.get("source_title") or ""),
        "uuid": str(record.get("uuid") or ""),
        "link_id": normalize_link_id(record.get("link_id")),
        "status": status,
        "reason": reason,
        "has_transcript": transcript is not None,
        "has_media_url": bool(media_url),
        "downloaded": downloaded,
        "has_thumbnail": thumbnail is not None,
        "transcript_path": str(transcript) if transcript else "",
        "transcript_updated_at": (
            datetime.fromtimestamp(transcript.stat().st_mtime, timezone.utc).isoformat()
            if transcript is not None
            else ""
        ),
    }


def build_coverage(
    course_id: object,
    records: Sequence[Mapping[str, Any]],
    *,
    cache_dir: Path,
    thumbnail_dir: Path | None = None,
    catalog_updated_at: str | None = None,
    syllabus_fetched_at: str | None = None,
) -> dict[str, Any]:
    """Per-course honesty: counts, missing items and freshness signals."""
    rows = [classify_embed(record, cache_dir=cache_dir, thumbnail_dir=thumbnail_dir) for record in records or []]
    with_transcript = [row for row in rows if row["has_transcript"]]
    missing = [row for row in rows if not row["has_transcript"]]
    latest_transcript = max((row["transcript_updated_at"] for row in with_transcript), default="")
    return {
        "canvas_course_id": str(course_id or ""),
        "discovered_links": len(rows),
        "resolved_media": sum(1 for row in rows if row["has_media_url"] or row["downloaded"]),
        "cue_valid_transcripts": len(with_transcript),
        "missing_transcripts": len(missing),
        "missing": missing,
        "last_catalog_refresh": catalog_updated_at or "",
        "last_syllabus_fetch": syllabus_fetched_at or "",
        "last_transcript_update": latest_transcript,
        "transcripts_current": bool(rows) and not missing,
    }

_WINDOWS_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def lesson_stem(lesson_id: object) -> str:
    """Cache-file stem for a direct Echo360 lesson id.

    Lesson ids carry ISO timestamps (``..._2026-07-28T17:05:00.000``) and the
    legacy cache replaced only filename-illegal characters, so the dots in
    ``.000`` survive on disk. ``safe_name`` is for embed keys and strips those
    dots, so it must never be applied to a lesson id.
    """
    return _WINDOWS_UNSAFE.sub("_", str(lesson_id or "").strip())


def lesson_inner(item: Mapping[str, Any]) -> Mapping[str, Any]:
    lesson = item.get("lesson") or {}
    return (lesson.get("lesson") or lesson) if isinstance(lesson, Mapping) else {}


def lesson_id_of(item: Mapping[str, Any]) -> str:
    return str(lesson_inner(item).get("id") or "").strip()


def lesson_title_of(item: Mapping[str, Any]) -> str:
    inner = lesson_inner(item)
    return str(inner.get("name") or inner.get("displayName") or "").strip()


def lesson_window(item: Mapping[str, Any]) -> tuple[str, str]:
    timing = lesson_inner(item).get("timing") or {}
    if not isinstance(timing, Mapping):
        return "", ""
    return str(timing.get("start") or ""), str(timing.get("end") or "")


def direct_lesson_stems(item: Mapping[str, Any]) -> list[str]:
    """Candidate cache stems for one syllabus lesson (never embed ids)."""
    lesson_id = lesson_id_of(item)
    if not lesson_id or lesson_id.startswith(EMBED_PREFIX):
        return []
    stems = [lesson_stem(lesson_id)]
    parts = lesson_id.split("_")
    start, end = lesson_window(item)
    if lesson_id.startswith("G_") and len(parts) > 2 and start and end:
        candidate = lesson_stem(f"G_{parts[1]}_{parts[2]}_{start}_{end}")
        if candidate not in stems:
            stems.append(candidate)
    return stems


def _cue_valid_file(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return is_cue_valid_vtt(text) and not is_notice_only_vtt(text)


def direct_lesson_transcript(cache_dir: Path, item: Mapping[str, Any]) -> Path | None:
    for stem in direct_lesson_stems(item):
        path = Path(cache_dir) / f"{stem}.vtt"
        if path.is_file() and _cue_valid_file(path):
            return path
    return None


def direct_lesson_present(cache_dir: Path, item: Mapping[str, Any]) -> bool:
    return direct_lesson_transcript(cache_dir, item) is not None


def build_echo360_coverage(
    sections: Sequence[Mapping[str, Any]],
    *,
    course_id: object,
    course_name: str = "",
    cache_dir: Path,
    syllabus_fetched_at: str = "",
) -> dict[str, Any]:
    """Coverage for the Echo360 direct lesson path (classroom recordings).

    This is the path the userscript ingested through ``scrapeEchoPage``. It was
    missing from the embed-only report, which made whole courses look empty.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in sections or []:
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("sectionId") or section.get("id") or "")
        for item in section.get("syllabus") or []:
            if not isinstance(item, Mapping):
                continue
            lesson_id = lesson_id_of(item)
            if not lesson_id or lesson_id.startswith(EMBED_PREFIX) or lesson_id in seen:
                continue
            seen.add(lesson_id)
            start, end = lesson_window(item)
            transcript = direct_lesson_transcript(cache_dir, item)
            inner = lesson_inner(item)
            rows.append(
                {
                    "lesson_id": lesson_id,
                    "title": lesson_title_of(item),
                    "section_id": section_id,
                    "date": start,
                    "end_date": end,
                    "has_media": bool(inner.get("hasMedia")) or bool(item.get("media_url")),
                    "has_transcript": transcript is not None,
                    "transcript_path": str(transcript) if transcript else "",
                    "status": "transcript" if transcript else "missing_transcript",
                    "reason": "" if transcript else "no_cached_vtt",
                }
            )

    with_transcript = [row for row in rows if row["has_transcript"]]
    missing = [row for row in rows if not row["has_transcript"]]
    latest = ""
    for row in with_transcript:
        try:
            stamp = datetime.fromtimestamp(
                Path(row["transcript_path"]).stat().st_mtime, timezone.utc
            ).isoformat()
        except OSError:
            continue
        latest = max(latest, stamp)
    return {
        "canvas_course_id": str(course_id or ""),
        "course_name": course_name,
        "source": "echo360_direct",
        "discovered_lessons": len(rows),
        "cue_valid_transcripts": len(with_transcript),
        "missing_transcripts": len(missing),
        "missing": missing,
        "last_syllabus_fetch": syllabus_fetched_at or "",
        "last_transcript_update": latest,
        "transcripts_current": bool(rows) and not missing,
    }
