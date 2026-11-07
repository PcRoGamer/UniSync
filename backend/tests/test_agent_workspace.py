"""Regression tests for cross-chat persistent workspace.

All tests run with isolated temporary UniSync data, never the student's files.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unisync.mcp import agent_workspace
from unisync.mcp.egress import scrub_mcp_response


@pytest.fixture()
def private_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        agent_workspace, "get_path_service",
        lambda: SimpleNamespace(get_workspace_dir=lambda: tmp_path / "workspace"),
    )
    return tmp_path / "workspace" / "agent_shared"


def test_initialization_and_discovery(private_data: Path):
    rows = agent_workspace.list_files()
    assert rows["total"] >= 1
    assert any(x["path"] == "handoffs/START_HERE.md" for x in rows["entries"])
    assert (private_data / "drafts").is_dir()
    assert (private_data / "handoffs").is_dir()


def test_create_edit_conflict_and_history(private_data: Path):
    initial = agent_workspace.save_file("drafts/BLAW10001/exam.md", "# Version 1\n")
    assert initial["status"] == "created"
    assert agent_workspace.read_file("drafts/BLAW10001/exam.md")["content"] == "# Version 1"
    with pytest.raises(FileExistsError):
        agent_workspace.save_file("drafts/BLAW10001/exam.md", "# Version 2\n")
    updated = agent_workspace.save_file(
        "drafts/BLAW10001/exam.md", "# Version 2\n", expected_sha256=initial["sha256"],
    )
    assert updated["status"] == "updated"
    with pytest.raises(FileExistsError):
        agent_workspace.save_file(
            "drafts/BLAW10001/exam.md", "# Stale revision",
            expected_sha256=initial["sha256"],
        )
    assert agent_workspace.read_file("drafts/BLAW10001/exam.md")["content"] == "# Version 2"
    backups = list((private_data / ".history" / "drafts" / "BLAW10001" / "exam.md").glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# Version 1\n"


@pytest.mark.parametrize("path", (
    "../secret.md",
    "handoffs/../../private.md",
    "C:/ExampleUser/secret.txt",
    "drafts/outside.exe",
    "notes\\private.md",
    "drafts/.hidden.md",
    "drafts/..",
))
def test_path_escapes_denied(private_data: Path, path: str):
    with pytest.raises((ValueError, PermissionError)):
        agent_workspace.read_file(path)
    with pytest.raises((ValueError, PermissionError)):
        agent_workspace.save_file(path, "not allowed")


def test_rejects_symlink_to_outside(private_data: Path, tmp_path: Path):
    agent_workspace.list_files()
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    link = private_data / "drafts" / "escape.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Windows symlinks require extra permissions")
    with pytest.raises(PermissionError):
        agent_workspace.read_file("drafts/escape.md")


def test_token_rejected_at_write(private_data: Path):
    with pytest.raises(ValueError, match="credential"):
        agent_workspace.save_file("notes/key.md", "Bearer " + "z" * 36)


def test_scrubbed_pagination_preserves_all_text(private_data: Path):
    source = ("Keep all text in the draft.\n" * 1000)
    agent_workspace.save_file("drafts/COMP10001/notes.md", source)
    chunks = []
    offset = 0
    while True:
        item = agent_workspace.read_file("drafts/COMP10001/notes.md", offset=offset, max_chars=512)
        filtered = json.loads(scrub_mcp_response(json.dumps(item)))
        chunks.append(filtered["content"])
        if not filtered["has_more"]:
            break
        offset = filtered["next_offset"]
    from unisync.mcp.egress import scrub_text
    assert "".join(chunks) == scrub_text(source)


def test_multiple_categories_and_offsets(private_data: Path):
    for i in range(6):
        agent_workspace.save_file(f"notes/test-{i}.md", f"Note {i}")
    full = agent_workspace.list_files(category="notes")
    paged1 = agent_workspace.list_files(category="notes", limit=2)
    paged2 = agent_workspace.list_files(category="notes", limit=2, offset=paged1["next_offset"])
    assert full["total"] == 6
    assert len(paged1["entries"]) == len(paged2["entries"]) == 2
    assert set(x["path"] for x in paged1["entries"]).isdisjoint(x["path"] for x in paged2["entries"])

def test_academic_overview_surfaces_latest_agent_handoff(private_data: Path):
    agent_workspace.save_file(
        "handoffs/2026-11-04-resume.md",
        "# Session handoff: Resume mathematics review tomorrow.",
    )
    from unisync.mcp.server import unisync_get_overview
    response = json.loads(unisync_get_overview())
    bridge = response["agent_workspace"]
    assert bridge["available"] is True
    assert bridge["latest_handoff_path"] == "handoffs/2026-11-04-resume.md"
    assert "Resume mathematics review" in bridge["latest_handoff_excerpt"]
