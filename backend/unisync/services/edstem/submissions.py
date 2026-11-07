"""Read-only, evidence-backed EdStem attempt, grade and feedback archive.

Only the authenticated student's Ed lesson attempt records are retrieved.
Staff-not-released grades stay unreleased. No guesses from completion state.
Artifacts are private, Git-ignored and accessible through UniSync's scrubbed MCP.
No POST/PUT/DELETE requests are made.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

from unisync.core.file_io import atomic_write_json
from unisync.services.edstem.client import EdStemClient, EdStemError, _as_record, _as_list, _id, _string, edstem_to_markdown

logger = logging.getLogger(__name__)
GRADE_STATUSES = {"graded", "ungraded", "unreleased", "not_submitted", "unavailable"}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        import math
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _safe_feedback(value: Any) -> str:
    return edstem_to_markdown(value, limit=300_000) if value is not None else ""


def _only_own_slides(slides: Any, user_id: str) -> list[dict[str, Any]]:
    """Allowlist only the subject's own marked slide results."""
    own: list[dict[str, Any]] = []
    for slide in _as_list(slides):
        r = _as_record(slide)
        if _id(r.get("user_id")) != user_id:
            continue
        own.append({
            "slide_id": _id(r.get("slide_id")),
            "title": _string(r.get("title")),
            "marks": _number(r.get("marks")),
            "auto_mark": _number(r.get("auto_mark")),
            "rubric_mark": _number(r.get("rubric_mark")),
            "completed_at": r.get("completed_at"),
            "feedback": _safe_feedback(r.get("feedback") or r.get("comment") or r.get("comments")),
        })
    return own


def _normalise_grade(marking: dict[str, Any], final: dict[str, Any], user_id: str) -> dict[str, Any]:
    result = _as_record(marking.get("attempt_result"))
    if _id(result.get("user_id")) != user_id:
        raise EdStemError("EdStem returned a marking result for another user.", kind="security")
    override = _number(final.get("mark_override"))
    mark = override if override is not None else _number(result.get("mark"))
    unmarked = any(
        user_id in {_id(x) for x in _as_list(_as_record(st).get("unmarked_user_ids"))}
        for st in _as_list(marking.get("lesson_markable_marking_status"))
    )
    totals = [_number(v) for v in _as_record(marking.get("slide_totals")).values()]
    maximum = sum(totals) if totals and all(v is not None and v >= 0 for v in totals) else None
    if mark is None or (override is None and unmarked):
        status = "ungraded"
        mark = None
    else:
        status = "graded"
    return {
        "status": status,
        "score": mark,
        "points_possible": maximum,
        "grade": str(mark) if mark is not None else None,
        "grade_source": "edstem_final_attempt_override" if override is not None else "edstem_marking_status",
        "final_comment": _safe_feedback(result.get("final_comment") or final.get("comment")),
        "marked_slides": _only_own_slides(result.get("slides"), user_id),
    }


