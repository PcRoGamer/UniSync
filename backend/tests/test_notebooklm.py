"""Tests for Google NotebookLM integration subsystem."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from unisync.api.main import app
from unisync.services.notebooklm.gatherer import CourseFileGatherer, CourseFile, compute_sha256, sanitize_title
from unisync.services.notebooklm.service import NotebookLMService, NotebookLMAuthError
from unisync.services.notebooklm import state as state_mod


def test_sanitize_title():
    """Verify title sanitization removes invalid characters and extra whitespace."""
    assert sanitize_title("Lecture 1: Intro / Basics") == "Lecture 1 - Intro - Basics"
    assert sanitize_title("   Topic   *special*?   ") == "Topic - special"
    assert sanitize_title("") == "Untitled Document"


def test_state_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify state persistence and source deduplication tracking."""
    test_state_file = tmp_path / "notebooklm_sync.json"
    monkeypatch.setattr(state_mod, "get_state_file_path", lambda: test_state_file)

    # Initial state
    st = state_mod.load_sync_state()
    assert st["notebooks"] == {}

    # Update course notebook
    updated = state_mod.update_course_notebook(
        course_code="BLAW10001",
        notebook_id="nb-12345",
        notebook_title="BLAW10001 — Principles of Business Law",
        course_name="Principles of Business Law",
    )
    assert updated["notebook_id"] == "nb-12345"
    assert "https://notebooklm.google.com/notebook/nb-12345" in updated["notebook_url"]

    # Record uploaded source
    state_mod.record_uploaded_source(
        course_code="BLAW10001",
        file_rel_path="downloads/slides.pdf",
        source_id="src-999",
        file_hash="hash-abc",
        title="Slides Week 1",
        size_bytes=1024,
    )

    # Check is_source_synced
    assert state_mod.is_source_synced("BLAW10001", "downloads/slides.pdf", "hash-abc") is True
    assert state_mod.is_source_synced("BLAW10001", "downloads/slides.pdf", "different-hash") is False
    assert state_mod.is_source_synced("BLAW10001", "downloads/other.pdf", "hash-abc") is False

    # Reload state from disk to confirm persistence
    reloaded = state_mod.load_sync_state()
    assert reloaded["notebooks"]["BLAW10001"]["source_count"] == 1
    assert "downloads/slides.pdf" in reloaded["notebooks"]["BLAW10001"]["sources"]


def test_course_file_gatherer_vtt_conversion(tmp_path: Path):
    """Verify VTT files are converted into clean markdown dialogue paragraphs."""
    gatherer = CourseFileGatherer(project_root=tmp_path)
    vtt_file = tmp_path / "lecture.vtt"
    vtt_content = (
        "WEBVTT\n\n"
        "1\n00:00:01.000 --> 00:00:03.000\nHello everyone.\n\n"
        "2\n00:00:03.500 --> 00:00:06.000\nWelcome to principles of business law.\n\n"
        "3\n00:00:06.500 --> 00:00:09.000\nToday we cover contract formation.\n"
    )
    vtt_file.write_text(vtt_content, encoding="utf-8")

    md = gatherer._vtt_to_markdown(vtt_file, "Contract Formation", "BLAW10001")
    assert "# Lecture Transcript: Contract Formation" in md
    assert "**Course**: BLAW10001" in md
    assert "Hello everyone. Welcome to principles of business law. Today we cover contract formation." in md
    assert "00:00:01.000" not in md  # Timestamp cues stripped
    assert "WEBVTT" not in md


def test_course_file_gatherer_forum_digests(tmp_path: Path):
    """Verify high-volume forum threads are bundled into batch digest files."""
    disc_dir = tmp_path / "data" / "courses" / "COMP10001_2026_SM2" / "edstem" / "discussions"
    disc_dir.mkdir(parents=True, exist_ok=True)

    # Create 5 mock thread markdown files
    for i in range(5):
        thread = disc_dir / f"36892-discussions-{i:04d}-topic-{i}.md"
        thread.write_text(f"Question body for topic {i}\nAnswer for topic {i}", encoding="utf-8")

    gatherer = CourseFileGatherer(project_root=tmp_path)
    course_info = {"course_code": "COMP10001", "kb_name": "COMP10001_2026_SM2"}

    digests = gatherer._generate_edstem_forum_digests(course_info, threads_per_digest=2)
    # 5 threads with 2 per digest = 3 digests
    assert len(digests) == 3
    assert digests[0].kind == "forum_digest"
    assert digests[0].path.exists()
    content = digests[0].path.read_text(encoding="utf-8")
    assert "EdStem Forum Discussions Digest — Part 01" in content
    assert "Question body for topic 0" in content


