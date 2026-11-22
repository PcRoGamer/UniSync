"""Camoufox-native Echo360 transcript, media and thumbnail resolution.

This is the replacement for the userscript's player hooks.  It runs inside the
authenticated persistent browser context, so it works without an open Canvas
tab: navigation to the LTI launch URL performs the SSO/LTI handshake, and the
transcript / HLS responses the player requests are captured in flight.

Every write goes through :mod:`unisync.services.sync.echo360_ingest`, which owns
the canonical ``(canvas_course_id, uuid, link_id)`` identity and rejects
transcripts that are not cue-valid WebVTT.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, quote, urlparse

from unisync.services.sync.echo360_ingest import (
    canonical_embed_key,
    embed_lesson_id,
    is_cue_valid_vtt,
    is_notice_only_vtt,
    normalize_link_id,
    parse_hls_subtitle_uris,
    read_transcript,
    transcript_from_json,
    transcript_shape,
    write_thumbnail,
    write_transcript,
)

logger = logging.getLogger(__name__)

_TRANSCRIPT_HINTS = ("/transcript", "transcript.", "/captions", "captions.")
_MANIFEST_HINTS = (".m3u8",)
_THUMBNAIL_HINTS = ("poster", "thumbnail", "thumb", "preview")

_HTML_MEDIA_ID = re.compile(r'\\?"(?:current)?mediaId\\?"\s*[:=]\s*\\?"([0-9a-fA-F-]{36})')
_HTML_SESSION_ID = re.compile(r'\\?"sessionId\\?"\s*[:=]\s*\\?"([0-9a-fA-F-]{36})')
_HTML_STREAM = re.compile(r'https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*', re.IGNORECASE)
_HTML_MP4 = re.compile(r'https?://[^"\'\\\s]+?\.mp4[^"\'\\\s]*', re.IGNORECASE)
_HTML_VTT = re.compile(r'https?://[^"\'\\\s]+?\.vtt[^"\'\\\s]*', re.IGNORECASE)
_HTML_POSTER = re.compile(r'https?://[^"\'\\\s]*thumbnails\.echo360\.net\.au[^"\'\\\s]*', re.IGNORECASE)


def _unique(target: list[str], value: object) -> None:
    text_value = str(value or "").replace("\\u0026", "&").strip()
    if text_value and text_value not in target:
        target.append(text_value)


def _extract_page_ids(html_sources: Sequence[str]) -> dict[str, list[str]]:
    """Ids and media URLs from Echo360 bootstrap JSON (public/LTI/player HTML).

    Canvas secure-link pages expose the real playback media id, the session id
    and the caption URL only inside this escaped bootstrap JSON, which is why
    the legacy bridge unescaped it before matching.  Without it an embed whose
    LTI shell never mounts a player looks unresolvable.
    """
    found: dict[str, list[str]] = {
        "media_ids": [],
        "session_ids": [],
        "streams": [],
        "posters": [],
        "vtt_urls": [],
    }
    for html in html_sources:
        body = str(html or "")
        for pattern, key in (
            (_HTML_MEDIA_ID, "media_ids"),
            (_HTML_SESSION_ID, "session_ids"),
            (_HTML_STREAM, "streams"),
            (_HTML_MP4, "streams"),
            (_HTML_VTT, "vtt_urls"),
            (_HTML_POSTER, "posters"),
        ):
            for match in pattern.finditer(body):
                value = match.group(1) if match.groups() else match.group(0)
                _unique(found[key], value.lower() if key.endswith("_ids") else value)
    return found


def _caption_urls(value: Any) -> list[str]:
    """Normalise ``data.captions`` (str, list[str] or list[dict]) to URLs."""
    urls: list[str] = []
    if isinstance(value, str):
        _unique(urls, value)
    elif isinstance(value, Mapping):
        for key in ("url", "uri", "href", "src"):
            if isinstance(value.get(key), str):
                _unique(urls, value[key])
                break
    elif isinstance(value, (list, tuple)):
        for item in value:
            for url in _caption_urls(item):
                _unique(urls, url)
    return urls

# Transcript/caption endpoints the real Echo360 player calls.  The previous
# guesses (``/api/v1/medias/<id>/transcript``) do not exist and 404'd, which made
# a live recording with captions look like "no transcript source".  These are
# taken from the legacy players' observed traffic (bridge-runtime.js) and the
# Echo player bundle's transcriptService -- note the plural ``/medias/``.
def _direct_transcript_urls(
    *,
    lesson_id: str,
    uuid: str = "",
    media_ids: Sequence[str] = ()
    ,
    secure_access_id: str = "",
    session_id: str = "",
) -> list[str]:
    """Every transcript endpoint the authenticated player is known to use.

    ``record["media_id"]`` is the LTI tool UUID, which is *not* the playback
    media id.  The real id only appears in the player URL
    (``/ui/player/<mediaId>``), so callers pass every id they can see.  The
    secure-link family also needs ``secureLinkAccessDataId`` from that URL.
    """
    base = "https://echo360.net.au"
    ids = [str(value).strip() for value in media_ids if str(value or "").strip()]
    if not ids and uuid:
        ids = [str(uuid).strip()]
    urls: list[str] = []

    def add(url: str) -> None:
        if url and url not in urls:
            urls.append(url)

    lesson = quote(str(lesson_id or "").strip(), safe="")
    public = quote(str(uuid or "").strip(), safe="")
    slad = quote(str(secure_access_id or "").strip(), safe="")
    session = quote(str(session_id or "").strip(), safe="")

    for media_id in ids:
        media = quote(media_id, safe="")
        # Most specific first: secure link, then public link, then lesson.
        if slad:
            add(f"{base}/api/ui/echoplayer/secure-link-access-datas/{slad}/medias/{media}/transcript-file?format=vtt")
        if public:
            add(f"{base}/api/ui/echoplayer/public-links/{public}/medias/{media}/transcript-file?format=vtt")
        if lesson:
            add(f"{base}/api/ui/echoplayer/lessons/{lesson}/medias/{media}/transcript-file?format=vtt")
        add(f"{base}/media/download/{media}/transcript.vtt?lessonId={lesson or media}")
        if session:
            add(f"{base}/media/download/{media}/transcript.vtt?lessonId={lesson or media}&sessionId={session}")
        # JSON cue sources from the same families (no -file suffix).
        if slad:
            add(f"{base}/api/ui/echoplayer/secure-link-access-datas/{slad}/medias/{media}/transcript")
        if public:
            add(f"{base}/api/ui/echoplayer/public-links/{public}/medias/{media}/transcript")
        if lesson:
            add(f"{base}/api/ui/echoplayer/lessons/{lesson}/medias/{media}/transcript")
    if lesson and not ids:
        add(f"{base}/media/download/{lesson}/transcript.vtt?lessonId={lesson}")
        add(f"{base}/api/ui/echoplayer/lessons/{lesson}/transcript")
    return urls


def _player_context(urls: Sequence[str]) -> dict[str, str]:
    """Media id, session id and secure-link access id from ``/ui/player`` URLs."""
    context = {"media_id": "", "session_id": "", "secure_link_access_data_id": ""}
    for url in urls:
        try:
            parsed = urlparse(str(url))
        except ValueError:
            continue
        parts = parsed.path.strip("/").split("/")
        for index, part in enumerate(parts):
            if part != "player" or index + 1 >= len(parts):
                continue
            candidate = parts[index + 1].strip()
            if candidate and "-" in candidate and not context["media_id"]:
                context["media_id"] = candidate
        query = parse_qs(parsed.query)
        if not context["session_id"]:
            context["session_id"] = (query.get("sessionId") or [""])[0]
        if not context["secure_link_access_data_id"]:
            context["secure_link_access_data_id"] = (
                query.get("secureLinkAccessDataId") or [""]
            )[0]
    return context


def _player_media_ids(urls: Sequence[str]) -> list[str]:
    """Extract media ids from captured ``/ui/player/<mediaId>`` URLs."""
    found: list[str] = []
    for url in urls:
        try:
            parts = urlparse(str(url)).path.strip("/").split("/")
        except ValueError:
            continue
        for index, part in enumerate(parts):
            if part != "player" or index + 1 >= len(parts):
                continue
            candidate = parts[index + 1].strip()
            if candidate and "-" in candidate and candidate not in found:
                found.append(candidate)
    return found


async def _player_urls_in_page(page):
    """Player URLs reachable from the loaded page (frames, DOM, inline scripts)."""
    found = []
    try:
        for frame in list(getattr(page, "frames", [])):
            url = str(getattr(frame, "url", "") or "")
            if "/ui/player/" in url and url not in found:
                found.append(url)
    except Exception:
        pass
    try:
        html = await page.content()
    except Exception:
        html = ""
    marker = "https://echo360.net.au/ui/player/"
    position = html.find(marker)
    while position >= 0:
        end = position + len(marker)
        while end < len(html) and not html[end].isspace():
            end += 1
        candidate = html[position:end]
        for cut in (chr(34), chr(39), "<", ">", ")"):
            candidate = candidate.split(cut)[0]
        candidate = candidate.replace("&amp;", "&")
        if candidate not in found:
            found.append(candidate)
        position = html.find(marker, end)
    return found


def _failure_reason(exc, *, browser_ready):
    name = type(exc).__name__
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return 'navigation_timeout' if browser_ready else 'browser_timeout'
    if name in ('TargetClosedError', 'BrowserClosedError', 'Error'):
        return 'browser_unavailable'
    return 'ingest_error' if browser_ready else 'browser_unavailable'


@dataclass
class IngestOutcome:
    key: str
    status: str
    reason: str = ""
    transcript_written: bool = False
    media_url: str = ""
    media_kind: str = ""
    thumbnail_saved: bool = False
    media_id: str = ""
    media_duration: float = 0.0
    cue_end: float = 0.0
    captured: list[str] = field(default_factory=list)
    probes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "reason": self.reason,
            "transcript_written": self.transcript_written,
            "media_url": self.media_url,
            "media_kind": self.media_kind,
            "thumbnail_saved": self.thumbnail_saved,
            "media_id": self.media_id,
            "media_duration": self.media_duration,
            "cue_end": self.cue_end,
            "captured": list(self.captured),
            "probes": list(self.probes)[:40],
        }


class Echo360NativeIngestor:
    """Resolve transcripts/media for embed records using the resident browser."""

    def __init__(
        self,
        *,
        service: Any | None = None,
        cache_dir: Path,
        thumbnail_dir: Path | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._service = service
        self.cache_dir = Path(cache_dir)
        self.thumbnail_dir = Path(thumbnail_dir) if thumbnail_dir else None
        self.timeout_seconds = timeout_seconds

    @property
    def service(self) -> Any:
        if self._service is None:
            from unisync.services.browser import get_camoufox_service

            self._service = get_camoufox_service()
        return self._service

    def _launch_url(self, record: Mapping[str, Any]) -> str:
        for key in ("canvas_launch_url", "player_url", "provider_launch_url"):
            value = str(record.get(key) or "").strip()
            if value.startswith("http"):
                return value
        return ""

    def _lti_page_url(self, uuid: str, link_id: object) -> str:
        """Echo360 LTI deep link for an embed (``linkId`` selects the video)."""
        value = str(uuid or "").strip()
        if not value:
            return ""
        base = f"https://echo360.net.au/lti/{quote(value, safe='')}"
        link = normalize_link_id(link_id)
        return f"{base}?linkId={quote(link, safe='')}" if link else base

    async def ingest(self, record: Mapping[str, Any]) -> IngestOutcome:
        key = canonical_embed_key(
            record.get("canvas_course_id"),
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        )
        lesson_id = embed_lesson_id(
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        )
        if read_transcript(self.cache_dir, lesson_id) is not None:
            return IngestOutcome(key=key, status="transcript", reason="already_present")

        launch_url = self._launch_url(record)
        if not launch_url:
            return IngestOutcome(key=key, status="unresolved", reason="missing_launch_url")

        try:
            async with self.service._context(headless=True) as context:
                try:
                    return await self._ingest_in_context(
                        context, record, lesson_id, launch_url, key
                    )
                except Exception as exc:
                    logger.info('Echo360 ingest failed after browser start (%s)', type(exc).__name__)
                    return IngestOutcome(
                        key=key, status='error',
                        reason=_failure_reason(exc, browser_ready=True),
                    )
        except Exception as exc:  # pragma: no cover - browser failures are environmental
            logger.info("Echo360 native ingest failed for %s (%s)", key, type(exc).__name__)
            return IngestOutcome(
                key=key, status='error',
                reason=_failure_reason(exc, browser_ready=False),
            )

    async def resolve_playable(self, record: Mapping[str, Any]) -> IngestOutcome:
        """Resolve a stream URL even when the transcript is already on disk.

        ``ingest`` returns before opening the player once a VTT is cached.
        Video download still needs the media URL. Canvas embeds are opened
        through the same authenticated browser context, so the LTI handshake
        can create an Echo360 session before player-properties is read.
        """
        key = canonical_embed_key(
            record.get("canvas_course_id"),
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        )
        lesson_id = embed_lesson_id(
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        )
        launch_url = self._launch_url(record)
        if not launch_url:
            return IngestOutcome(key=key, status="unresolved", reason="missing_launch_url")
        try:
            async with self.service._context(headless=True) as context:
                try:
                    return await self._ingest_in_context(
                        context, record, lesson_id, launch_url, key
                    )
                except Exception as exc:
                    logger.info(
                        "Echo360 media resolve failed after browser start (%s)",
                        type(exc).__name__,
                    )
                    return IngestOutcome(
                        key=key,
                        status="error",
                        reason=_failure_reason(exc, browser_ready=True),
                    )
        except Exception as exc:  # pragma: no cover - browser failures are environmental
            logger.info("Echo360 media resolve failed for %s (%s)", key, type(exc).__name__)
            return IngestOutcome(
                key=key,
                status="error",
                reason=_failure_reason(exc, browser_ready=False),
            )

    async def _ingest_in_context(
        self,
        context: Any,
        record: Mapping[str, Any],
        lesson_id: str,
        launch_url: str,
        key: str,
    ) -> IngestOutcome:
        outcome = IngestOutcome(
            key=key,
            status="missing_transcript",
            media_url=str(record.get("media_url") or ""),
            media_kind=str(record.get("media_kind") or ""),
        )
        transcript_candidates: list[str] = []
        manifest_urls: list[str] = []
        thumbnail_urls: list[str] = []
        player_urls: list[str] = []

        page = await context.new_page()

        def on_response(response: Any) -> None:
            url = str(getattr(response, "url", ""))
            lowered = url.lower()
            if "/ui/player/" in lowered and url not in player_urls:
                player_urls.append(url)
            if any(hint in lowered for hint in _TRANSCRIPT_HINTS):
                transcript_candidates.append(url)
            elif lowered.endswith(tuple(_MANIFEST_HINTS)) or ".m3u8" in lowered:
                manifest_urls.append(url)
            elif any(hint in lowered for hint in _THUMBNAIL_HINTS) and any(
                ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp")
            ):
                thumbnail_urls.append(url)
            outcome.captured.append(url)

        page.on("response", on_response)
        try:
            await page.goto(launch_url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
            await asyncio.sleep(3)
            # Nudge the player so the transcript/HLS requests actually fire.
            for selector in ("video", "button[class*='play' i]", "[id*='play' i]"):
                try:
                    element = page.locator(selector).first
                    if await element.count():
                        await element.click(timeout=2000)
                        break
                except Exception:
                    continue
            await asyncio.sleep(3)
            for url in await _player_urls_in_page(page):
                if url not in player_urls:
                    player_urls.append(url)
            await self._nudge_echo360_frames(page)
            if not player_urls:
                known = str(record.get("resolved_media_id") or "").strip()
                if known:
                    # A remembered playback id still lets us load the player
                    # directly when the LTI shell renders no player frame.
                    player_urls.append(f"https://echo360.net.au/ui/player/{known}")
            # The LTI shell embeds the player lazily and never autoplays, so no
            # transcript request fires yet.  Opening the captured player URL
            # directly (same authenticated context) makes the player load the
            # media and issue its caption/transcript XHRs.
            if player_urls:
                try:
                    await page.goto(
                        player_urls[0],
                        wait_until="domcontentloaded",
                        timeout=self.timeout_seconds * 1000,
                    )
                    await asyncio.sleep(4)
                    for selector in (
                        "video",
                        ".vjs-big-play-button",
                        "button[class*='play' i]",
                        "[id*='play' i]",
                    ):
                        try:
                            element = page.locator(selector).first
                            if await element.count():
                                await element.click(timeout=2000)
                                break
                        except Exception:
                            continue
                    await asyncio.sleep(4)
                    for url in await _player_urls_in_page(page):
                        if url not in player_urls:
                            player_urls.append(url)
                    await self._nudge_echo360_frames(page)
                    await asyncio.sleep(2)
                except Exception:
                    pass
        finally:
            page.remove_listener("response", on_response)

        # The LTI shell frequently renders no player frame at all, so ask
        # Echo360 directly: the public page and the LTI page carry the real
        # playback media id, session id, HLS stream and caption URL in their
        # (string-escaped) bootstrap JSON.
        page_ids: dict[str, list[str]] = {
            "media_ids": [],
            "session_ids": [],
            "streams": [],
            "posters": [],
            "vtt_urls": [],
        }
        uuid_value = str(record.get("uuid") or record.get("media_id") or "").strip()
        page_html: list[str] = []
        if uuid_value:
            for html_url in (
                f"https://echo360.net.au/media/{quote(uuid_value, safe='')}/public",
                self._lti_page_url(uuid_value, record.get("link_id")),
            ):
                if not html_url:
                    continue
                status, body = await self._fetch_status_text(context, html_url)
                if status == 200 and body:
                    page_html.append(body)
                    if len(outcome.probes) < 40:
                        outcome.probes.append(f"200 page {html_url}")
        page_ids = _extract_page_ids(page_html)
        for stream in page_ids["streams"]:
            if not outcome.media_url:
                outcome.media_url = stream
                outcome.media_kind = "m3u8" if ".m3u8" in stream.lower() else "mp4"
        for poster in page_ids["posters"]:
            if poster not in thumbnail_urls:
                thumbnail_urls.append(poster)
        for vtt_url in page_ids["vtt_urls"]:
            if vtt_url not in transcript_candidates:
                transcript_candidates.append(vtt_url)

        try:
            for url in manifest_urls:
                status, manifest = await self._fetch_status_text(context, url)
                outcome.probes.append(f"{status} manifest {url}")
                if status != 200 or not manifest or "#EXTM3U" not in manifest:
                    continue
                if not outcome.media_url:
                    outcome.media_url = url
                    outcome.media_kind = "m3u8"
                transcript_candidates.extend(parse_hls_subtitle_uris(manifest, url))

            if outcome.media_url:
                outcome.media_duration = await self._media_duration(context, outcome.media_url)

            player = _player_context(player_urls)
            if player["media_id"] and not outcome.media_url:
                # Persist the resolved playback URL: it used to be seen and dropped.
                outcome.media_url = player_urls[0]
                outcome.media_kind = "echo360_player"
            tool_uuid = str(record.get("uuid") or record.get("media_id") or "").strip().lower()
            # A public media page (/media/<uuid>/public) uses the embed uuid AS
            # the playback id; only the shared LTI tool uuid must be rejected.
            launch = str(self._launch_url(record) or "").lower()
            public_media = "/media/" in launch and "/public" in launch
            media_ids: list[str] = []
            candidates = [
                player["media_id"],
                *_player_media_ids(player_urls),
                *page_ids["media_ids"],
                record.get("resolved_media_id"),
            ]
            if public_media:
                candidates.append(tool_uuid)
            for candidate in candidates:
                value = str(candidate or "").strip().lower()
                if value and value not in media_ids and (value != tool_uuid or public_media):
                    media_ids.append(value)
            if media_ids:
                # Remember the real playback id, not the LTI tool uuid.
                outcome.media_id = media_ids[0]
            if not media_ids:
                outcome.status = "unresolved"
                outcome.reason = "media_id_unresolved"
            else:
                transcript_candidates.extend(
                    _direct_transcript_urls(
                        lesson_id="" if lesson_id.startswith("BLAW_EMBED_") else lesson_id,
                        uuid=str(record.get("uuid") or record.get("media_id") or ""),
                        media_ids=media_ids,
                        secure_access_id=player["secure_link_access_data_id"],
                        session_id=player["session_id"],
                    )
                )

            captions, playable = await self._player_properties(
                context,
                uuid=uuid_value,
                media_ids=media_ids,
                lesson_id=(
                    (page_ids["session_ids"][0] if page_ids["session_ids"] else "")
                    or ("" if lesson_id.startswith("BLAW_EMBED_") else lesson_id)
                ),
                secure_access_id=player["secure_link_access_data_id"],
            )
            for url in captions:
                if url not in transcript_candidates:
                    transcript_candidates.append(url)
            if playable and (not outcome.media_url or outcome.media_kind not in ("m3u8", "mp4")):
                preferred = next(
                    (item for item in playable if item["kind"] == "m3u8"), playable[0]
                )
                outcome.media_url = preferred["url"]
                outcome.media_kind = preferred["kind"]
            if outcome.media_url and not outcome.media_duration:
                outcome.media_duration = await self._media_duration(context, outcome.media_url)

            seen: set[str] = set()
            best = None
            best_notice = None
            for url in transcript_candidates:
                if url in seen:
                    continue
                seen.add(url)
                status, body = await self._fetch_status_text(context, url)
                if len(outcome.probes) < 40:
                    outcome.probes.append(f"{status} {url}")
                if status != 200 or not body:
                    continue
                vtt = body if is_cue_valid_vtt(body) else transcript_from_json(_maybe_json(body))
                if not vtt or not is_cue_valid_vtt(vtt):
                    continue
                span, cues = transcript_shape(vtt)
                candidate = (span, cues, url, vtt)
                if is_notice_only_vtt(vtt):
                    if best_notice is None or candidate[:2] > best_notice[:2]:
                        best_notice = candidate
                elif best is None or candidate[:2] > best[:2]:
                    best = candidate
            # Never settle for the ~15 s copyright notice when a real lecture
            # track is also reachable; keep the best track, not the first one.
            chosen = best or best_notice
            if chosen is not None:
                span, _cues, chosen_url, vtt = chosen
                outcome.cue_end = span
                if write_transcript(self.cache_dir, lesson_id, vtt):
                    outcome.transcript_written = True
                if is_notice_only_vtt(vtt):
                    outcome.status = "notice_only"
                    outcome.reason = "notice_only_track"
                    if len(outcome.probes) < 40:
                        outcome.probes.append(f"notice-only track chosen {chosen_url}")
                else:
                    outcome.status = "transcript"
                    outcome.reason = ""

            if self.thumbnail_dir is not None:
                for url in thumbnail_urls[:2]:
                    raw = await self._fetch_bytes(context, url)
                    if raw and write_thumbnail(self.thumbnail_dir, record, raw):
                        outcome.thumbnail_saved = True
                        break
        finally:
            try:
                await page.close()
            except Exception:
                pass

        if outcome.status != "transcript" and not outcome.reason:
            statuses = {probe.split(" ", 1)[0] for probe in outcome.probes}
            if statuses & {"401", "403"}:
                # A permission failure is not proof that captions are absent.
                outcome.reason = "transcript_forbidden"
            elif outcome.probes:
                outcome.reason = "no_transcript_source"
            else:
                outcome.reason = "no_transcript_probe"
        return outcome

    async def _player_properties(
        self,
        context: Any,
        *,
        uuid: str,
        media_ids: Sequence[str],
        lesson_id: str,
        secure_access_id: str,
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Caption URLs and playable media from the player-properties API.

        The Echo player bundle fetches ``player-properties`` for every context
        family (secure link, public link, lesson) and reads ``data.captions``
        plus ``playableAudioVideo.playableMedias[].uri``.  That is the only
        place a real caption track appears for some embeds.
        """
        captions: list[str] = []
        playable: list[dict[str, str]] = []
        base = "https://echo360.net.au/api/ui/echoplayer"
        lesson = quote(str(lesson_id or "").strip(), safe="")
        public = quote(str(uuid or "").strip(), safe="")
        slad = quote(str(secure_access_id or "").strip(), safe="")
        for media_id in media_ids:
            media = quote(str(media_id or "").strip(), safe="")
            if not media:
                continue
            paths: list[str] = []
            if slad:
                paths.append(
                    f"{base}/secure-link-access-datas/{slad}/media/{media}/player-properties"
                )
            if public:
                paths.append(f"{base}/public-links/{public}/media/{media}/player-properties")
            if lesson:
                paths.append(f"{base}/lessons/{lesson}/medias/{media}/player-properties")
            for path in paths:
                status, body = await self._fetch_status_text(context, path)
                if status != 200 or not body:
                    continue
                payload = _maybe_json(body)
                data = payload
                if isinstance(payload, Mapping) and isinstance(payload.get("data"), Mapping):
                    data = payload["data"]
                if not isinstance(data, Mapping):
                    continue
                for url in _caption_urls(data.get("captions")):
                    if url not in captions:
                        captions.append(url)
                audio_video = data.get("playableAudioVideo")
                medias = audio_video.get("playableMedias") if isinstance(audio_video, Mapping) else None
                for item in medias or []:
                    if not isinstance(item, Mapping):
                        continue
                    uri = str(item.get("uri") or "").strip()
                    if not uri:
                        continue
                    kind = "m3u8" if item.get("isHls") or ".m3u8" in uri.lower() else "mp4"
                    entry = {"url": uri, "kind": kind}
                    if entry not in playable:
                        playable.append(entry)
        return captions, playable

    async def _media_duration(self, context: Any, master_url: str) -> float:
        """Total #EXTINF duration of the video variant playlist (0 if unknown).

        Used to tell "this lecture has no captions" apart from "we only fetched
        the 15 s copyright disclaimer".
        """
        base, _, query = str(master_url).partition("?")
        prefix = base.rsplit("/", 1)[0]
        suffix = f"?{query}" if query else ""
        for name in ("s1q1.m3u8", "s1q0.m3u8", "s1_av.m3u8"):
            status, body = await self._fetch_status_text(context, f"{prefix}/{name}{suffix}")
            if status != 200 or not body:
                continue
            spans = re.findall(r"#EXTINF:([0-9.]+)", body)
            if spans:
                return round(sum(float(value) for value in spans), 3)
        return 0.0

    async def _nudge_echo360_frames(self, page) -> None:
        """Click play controls inside Echo360 iframes so playback can start."""
        for frame in list(getattr(page, "frames", [])):
            if "echo360.net.au" not in str(getattr(frame, "url", "") or ""):
                continue
            for selector in (
                "video",
                ".vjs-big-play-button",
                "button[aria-label*='lay' i]",
                "button[title*='lay' i]",
            ):
                try:
                    element = frame.locator(selector).first
                    if await element.count():
                        await element.click(timeout=2000)
                        break
                except Exception:
                    continue

    async def _fetch_status_text(self, context: Any, url: str) -> tuple[int, str]:

        """GET a URL in the authenticated context, returning ``(status, body)``."""
        response = None
        try:
            response = await context.request.get(url, timeout=self.timeout_seconds * 1000, max_redirects=5)
            status = int(getattr(response, "status", 0) or 0)
            if status != 200:
                return status, ""
            return status, await response.text()
        except Exception:
            return 0, ""
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    async def _fetch_bytes(self, context: Any, url: str) -> bytes:
        response = None
        try:
            response = await context.request.get(url, timeout=self.timeout_seconds * 1000, max_redirects=5)
            if getattr(response, "status", 0) != 200:
                return b""
            return await response.body()
        except Exception:
            return b""
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass


def _maybe_json(body: str) -> Any:
    import json

    stripped = body.lstrip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def is_echo360_url(url: object) -> bool:
    try:
        host = urlparse(str(url)).hostname or ""
    except ValueError:
        return False
    return host.endswith("echo360.net.au")


async def ingest_many(
    ingestor: Echo360NativeIngestor,
    records: Sequence[Mapping[str, Any]],
    *,
    concurrency: int = 2,
) -> list[IngestOutcome]:
    """Resolve a queue with a small throttle; never skip or hide failures."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(record: Mapping[str, Any]) -> IngestOutcome:
        async with semaphore:
            return await ingestor.ingest(record)

    return list(await asyncio.gather(*(run(record) for record in records)))


def link_identity(record: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """Convenience accessor used by queue persistence and status reporting."""
    return (
        str(record.get("canvas_course_id") or ""),
        str(record.get("uuid") or record.get("media_id") or "").lower(),
        normalize_link_id(record.get("link_id")),
    )
