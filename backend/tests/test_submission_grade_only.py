"""Canvas graded-without-submission-time feedback regression tests."""
from __future__ import annotations
import asyncio
import json

from unisync.services.sync.submissions import SubmissionSync, has_meaningful_assessment
from unisync.mcp.submission_archive import load_submission_archive


def test_grade_only_assessments_are_meaningful():
    assert has_meaningful_assessment({"score": 0})
    assert has_meaningful_assessment({"grade": "Fail"})
    assert has_meaningful_assessment({"workflow_state": "graded"})
    assert has_meaningful_assessment({"submission_comments": [{"comment": "Needs evidence."}]})
    assert has_meaningful_assessment({"rubric_assessment": {"criterion": {"points": 2}}})
    assert has_meaningful_assessment({"submitted_at": "2026-10-15T12:00:00Z"})
    assert not has_meaningful_assessment({})
    assert not has_meaningful_assessment({"workflow_state": "unsubmitted"})
    assert not has_meaningful_assessment({"submission_type": "none"})


def test_grade_only_submission_is_archived_as_feedback_not_submission(tmp_path):
    course = {"id": 77, "course_code": "BLAW10001_2026_SM2"}
    assignment = {"id": 123, "name": "Essay Feedback", "points_possible": 20,
                  "html_url": "https://canvas.lms.unimelb.edu.au/courses/77/assignments/123"}
    grade_only = {"attempt": 1, "score": 0, "grade": "0", "submitted_at": None,
                  "submission_type": "none", "graded_at": "2026-11-01T00:00:00Z",
                  "submission_comments": [{"author_name": "Marker", "comment": "Please cite cases."}],
                  "rubric_assessment": {"criterion": {"points": 0, "comments": "Use the cases."}}}
    client = object()
    result = asyncio.run(SubmissionSync(client, tmp_path / "courses").archive(course, assignment, grade_only))
    assert result == {"files": 0, "failed": 0, "feedback_only": 1}
    manifest = tmp_path / "courses" / "BLAW10001_2026_SM2" / "submissions" / "123" / "attempt_1" / "submission.json"
    archived = json.loads(manifest.read_text(encoding="utf-8"))
    assert archived["record_kind"] == "grade_or_feedback_only"
    assert archived["score"] == 0
    md = next(manifest.parent.glob("*_submission.md")).read_text(encoding="utf-8")
    assert "Assessment feedback (no submitted-at timestamp)" in md
    assert "Submitted work:" not in md
    result = load_submission_archive(
        user_root=tmp_path, course="BLAW10001_2026_SM2",
        canvas_url=assignment["html_url"],
    )
    assert result["submission_record_kind"] == "grade_or_feedback_only"
    assert result["archived_score"] == 0
    assert len(result["submission_comments"]) == 1


def test_sync_includes_graded_without_submitted_timestamp(tmp_path):
    assignment = {"id": 123, "name": "Quiz", "submission": {"score": 0, "workflow_state": "graded"}}
    response_data = {"attempt": 1, "submitted_at": None, "submission_type": "none",
                     "score": 0, "workflow_state": "graded"}
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return response_data
    class FakeClient:
        async def get(self, *args, **kwargs): return FakeResponse()
    class FakeSync(SubmissionSync):
        def __init__(self):
            super().__init__(FakeClient(),tmp_path)
            self.seen=[]
        async def pages(self, url):
            if "enrollment_type=student" in url:
                return [{"id": 77, "course_code": "BLAW10001_2026_SM2"}]
            return [assignment]
        async def archive(self, course, assignment, submission):
            self.seen.append(submission)
            return {"files": 0, "failed": 0, "feedback_only": 1}
    sync=FakeSync()
    counts=asyncio.run(sync.run())
    assert len(sync.seen)==1
    assert counts["attempts"] == counts["feedback_only"] == 1
    assert counts["errors"] == []


def test_sync_ignores_unsubmitted_placeholders(tmp_path):
    class FakeSync(SubmissionSync):
        def __init__(self):super().__init__(object(),tmp_path)
        async def pages(self,url):
            if "enrollment_type=student" in url:return [{"id": 77, "course_code": "BLAW10001_2026_SM2"}]
            return [{"id": 123, "submission": {"workflow_state": "unsubmitted"}}]
    result=asyncio.run(FakeSync().run())
    assert result["attempts"] == 0
    assert result["errors"] == []
