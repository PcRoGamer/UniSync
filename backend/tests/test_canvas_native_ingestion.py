"""Focused coverage for Camoufox-native Canvas material persistence."""

import httpx
import pytest

from unisync.api.sync_engine import _upsert_canvas_assignment
from unisync.services.sync.canvas_client import CanvasClient
from unisync.services.sync.canvas_snapshot import CanvasSnapshotWriter, extract_canvas_file_ids
from unisync.services.todos.service import TodoService


@pytest.mark.asyncio
async def test_assignment_collection_hydrates_missing_rubric() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/assignments"):
            return httpx.Response(200, json=[{"id": 660629, "name": "Interactive Oral Assessment"}])
        assert request.url.path.endswith("/assignments/660629")
        return httpx.Response(
            200,
            json={
                "id": 660629,
                "name": "Interactive Oral Assessment",
                "rubric": [{"description": "Reflection", "points": 10, "ratings": []}],
            },
        )

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(base_url="https://canvas.example", transport=httpx.MockTransport(handle))
    try:
        assignments = await client.get_course_assignments("237296")
    finally:
        await client.close()

    assert assignments[0]["rubric"][0]["description"] == "Reflection"
    assert any("assignments/660629" in url for url in requests)


def test_snapshot_writer_retains_rubric_ratings_and_file_links(tmp_path) -> None:
    writer = CanvasSnapshotWriter(tmp_path, "237296")
    writer.write_assignment(
        {
            "id": 660629,
            "name": "Interactive Oral Assessment",
            "description": '<p>See <a href="/courses/237296/files/42/download">brief</a>.</p>',
            "rubric": [
                {
                    "description": "Reflection",
                    "points": 10,
                    "long_description": "Use Kolb's cycle.",
                    "ratings": [{"description": "Excellent", "points": 10, "long_description": "Critical insight."}],
                }
            ],
        }
    )

    output = (tmp_path / "pages" / "canvas-237296-assignments-660629.md").read_text(encoding="utf-8")
    assert "## Rubric" in output
    assert "### Reflection (10 pts)" in output
    assert "| Excellent | 10 | Critical insight. |" in output
    assert extract_canvas_file_ids('<a href="/courses/237296/files/42/download">brief</a>') == {"42"}


def test_partial_assignment_refresh_does_not_erase_a_saved_rubric(tmp_path, monkeypatch) -> None:
    service = TodoService(str(tmp_path / "todos"))
    monkeypatch.setattr("unisync.services.todos.get_todo_service", lambda: service)
    full_assignment = {
        "id": 660629,
        "name": "Interactive Oral Assessment",
        "html_url": "https://canvas.lms.unimelb.edu.au/courses/237296/assignments/660629",
        "rubric": [{"description": "Reflection", "points": 10, "ratings": []}],
    }
    _upsert_canvas_assignment("237296", "INFO10003", full_assignment)
    _upsert_canvas_assignment("237296", "INFO10003", {**full_assignment, "description": "New detail", "rubric": None})

    item = service.list_items()[0]
    assert item.rubric == full_assignment["rubric"]
    assert item.description == "New detail"


@pytest.mark.asyncio
async def test_canvas_pages_pagination_and_homepage_link_discovery() -> None:
    """Pages omitted from modules must still be fetched from homepage links."""
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        path = request.url.path
        if path.endswith("/pages"):
            if request.url.params.get("page") == "2":
                return httpx.Response(
                    200, json=[{"url": "faq", "title": "FAQ", "body": "<p>Help</p>"}]
                )
            return httpx.Response(
                200,
                headers={"Link": '<https://canvas.example/api/v1/courses/241246/pages?page=2>; rel="next"'},
                json=[{"url": "home", "title": "Home"}],
            )
        if path.endswith("/pages/home"):
            return httpx.Response(
                200,
                json={
                    "url": "home",
                    "title": "Home",
                    "body": '<a href="https://canvas.lms.unimelb.edu.au/courses/241246/pages/mst-and-exam">Exam</a>',
                },
            )
        if path.endswith("/pages/mst-and-exam"):
            return httpx.Response(
                200,
                json={
                    "url": "mst-and-exam",
                    "title": "Exam",
                    "body": '<a href="/courses/241246/files/123456/download">2024 exam</a>',
                },
            )
        return httpx.Response(404)

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        pages = await client.get_course_pages("241246")
    finally:
        await client.close()

    assert {p["url"] for p in pages} == {"home", "faq", "mst-and-exam"}
    assert any("page=2" in url for url in requests)
    assert any("pages/mst-and-exam" in url for url in requests)
    assert extract_canvas_file_ids(
        next(p["body"] for p in pages if p["url"] == "mst-and-exam")
    ) == {"123456"}


