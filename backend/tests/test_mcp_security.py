"""Safety and paginated reads of scrubbed UniSync coursework."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unisync.mcp import retrieval
from unisync.mcp.egress import scrub_mcp_response, scrub_text
from unisync.mcp.server import unisync_read_course_file


@pytest.fixture
def academic_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    (root / "courses").mkdir(parents=True)
    (root / "settings").mkdir()
    monkeypatch.setattr(retrieval, "get_path_service",
                        lambda: SimpleNamespace(get_user_root=lambda: root))
    return root


def test_rejects_outside_paths(academic_root: Path, tmp_path: Path):
    setting = academic_root / "settings" / "fake.txt"
    setting.write_text("private data", encoding="utf-8")
    outside = tmp_path / "private.txt"
    outside.write_text("private data", encoding="utf-8")
    for path in (setting, outside, Path(__file__)):
        result = json.loads(unisync_read_course_file(str(path)))
        assert "error" in result


def test_rejects_symlink_escape(academic_root: Path, tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    link = academic_root / "courses" / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Windows symlink creation unavailable")
    assert "error" in json.loads(unisync_read_course_file(str(link)))


def test_rejects_windows_junction_escape(academic_root: Path, tmp_path: Path):
    """A directory junction must not evade the approved-course-root boundary."""
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction regression test")
    outside = tmp_path / "private_dir"
    outside.mkdir()
    (outside / "private_note.txt").write_text("private", encoding="utf-8")
    junction = academic_root / "courses" / "linked_private_dir"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("Junction creation not permitted: " + result.stderr[:100])
    response = json.loads(unisync_read_course_file(
        str(junction / "private_note.txt")
    ))
    assert "error" in response
    assert "private" not in response.get("content", "")


def test_continuation_returns_all_scrubbed_text(academic_root: Path):
    path = academic_root / "courses" / "long.txt"
    source = ("Practical lecture material.\n" * 1500)
    path.write_text(source, encoding="utf-8")
    parts = []
    offset = 0
    while True:
        result = json.loads(unisync_read_course_file(str(path), max_chars=1000, offset=offset))
        assert "error" not in result
        parts.append(result["content"])
        if not result["has_more"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert "".join(parts) == scrub_text(source)
    assert result["file_path"] == "courses/long.txt"


def test_nested_json_scrubbing_preserves_structure():
    raw = json.dumps({"nested": {"content": "Useful academic content."},
                      "list": ["Other notes."], "count": 7})
    result = json.loads(scrub_mcp_response(raw))
    assert result == json.loads(raw)
