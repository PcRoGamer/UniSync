from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unisync.services.edstem import (
    EdStemClient,
    EdStemError,
    EdStemSnapshot,
    extract_due_items,
    normalize_resource,
    render_snapshot,
)


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.response


def test_validate_redacts_token_and_normalizes_enrolled_courses() -> None:
    session = FakeSession(
        FakeResponse(
            {
                "user": {"id": 9, "name": "Student", "email": "student@example.test"},
                "courses": [
                    {"course": {"id": 36892, "name": "COMP10001", "code": "COMP10001"}},
                    {"course": {"id": 36892, "name": "duplicate"}},
                ],
            }
        )
    )
    client = EdStemClient("secret-token", api_base_url="https://example.test/api", session=session)

    result = client.validate()

    assert result["authenticated"] is True
    assert result["courses"] == [
        {
            "id": "36892",
            "name": "COMP10001",
            "code": "COMP10001",
            "year": "",
            "session": "",
            "status": "",
            "role": "",
            "url": "https://edstem.org/au/courses/36892",
        }
    ]
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in repr(result)


def test_auth_failure_is_actionable() -> None:
    session = FakeSession(FakeResponse({"error": "unauthorized"}, status_code=401))

    with pytest.raises(EdStemError, match="rejected or has expired") as caught:
        EdStemClient("expired", session=session).validate()

    assert caught.value.kind == "auth"
    assert caught.value.status_code == 401


def test_resource_normalization_and_download_host_restriction(tmp_path: Path) -> None:
    resource = normalize_resource(
        {"file_id": 42, "filename": "slides.pdf", "href": "https://edstem.org/files/42"},
        0,
    )
    assert resource["id"] == "42"
    assert resource["title"] == "slides.pdf"
    assert resource["download_url"] == "https://edstem.org/files/42"

    with pytest.raises(EdStemError, match="unsupported host"):
        EdStemClient("token").download_resource(
            "https://evil.example/files/slides.pdf", tmp_path / "slides.pdf"
        )


def test_render_snapshot_has_stable_source_documents_and_partial_failure_notes() -> None:
    snapshot = EdStemSnapshot(
        course={"id": "36892", "name": "COMP10001", "code": "COMP10001"},
        modules=[{"id": "m1", "name": "Week 1"}],
        lessons=[
            {
                "id": "l1",
                "title": "Introduction",
                "module_name": "Week 1",
                "content": "<p>Read <strong>this</strong>.</p>",
                "due_at": "2026-09-01T00:00:00Z",
            }
        ],
        resources=[{"id": "r1", "title": "Slides", "url": "https://edstem.org/slides.pdf"}],
        threads=[{"id": "t1", "title": "Question", "content": "<p>Hello</p>", "comments": [{"body": "Answer"}]}],
        announcements=[],
        fetched_at="2026-08-23T00:00:00+00:00",
        errors={"resources": "temporary failure"},
    )

    docs = dict(render_snapshot(snapshot, "36892"))

    assert "edstem/00-course-overview.md" in docs
    assert "edstem/lessons/comp10001-lessons-0001-introduction.md" in docs
    assert "Source: EdStem lesson" in docs["edstem/lessons/comp10001-lessons-0001-introduction.md"]
    assert "**this**" in docs["edstem/lessons/comp10001-lessons-0001-introduction.md"]
    assert "Reply 1" in docs["edstem/discussions/comp10001-discussions-0001-question.md"]
    assert "temporary failure" in docs["edstem/99-sync-notes.md"]


def test_due_items_use_stable_lesson_ids() -> None:
    snapshot = EdStemSnapshot(
        course={"id": "36892"},
        modules=[],
        lessons=[
            {"id": "lesson-7", "course_id": "36892", "title": "Assignment", "due_at": "2026-09-01T00:00:00Z"},
            {"id": "", "course_id": "36892", "name": "Missing due date"},
        ],
        resources=[],
        threads=[],
        announcements=[],
        fetched_at="",
    )

    items = extract_due_items(snapshot)
    assert len(items) == 1
    assert items[0]["id"] == "lesson-7"
    assert items[0]["title"] == "Assignment"
    assert items[0]["due_at"] == "2026-09-01T00:00:00Z"
    assert items[0]["submission_status"] == "unsubmitted"
    assert items[0]["status"] == "open"
    assert items[0]["url"] == "https://edstem.org/au/courses/36892/lessons/lesson-7"


def test_due_items_extracts_submission_and_points() -> None:
    """Verify that completed lessons extract submitted status, scores, and points possible."""
    snapshot = EdStemSnapshot(
        course={"id": "36892"},
        modules=[],
        lessons=[
            {
                "id": "114327",
                "course_id": "36892",
                "title": "Type conversions and input (2 points)",
                "due_at": "2026-08-07T18:00:00+10:00",
                "status": "completed",
            },
            {
                "id": "114328",
                "course_id": "36892",
                "title": "In-progress task (3 points)",
                "due_at": "2026-08-07T18:00:00+10:00",
                "status": "attempted",
            },
        ],
        resources=[],
        threads=[],
        announcements=[],
        fetched_at="",
    )

    items = extract_due_items(snapshot)
    assert len(items) == 2

    # Completed item
    assert items[0]["id"] == "114327"
    assert items[0]["status"] == "done"
    assert items[0]["submission_status"] == "submitted"
    assert items[0]["is_completed"] is True
    assert items[0]["is_graded"] is True
    assert items[0]["points_possible"] == 2.0
    assert items[0]["score"] == 2.0

    # In-progress item
    assert items[1]["id"] == "114328"
    assert items[1]["status"] == "open"
    assert items[1]["submission_status"] == "pending_review"
    assert items[1]["is_completed"] is False
    assert items[1]["points_possible"] == 3.0
    assert items[1]["score"] is None