@pytest.mark.asyncio
async def test_page_catalog_failure_falls_back_to_front_page_and_linked_pages() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/pages"):
            return httpx.Response(404, json={"errors": ["page catalog unavailable"]})
        if request.url.path.endswith("/front_page"):
            return httpx.Response(
                200,
                json={
                    "url": "home",
                    "title": "Home",
                    "body": '<a href="/courses/241246/pages/mst-and-exam">Exam</a>',
                },
            )
        if request.url.path.endswith("/pages/mst-and-exam"):
            return httpx.Response(
                200,
                json={
                    "url": "mst-and-exam",
                    "title": "MST and Exam",
                    "body": '<a href="/courses/241246/files/27887413/download">2023 exam</a>',
                },
            )
        return httpx.Response(404)

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        pages = await client.get_course_pages("241246")
        report = client.last_page_crawl
    finally:
        await client.close()

    assert {page["url"] for page in pages} == {"home", "mst-and-exam"}
    assert "/api/v1/courses/241246/front_page" in requests
    assert report["catalog_pages"] == 0
    assert report["status"] == "incomplete"
    assert report["error"] == "HTTPStatusError"
    assert report["catalog_error_details"]["status_code"] == 404
    assert report["front_page_fallback"] is True


@pytest.mark.asyncio
async def test_file_catalog_403_records_safe_http_facts_without_query() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "content-type": "application/json",
                "server": "Canvas",
                "location": "/login?state=private-query",
            },
            json={"errors": ["forbidden"]},
        )

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        assert await client.get_course_files("241246") == []
        details = client.last_file_catalog_error_details
    finally:
        await client.close()

    assert details["status_code"] == 403
    assert details["host"] == "canvas.example"
    assert details["path"] == "/api/v1/courses/241246/files"
    assert details["redirect"] == {"host": "canvas.example", "path": "/login"}
    assert "private-query" not in str(details)


def test_incomplete_canvas_crawl_blocks_global_up_to_date(tmp_path, monkeypatch) -> None:
    import json
    import time
    from types import SimpleNamespace

    from unisync.api.sync_engine import get_computed_sync_status, sync_metadata, sync_status

    courses = tmp_path / "courses" / "COMP10001_2026_SM2" / "pages"
    courses.mkdir(parents=True)
    (courses / "canvas-241246-crawl-report.json").write_text(
        json.dumps({"course_id": "241246", "status": "incomplete"}),
        encoding="utf-8",
    )
    cache = tmp_path / "cache" / "echo360_subtitles"
    cache.mkdir(parents=True)
    (tmp_path / "cache" / "echo360_syllabus.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        "unisync.core.path_service.get_path_service",
        lambda: SimpleNamespace(get_user_root=lambda: tmp_path),
    )
    monkeypatch.setattr(
        "unisync.core.download_registry.DownloadRegistry.get_instance",
        lambda: SimpleNamespace(_items={}),
    )
    monkeypatch.setattr(
        "unisync.services.browser.get_camoufox_service",
        lambda: SimpleNamespace(get_status=lambda: {
            "installed": True, "has_persistent_profile": True, "cookies_in_memory": 1,
        }),
    )
    monkeypatch.setitem(sync_status, "is_syncing", False)
    monkeypatch.setitem(sync_status, "progress", {})
    monkeypatch.setitem(sync_metadata, "last_bridge_ping", time.time() - 1)
    monkeypatch.setitem(sync_metadata, "auth_status", "authenticated")
    monkeypatch.setitem(sync_metadata, "canvas_auth_status", "authenticated")
    monkeypatch.setitem(sync_metadata, "echo360_auth_status", "authenticated")

    result = get_computed_sync_status()
    assert result["state"] == "sync_incomplete"
    assert result["incomplete_canvas_crawls"][0]["course_id"] == "241246"


def test_canvas_rce_preview_file_and_linked_page_extraction() -> None:
    from unisync.services.sync.canvas_snapshot import extract_canvas_page_slugs

    assert extract_canvas_file_ids(
        '<a href="/courses/241246/files?preview=28255019">File</a>'
    ) == {"28255019"}
    assert extract_canvas_page_slugs(
        "241246",
        '<a href="/courses/241246/pages/mst-and-exam">Exam</a>'
        '<a href="https://evil.example/courses/241246/pages/ignore">Untrusted</a>'
        '<a href="/courses/999/pages/wrong-course">Other course</a>',
    ) == {"mst-and-exam"}