def test_notebooklm_service_auth_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify service accurately reports unauthenticated vs authenticated status."""
    service = NotebookLMService(project_root=tmp_path)
    storage_file = tmp_path / "storage_state.json"
    monkeypatch.setattr(NotebookLMService, "get_storage_file_path", staticmethod(lambda: storage_file))

    # Missing storage file
    assert service.is_authenticated() is False
    status = service.get_auth_status()
    assert status["authenticated"] is False

    # Valid storage file with SID cookie
    storage_file.write_text(
        json.dumps({"cookies": [{"name": "SID", "value": "test-sid-token"}]}),
        encoding="utf-8",
    )
    assert service.is_authenticated() is True
    status = service.get_auth_status()
    assert status["authenticated"] is True


@pytest.mark.asyncio
async def test_notebooklm_mock_provision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verify get_or_create_subject_notebooks correctly provisions and links notebooks."""
    test_state_file = tmp_path / "notebooklm_sync.json"
    monkeypatch.setattr(state_mod, "get_state_file_path", lambda: test_state_file)

    service = NotebookLMService(project_root=tmp_path)
    monkeypatch.setattr(service, "is_authenticated", lambda: True)

    # Mock course list
    monkeypatch.setattr(
        service.gatherer,
        "get_enrolled_academic_courses",
        lambda: {
            "BLAW10001": {"course_code": "BLAW10001", "course_name": "Principles of Business Law"},
            "COMP10001": {"course_code": "COMP10001", "course_name": "Foundations of Computing"},
        },
    )

    # Mock client
    mock_client = MagicMock()
    mock_client.get_account_email.return_value = "user@example.com"

    mock_existing_nb = MagicMock()
    mock_existing_nb.id = "nb-existing-blaw"
    mock_existing_nb.title = "BLAW10001 — Principles of Business Law"

    mock_created_nb = MagicMock()
    mock_created_nb.id = "nb-new-comp"
    mock_created_nb.title = "COMP10001 — Foundations of Computing"

    mock_client.notebooks.list = AsyncMock(return_value=[mock_existing_nb])
    mock_client.notebooks.create = AsyncMock(return_value=mock_created_nb)

    result = await service.get_or_create_subject_notebooks(client=mock_client)

    assert "BLAW10001" in result
    assert result["BLAW10001"]["notebook_id"] == "nb-existing-blaw"
    assert "COMP10001" in result
    assert result["COMP10001"]["notebook_id"] == "nb-new-comp"
    mock_client.notebooks.create.assert_awaited_once_with(title="COMP10001 — Foundations of Computing")


def test_notebooklm_api_router():
    """Verify FastAPI NotebookLM endpoints response schemas and error codes."""
    client = TestClient(app)

    # GET /api/v1/notebooklm/status
    res = client.get("/api/v1/notebooklm/status")
    assert res.status_code == 200
    data = res.json()
    assert "auth" in data
    assert "courses" in data
    assert "sync_job" in data

    # GET /api/v1/notebooklm/courses
    res_courses = client.get("/api/v1/notebooklm/courses")
    assert res_courses.status_code == 200
    c_data = res_courses.json()
    assert "courses" in c_data

    # POST /api/v1/notebooklm/provision when unauthenticated -> 401
    with patch.object(NotebookLMService, "is_authenticated", return_value=False):
        res_prov = client.post("/api/v1/notebooklm/provision")
        assert res_prov.status_code == 401

    # POST /api/v1/notebooklm/sync when unauthenticated -> 401
    with patch.object(NotebookLMService, "is_authenticated", return_value=False):
        res_sync = client.post("/api/v1/notebooklm/sync", json={"course_code": "BLAW10001"})
        assert res_sync.status_code == 401
