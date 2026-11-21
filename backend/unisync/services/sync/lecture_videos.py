"""Opt-in download of Echo360 lecture videos.

A normal sync keeps saving subtitles only. Video files are fetched when this
module is asked explicitly (CLI or MCP) or when ``UNISYNC_DOWNLOAD_LECTURE_VIDEOS``
is turned on for a sync.

Two recording shapes are covered:

* Echo360 course lessons. The syllabus lists a media id. The player-properties
  API, then the classroom page, yields a stream. A progressive ``hd1``/``sd1``
  or ``s1q1`` file next to an HLS playlist is preferred when it is within the
  size cap. Otherwise the muxed HLS playlist is saved with ffmpeg, or by
  concatenating segments when ffmpeg is not installed.
* Canvas module embeds (BLAW). ``/lti/`` and ``/ui/player/`` do not play on
  their own. The resident browser session opens the Canvas launch URL already
  stored on the embed, which is the same LTI handshake transcript ingest uses,
  and then downloads the stream that handshake reveals.

Signed media URLs, cookies, and access-grant ids are used for the request and
are not written to the manifest, logs, or tool output.

The layout of Echo360 playlists (``s1_av.m3u8``, ``hd1``/``sd1``, ``s1q0``/``s1q1``)
is the public CDN shape already observed by this project. No code is copied
from the unlicensed Echo360 Downloader extension or from EchoPlus.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import quote, urljoin, urlparse, urlunparse

from unisync.services.sync.echo360_ingest import lesson_stem
from unisync.services.sync.echo360_links import _PLACEMENT_IDS, classroom_url

logger = logging.getLogger(__name__)

_HOST = "https://echo360.net.au"
_MAX_HEIGHT_DEFAULT = 720
_MAX_BYTES_DEFAULT = 1610612736  # 1.5 GiB
_ALLOWED_SUFFIXES = ("echo360.net.au", "amazonaws.com", "cloudfront.net")
_STREAM_URL = re.compile(r"https://[^\s\"'\\<>]+?\.(?:m3u8|mp4|m4s|mpd)", re.IGNORECASE)
_HEIGHT = re.compile(r"(\d{3,4})p", re.IGNORECASE)
_COURSE_CODE = re.compile(r"[A-Za-z]{3,}\d{4,}")
_MUXED_NAMES = ("hd1.mp4", "hd1.m4s", "sd1.mp4", "sd1.m4s", "hd2.mp4", "hd2.m4s", "sd2.mp4", "sd2.m4s")
_VIDEO_NAMES = ("s1q1.mp4", "s1q1.m4s", "s1q0.mp4", "s1q0.m4s", "s2q1.mp4", "s2q1.m4s", "s2q0.mp4", "s2q0.m4s")
_AUDIO_NAMES = ("s0q1.mp4", "s0q1.m4s", "s0q0.mp4", "s0q0.m4s")
_EMPTY_VIDEO = {
    "video_downloaded": False,
    "video_status": "",
    "video_filepath": "",
    "video_bytes": 0,
    "video_quality": "",
}


class DownloadError(Exception):
    """A failure safe to show to a caller. The message has no credentials."""


@dataclass
class FetchResult:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ProbeResult:
    status: int
    total: int | None = None


@dataclass
class StreamResult:
    status: int
    bytes_written: int
    total: int | None = None


@dataclass
class LaunchResult:
    media_url: str = ""
    media_kind: str = ""
    media_id: str = ""
    status: str = ""
    reason: str = ""


class MediaClient(Protocol):
    async def get(self, url: str, headers: dict[str, str] | None = None) -> FetchResult: ...

    async def probe(self, url: str) -> ProbeResult: ...

    async def stream_to(
        self, url: str, dest: Path, headers: dict[str, str] | None = None
    ) -> StreamResult: ...

    async def launch(self, record: Mapping[str, Any]) -> LaunchResult: ...

    async def auth_headers(self, url: str) -> dict[str, str]: ...


@dataclass
class MediaChoice:
    container: str
    url: str
    audio_url: str = ""
    height: int = 0
    label: str = ""


@dataclass
class VideoTarget:
    lesson_id: str
    title: str
    course_id: str
    course_name: str
    date: str
    kind: str
    media_id: str
    public_id: str
    secure_id: str
    page_url: str
    recording_url: str
    source: str
    record: dict[str, Any] = field(default_factory=dict)


def videos_enabled() -> bool:
    """True only when a sync is allowed to fetch video bytes."""
    return os.environ.get("UNISYNC_DOWNLOAD_LECTURE_VIDEOS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def max_height() -> int:
    raw = os.environ.get("UNISYNC_LECTURE_VIDEO_MAX_HEIGHT", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _MAX_HEIGHT_DEFAULT


def max_bytes() -> int:
    raw = os.environ.get("UNISYNC_LECTURE_VIDEO_MAX_BYTES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _MAX_BYTES_DEFAULT


def is_allowed_media_url(url: object) -> bool:
    """HTTPS Echo360, CloudFront, or S3 URL with no userinfo."""
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
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_SUFFIXES)


def public_failure(status: int, url: str) -> str:
    """Host and file name only. Query strings stay off the message."""
    parsed = urlparse(url)
    host = parsed.hostname or "remote host"
    name = Path(parsed.path).name or "media"
    if status:
        return f"HTTP {status} from {host} ({name})"
    return f"Request to {host} failed ({name})"


def container_of(url: str, kind: str = "") -> str:
    lowered = urlparse(url).path.lower()
    hint = kind.lower()
    if lowered.endswith(".mp4") or hint == "mp4":
        return "mp4"
    if lowered.endswith(".m4s"):
        return "m4s"
    if lowered.endswith(".mpd") or hint == "dash":
        return "dash"
    if lowered.endswith(".m3u8") or hint in {"m3u8", "hls"} or ".m3u8" in lowered:
        return "hls"
    return "hls" if hint in {"m3u8", "hls"} else "mp4"


def join_with_query(base: str, ref: str) -> str:
    """Resolve a playlist reference and keep the parent's query when the child has none.

    CloudFront and Echo360 sign the master playlist. Variant and segment URLs
    are often relative and only work when that query travels with them.
    """
    if ref.startswith(("https://", "http://")):
        return ref
    joined = urljoin(base, ref)
    parent_query = urlparse(base).query
    parsed = urlparse(joined)
    if parent_query and not parsed.query:
        return urlunparse(parsed._replace(query=parent_query))
    return joined


def sibling_url(playlist_url: str, name: str) -> str:
    parsed = urlparse(playlist_url)
    directory = parsed.path.rsplit("/", 1)[0]
    return urlunparse(parsed._replace(path=f"{directory}/{name}"))


def progressive_names() -> tuple[str, ...]:
    return _MUXED_NAMES + _VIDEO_NAMES + _AUDIO_NAMES


def redact_argv(argv: list[str]) -> list[str]:
    """Drop header blobs and query strings before anything is logged."""
    redacted: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            redacted.append("[redacted]")
            hide_next = False
            continue
        if arg == "-headers":
            redacted.append(arg)
            hide_next = True
            continue
        if "://" in arg and "?" in arg:
            parsed = urlparse(arg)
            redacted.append(urlunparse(parsed._replace(query="")))
            continue
        redacted.append(arg)
    return redacted


def _height_of(item: Mapping[str, Any]) -> int:
    for key in ("height", "videoHeight"):
        raw = item.get(key)
        if isinstance(raw, int) and raw > 0:
            return raw
        if str(raw or "").isdigit():
            return int(raw)
    match = _HEIGHT.search(str(item.get("label") or ""))
    return int(match.group(1)) if match else 0


def _is_audio_only(item: Mapping[str, Any]) -> bool:
    tracks = item.get("trackType") or item.get("trackTypes") or []
    if isinstance(tracks, str):
        tracks = [tracks]
    names = {str(track).lower() for track in tracks}
    if "video" in names:
        return False
    if "audio" in names:
        return True
    mime = str(item.get("mimeType") or item.get("mime") or "").lower()
    return mime.startswith("audio/")


def _payload_root(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
        return payload["data"]
    return payload if isinstance(payload, Mapping) else {}


def choose_playable(payload: Any, *, max_height_px: int) -> MediaChoice | None:
    """Pick the best video description in a player-properties or syllabus blob.

    Progressive MP4 beats HLS, and HLS beats DASH. Among one container, the
    tallest rendition that does not exceed ``max_height_px`` wins. A taller
    rendition is used only when nothing fits under the cap.
    """
    root = _payload_root(payload)
    candidates: list[MediaChoice] = []

    playable = root.get("playableAudioVideo")
    medias = playable.get("playableMedias") if isinstance(playable, Mapping) else None
    for item in medias or []:
        if not isinstance(item, Mapping) or _is_audio_only(item):
            continue
        uri = str(item.get("uri") or item.get("url") or "").strip()
        if not uri or not is_allowed_media_url(uri):
            continue
        kind = "hls" if item.get("isHls") else ""
        candidates.append(
            MediaChoice(
                container=container_of(uri, kind),
                url=uri,
                height=_height_of(item),
                label=str(item.get("label") or ""),
            )
        )

    files = root.get("primaryFiles")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                continue
            uri = str(item.get("s3Url") or item.get("url") or "").strip()
            if uri and is_allowed_media_url(uri):
                candidates.append(MediaChoice(container=container_of(uri), url=uri, height=_height_of(item)))

    manifests = root.get("manifests")
    if isinstance(manifests, list):
        for item in manifests:
            if not isinstance(item, Mapping):
                continue
            uri = str(item.get("uri") or "").strip()
            if uri and is_allowed_media_url(uri):
                candidates.append(MediaChoice(container="hls", url=uri))

    if not candidates:
        return None

    def rank(pair: tuple[int, MediaChoice]) -> tuple[int, int, int]:
        index, choice = pair
        if choice.height and choice.height <= max_height_px:
            fit = max_height_px - choice.height
        elif choice.height:
            fit = 10000 + choice.height
        else:
            fit = 800 + abs(index - (len(candidates) // 2))
        container_penalty = {"mp4": 0, "m4s": 5, "hls": 20, "dash": 40}.get(choice.container, 60)
        return (fit, container_penalty, index)

    return min(enumerate(candidates), key=rank)[1]


def parse_variants(playlist: str, base_url: str) -> list[tuple[str, int, int]]:
    """``(url, bandwidth, height)`` for each stream in a master playlist."""
    variants: list[tuple[str, int, int]] = []
    pending: tuple[int, int] | None = None
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            bandwidth = re.search(r"BANDWIDTH=(\d+)", line, re.IGNORECASE)
            resolution = re.search(r"RESOLUTION=\d+x(\d+)", line, re.IGNORECASE)
            pending = (
                int(bandwidth.group(1)) if bandwidth else 0,
                int(resolution.group(1)) if resolution else 0,
            )
            continue
        if line.startswith("#"):
            continue
        if pending is not None:
            variants.append((join_with_query(base_url, line), pending[0], pending[1]))
            pending = None
    return variants


def select_variant(playlist: str, base_url: str, *, max_height_px: int) -> str:
    """Media playlist URL at or under the height cap, or ``base_url`` when already media."""
    variants = parse_variants(playlist, base_url)
    if not variants:
        return base_url
    under = [item for item in variants if item[2] and item[2] <= max_height_px]
    if under:
        return max(under, key=lambda item: (item[2], -item[1]))[0]
    known = [item for item in variants if item[2]]
    if known:
        return min(known, key=lambda item: item[2])[0]
    ordered = sorted(variants, key=lambda item: item[1])
    return ordered[len(ordered) // 2][0]


def segment_urls(playlist: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ".m3u8" in line.lower():
            continue
        urls.append(join_with_query(base_url, line))
    return urls


def streams_in_html(html: str) -> list[str]:
    found: list[str] = []
    for match in _STREAM_URL.finditer(html or ""):
        url = match.group(0).rstrip("\\.,);]")
        if is_allowed_media_url(url) and url not in found:
            found.append(url)
    found.sort(key=lambda url: 0 if container_of(url) in {"mp4", "m4s"} else 1)
    return found


def player_property_urls(target: VideoTarget) -> list[str]:
    """Echo360 player-properties URLs for one lesson. Access ids are not logged."""
    urls: list[str] = []
    media = target.media_id.strip()
    if not media or media.lower() in _PLACEMENT_IDS:
        media = ""
    base = f"{_HOST}/api/ui/echoplayer"
    if target.secure_id and media:
        urls.append(
            f"{base}/secure-link-access-datas/{quote(target.secure_id, safe='')}"
            f"/media/{quote(media, safe='')}/player-properties"
        )
    public = target.public_id.strip()
    if public and media and public.lower() not in _PLACEMENT_IDS:
        urls.append(
            f"{base}/public-links/{quote(public, safe='')}/media/{quote(media, safe='')}/player-properties"
        )
    lesson = target.lesson_id.strip()
    if lesson and media and not lesson.startswith("BLAW_EMBED_"):
        urls.append(
            f"{base}/lessons/{quote(lesson, safe='')}/medias/{quote(media, safe='')}/player-properties"
        )
    return urls


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _libraries(data_root: Path) -> list[Mapping[str, Any]]:
    loaded = _read_json(data_root / "libraries" / "libraries.json")
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, Mapping)]
    if isinstance(loaded, Mapping):
        return [item for item in loaded.values() if isinstance(item, Mapping)]
    return []


def _blob(*parts: object) -> str:
    return " ".join(str(part or "") for part in parts).lower()


def _course_ok(query: str, *parts: object) -> bool:
    text = query.strip().lower()
    return bool(text) and text in _blob(*parts)


def _library_text_for_section(libraries: list[Mapping[str, Any]], section_id: str) -> str:
    chunks: list[str] = []
    for lib in libraries:
        if str(lib.get("echo360_course_id") or "") == section_id:
            chunks.append(_blob(lib.get("name"), lib.get("kb_name"), lib.get("canvas_course_id")))
    return " ".join(chunks)


def _library_text_for_canvas(libraries: list[Mapping[str, Any]], canvas_id: str) -> str:
    chunks: list[str] = []
    for lib in libraries:
        if str(lib.get("canvas_course_id") or "") == str(canvas_id):
            chunks.append(_blob(lib.get("name"), lib.get("kb_name"), lib.get("echo360_course_id")))
    return " ".join(chunks)


def _lesson_inner(item: Mapping[str, Any]) -> Mapping[str, Any]:
    lesson = item.get("lesson") or {}
    if isinstance(lesson, Mapping) and isinstance(lesson.get("lesson"), Mapping):
        return lesson["lesson"]
    return lesson if isinstance(lesson, Mapping) else {}


def _media_id_from_lesson(item: Mapping[str, Any]) -> str:
    lesson = item.get("lesson") or {}
    medias = lesson.get("medias") if isinstance(lesson, Mapping) else None
    if isinstance(medias, list):
        for media in medias:
            if isinstance(media, Mapping):
                media_id = str(media.get("id") or "").strip()
                if media_id and media_id.lower() not in _PLACEMENT_IDS:
                    return media_id
    return ""


def select_targets(
    data_root: Path,
    *,
    course: str,
    lecture: str = "",
) -> list[VideoTarget]:
    """Echo360 lessons and Canvas embeds for one course, newest first."""
    root = Path(data_root)
    libraries = _libraries(root)
    lecture_q = lecture.strip().lower()
    targets: list[VideoTarget] = []

    syllabus = _read_json(root / "cache" / "echo360_syllabus.json")
    sections: list[Mapping[str, Any]] = []
    if isinstance(syllabus, list):
        sections = [item for item in syllabus if isinstance(item, Mapping)]
    elif isinstance(syllabus, Mapping):
        for key, value in syllabus.items():
            if isinstance(value, Mapping) and "syllabus" in value:
                sections.append(value)
            elif isinstance(value, list):
                sections.append({"sectionId": key, "syllabus": value})

    for section in sections:
        section_id = str(section.get("sectionId") or section.get("id") or "")
        course_name = str(section.get("courseName") or section.get("name") or section_id)
        extra = _library_text_for_section(libraries, section_id)
        if not _course_ok(course, section_id, course_name, section.get("name"), extra):
            continue
        for item in section.get("syllabus") or []:
            if not isinstance(item, Mapping):
                continue
            inner = _lesson_inner(item)
            lesson_id = str(inner.get("id") or "").strip()
            if not lesson_id or lesson_id.startswith("BLAW_EMBED_"):
                continue
            title = str(inner.get("name") or inner.get("displayName") or "Echo360 lecture")
            timing = inner.get("timing") if isinstance(inner.get("timing"), Mapping) else {}
            date = str((timing or {}).get("start") or "")
            if lecture_q and lecture_q not in _blob(lesson_id, title, date, lesson_stem(lesson_id)):
                continue
            page = classroom_url(lesson_id)
            targets.append(
                VideoTarget(
                    lesson_id=lesson_id,
                    title=title,
                    course_id=section_id,
                    course_name=course_name,
                    date=date,
                    kind="echo360_lesson",
                    media_id=_media_id_from_lesson(item),
                    public_id="",
                    secure_id="",
                    page_url=page,
                    recording_url=page,
                    source="echo360_course",
                )
            )

    embeds = _read_json(root / "libraries" / "echo360_embeds.json")
    if isinstance(embeds, Mapping):
        from unisync.services.sync.echo360_ingest import embed_lesson_id
        from unisync.services.sync.echo360_links import _embed_choice

        for canvas_id, items in embeds.items():
            if not isinstance(items, list):
                continue
            lib_text = _library_text_for_canvas(libraries, str(canvas_id))
            for record in items:
                if not isinstance(record, Mapping):
                    continue
                title = str(record.get("title") or record.get("source_title") or "Echo360 embed")
                if not _course_ok(course, canvas_id, title, record.get("source_title"), lib_text):
                    continue
                lesson_id = embed_lesson_id(record.get("uuid") or record.get("media_id"), record.get("link_id"))
                link_id = str(record.get("link_id") or "")
                if lecture_q and lecture_q not in _blob(lesson_id, title, link_id, record.get("uuid")):
                    continue
                choice = _embed_choice(record)
                kind = choice[1] if choice else "unplayable_lti"
                recording = choice[0] if choice else ""
                media_id = str(record.get("resolved_media_id") or "").strip()
                if not media_id or media_id.lower() in _PLACEMENT_IDS:
                    fallback = str(record.get("media_id") or "").strip()
                    media_id = "" if fallback.lower() in _PLACEMENT_IDS else fallback
                public_id = ""
                if kind == "echo360_public":
                    public_id = str(record.get("uuid") or record.get("media_id") or "")
                launch = dict(record)
                launch["canvas_course_id"] = str(canvas_id)
                targets.append(
                    VideoTarget(
                        lesson_id=lesson_id,
                        title=title,
                        course_id=str(canvas_id),
                        course_name=title,
                        date="",
                        kind=kind or "unplayable_lti",
                        media_id=media_id,
                        public_id=public_id,
                        secure_id=str(record.get("secure_link_access_data_id") or ""),
                        page_url=recording,
                        recording_url=recording,
                        source="canvas_embed",
                        record=launch,
                    )
                )

    targets.sort(key=lambda item: item.date or "", reverse=True)
    return targets


def _course_slug(name: str, course_id: str) -> str:
    match = _COURSE_CODE.search(name or "")
    if match:
        return match.group(0).upper()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("_")
    return (safe[:40] or course_id[:12] or "course")


def _video_dir(data_root: Path, target: VideoTarget) -> Path:
    return Path(data_root) / "lecture_videos" / _course_slug(target.course_name, target.course_id)


def _existing_video(directory: Path, stem: str) -> Path | None:
    for ext in (".mp4", ".m4s", ".ts"):
        path = directory / f"{stem}{ext}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _same_lesson(item: Any, lesson_id: str) -> bool:
    stem = lesson_stem(lesson_id)
    meta = item.metadata or {}
    raw_id = str(item.item_id or "")
    trimmed = raw_id.removesuffix(":subtitles").removesuffix(":video")
    local_stem = Path(item.local_filepath).stem if item.local_filepath else ""
    candidates = {
        str(item.lesson_id or ""),
        str(meta.get("lesson_id") or ""),
        trimmed,
        lesson_stem(trimmed),
        local_stem,
    }
    return lesson_id in candidates or stem in candidates


def _video_metadata(target: VideoTarget, *, status: str, path: Path | None, nbytes: int, quality: str, error: str) -> dict[str, Any]:
    keep = status in {"downloaded", "skipped"} and path is not None
    return {
        "lesson_id": target.lesson_id,
        "video_status": status,
        "video_filepath": str(path) if keep else "",
        "video_bytes": nbytes if keep else 0,
        "video_quality": quality,
        "video_error": error,
    }


def record_video_result(
    registry: Any,
    target: VideoTarget,
    *,
    status: str,
    path: Path | None,
    nbytes: int,
    quality: str,
    error: str = "",
) -> None:
    """Store video status on the lecture row. The signed media URL is not stored."""
    extra = _video_metadata(target, status=status, path=path, nbytes=nbytes, quality=quality, error=error)
    for item in registry.list_all("echo360"):
        if _same_lesson(item, target.lesson_id):
            registry.update_metadata(item.key, extra, auto_flush=False)
    if status in {"downloaded", "skipped"} and path is not None:
        content_type = "video/mp2t" if path.suffix == ".ts" else "video/mp4"
        registry.register(
            platform="echo360",
            item_id=f"{target.lesson_id}:video",
            title=target.title,
            direct_link=target.recording_url,
            local_filepath=path,
            course_id=target.course_id,
            course_name=target.course_name,
            size_bytes=nbytes,
            content_type=content_type,
            metadata={**extra, "asset_type": "video"},
            auto_flush=False,
        )
    registry.flush()


def _result(
    target: VideoTarget,
    *,
    status: str,
    path: Path | None = None,
    nbytes: int = 0,
    quality: str = "",
    error: str = "",
) -> dict[str, Any]:
    keep = status in {"downloaded", "skipped"} and path is not None
    return {
        "lesson_id": target.lesson_id,
        "title": target.title,
        "course_id": target.course_id,
        "course_name": target.course_name,
        "recording_url": target.recording_url,
        "recording_url_kind": target.kind,
        "video_status": status,
        "video_downloaded": keep,
        "video_filepath": str(path) if keep else "",
        "video_bytes": nbytes if keep else 0,
        "video_quality": quality,
        "video_error": error,
    }


def _fields_from_row(item: Mapping[str, Any]) -> dict[str, Any]:
    meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    status = str(meta.get("video_status") or "")
    path_text = str(meta.get("video_filepath") or "")
    item_id = str(item.get("item_id") or "")
    local = str(item.get("local_filepath") or "")
    if item_id.endswith(":video") and local:
        path_text = path_text or local
        status = status or "downloaded"
    path = Path(path_text) if path_text else None
    exists = bool(path and path.is_file() and path.stat().st_size > 0)
    if exists and status in {"", "downloaded", "skipped"}:
        return {
            "video_downloaded": True,
            "video_status": status or "downloaded",
            "video_filepath": str(path),
            "video_bytes": path.stat().st_size if path else 0,
            "video_quality": str(meta.get("video_quality") or ""),
        }
    if status == "downloaded" and not exists:
        status = "missing_file"
    return {
        "video_downloaded": False,
        "video_status": status,
        "video_filepath": "",
        "video_bytes": 0,
        "video_quality": str(meta.get("video_quality") or ""),
    }


def lookup_video_status(data_root: Path, *idents: str) -> dict[str, Any]:
    """Read video status from the manifest. Missing files report ``missing_file``."""
    wanted = {str(item) for item in idents if str(item or "").strip()}
    if not wanted:
        return dict(_EMPTY_VIDEO)
    manifest = Path(data_root) / "downloads_manifest.json"
    if not manifest.is_file():
        return dict(_EMPTY_VIDEO)
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return dict(_EMPTY_VIDEO)
    items = raw.get("items") if isinstance(raw, Mapping) else None
    if not isinstance(items, Mapping):
        return dict(_EMPTY_VIDEO)
    fallback: dict[str, Any] | None = None
    stems = {lesson_stem(item) for item in wanted}
    stems.update(wanted)
    for item in items.values():
        if not isinstance(item, Mapping):
            continue
        meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        lesson = str(item.get("lesson_id") or meta.get("lesson_id") or "")
        item_id = str(item.get("item_id") or "")
        trimmed = item_id.removesuffix(":subtitles").removesuffix(":video")
        local = str(item.get("local_filepath") or "")
        keys = {lesson, lesson_stem(lesson), trimmed, lesson_stem(trimmed), Path(local).stem if local else ""}
        if not keys & stems:
            continue
        fields = _fields_from_row(item)
        if fields["video_downloaded"]:
            return fields
        if fields["video_status"] and fallback is None:
            fallback = fields
    return fallback or dict(_EMPTY_VIDEO)


def lesson_video_present(data_root: Path, lesson_id: str) -> bool:
    """True when a non-empty video file for this lesson is already on disk."""
    stem = lesson_stem(lesson_id)
    root = Path(data_root) / "lecture_videos"
    if not stem or not root.is_dir():
        return False
    for path in root.glob(f"**/{stem}.*"):
        if path.suffix in {".mp4", ".m4s", ".ts"} and path.is_file() and path.stat().st_size > 0:
            return True
    return False


def video_fields_from_item(item: Any) -> dict[str, Any]:
    """Video fields for a registry row already in memory."""
    meta = item.metadata or {}
    return _fields_from_row(
        {
            "item_id": item.item_id,
            "local_filepath": item.local_filepath,
            "metadata": meta,
        }
    )


async def _probe_ok(client: MediaClient, url: str, max_size: int) -> ProbeResult | None:
    if not is_allowed_media_url(url):
        return None
    try:
        probed = await client.probe(url)
    except DownloadError:
        return None
    except Exception:
        logger.info("Media probe failed (%s)", public_failure(0, url))
        return None
    if probed.status not in {200, 206}:
        return None
    if probed.total is not None and (probed.total <= 0 or probed.total > max_size):
        return None
    return probed


async def prefer_progressive(
    choice: MediaChoice,
    client: MediaClient,
    *,
    max_size: int,
) -> MediaChoice:
    """Use a single mp4 beside the playlist when one exists inside the size cap."""
    if choice.container not in {"hls", "dash"}:
        return choice
    found: list[tuple[int, str, int | None]] = []
    for rank, name in enumerate(_MUXED_NAMES):
        url = sibling_url(choice.url, name)
        probed = await _probe_ok(client, url, max_size)
        if probed is not None:
            found.append((rank, url, probed.total))
    known = [item for item in found if item[2]]
    pool = known or found
    if pool:
        # Known sizes: highest quality that fit (lower rank). Unknown sizes: SD.
        pick = min(pool, key=lambda item: item[0]) if known else max(pool, key=lambda item: item[0])
        return MediaChoice(container=container_of(pick[1]), url=pick[1], label="hd" if pick[0] < 2 else "sd")

    video_url = ""
    audio_url = ""
    for name in _VIDEO_NAMES:
        probed = await _probe_ok(client, sibling_url(choice.url, name), max_size)
        if probed is not None:
            video_url = sibling_url(choice.url, name)
            break
    for name in _AUDIO_NAMES:
        probed = await _probe_ok(client, sibling_url(choice.url, name), max_size)
        if probed is not None:
            audio_url = sibling_url(choice.url, name)
            break
    if video_url and audio_url and shutil.which("ffmpeg"):
        label = "hd" if "q1" in video_url else "sd"
        return MediaChoice(container="mux", url=video_url, audio_url=audio_url, label=label)
    return choice


async def _choice_from_properties(
    client: MediaClient,
    target: VideoTarget,
    *,
    max_height_px: int,
) -> tuple[MediaChoice | None, str]:
    last = ""
    for url in player_property_urls(target):
        try:
            response = await client.get(url)
        except DownloadError as exc:
            last = str(exc)
            continue
        except Exception:
            last = public_failure(0, url)
            continue
        if response.status != 200 or not response.body:
            last = public_failure(response.status, url)
            continue
        try:
            payload = json.loads(response.body.decode("utf-8", errors="replace"))
        except ValueError:
            last = public_failure(response.status, url)
            continue
        choice = choose_playable(payload, max_height_px=max_height_px)
        if choice is not None:
            return choice, ""
        last = "player-properties had no video"
    return None, last


async def _choice_from_html(client: MediaClient, page_url: str) -> tuple[MediaChoice | None, str]:
    if not page_url or not is_allowed_media_url(page_url):
        return None, ""
    try:
        response = await client.get(page_url)
    except Exception:
        return None, public_failure(0, page_url)
    if response.status != 200 or not response.body:
        return None, public_failure(response.status, page_url)
    html = response.body.decode("utf-8", errors="replace")
    for url in streams_in_html(html):
        return MediaChoice(container=container_of(url), url=url, label="page"), ""
    return None, ""


def _choice_from_launch(launch: LaunchResult) -> MediaChoice | None:
    if launch.media_url and is_allowed_media_url(launch.media_url):
        kind = launch.media_kind
        if kind == "echo360_player":
            return None
        return MediaChoice(container=container_of(launch.media_url, kind), url=launch.media_url, label="lti")
    return None


async def resolve_media(
    target: VideoTarget,
    client: MediaClient,
    *,
    max_height_px: int,
    max_size: int,
) -> tuple[MediaChoice | None, str]:
    """Resolve one recording to a downloadable URL. The URL is not returned to callers."""
    note = ""
    if target.kind in {"canvas_embed_page", "unplayable_lti"} or (
        target.source == "canvas_embed" and target.kind != "echo360_public"
    ):
        try:
            launch = await client.launch(target.record)
        except Exception:
            launch = LaunchResult(status="error", reason="browser_unavailable")
        choice = _choice_from_launch(launch)
        if choice is None and launch.media_id and launch.media_id.lower() not in _PLACEMENT_IDS:
            target = VideoTarget(**{**target.__dict__, "media_id": launch.media_id})
        if choice is not None:
            return await prefer_progressive(choice, client, max_size=max_size), ""
        note = launch.reason or launch.status or "lti_unresolved"

    choice, properties_note = await _choice_from_properties(client, target, max_height_px=max_height_px)
    if choice is not None:
        return await prefer_progressive(choice, client, max_size=max_size), ""
    html_choice, html_note = await _choice_from_html(client, target.page_url)
    if html_choice is not None:
        return await prefer_progressive(html_choice, client, max_size=max_size), ""
    return None, note or properties_note or html_note or "no_video"


def _ffmpeg_argv(ffmpeg: str, inputs: list[str], dest: Path, headers: dict[str, str]) -> list[str]:
    argv = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    blob = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    if blob.strip():
        argv.extend(["-headers", blob])
    for item in inputs:
        argv.extend(["-i", item])
    argv.extend(["-c", "copy"])
    if len(inputs) > 1:
        argv.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    else:
        argv.extend(["-bsf:a", "aac_adtstoasc"])
    argv.append(str(dest))
    return argv


async def _run_ffmpeg(argv: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise DownloadError(f"ffmpeg exited {proc.returncode}")


async def _download_bytes(
    client: MediaClient,
    url: str,
    dest: Path,
    *,
    max_size: int,
) -> int:
    if not is_allowed_media_url(url):
        raise DownloadError(public_failure(0, url))
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    existing = part.stat().st_size if part.is_file() else 0
    headers: dict[str, str] = {}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    try:
        result = await client.stream_to(url, part, headers)
    except DownloadError:
        raise
    except Exception:
        raise DownloadError(public_failure(0, url))
    if result.status == 403 and existing:
        part.unlink(missing_ok=True)
        raise DownloadError(public_failure(403, url) + "; retry the download")
    if result.status not in {200, 206}:
        raise DownloadError(public_failure(result.status, url))
    total = part.stat().st_size if part.is_file() else 0
    if result.total and result.total > max_size:
        part.unlink(missing_ok=True)
        raise DownloadError("file is larger than UNISYNC_LECTURE_VIDEO_MAX_BYTES")
    if total <= 0 or total > max_size:
        part.unlink(missing_ok=True)
        raise DownloadError("downloaded file was empty or larger than the size cap")
    part.replace(dest)
    return total


async def _download_hls(
    client: MediaClient,
    playlist_url: str,
    dest: Path,
    *,
    max_height_px: int,
    max_size: int,
    headers: dict[str, str],
) -> tuple[Path, int, str]:
    response = await client.get(playlist_url)
    if response.status != 200 or not response.body:
        raise DownloadError(public_failure(response.status, playlist_url))
    text = response.body.decode("utf-8", errors="replace")
    variant = select_variant(text, playlist_url, max_height_px=max_height_px)
    if variant != playlist_url:
        response = await client.get(variant)
        if response.status != 200 or not response.body:
            raise DownloadError(public_failure(response.status, variant))
        text = response.body.decode("utf-8", errors="replace")
        playlist_url = variant
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        argv = _ffmpeg_argv(ffmpeg, [playlist_url], part, headers)
        try:
            await _run_ffmpeg(argv)
        except DownloadError:
            part.unlink(missing_ok=True)
            raise
        if not part.is_file() or part.stat().st_size <= 0 or part.stat().st_size > max_size:
            part.unlink(missing_ok=True)
            raise DownloadError("ffmpeg produced an empty or oversized file")
        part.replace(dest)
        return dest, dest.stat().st_size, "hls ffmpeg"
    segments = [url for url in segment_urls(text, playlist_url) if is_allowed_media_url(url)]
    if not segments:
        raise DownloadError("playlist had no media segments")
    temp = dest.parent / f".{dest.stem}.segments"
    temp.mkdir(parents=True, exist_ok=True)
    total = 0
    paths: list[Path] = []
    try:
        for index, url in enumerate(segments):
            chunk = temp / f"{index:05d}.bin"
            if not (chunk.is_file() and chunk.stat().st_size > 0):
                await _download_bytes(client, url, chunk, max_size=max_size)
            total += chunk.stat().st_size
            if total > max_size:
                raise DownloadError("playlist is larger than UNISYNC_LECTURE_VIDEO_MAX_BYTES")
            paths.append(chunk)
        part = dest.with_suffix(".ts.part")
        with part.open("wb") as handle:
            for chunk in paths:
                handle.write(chunk.read_bytes())
        final = dest.with_suffix(".ts")
        part.replace(final)
        return final, final.stat().st_size, "hls segments"
    finally:
        shutil.rmtree(temp, ignore_errors=True)


async def _store_choice(
    target: VideoTarget,
    choice: MediaChoice,
    *,
    client: MediaClient,
    dest_dir: Path,
    registry: Any,
    max_height_px: int,
    max_size: int,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    stem = lesson_stem(target.lesson_id) or "lecture"
    present = _existing_video(dest_dir, stem)
    if present is not None:
        record_video_result(
            registry,
            target,
            status="skipped",
            path=present,
            nbytes=present.stat().st_size,
            quality=choice.label or choice.container,
        )
        return _result(
            target,
            status="skipped",
            path=present,
            nbytes=present.stat().st_size,
            quality=choice.label or choice.container,
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    header_map = dict(headers or {})
    try:
        if not header_map:
            header_map = dict(await client.auth_headers(choice.url))
        if choice.container == "dash":
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise DownloadError("DASH stream needs ffmpeg, which is not installed")
            dest = dest_dir / f"{stem}.mp4"
            part = dest.with_name(dest.name + ".part")
            await _run_ffmpeg(_ffmpeg_argv(ffmpeg, [choice.url], part, header_map))
            part.replace(dest)
            path, nbytes, quality = dest, dest.stat().st_size, "dash ffmpeg"
        elif choice.container == "mux":
            video = dest_dir / f".{stem}.video.part"
            audio = dest_dir / f".{stem}.audio.part"
            await _download_bytes(client, choice.url, video, max_size=max_size)
            await _download_bytes(client, choice.audio_url, audio, max_size=max_size)
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise DownloadError("separate audio and video need ffmpeg")
            dest = dest_dir / f"{stem}.mp4"
            part = dest.with_name(dest.name + ".part")
            await _run_ffmpeg(_ffmpeg_argv(ffmpeg, [str(video), str(audio)], part, {}))
            video.unlink(missing_ok=True)
            audio.unlink(missing_ok=True)
            part.replace(dest)
            path, nbytes, quality = dest, dest.stat().st_size, f"mux {choice.label}".strip()
        elif choice.container == "hls":
            dest = dest_dir / f"{stem}.mp4"
            path, nbytes, quality = await _download_hls(
                client,
                choice.url,
                dest,
                max_height_px=max_height_px,
                max_size=max_size,
                headers=header_map,
            )
        else:
            ext = ".m4s" if choice.container == "m4s" else ".mp4"
            dest = dest_dir / f"{stem}{ext}"
            nbytes = await _download_bytes(client, choice.url, dest, max_size=max_size)
            path, quality = dest, choice.label or choice.container
    except DownloadError as exc:
        message = str(exc)
        status = "too_large" if "larger than" in message else "needs_ffmpeg" if "ffmpeg" in message else "failed"
        record_video_result(registry, target, status=status, path=None, nbytes=0, quality="", error=message)
        return _result(target, status=status, error=message)
    record_video_result(
        registry,
        target,
        status="downloaded",
        path=path,
        nbytes=nbytes,
        quality=quality,
    )
    logger.info(
        "Saved lecture video %s (%s, %d bytes)",
        target.lesson_id[:80],
        quality,
        nbytes,
    )
    return _result(target, status="downloaded", path=path, nbytes=nbytes, quality=quality)


async def download_one(
    target: VideoTarget,
    *,
    client: MediaClient,
    data_root: Path,
    registry: Any,
    max_height_px: int | None = None,
    max_size: int | None = None,
) -> dict[str, Any]:
    height = max_height() if max_height_px is None else max_height_px
    limit = max_bytes() if max_size is None else max_size
    dest_dir = _video_dir(data_root, target)
    present = _existing_video(dest_dir, lesson_stem(target.lesson_id) or "lecture")
    if present is not None:
        record_video_result(
            registry,
            target,
            status="skipped",
            path=present,
            nbytes=present.stat().st_size,
            quality="",
        )
        return _result(target, status="skipped", path=present, nbytes=present.stat().st_size)
    choice, reason = await resolve_media(target, client, max_height_px=height, max_size=limit)
    if choice is None:
        record_video_result(registry, target, status="unavailable", path=None, nbytes=0, quality="", error=reason)
        return _result(target, status="unavailable", error=reason)
    return await _store_choice(
        target,
        choice,
        client=client,
        dest_dir=dest_dir,
        registry=registry,
        max_height_px=height,
        max_size=limit,
    )


def build_client() -> MediaClient:
    from unisync.services.sync.lecture_video_browser import BrowserLectureSession

    return BrowserLectureSession()


async def download_lectures(
    *,
    data_root: Path,
    course: str,
    lecture: str = "",
    limit: int = 1,
    client: MediaClient | None = None,
    registry: Any = None,
    max_height_px: int | None = None,
    max_size: int | None = None,
) -> list[dict[str, Any]]:
    """Download up to ``limit`` recordings for one course. This is the opt-in entry."""
    if not str(course or "").strip():
        return [{"video_status": "failed", "video_error": "A course code or name is required.", "video_downloaded": False}]
    targets = select_targets(data_root, course=course, lecture=lecture)
    if not targets:
        return [{
            "video_status": "unavailable",
            "video_error": "No matching Echo360 lesson or Canvas embed was found in the local catalog.",
            "video_downloaded": False,
            "course_name": course,
        }]
    if registry is None:
        from unisync.core.download_registry import DownloadRegistry

        registry = DownloadRegistry(Path(data_root) / "downloads_manifest.json")
    media_client = client or build_client()
    results: list[dict[str, Any]] = []
    for target in targets[: max(1, limit)]:
        results.append(
            await download_one(
                target,
                client=media_client,
                data_root=data_root,
                registry=registry,
                max_height_px=max_height_px,
                max_size=max_size,
            )
        )
    return results


def _note_library_item(data_root: Path, item_id: str, result: Mapping[str, Any]) -> None:
    """Point an existing media-library row at the saved video without storing the stream URL."""
    if not item_id:
        return
    meta_path = Path(data_root) / "media_library" / item_id / "metadata.json"
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    saved = bool(result.get("video_downloaded"))
    meta["status"] = "downloaded" if saved else str(result.get("video_status") or "failed")
    meta["video_filepath"] = str(result.get("video_filepath") or "")
    meta["progress"] = meta["video_filepath"] or str(result.get("video_error") or "")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


async def download_resolved_source(
    item_id: str,
    source: Any,
    *,
    client: MediaClient | None = None,
    data_root: Path | None = None,
    registry: Any = None,
) -> dict[str, Any]:
    """Download a stream the sync pipeline already resolved. Used only when the flag is on."""
    if data_root is None:
        from unisync.core.path_service import get_path_service

        data_root = get_path_service().get_user_root()
    lesson_id = str(getattr(source, "id", "") or "")
    media_url = str(getattr(source, "media_url", "") or "")
    page = classroom_url(lesson_id) if lesson_id and not lesson_id.startswith("BLAW_EMBED_") else ""
    target = VideoTarget(
        lesson_id=lesson_id or "lecture",
        title=str(getattr(source, "title", "") or "Echo360 lecture"),
        course_id=str(getattr(source, "course_id", "") or ""),
        course_name=str(getattr(source, "course_name", "") or ""),
        date=str(getattr(source, "date", "") or ""),
        kind="echo360_lesson" if page else "canvas_embed_page",
        media_id="",
        public_id="",
        secure_id="",
        page_url=page,
        recording_url=page,
        source="echo360_course",
    )
    if registry is None:
        from unisync.core.download_registry import DownloadRegistry

        registry = DownloadRegistry(Path(data_root) / "downloads_manifest.json")
    if not is_allowed_media_url(media_url):
        record_video_result(registry, target, status="failed", path=None, nbytes=0, quality="", error="unsupported media URL")
        return _result(target, status="failed", error="unsupported media URL")
    media_client = client or build_client()
    choice = MediaChoice(
        container=container_of(media_url, str(getattr(source, "media_kind", "") or "")),
        url=media_url,
        label=str(getattr(source, "media_kind", "") or ""),
    )
    choice = await prefer_progressive(choice, media_client, max_size=max_bytes())
    result = await _store_choice(
        target,
        choice,
        client=media_client,
        dest_dir=_video_dir(data_root, target),
        registry=registry,
        max_height_px=max_height(),
        max_size=max_bytes(),
    )
    _note_library_item(data_root, str(item_id or ""), result)
    return result


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download Echo360 lecture videos. A normal sync does not do this.",
    )
    parser.add_argument("--course", required=True, help="Course code or name, for example COMP10001 or BLAW10001.")
    parser.add_argument("--lecture", default="", help="Date, title fragment, or lesson id. Omit to take the newest matches.")
    parser.add_argument("--limit", type=int, default=1, help="How many recordings to download (default 1).")
    parser.add_argument("--data-root", default="", help="UniSync data directory. Defaults to the path service root.")
    args = parser.parse_args(argv)
    if args.data_root:
        data_root = Path(args.data_root)
    else:
        from unisync.core.path_service import get_path_service

        data_root = get_path_service().get_user_root()
    results = asyncio.run(
        download_lectures(
            data_root=data_root,
            course=args.course,
            lecture=args.lecture,
            limit=max(1, args.limit),
        )
    )
    from unisync.mcp.egress import scrub_lecture_payload

    print(json.dumps(scrub_lecture_payload(results), indent=2))
    return 0 if any(item.get("video_downloaded") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
