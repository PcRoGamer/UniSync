"""Read-only, course-scoped Canvas submission cache enrichment for MCP.

Uses existing user-owned submission.json files, never contacts Canvas and never
modifies original assignments. Raw documents stay on disk; only safe summaries,
comments, and rubric evaluations are returned through MCP's egress scrubber.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load_submission_archive(*, user_root: Path, course: str, canvas_url: str | None) -> dict[str, Any]:
    if not canvas_url:
        return {}
    assignment = re.search(r"/assignments/(\d+)(?:[/?#]|$)", canvas_url)
    code_match = re.search(r"(?<![A-Za-z0-9])([A-Za-z]{4}\d{5})(?![A-Za-z0-9])", course or "")
    if not assignment or not code_match:
        return {}
    assignment_id = assignment.group(1)
    course_code = code_match.group(1).upper()
    root = (user_root / "courses").resolve()
    if not root.is_dir():
        return {}
    # Two bounded directory levels, exact assignment id, no scanning other subjects.
    candidates = []
    for course_dir in root.iterdir():
        if not course_dir.is_dir() or not course_dir.name.upper().startswith(course_code + "_"):
            continue
        attempts = course_dir / "submissions" / assignment_id
        if not attempts.is_dir():
            continue
        for attempt_dir in attempts.iterdir():
            if not attempt_dir.is_dir() or not re.fullmatch(r"attempt_\d+", attempt_dir.name):
                continue
            metadata = attempt_dir / "submission.json"
            if metadata.is_file() and metadata.resolve().is_relative_to(root):
                candidates.append((int(attempt_dir.name.split("_")[1]), metadata))
    if not candidates:
        return {}
    candidates.sort(key=lambda item: item[0], reverse=True)
    for attempt, metadata in candidates:
        try:
            if metadata.stat().st_size > 2_000_000:
                continue
            record = json.loads(metadata.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError, UnicodeError):
            continue
        if str(record.get("assignment_id", "")) != assignment_id:
            continue
        archive_course = str(record.get("course", "")).upper()
        if archive_course and not archive_course.startswith(course_code):
            continue
        comments = []
        for item in record.get("submission_comments", []) or []:
            if not isinstance(item, dict) or not item.get("comment"):
                continue
            comments.append({
                "author_name": item.get("author_name") or "",
                "created_at": item.get("created_at") or "",
                "comment": item["comment"],
            })
        if not comments:
            for item in record.get("submission_html_comments", []) or []:
                if isinstance(item, dict) and item.get("comment"):
                    comments.append({
                        "author_name": item.get("author_name") or "",
                        "created_at": item.get("created_at") or "",
                        "comment": item["comment"],
                    })
        criteria = record.get("rubric_assessment") or {}
        if not isinstance(criteria, dict):
            criteria = {}
        criterion_scores = {
            str(k): {
                "points": v.get("points"),
                "rating_id": v.get("rating_id"),
                "comments": v.get("comments") or "",
            }
            for k, v in criteria.items() if isinstance(v, dict)
        }
        files = []
        for file in record.get("files", []) or []:
            if not isinstance(file, dict):
                continue
            raw_path = file.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            try:
                resolved = Path(raw_path).resolve()
                if not resolved.is_relative_to(root):
                    continue
                relative_path = resolved.relative_to(user_root.resolve()).as_posix()
                files.append({
                    "filename": file.get("filename") or resolved.name,
                    "path": relative_path,
                    "available": resolved.is_file(),
                })
            except (OSError, ValueError):
                continue
        return {
            "submission_archive_found": True,
            "submission_attempt": attempt,
            "submission_record_kind": record.get("record_kind") or "submitted",
            "archived_submitted_at": record.get("submitted_at"),
            "archived_grade": record.get("grade"),
            "archived_score": record.get("score"),
            "submission_type": record.get("submission_type") or "",
            "group_assignment": record.get("group_assignment"),
            "submitted_files": files,
            "rubric_assessment": criterion_scores,
            "submission_comments": comments,
        }
    return {}