def test_normalize_resource_constructs_edstem_download_url() -> None:
    res = normalize_resource({"id": 24670, "name": "tutesheet_w3", "extension": ".pdf"}, 0)
    assert res["id"] == "24670"
    assert "https://edstem.org/api/resources/24670/download/tutesheet_w3.pdf?dl=1" in res["download_url"]


def test_sync_edstem_course_full(tmp_path: Path, monkeypatch) -> None:
    from unisync.api.edstem import sync_edstem_course_full

    class MockClient:
        def snapshot(self, course_id: str, max_threads: int = 500):
            return EdStemSnapshot(
                course={"id": course_id, "name": "Foundations of Computing", "code": "COMP10001"},
                modules=[{"id": "m1", "name": "Week 1"}],
                lessons=[{"id": "l1", "name": "Introduction", "slides": [{"title": "Welcome", "content": "Hello"}]}],
                resources=[{"id": "r1", "name": "FAQ", "extension": ".pdf", "url": "https://edstem.org/api/resources/r1/download/FAQ.pdf?dl=1", "size": 100}],
                threads=[{"id": "t1", "title": "Setup", "content": "How to install?", "answers": []}],
                announcements=[],
                fetched_at="2026-09-12T00:00:00Z",
            )

        def download_resource(self, url: str, dest: Path):
            dest.write_bytes(b"dummy pdf bytes")
            return 15

    client = MockClient()
    out_dir = tmp_path / "course_test"
    res = sync_edstem_course_full(
        client,
        "36892",
        course_name="COMP10001",
        output_dir=out_dir,
        trigger_kb_index=False,
        persist_snapshot=False,
    )

    assert res["doc_count"] > 0
    assert res["downloaded_files"] == 1
    # Check that files were written on disk
    overview = out_dir / "edstem" / "00-course-overview.md"
    modules = out_dir / "edstem" / "01-modules.md"
    faq = out_dir / "edstem" / "files" / "FAQ.pdf"
    assert overview.exists()
    assert modules.exists()
    assert faq.exists()
    assert faq.read_bytes() == b"dummy pdf bytes"


def test_edstem_xml_linearizer() -> None:
    from unisync.services.edstem.client import edstem_to_markdown

    xml = (
        '<document version="2.0">'
        '<heading level="2">Topic Overview</heading>'
        '<paragraph>This is <bold>important</bold> and <italic>vital</italic>.</paragraph>'
        '<callout type="info"><paragraph>Be mindful of syntax.</paragraph></callout>'
        '<list style="bullet"><list-item>Item One</list-item><list-item>Item Two</list-item></list>'
        '<snippet-file filename="solution.py" language="python">print("hello world")</snippet-file>'
        '<paragraph>Refer to <link href="https://example.com/spec">Spec</link> and <math>x^2</math>.</paragraph>'
        '</document>'
    )
    md = edstem_to_markdown(xml)
    assert "## Topic Overview" in md
    assert "**important**" in md
    assert "*vital*" in md
    assert "> Be mindful of syntax." in md
    assert "- Item One" in md
    assert "- Item Two" in md
    assert "```python:solution.py" in md
    assert 'print("hello world")' in md
    assert "[Spec](https://example.com/spec)" in md
    assert "$x^2$" in md


def test_render_snapshot_with_quiz_questions() -> None:
    snapshot = EdStemSnapshot(
        course={"id": "36892", "code": "COMP10001", "name": "Foundations"},
        modules=[],
        lessons=[
            {
                "id": "l_quiz",
                "title": "Quiz Review",
                "slides": [
                    {
                        "id": "s_q1",
                        "type": "quiz",
                        "title": "Commenting Quiz",
                        "content": "<paragraph>Consider the program comments.</paragraph>",
                        "questions": [
                            {
                                "id": "q1",
                                "data": {
                                    "type": "multiple-choice",
                                    "content": "<paragraph>Which statement is correct?</paragraph>",
                                    "answers": [
                                        "<paragraph>Option A is false</paragraph>",
                                        "<paragraph>Option B is true</paragraph>",
                                    ],
                                    "solution": [1],
                                    "explanation": "<paragraph>Because B satisfies condition.</paragraph>",
                                },
                            }
                        ],
                    }
                ],
            }
        ],
        resources=[],
        threads=[],
        announcements=[],
        fetched_at="2026-09-12T00:00:00Z",
    )
    docs = dict(render_snapshot(snapshot, "36892"))
    quiz_doc = [v for k, v in docs.items() if "quiz" in k.lower()][0]

    assert "## Commenting Quiz" in quiz_doc
    assert "### Questions & Exercises" in quiz_doc
    assert "**Question 1:** Which statement is correct?" in quiz_doc
    assert "- [ ] Option A is false" in quiz_doc
    assert "- [x] Option B is true" in quiz_doc
    assert "> **Explanation:** Because B satisfies condition." in quiz_doc


