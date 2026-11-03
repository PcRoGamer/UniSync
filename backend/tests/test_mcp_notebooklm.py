"""Unit tests for UniSync FastMCP Google NotebookLM tools.

Verifies that NotebookLM MCP tools correctly expose grounded subject Q&A,
notebook listing, artifact generation, and note creation for AI agents.
"""

from __future__ import annotations

import json
from unittest.mock import patch
import pytest

from unisync.mcp.server import (
    unisync_notebooklm_list_notebooks,
    unisync_notebooklm_query,
    unisync_notebooklm_generate_artifact,
    unisync_notebooklm_add_note,
    _resolve_notebooklm_course,
)


def test_unisync_notebooklm_list_notebooks() -> None:
    """Verify listing notebooks returns a self-consistent schema on fresh or synced installs."""
    raw = unisync_notebooklm_list_notebooks()
    data = json.loads(raw)
    assert isinstance(data, dict)
    assert "auth" in data
    assert "courses" in data
    assert isinstance(data["courses"], list)
    assert data["total_courses"] == len(data["courses"])
    for course in data["courses"]:
        assert "course_code" in course


def test_resolve_notebooklm_course() -> None:
    """Verify course resolution independently of the developer's synced subjects."""
    state = {
        "notebooks": {
            "BLAW10001": {
                "course_code": "BLAW10001",
                "notebook_id": "fixture-notebook",
            }
        }
    }
    with patch("unisync.services.notebooklm.state.load_sync_state", return_value=state):
        blaw = _resolve_notebooklm_course("blaw10001")
        assert blaw["course_code"] == "BLAW10001"
        assert blaw["notebook_id"] == "fixture-notebook"
        with pytest.raises(ValueError, match="No provisioned NotebookLM notebook found"):
            _resolve_notebooklm_course("INVALID99999")


@pytest.mark.asyncio
async def test_unisync_notebooklm_query_unauthenticated() -> None:
    """Verify query returns clear error when NotebookLM is unauthenticated."""
    with patch("unisync.services.notebooklm.service.NotebookLMService.is_authenticated", return_value=False):
        raw = await unisync_notebooklm_query(query="Test question", course="BLAW10001")
        data = json.loads(raw)
        assert "error" in data
        assert "not authenticated" in data["error"]


@pytest.mark.asyncio
async def test_unisync_notebooklm_query_unknown_course() -> None:
    """Verify query returns error for unprovisioned course."""
    raw = await unisync_notebooklm_query(query="Test question", course="UNKNOWN_COURSE")
    data = json.loads(raw)
    assert "error" in data
    assert "NotebookLM query failed" in data["error"]


@pytest.mark.asyncio
async def test_unisync_notebooklm_generate_unsupported_type() -> None:
    """Verify artifact generation handles invalid type appropriately."""
    raw = await unisync_notebooklm_generate_artifact(
        course="BLAW10001",
        artifact_type="unsupported_type",  # type: ignore[arg-type]
    )
    data = json.loads(raw)
    assert "error" in data
