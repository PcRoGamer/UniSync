"""Download lecture media with the resident Canvas and Echo360 browser session.

Cookies stay in the browser profile and on the request. They are not logged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

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

    @property
    def service(self) -> Any:
        if self._service is None:
            from unisync.services.browser import get_camoufox_service

            self._service = get_camoufox_service()
        return self._service

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

        root = get_path_service().get_user_root()
        ingestor = Echo360NativeIngestor(
            service=self.service,
            cache_dir=root / "cache" / "echo360_subtitles",
            thumbnail_dir=root / "cache" / "echo360_thumbnails",
        )
        try:
            outcome = await ingestor.resolve_playable(record)
        except Exception:
            return LaunchResult(status="error", reason="browser_unavailable")
        url = outcome.media_url if outcome.media_kind in {"m3u8", "mp4", "hls", "m4s"} else ""
        if url and not is_allowed_media_url(url):
            url = ""
        return LaunchResult(
            media_url=url,
            media_kind=outcome.media_kind if url else "",
            media_id=outcome.media_id,
            status=outcome.status,
            reason=outcome.reason,
        )
