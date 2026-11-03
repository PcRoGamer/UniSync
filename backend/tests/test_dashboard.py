import re

from fastapi.testclient import TestClient
from unisync.api.main import app

client = TestClient(app)


def test_dashboard_html_serves() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "UniSync" in response.text
    assert "Downloads & Provenance Inventory" in response.text


def test_userscript_endpoint() -> None:
    response = client.get("/userscript/echo360-connector.user.js")
    assert response.status_code == 200
    assert "application/javascript" in response.headers["content-type"]
    assert "echo360" in response.text.lower()


def test_userscript_version_is_consistent_across_bridge_and_dashboard() -> None:
    script = client.get("/userscript/echo360-connector.user.js")
    match = re.search(r"@version\s+(\S+)", script.text)
    assert match is not None
    version = match.group(1)
    assert f'version: "{version}"' in script.text
    assert f"Download Userscript v{version}" in client.get("/").text


def test_sync_status_latest_userscript_version() -> None:
    """Verify that sync status endpoint reports latest userscript version for UI lifecycle."""
    response = client.get("/api/v1/sync/status")
    assert response.status_code == 200
    data = response.json()
    assert "latest_userscript_version" in data
    assert data["latest_userscript_version"] == "4.0.3"



def test_backend_runtime_is_separate_and_not_cached() -> None:
    runtime = client.get("/userscript/bridge-runtime.js")
    loader = client.get("/userscript/echo360-connector.user.js")
    assert runtime.status_code == 200
    assert runtime.headers["cache-control"] == "no-store"
    assert runtime.text.startswith("// UniSync backend runtime")
    assert "async function scrapeEchoPage" in runtime.text
    assert "async function scrapeEchoPage" not in loader.text
    assert "/userscript/bridge-runtime.js" in loader.text


def test_inventory_stats_endpoint() -> None:
    response = client.get("/api/v1/sync/inventory/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["total_assets"] >= 0
    assert data["total_courses"] >= 0
    assert data["total_storage_bytes"] >= 0
    assert isinstance(data["by_platform"], dict)


def test_inventory_resolve_endpoint() -> None:
    # Positive provenance resolution is covered with deterministic fixtures in
    # test_download_registry.py. The dashboard route must also have a stable
    # empty-state contract on a fresh install with no personal LMS data.
    response = client.get("/api/v1/sync/inventory/resolve", params={"query": "definitely-not-synced.fixture"})
    assert response.status_code == 200
    data = response.json()
    assert data == {"found": False, "query": "definitely-not-synced.fixture", "item": None}


def test_enrolled_courses_endpoint() -> None:
    response = client.get("/api/v1/sync/courses")
    assert response.status_code == 200
    courses = response.json()
    assert isinstance(courses, list)
    codes = []
    for course in courses:
        assert "code" in course
        assert "name" in course
        assert "is_academic" in course
        codes.append(course["code"])
    assert len(codes) == len(set(codes))


def test_lecture_catalog_endpoint() -> None:
    response = client.get("/api/v1/lectures/catalog")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data.get("lectures"), list)
    assert data["total"] == len(data["lectures"])
    if data["lectures"]:
        first = data["lectures"][0]
        assert "id" in first
        assert "thumbnail_url" in first


def test_lecture_catalog_normalizes_known_course_titles_when_present() -> None:
    response = client.get("/api/v1/lectures/catalog")
    assert response.status_code == 200
    lectures = response.json()["lectures"]

    # Fresh installs legitimately have no synced lectures. If these courses are
    # present, their user-facing titles must never expose raw timetable names.
    for lecture in lectures:
        code = lecture.get("course_code") or ""
        title = lecture.get("title") or ""
        if "CCDP" in code:
            assert not title.startswith("CCDP10003_2026_SM2 TH")
        if "INFO" in code:
            assert not title.startswith("INFO10003_2026_SM2 TH")


def test_lecture_thumbnail_endpoint() -> None:
    response = client.get("/api/v1/lectures/sample-lecture-id/thumbnail")
    assert response.status_code == 200
    assert "image" in response.headers["content-type"]


def test_course_thumbnail_endpoint() -> None:
    """Verify thumbnail behavior without requiring the developer's Canvas cache."""
    courses_response = client.get("/api/v1/sync/courses")
    assert courses_response.status_code == 200
    for course in courses_response.json()[:4]:
        code = course.get("code")
        if not code:
            continue
        response = client.get(f"/api/v1/sync/courses/{code}/thumbnail")
        assert response.status_code in {200, 404}
        if response.status_code == 200:
            assert "image/" in response.headers["content-type"]
            assert response.content

    missing = client.get("/api/v1/sync/courses/NOT_A_REAL_COURSE/thumbnail")
    assert missing.status_code == 404


def test_sync_probes_endpoint() -> None:
    """Verify the 8 subsystem diagnostic health probes endpoint."""
    response = client.get("/api/v1/sync/probes")
    assert response.status_code == 200
    data = response.json()
    expected_probes = ["backend", "canvas", "echo360", "camoufox", "bridge", "edstem", "websocket", "storage"]
    for probe in expected_probes:
        assert probe in data, f"Missing expected probe: {probe}"
        assert "status" in data[probe]
        assert "label" in data[probe]
        assert "latency_ms" in data[probe]
        assert "details" in data[probe]



