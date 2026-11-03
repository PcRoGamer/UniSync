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
    assert data["total_assets"] >= 390
    assert "canvas" in data["by_platform"]


def test_inventory_resolve_endpoint() -> None:
    response = client.get("/api/v1/sync/inventory/resolve", params={"query": "banner-blaw10001-1.png"})
    assert response.status_code == 200
    data = response.json()
    assert data["found"] is True
    assert "canvas.lms.unimelb.edu.au" in data["item"]["direct_link"]
    assert data["item"]["item_id"] == "26804961"


def test_enrolled_courses_endpoint() -> None:
    response = client.get("/api/v1/sync/courses")
    assert response.status_code == 200
    courses = response.json()
    assert len(courses) >= 4
    academic_codes = {c["code"] for c in courses if c["is_academic"]}
    assert "COMP10001" in academic_codes
    assert "BLAW10001" in academic_codes
    assert "INFO10003" in academic_codes
    assert "CCDP10003" in academic_codes
    # Ensure collaborator courses are NOT present
    assert "MECM10006" not in academic_codes
    assert "LING10002" not in academic_codes
    assert "MULT10020" not in academic_codes
    assert "AHIS10003" not in academic_codes


def test_lecture_catalog_endpoint() -> None:
    response = client.get("/api/v1/lectures/catalog")
    assert response.status_code == 200
    data = response.json()
    assert "lectures" in data
    assert data["total"] > 0
    first = data["lectures"][0]
    assert "id" in first
    assert "thumbnail_url" in first


def test_lecture_catalog_ccdp_and_info_weeks() -> None:
    response = client.get("/api/v1/lectures/catalog")
    assert response.status_code == 200
    data = response.json()
    lectures = data["lectures"]

    ccdp_lecs = [l for l in lectures if "CCDP" in (l.get("course_code") or "")]
    info_lecs = [l for l in lectures if "INFO" in (l.get("course_code") or "")]

    # Both courses have 13 scheduled semester weeks
    assert len(ccdp_lecs) == 13, f"Expected 13 CCDP lectures, got {len(ccdp_lecs)}"
    assert len(info_lecs) == 13, f"Expected 13 INFO lectures, got {len(info_lecs)}"

    # Check clean titles with semester weeks
    for l in ccdp_lecs:
        assert "Week " in l["title"], f"CCDP lecture title not formatted with week: {l['title']}"
        assert not l["title"].startswith("CCDP10003_2026_SM2 TH"), f"Raw timetable name leaked: {l['title']}"

    for l in info_lecs:
        assert "Week " in l["title"], f"INFO lecture title not formatted with week: {l['title']}"
        assert not l["title"].startswith("INFO10003_2026_SM2 TH"), f"Raw timetable name leaked: {l['title']}"

    # Verify first 7+ weeks have media recorded as semester progresses
    ccdp_media = [l for l in ccdp_lecs if l.get("has_media")]
    info_media = [l for l in info_lecs if l.get("has_media")]
    assert len(ccdp_media) >= 7, f"Expected at least 7 recorded CCDP lectures, got {len(ccdp_media)}"
    assert len(info_media) >= 7, f"Expected at least 7 recorded INFO lectures, got {len(info_media)}"


def test_lecture_thumbnail_endpoint() -> None:
    response = client.get("/api/v1/lectures/sample-lecture-id/thumbnail")
    assert response.status_code == 200
    assert "image" in response.headers["content-type"]


def test_course_thumbnail_endpoint() -> None:
    """Verify local Canvas banner images are served with image content-type."""
    for code in ["COMP10001", "BLAW10001", "CCDP10003", "INFO10003"]:
        response = client.get(f"/api/v1/sync/courses/{code}/thumbnail")
        assert response.status_code == 200, f"Expected 200 for {code} thumbnail, got {response.status_code}"
        assert "image/" in response.headers["content-type"]
        assert len(response.content) > 1000


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



