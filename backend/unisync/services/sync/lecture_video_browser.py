"""Download lecture media with the resident Canvas and Echo360 browser session.

Cookies stay in the browser profile and on the request. They are not logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import httpx

from unisync.services.sync.lecture_videos import (
    DownloadError,
    FetchResult,
    LaunchResult,
    ProbeResult,
    StreamResult,
    is_allowed_media_url,
    public_failure,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(30.0, read=300.0)
_CAPTURE_SECONDS = 45
_ECHO_HOST = "echo360.net.au"
_FETCH_JSON = """async (p) => {
  try { const r = await fetch(p, {credentials: 'include'});
        if (!r.ok) return {status: r.status};
        return {status: r.status, body: await r.json()}; }
  catch (e) { return {status: 0}; } }"""
# Follow Echo360's download redirect to the signed CDN URL, then stop the body.
_FETCH_FINAL_URL = """async (p) => {
  const c = new AbortController();
  try { const r = await fetch(p, {credentials: 'include', signal: c.signal});
        const u = r.ok ? r.url : ''; c.abort(); return u; }
  catch (e) { return ''; } }"""
_MUTE_STATE = """() => [...document.querySelectorAll('video,audio')].map(v => v.muted || v.volume === 0)"""
logger = logging.getLogger(__name__)

# Belt and braces on top of the browser-wide ``media.volume_scale=0`` pref:
# every <video>/<audio> in every frame is muted before it can play.
MUTE_SCRIPT = """
(() => {
  const hush = (el) => { try { el.muted = true; el.defaultMuted = true; el.volume = 0; } catch (e) {} };
  const proto = window.HTMLMediaElement && HTMLMediaElement.prototype;
  if (proto && !proto.__unisyncMuted) {
    const play = proto.play;
    proto.play = function (...args) { hush(this); return play.apply(this, args); };
    Object.defineProperty(proto, "__unisyncMuted", { value: true });
  }
  for (const type of ["play", "playing", "loadedmetadata", "volumechange"]) {
    document.addEventListener(type, (event) => {
      if (event.target instanceof HTMLMediaElement && !event.target.muted) hush(event.target);
    }, true);
  }
})();
"""


def _is_properties(url: str) -> bool:
    return "/api/ui/echoplayer/" in url and url.split("?", 1)[0].endswith("/player-properties")


async def mute_context(context: Any) -> None:
    """Install the mute script once per browser context."""
    if getattr(context, "_unisync_muted", False):
        return
    await context.add_init_script(MUTE_SCRIPT)
    try:
        setattr(context, "_unisync_muted", True)
    except Exception:
        pass


class _PropertiesRecorder:
    """Collect player-properties bodies the Echo360 player fetches, in any frame."""

    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self._tasks: list[asyncio.Future] = []

    async def _read(self, response: Any) -> None:
        try:
            if response.status != 200:
                return
            self.payloads.append(json.loads(await response.text()))
        except Exception:
            return

    def __call__(self, response: Any) -> None:
        if _is_properties(str(getattr(response, "url", ""))):
            self._tasks.append(asyncio.ensure_future(self._read(response)))

    async def settle(self) -> list[Any]:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=15)
        return list(self.payloads)


def _total(headers: httpx.Headers, status: int) -> int | None:
    match = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
    if match:
        return int(match.group(1))
    if status == 200 and (headers.get("content-length") or "").isdigit():
        return int(headers["content-length"])
    return None


class BrowserLectureSession:
    """HTTP media access through the Camoufox profile that already holds the session."""

    def __init__(self, service: Any = None) -> None:
        self._service = service
        # Why the last capture found nothing (for example a sign-in redirect).
        self.last_capture_note = ""

    @property
    def service(self) -> Any:
        if self._service is None:
            from unisync.services.browser import get_camoufox_service

            self._service = get_camoufox_service()
        return self._service

    async def capture_properties(self, page_url: str, timeout_seconds: float = _CAPTURE_SECONDS) -> list[Any]:
        """Open a player page muted and return the player-properties it loads.

        The page is given time after the first payload so a pre-roll (the
        ~15 s disclaimer) and the lecture both arrive; the caller keeps the
        longest. Bodies are kept in memory only.
        """
        self.last_capture_note = ""
        if not is_allowed_media_url(page_url):
            return []
        recorder = _PropertiesRecorder()
        async with self.service._context(headless=True) as context:
            await mute_context(context)
            context.on("response", recorder)
            page = await context.new_page()
            try:
                try:
                    await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    return await recorder.settle()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout_seconds
                first_seen = 0.0
                nudged = False
                while loop.time() < deadline:
                    await asyncio.sleep(1)
                    if recorder.payloads and not first_seen:
                        first_seen = loop.time()
                    if first_seen and loop.time() - first_seen > 6:
                        break
                    if not nudged and loop.time() > deadline - timeout_seconds + 8:
                        nudged = True
                        for frame in page.frames:
                            try:
                                await frame.evaluate(
                                    "() => document.querySelectorAll('video').forEach(v => { v.muted = true; v.play().catch(() => {}); })"
                                )
                            except Exception:
                                continue
                if not recorder.payloads and "login." in (urlparse(page.url).hostname or ""):
                    self.last_capture_note = "echo360_sign_in_required"
            finally:
                try:
                    context.remove_listener("response", recorder)
                except Exception:
                    pass
                try:
                    await page.close()
                except Exception:
                    pass
        return await recorder.settle()

    async def lesson_files(
        self, record: Mapping[str, Any], media_id: str, lesson_id: str, *, full: bool = True
    ) -> dict[str, Any]:
        """Signed file URL per source for a lesson only reachable through Canvas.

        Echo360 keeps the session for these lessons inside the Canvas Lecture
        Capture frame, so the lesson's ``download-info`` and download redirects
        are requested from that frame. Returns ``{"files": {source: url},
        "labels": {source: label}, "muted": bool | None, "note": str}``.
        URLs stay in memory and are never logged.
        """
        from unisync.services.sync import lecture_video_feeds as feeds

        out: dict[str, Any] = {"files": {}, "labels": {}, "muted": None, "note": ""}
        launch_url = str(record.get("lecture_capture_url") or "")
        if not launch_url or not media_id or not lesson_id:
            out["note"] = "lecture_capture_unknown"
            return out
        async with self.service._context(headless=True) as context:
            if not await self.service._probe(context):
                out["note"] = "canvas_sign_in_required"
                return out
            await mute_context(context)
            page = await context.new_page()
            try:
                try:
                    await page.goto(launch_url, wait_until="load", timeout=60000)
                except Exception:
                    pass
                frame = None
                for _ in range(30):
                    frame = next((f for f in page.frames if (urlparse(f.url).hostname or "") == _ECHO_HOST), None)
                    if frame is not None:
                        break
                    if any((urlparse(f.url).hostname or "").startswith("login.echo360") for f in page.frames):
                        out["note"] = "echo360_sign_in_required"
                        return out
                    await asyncio.sleep(1)
                if frame is None:
                    out["note"] = "lecture_capture_frame_missing"
                    return out
                lesson = quote(lesson_id, safe=":")
                media = quote(media_id, safe="")
                info = await frame.evaluate(_FETCH_JSON, f"/api/ui/echoplayer/lessons/{lesson}/medias/{media}/download-info")
                data = ((info or {}).get("body") or {}).get("data") or {}
                if not data:
                    out["note"] = f"lecture_capture_info_{(info or {}).get('status', 0)}"
                    return out
                want = "full" if full else "lower"
                for group, default_source in (("primaryFiles", 1), ("secondaryFiles", 2)):
                    files = [f for f in ((data.get(group) or {}).get("files") or []) if isinstance(f, Mapping)]
                    files = [f for f in files if str(f.get("fileName") or "").lower().endswith(".mp4")]
                    if not files:
                        continue
                    pick = next((f for f in files if str(f.get("label") or "").lower() == want), files[-1])
                    name = str(pick.get("fileName"))
                    source = feeds.source_of_url("/" + name) or default_source
                    url = await frame.evaluate(
                        _FETCH_FINAL_URL, f"/media/download/{media}/{quote(name)}?lessonId={quote(lesson_id, safe='')}"
                    )
                    if url and is_allowed_media_url(url):
                        out["files"][source] = url
                        out["labels"][source] = str(pick.get("label") or "")
                states: list[bool] = []
                for f in page.frames:
                    try:
                        states.extend(await f.evaluate(_MUTE_STATE))
                    except Exception:
                        continue
                out["muted"] = all(states) if states else None
                if not out["files"]:
                    out["note"] = "lecture_capture_no_files"
                return out
            finally:
                await page.close()

    async def _cookie_header(self, url: str) -> str:
        try:
            async with self.service._context(headless=True) as context:
                cookies = await context.cookies([url])
        except Exception:
            raise DownloadError("browser session is unavailable")
        pairs = []
        for cookie in cookies or []:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name:
                pairs.append(f"{name}={value}")
        return "; ".join(pairs)

    async def auth_headers(self, url: str) -> dict[str, str]:
        if not is_allowed_media_url(url):
            raise DownloadError(public_failure(0, url))
        headers = {"User-Agent": _USER_AGENT, "Referer": "https://echo360.net.au/"}
        cookie = await self._cookie_header(url)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _check_final(self, url: str, final: str, status: int) -> None:
        if not is_allowed_media_url(final):
            raise DownloadError(public_failure(status, url))

    async def get(self, url: str, headers: dict[str, str] | None = None) -> FetchResult:
        if not is_allowed_media_url(url):
            raise DownloadError(public_failure(0, url))
        request_headers = await self.auth_headers(url)
        request_headers.update(headers or {})
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, headers=request_headers)
        except DownloadError:
            raise
        except Exception:
            raise DownloadError(public_failure(0, url))
        self._check_final(url, str(response.url), response.status_code)
        return FetchResult(
            response.status_code,
            response.content,
            {"content-type": response.headers.get("content-type", "")},
        )

    async def probe(self, url: str) -> ProbeResult:
        if not is_allowed_media_url(url):
            return ProbeResult(0, None)
        request_headers = await self.auth_headers(url)
        request_headers["Range"] = "bytes=0-0"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=request_headers) as response:
                    self._check_final(url, str(response.url), response.status_code)
                    return ProbeResult(response.status_code, _total(response.headers, response.status_code))
        except DownloadError:
            raise
        except Exception:
            return ProbeResult(0, None)

    async def stream_to(
        self,
        url: str,
        dest: Path,
        headers: dict[str, str] | None = None,
    ) -> StreamResult:
        if not is_allowed_media_url(url):
            raise DownloadError(public_failure(0, url))
        request_headers = await self.auth_headers(url)
        request_headers.update(headers or {})
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
                async with client.stream("GET", url, headers=request_headers) as response:
                    self._check_final(url, str(response.url), response.status_code)
                    status = response.status_code
                    total = _total(response.headers, status)
                    if status == 206 and dest.is_file():
                        mode = "ab"
                    else:
                        mode = "wb"
                    written = 0
                    with dest.open(mode) as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                                written += len(chunk)
                    return StreamResult(status, written, total)
        except DownloadError:
            raise
        except Exception:
            raise DownloadError(public_failure(0, url))

    async def launch(self, record: Mapping[str, Any]) -> LaunchResult:
        from unisync.core.path_service import get_path_service
        from unisync.services.sync.echo360_native import Echo360NativeIngestor

        from unisync.services.sync.echo360_ingest import canonical_embed_key, embed_lesson_id

        root = get_path_service().get_user_root()
        ingestor = Echo360NativeIngestor(
            service=self.service,
            cache_dir=root / "cache" / "echo360_subtitles",
            thumbnail_dir=root / "cache" / "echo360_thumbnails",
        )
        launch_url = ingestor._launch_url(record)
        if not launch_url:
            return LaunchResult(status="unresolved", reason="missing_launch_url")
        key = canonical_embed_key(
            record.get("canvas_course_id"), record.get("uuid") or record.get("media_id"), record.get("link_id")
        )
        lesson_id = embed_lesson_id(record.get("uuid") or record.get("media_id"), record.get("link_id"))
        recorder = _PropertiesRecorder()
        try:
            async with self.service._context(headless=True) as context:
                await mute_context(context)
                context.on("response", recorder)
                try:
                    outcome = await ingestor._ingest_in_context(context, record, lesson_id, launch_url, key)
                    # The lecture media often loads a few seconds after the pre-roll.
                    await asyncio.sleep(4)
                    signed_in = True
                    if not recorder.payloads:
                        try:
                            signed_in = await self.service._probe(context) is not False
                        except Exception:
                            signed_in = True
                finally:
                    try:
                        context.remove_listener("response", recorder)
                    except Exception:
                        pass
        except Exception as exc:
            logger.info("Canvas launch for lecture video failed (%s)", type(exc).__name__)
            return LaunchResult(status="error", reason="browser_unavailable")
        properties = await recorder.settle()
        url = outcome.media_url if outcome.media_kind in {"m3u8", "mp4", "hls", "m4s"} else ""
        # ``media_url`` starts from the value stored on the embed. Its CloudFront
        # signature expires, so only a URL the player requested on this launch is used.
        if url and url not in (getattr(outcome, "captured", None) or []):
            url = ""
        if url and not is_allowed_media_url(url):
            url = ""
        if not url and not properties and not signed_in:
            return LaunchResult(status="unresolved", reason="canvas_sign_in_required", media_id=outcome.media_id)
        return LaunchResult(
            media_url=url,
            media_kind=outcome.media_kind if url else "",
            media_id=outcome.media_id,
            status=outcome.status,
            reason=outcome.reason,
            properties=properties,
        )
