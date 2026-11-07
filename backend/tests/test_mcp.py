"""Tests for the UniSync FastMCP Server and AI Agent Tools.

Verifies that all 9 academic tools return valid JSON payloads without
invoking any external LLMs, ensuring deterministic local retrieval for
Hermes, Antigravity, and other MCP clients.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from unisync.mcp.server import (
    mcp,
    unisync_get_deadlines,
    unisync_get_assignment_detail,
    unisync_search_course_materials,
    unisync_read_course_file,
    unisync_search_lectures,
    unisync_get_lecture_transcript,
    unisync_search_announcements,
    unisync_get_sync_status,
    unisync_manage_todo,
)


def test_mcp_instance_configuration() -> None:
    """Verify FastMCP app initialization and dependency declaration."""
    # FastMCP should be named 'unisync' and declare pypdf dependency
    assert mcp.name == "unisync"
    assert "pypdf" in mcp.dependencies


def test_unisync_get_sync_status() -> None:
    """Verify sync status returns valid state and telemetry."""
    res_raw = unisync_get_sync_status()
    res = json.loads(res_raw)
    assert isinstance(res, dict)
    # Must report standard state fields for UI and agents
    assert "state" in res
    assert "state_label" in res
    assert "latest_lecture_date" in res


def test_unisync_get_deadlines_filtering() -> None:
    """Verify deadline retrieval across upcoming, missed, graded, and all filters."""
    # 1. All items
    res_all = json.loads(unisync_get_deadlines(filter="all", limit=50))
    assert res_all["filter"] == "all"
    assert isinstance(res_all["deadlines"], list)
    assert res_all["total_matches"] >= 0

    # 2. Upcoming filter
    res_up = json.loads(unisync_get_deadlines(filter="upcoming", limit=10))
    assert res_up["filter"] == "upcoming"

    # 3. Graded filter
    res_gr = json.loads(unisync_get_deadlines(filter="graded", limit=10))
    assert res_gr["filter"] == "graded"


def test_unisync_get_assignment_detail() -> None:
    """Verify assignment detail resolution by fuzzy title and exact ID."""
    # Query deadlines first to get a real ID if available
    dl = json.loads(unisync_get_deadlines(filter="all", limit=5))
    if dl["deadlines"]:
        target_id = dl["deadlines"][0]["id"]
        res = json.loads(unisync_get_assignment_detail(target_id))
        assert res["id"] == str(target_id)
        assert "title" in res
        assert "instructions" in res
        assert "rubric" in res

    # Non-existent item lookup should return structured error
    err = json.loads(unisync_get_assignment_detail("NonExistentTask999999"))
    assert "error" in err


def test_unisync_search_course_materials_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify material search shape and deterministic local file extraction."""
    res = json.loads(unisync_search_course_materials(query="pdf", limit=5, search_content=False))
    assert "total_found" in res
    assert isinstance(res["files"], list)
    for item in res["files"]:
        assert "filename" in item
        assert "local_filepath" in item

    # Do not depend on the developer's downloaded university files. A tiny
    # plain-text fixture verifies the actual read/cap path on every machine.
    from types import SimpleNamespace
    from unisync.mcp import retrieval
    fixture = tmp_path / "courses" / "course-note.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(retrieval, "get_path_service",
                        lambda: SimpleNamespace(get_user_root=lambda: tmp_path))
    fixture.write_text("Contracts and consideration\n" * 80, encoding="utf-8")
    read_res = json.loads(unisync_read_course_file(str(fixture), max_chars=500))
    assert read_res["file"] == "course-note.txt"
    assert "content" in read_res
    assert len(read_res["content"]) <= 500


def test_unisync_search_lectures_and_transcript() -> None:
    """Verify searching spoken concepts in WebVTT files with timestamp cues."""
    # Search for common spoken word
    search_res = json.loads(unisync_search_lectures(query="the", limit=3))
    assert "total_lectures_matched" in search_res
    assert isinstance(search_res["results"], list)

    if search_res["results"]:
        target_lec = search_res["results"][0]
        assert "lecture_date" in target_lec
        assert "cues" in target_lec
        # Cues should have timestamps
        if target_lec["cues"]:
            assert "timestamp" in target_lec["cues"][0]
            assert "text" in target_lec["cues"][0]

        # Retrieve full transcript by date or file ID
        tr_res = json.loads(unisync_get_lecture_transcript(target_lec["lecture_date"], max_chars=1000))
        assert "file" in tr_res
        assert "transcript" in tr_res
        assert len(tr_res["transcript"]) <= 1000


def test_unisync_search_announcements() -> None:
    """Verify searching email notifications and course announcements."""
    res = json.loads(unisync_search_announcements(query="exam", limit=5))
    assert "total_matched" in res
    assert isinstance(res["announcements"], list)


def test_unisync_manage_todo_lifecycle() -> None:
    """Verify create, complete, and delete lifecycle of agent personal study tasks."""
    title = "PyTest Automated MCP Task"

    # 1. Create
    cr = json.loads(unisync_manage_todo(action="create", title=title, course="Testing 101", notes="Created by pytest"))
    assert cr["status"] == "created"
    todo_id = cr["todo_id"]
    assert todo_id

    # 2. Complete
    co = json.loads(unisync_manage_todo(action="complete", title=title, todo_id=todo_id))
    assert co["status"] == "completed"
    assert co["todo_id"] == todo_id

    # 3. Delete
    dl = json.loads(unisync_manage_todo(action="delete", title=title, todo_id=todo_id))
    assert dl["status"] == "deleted"
    assert dl["success"] is True


def test_mcp_discover_handshake() -> None:
    """Verify discovery fallback and initialization without an unbounded pipe read."""
    import os
    import subprocess
    import sys

    backend_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend_root)

    requests = [
        {
            "jsonrpc": "2.0",
            "id": "discover-1",
            "method": "server/discover",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "init-2",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "Antigravity", "version": "1.0"},
            },
        },
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests)

    completed = subprocess.run(
        [sys.executable, "-m", "unisync.mcp.server"],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(backend_root),
        env=env,
        timeout=10,
        check=False,
    )
    responses = []
    for line in completed.stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("id") is not None:
            responses.append(value)

    by_id = {response["id"]: response for response in responses}
    # Depending on the installed MCP SDK version, validation can reject the
    # extension probe as either unknown-method (-32601) or invalid-params
    # (-32602). The compatibility contract is that the server stays alive and
    # accepts the standard initialize handshake immediately afterwards.
    assert by_id["discover-1"]["error"]["code"] in {-32601, -32602}
    assert "result" in by_id["init-2"]