def archive_edstem_assessments(
    client: EdStemClient, lessons: list[dict[str, Any]], course_root: Path,
) -> dict[str, Any]:
    """Import accessible own Ed submissions and grading using GET endpoints.

    Returns summary and a grade_by_lesson map for the Todo synchronization.
    A fresh archive is written only for a verifiable attempt or grade status.
    """
    dest = course_root / "edstem" / "submissions"
    dest.mkdir(parents=True, exist_ok=True)
    # No auth call or unnecessary network requests for content-only courses.
    if not any(_string(x.get("kind")).lower() == "attempt" for x in lessons):
        summary = {"attempt_lessons": 0, "submitted_lessons": 0,
                   "graded_lessons": 0, "errors": 0,
                   "data_completeness": "no_submission_type_lessons"}
        atomic_write_json(dest / "sync_summary.json", summary)
        return {"summary": summary, "grades_by_lesson": {}}
    response = _as_record(client._request("user"))
    me = _id(_as_record(response.get("user")).get("id"))
    if not me:
        raise EdStemError("Cannot verify the authenticated EdStem user ID.", kind="auth")
    count = {"attempt_lessons": 0, "submitted_lessons": 0, "graded_lessons": 0,
             "unreleased_lessons": 0, "ungraded_lessons": 0, "saved_quiz_responses": 0,
             "graded_slides": 0, "feedback_comments": 0, "errors": 0}
    grade_by_lesson: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for lesson in lessons:
        kind = _string(lesson.get("kind")).lower()
        if kind != "attempt":
            continue
        count["attempt_lessons"] += 1
        lesson_id = _id(lesson.get("id"))
        if not lesson_id.isdigit():
            count["errors"] += 1
            errors.append({"lesson_id": lesson_id[:25], "kind": "invalid_lesson_id"})
            continue
        status = {"status": "not_submitted", "score": None, "points_possible": None,
                  "grade": None, "grade_source": None}
        grade_by_lesson[lesson_id] = status
        # No submitted_at is authoritative evidence that the learner has not
        # submitted this lesson, even if they viewed/completed other slides.
        if not lesson.get("submitted_at"):
            continue
        try:
            history = client.fetch_own_lesson_attempts(lesson_id, me)
            attempts = history["attempts"]
            final_id = history.get("final_id")
            if not final_id:
                # Ed may preserve non-final drafts; do not call them submitted.
                grade_by_lesson[lesson_id] = {**status, "status": "unavailable"}
                continue
            final = next((item for item in attempts if _id(item.get("id")) == final_id), None)
            if final is None or not final.get("submitted_at"):
                raise EdStemError("Final EdStem attempt missing or invalid.", kind="incomplete")
            count["submitted_lessons"] += 1
            grade = {**status, "status": "ungraded"}
            try:
                marking = client.fetch_own_marking_status(lesson_id, final_id, me)
                grade = _normalise_grade(marking, final, me)
            except EdStemError as exc:
                if exc.kind == "unreleased":
                    grade["status"] = "unreleased"
                elif exc.kind == "auth":
                    raise
                else:
                    grade["status"] = "unavailable"
                    errors.append({"lesson_id": lesson_id, "kind": exc.kind, "status_code": exc.status_code})
                    count["errors"] += 1
            grade["submitted_at"] = final.get("submitted_at")
            grade_by_lesson[lesson_id] = grade
            if grade["status"] in ("graded", "ungraded", "unreleased"):
                count[grade["status"] + "_lessons"] += 1
            marked_slides = grade.get("marked_slides", [])
            count["graded_slides"] += sum(s.get("marks") is not None for s in marked_slides)
            count["feedback_comments"] += sum(bool(s.get("feedback")) for s in marked_slides) + bool(grade.get("final_comment"))
            # Ed's quiz response endpoint may contain answers for other students
            # in staff scenarios; the client filters strictly to the account owner.
            responses: dict[str, list[dict[str, Any]]] = {}
            for slide in _as_list(lesson.get("slides")):
                slide = _as_record(slide)
                if _string(slide.get("type")) != "quiz" or not _id(slide.get("id")).isdigit():
                    continue
                try:
                    own = client.fetch_own_slide_responses(_id(slide["id"]), me)
                    if own:
                        responses[_id(slide["id"])] = [{
                            "question_id": _id(x.get("question_id")),
                            "created_at": x.get("created_at"),
                            "correct": x.get("correct") if isinstance(x.get("correct"), bool) else None,
                            "data": x.get("data"),
                        } for x in own]
                        count["saved_quiz_responses"] += len(own)
                except EdStemError as exc:
                    if exc.kind == "auth":
                        raise
                    count["errors"] += 1
                    errors.append({"lesson_id": lesson_id, "kind": "quiz_response_"+exc.kind})
            document = {
                "source": "edstem",
                "record_kind": "final_student_submission",
                "lesson_id": lesson_id,
                "lesson_title": _string(lesson.get("title") or lesson.get("name")),
                "lesson_kind": "attempt",
                "final_attempt_id": final_id,
                "submitted_at": final.get("submitted_at"),
                "attempts": [{
                    "id": _id(a.get("id")),
                    "submitted_at": a.get("submitted_at"),
                    "submission_type": a.get("submission_type"),
                    "comment": _safe_feedback(a.get("comment")),
                    "mark_override": _number(a.get("mark_override")),
                } for a in attempts],
                "grade": grade,
                "quiz_responses": responses,
                "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                "fidelity": {
                    "teacher_comment_supported": True,
                    "per_slide_mark_supported": True,
                    "submitted_source_files_downloaded": False,
                    "note": "This read-only API includes final attempt, mark and accessible staff comments. Original code workspaces or files may require a separate API.",
                },
            }
            lesson_dir = dest / lesson_id
            lesson_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(lesson_dir / "submission.json", document)
            lines = [
                f"# EdStem submission: {document['lesson_title']}",
                f"Lesson ID: {lesson_id}",
                f"Submitted at: {document['submitted_at']}",
                f"Status: {grade['status']}",
                f"Verified score: {grade.get('score')}",
                f"Available points: {grade.get('points_possible')}",
                "Source: authenticated EdStem final submission and marking API",
            ]
            if grade.get("final_comment"):
                lines.extend(["", "## Teacher feedback", grade["final_comment"]])
            for slide in grade.get("marked_slides", []):
                lines.extend(["", f"## {slide.get('title') or 'Slide'}", f"Mark: {slide.get('marks')}", f"Rubric: {slide.get('rubric_mark')}"])
                if slide.get("feedback"):
                    lines.append("Feedback: " + slide["feedback"])
            (lesson_dir / "submission.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
        except EdStemError as exc:
            count["errors"] += 1
            errors.append({"lesson_id": lesson_id, "kind": exc.kind, "status_code": exc.status_code})
            if exc.kind in ("auth", "security"):
                raise
            grade_by_lesson[lesson_id] = {**status, "status": "unavailable"}
    summary = {**count, "errors_detail": errors, "data_completeness": "published_student_accessible_api_only"}
    atomic_write_json(dest / "sync_summary.json", summary)
    return {"summary": summary, "grades_by_lesson": grade_by_lesson}