@pytest.mark.asyncio
async def test_canvas_breadth_first_no_depth_limit_and_cycle_dedup() -> None:
    """Do not discard levels beyond four and do not revisit cycles."""
    visits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pages"):
            return httpx.Response(200, json=[{"url": "level0"}])
        slug = path.rsplit("/", 1)[-1]
        visits.append(slug)
        n = int(slug.replace("level", ""))
        dest = f"level{n+1}" if n < 7 else "level0"
        return httpx.Response(
            200, json={
                "url": slug, "title": slug,
                "body": f'<a href="/courses/241246/pages/{dest}">Next</a>',
            }
        )

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        pages = await client.get_course_pages("241246")
    finally:
        await client.close()
    assert len(pages) == 8
    assert sorted(visits) == [f"level{i}" for i in range(8)]
    assert client.last_page_crawl["pages_by_depth"]["7"] == 1
    assert client.last_page_crawl["status"] == "complete"


@pytest.mark.asyncio
async def test_canvas_page_cache_avoids_unchanged_body_requests(tmp_path) -> None:
    """Updated-at keyed cache preserves linked file discovery across syncs."""
    visits: list[str] = []
    cache = tmp_path / "cached_pages.json"

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        visits.append(path)
        if path.endswith("/pages"):
            return httpx.Response(
                200, json=[
                    {"url": "home", "title": "Home", "updated_at": "2026-11-01T01:00:00Z"}
                ]
            )
        if path.endswith("/pages/home"):
            return httpx.Response(
                200, json={
                    "url": "home", "title": "Home", "updated_at": "2026-11-01T01:00:00Z",
                    "body": '<a href="/courses/241246/files/123/download">Paper</a>',
                }
            )
        return httpx.Response(404)

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        a = await client.get_course_pages("241246", cache_path=cache)
        b = await client.get_course_pages("241246", cache_path=cache)
    finally:
        await client.close()
    assert sum(p.endswith("/pages/home") for p in visits) == 1
    assert client.last_page_crawl["cached_bodies_reused"] == 1
    assert extract_canvas_file_ids(b[0]["body"]) == {"123"}
    assert a == b


def test_canvas_manifest_entry_is_not_enough_if_file_missing(tmp_path) -> None:
    from unisync.api.sync_engine import _canvas_manifest_file_present

    record = {"kb_path": "pages/exam.pdf", "size": 4}
    assert not _canvas_manifest_file_present(record, tmp_path)
    target = tmp_path / "pages" / "exam.pdf"
    target.parent.mkdir()
    target.write_bytes(b"abcd")
    assert _canvas_manifest_file_present(record, tmp_path)
    target.write_bytes(b"ab")
    assert not _canvas_manifest_file_present(record, tmp_path)

@pytest.mark.asyncio
async def test_canvas_all_file_catalog_pages_and_additional_sources() -> None:
    from unisync.api.sync_engine import _canvas_attachment_candidates

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200, json=[{"id": 202, "filename": "year-2024.pdf"}]
            )
        return httpx.Response(
            200,
            headers={"Link": '<https://canvas.example/api/v1/courses/241246/files?page=2>; rel="next"'},
            json=[{"id": 201, "filename": "year-2023.pdf"}],
        )

    client = CanvasClient("https://canvas.example", {})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://canvas.example", transport=httpx.MockTransport(handle)
    )
    try:
        files = await client.get_course_files("241246")
    finally:
        await client.close()

    candidates = _canvas_attachment_candidates(
        [], [], [],
        files=files,
        announcements=[{
            "title": "Exam notes",
            "message": '<a href="/courses/241246/files/203/download">Exam link</a>',
        }],
        discussions=[{
            "title": "Exam discussion",
            "message": '<a href="/courses/241246/files/204/download">Discussion file</a>',
        }],
        quizzes=[{
            "title": "Practice",
            "description": '<a href="/courses/241246/files/205/download">Quiz file</a>',
        }],
    )
    assert set(candidates) == {"201", "202", "203", "204", "205"}
    assert candidates["202"]["title"] == "year-2024.pdf"
    assert candidates["203"]["folder"] == "announcements/Exam notes"
