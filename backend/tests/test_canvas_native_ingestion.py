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
