import httpx
import logging
import asyncio
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

# P1-9: parallelize module fetch — semaphore caps concurrent Canvas API hits
_MODULE_FETCH_SEM = asyncio.Semaphore(5)
_ASSIGNMENT_FETCH_SEM = asyncio.Semaphore(5)
_PAGE_FETCH_SEM = asyncio.Semaphore(5)

# P2-13: ETag — avoids re-downloading unchanged Canvas modules/pages
_etag_cache: dict[str, str] = {}
_etag_data: dict[str, Any] = {}


class CanvasClient:
    def __init__(self, base_url: str, cookies: dict[str, str]):
        self.base_url = base_url.rstrip("/")
        transport = None
        if urlparse(self.base_url).hostname == "canvas.lms.unimelb.edu.au":
            from unisync.services.browser.canvas_transport import CanvasSessionTransport
            transport = CanvasSessionTransport()
            cookies = {}  # The browser owns cookie scope, expiry and rotation.
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            transport=transport,
            timeout=httpx.Timeout(30.0, read=600.0)
        )

    async def close(self):
        await self.client.aclose()

    async def get_courses(self) -> list[dict[str, Any]]:
        """Fetch all active courses."""
        # P2-13: ETag — avoids re-downloading unchanged Canvas modules/pages
        url = "/api/v1/courses?enrollment_type=student&enrollment_state=active&per_page=100"
        courses = []
        try:
            headers: dict[str, str] = {}
            if url in _etag_cache:
                headers["If-None-Match"] = _etag_cache[url]
            res = await self.client.get(url, headers=headers)
            if res.status_code == 304:
                # P2-13: Not Modified — return cached data without re-downloading
                return _etag_data.get(url, courses)
            res.raise_for_status()
            data = res.json()
            # P2-13: Store ETag and response data for future conditional requests
            etag = res.headers.get("etag")
            if etag:
                _etag_cache[url] = etag
                _etag_data[url] = data
            for c in data:
                if c.get("id"):
                    courses.append(c)
        except Exception as e:
            logger.error(f"Failed to fetch Canvas courses: {e}")
        return courses

    async def get_course_modules(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch all modules and items for a single course."""
        # P2-13: ETag — avoids re-downloading unchanged Canvas modules/pages
        url = f"/api/v1/courses/{course_id}/modules?include[]=items&per_page=100"
        try:
            headers: dict[str, str] = {}
            if url in _etag_cache:
                headers["If-None-Match"] = _etag_cache[url]
            res = await self.client.get(url, headers=headers)
            if res.status_code == 304:
                # P2-13: Not Modified — return cached data without re-downloading
                return _etag_data.get(url, [])
            res.raise_for_status()
            data = res.json()
            # P2-13: Store ETag and response data for future conditional requests
            etag = res.headers.get("etag")
            if etag:
                _etag_cache[url] = etag
                _etag_data[url] = data
            return data
        except Exception as e:
            logger.error(f"Failed to fetch Canvas modules for {course_id}: {e}")
            return []

    # P1-9: parallelize module fetch — gather all courses under a semaphore
    async def get_all_modules(self, course_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch modules for every *course_id* concurrently (max 5 in-flight).

        Each individual failure is swallowed so one bad course does not
        abort the rest — the failing course maps to an empty list.
        """
        async def _guarded(cid: str) -> tuple[str, list[dict[str, Any]]]:
            async with _MODULE_FETCH_SEM:
                try:
                    mods = await self.get_course_modules(cid)
                    return (cid, mods)
                except Exception as exc:
                    logger.error(f"P1-9: parallel module fetch failed for {cid}: {exc}")
                    return (cid, [])

        results = await asyncio.gather(*[_guarded(cid) for cid in course_ids])
        return dict(results)

    async def get_course_pages(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch every course wiki page and hydrate its body when necessary."""
        url = f"/api/v1/courses/{course_id}/pages?include[]=body&per_page=100"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            pages = data if isinstance(data, list) else []

            # The Canvas collection endpoint often returns page metadata only,
            # despite ``include[]=body``. Fetch those pages individually so the
            # local snapshot remains useful for search and course materials.
            async def fetch_body(page: dict[str, Any]) -> dict[str, Any]:
                if isinstance(page.get("body"), str) or not page.get("url"):
                    return page
                async with _PAGE_FETCH_SEM:
                    detail = await self.get_course_page(course_id, str(page["url"]))
                return detail or page

            return list(await asyncio.gather(*(fetch_body(page) for page in pages)))
        except Exception as e:
            logger.error(f"Failed to fetch Canvas pages for {course_id}: {e}")
            return []

    async def get_course_page(self, course_id: str, page_url: str) -> dict[str, Any] | None:
        """Fetch one Canvas wiki page, including its HTML body and updated date."""
        url = f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug("Failed to fetch Canvas page %s for %s (%s)", page_url, course_id, type(e).__name__)
            return None

    async def get_course_tabs(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch course navigation tabs to discover institution-specific tools."""
        try:
            res = await self.client.get(f"/api/v1/courses/{course_id}/tabs")
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Failed to fetch Canvas tabs for %s (%s)", course_id, type(e).__name__)
            return []

    async def get_course_files(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch direct files list for a course."""
        url = f"/api/v1/courses/{course_id}/files?per_page=100"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"Failed to fetch Canvas files list for {course_id}: {e}")
            return []

    async def get_course_discussions(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch Canvas discussion topics and their introductory messages."""
        try:
            res = await self.client.get(
                f"/api/v1/courses/{course_id}/discussion_topics?include[]=all_dates&per_page=100"
            )
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Failed to fetch Canvas discussions for %s (%s)", course_id, type(e).__name__)
            return []

    async def get_course_announcements(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch announcements, which Canvas exposes through a global endpoint."""
        try:
            res = await self.client.get(
                f"/api/v1/announcements?context_codes[]=course_{course_id}&per_page=100"
            )
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Failed to fetch Canvas announcements for %s (%s)", course_id, type(e).__name__)
            return []

    async def get_course_quizzes(self, course_id: str) -> list[dict[str, Any]]:
        """Fetch classic Canvas quiz metadata and instructions."""
        try:
            res = await self.client.get(f"/api/v1/courses/{course_id}/quizzes?per_page=100")
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Failed to fetch Canvas quizzes for %s (%s)", course_id, type(e).__name__)
            return []

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        """Fetch file metadata, including its authenticated download URL."""
        try:
            res = await self.client.get(f"/api/v1/files/{file_id}")
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug("Failed to fetch Canvas file %s (%s)", file_id, type(e).__name__)
            return None

    async def download_file(
        self,
        download_url: str,
        dest_path: Path,
        *,
        item_id: str = "",
        course_id: str = "",
        course_name: str = "",
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Stream an authenticated Canvas file to disk and register its provenance."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Canvas often responds with a short-lived redirect to its file CDN.
            # ``CanvasSessionTransport`` refreshes browser cookies before every
            # hop and deliberately does not forward them to that CDN.
            async with self.client.stream("GET", download_url, follow_redirects=True) as response:
                response.raise_for_status()
                with dest_path.open("wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
            logger.info(f"Downloaded Canvas file to {dest_path}")
            try:
                from unisync.core.download_registry import register_download
                register_download(
                    platform="canvas",
                    item_id=item_id or dest_path.stem,
                    title=dest_path.name,
                    direct_link=download_url,
                    local_filepath=dest_path,
                    course_id=course_id,
                    course_name=course_name,
                    size_bytes=dest_path.stat().st_size if dest_path.exists() else 0,
                    content_type=content_type,
                    metadata=metadata,
                )
            except Exception as reg_err:
                logger.debug("Download registry hook error: %s", reg_err)
            return True
        except Exception as e:
            logger.error("Canvas file download failed (%s)", type(e).__name__)
            return False

    async def get_assignment(
        self,
        course_id: str,
        assignment_id: str,
        include_submission: bool = True,
    ) -> dict[str, Any] | None:
        """Fetch one assignment with its full rubric and rating tiers."""
        includes = ["include[]=rubric", "include[]=score_statistics"]
        if include_submission:
            includes.append("include[]=submission")
        url = f"/api/v1/courses/{course_id}/assignments/{assignment_id}?{'&'.join(includes)}"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.error("Failed to fetch Canvas assignment %s for %s: %s", assignment_id, course_id, type(e).__name__)
            return None

    async def get_course_assignments(self, course_id: str, include_submission: bool = True) -> list[dict[str, Any]]:
        """Fetch assignments and hydrate rubric data Canvas omits from list responses."""
        includes = ["include[]=rubric", "include[]=score_statistics"]
        if include_submission:
            includes.append("include[]=submission")
        url = f"/api/v1/courses/{course_id}/assignments?per_page=100&{'&'.join(includes)}"
        try:
            res = await self.client.get(url)
            res.raise_for_status()
            data = res.json()
            assignments = data if isinstance(data, list) else []

            # Canvas can omit rubric criteria from the collection endpoint even
            # when the assignment page displays a complete ratings table. Read
            # individual records only where the list has no rubric, retaining
            # the fast one-request path for assignments already fully present.
            missing_rubrics = [
                assignment
                for assignment in assignments
                if assignment.get("id")
                and not isinstance(assignment.get("rubric"), list)
            ]
            if missing_rubrics:
                async def fetch_detail(assignment: dict[str, Any]) -> dict[str, Any] | None:
                    async with _ASSIGNMENT_FETCH_SEM:
                        return await self.get_assignment(
                            course_id,
                            str(assignment["id"]),
                            include_submission=include_submission,
                        )

                details = await asyncio.gather(*(fetch_detail(a) for a in missing_rubrics))
                for assignment, detail in zip(missing_rubrics, details):
                    if isinstance(detail, dict):
                        assignment.update(detail)
            return assignments
        except Exception as e:
            logger.error(f"Failed to fetch Canvas assignments for {course_id}: {e}")
            return []

    async def get_all_course_assignments(
        self,
        course_ids: list[str],
        include_submission: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch assignment details for all courses with bounded concurrency."""
        semaphore = asyncio.Semaphore(5)

        async def fetch(course_id: str) -> tuple[str, list[dict[str, Any]]]:
            async with semaphore:
                return course_id, await self.get_course_assignments(
                    course_id,
                    include_submission=include_submission,
                )

        return dict(await asyncio.gather(*(fetch(course_id) for course_id in course_ids)))

