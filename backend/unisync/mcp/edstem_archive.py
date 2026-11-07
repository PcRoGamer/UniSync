"""Read the authenticated user's EdStem submission archive, not guessed grades."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


def load_edstem_submission(
    *, user_root: Path, course: str, lesson_id: str
) -> dict[str, Any]:
    """Return the locally archived student submission/feedback for one Ed lesson.

    The student must have an actual attempt record. Results point to full JSON
    available through UniSync's paginated academic-file reader; no other file
    path or user may be passed through this interface.
    """
    if not str(lesson_id).isdigit():
        return {}
    root = (user_root / "courses").resolve()
    scope = re.search(r"[A-Za-z]{3,5}\d{5}", course or "")
    code = scope.group(0).lower() if scope else ""
    if not root.is_dir():
        return {}
    matches: list[Path] = []
    for folder in root.iterdir():
        if not folder.is_dir() or not folder.resolve().is_relative_to(root):
            continue
        if code and code not in folder.name.lower():
            continue
        candidate = (folder / "edstem" / "submissions" / lesson_id / "submission.json").resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            matches.append(candidate)
    if not matches:
        return {}
    if len(matches) > 1:
        return {"edstem_submission_error": "Ambiguous course; use the exact course code."}
    path = matches[0]
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("source") != "edstem" or str(raw.get("lesson_id")) != str(lesson_id):
        return {}
    grade = raw.get("grade") or {}
    slides = grade.get("marked_slides") or []
    return {
        "edstem_submission_archive_found": True,
        "edstem_submission_archive_path": path.relative_to(user_root).as_posix(),
        "edstem_submission_kind": raw.get("record_kind"),
        "edstem_submitted_at": raw.get("submitted_at"),
        "edstem_grade_status": grade.get("status"),
        "edstem_verified_score": grade.get("score") if grade.get("status") == "graded" else None,
        "edstem_verified_points_possible": grade.get("points_possible") if grade.get("status") == "graded" else None,
        "edstem_grade_source": grade.get("grade_source"),
        "edstem_teacher_comment": grade.get("final_comment") or None,
        "edstem_slide_marks": [
            {"title": slide.get("title"), "marks": slide.get("marks"),
             "rubric_mark": slide.get("rubric_mark"), "feedback": slide.get("feedback")}
            for slide in slides
        ],
        "edstem_quiz_response_count": sum(len(entries) for entries in (raw.get("quiz_responses") or {}).values()),
        "edstem_fidelity_note": raw.get("fidelity",{}).get("note",""),
    }
