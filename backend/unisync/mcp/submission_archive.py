"""Read archived Canvas and Gradescope submission files for MCP tools."""
from __future__ import annotations

import json
import re
from pathlib import Path

from unisync.services.sync.gradescope import titles_match


_CODE = re.compile(r"[A-Za-z]{3,8}\d{4,6}")


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _assignment_id_from_url(url: str) -> str:
    match = re.search(r"/assignments/(\d+)", url or "")
    return match.group(1) if match else ""


def _codes(value: object) -> set[str]:
    return {match.group(0).casefold() for match in _CODE.finditer(str(value or ""))}


def course_dirs(user_root: Path, course: str) -> list[Path]:
    root = Path(user_root) / "courses"
    if not root.is_dir():
        return []
    needle = (course or "").strip()
    if needle:
        exact = root / needle
        if exact.is_dir():
            return [exact]
    folders = [path for path in root.iterdir() if path.is_dir()]
    if not needle or needle.isdigit():
        return folders
    folded = needle.casefold()
    needle_codes = _codes(needle)
    matched = []
    for path in folders:
        name = path.name.casefold()
        if folded == name or (len(folded) >= 6 and (folded in name or name in folded)):
            matched.append(path)
            continue
        if needle_codes and needle_codes & _codes(path.name):
            matched.append(path)
    return matched


def _attempt_number(data: dict) -> int:
    try:
        return int(data.get("attempt") or 0)
    except (TypeError, ValueError):
        return 0


def load_submission_archive(
    *,
    user_root: Path,
    course: str = "",
    canvas_url: str = "",
    assignment_id: str = "",
    title: str = "",
) -> dict:
    """Load the newest archived Canvas attempt for one assignment."""
    assignment_id = str(assignment_id or _assignment_id_from_url(canvas_url))
    candidates = []
    for course_dir in course_dirs(Path(user_root), course):
        base = course_dir / "submissions"
        folders = [base / assignment_id] if assignment_id else list(base.glob("*")) if base.is_dir() else []
        for folder in folders:
            if not folder.is_dir():
                continue
            for manifest in folder.glob("attempt_*/submission.json"):
                data = _load_json(manifest)
                if not data or data.get("source") == "gradescope":
                    continue
                if assignment_id and str(data.get("assignment_id") or "") not in {"", assignment_id}:
                    continue
                if title and data.get("assignment") and assignment_id in {"", None} and not titles_match(title, data.get("assignment")):
                    continue
                candidates.append(data)
    if not candidates:
        return {"found": False}
    best = max(candidates, key=_attempt_number)
    comments = best.get("submission_comments") if isinstance(best.get("submission_comments"), list) else []
    return {
        "found": True,
        "submission_record_kind": best.get("record_kind"),
        "archived_score": best.get("score"),
        "archived_grade": best.get("grade"),
        "points_possible": best.get("points_possible"),
        "graded_at": best.get("graded_at"),
        "submitted_at": best.get("submitted_at"),
        "submission_comments": comments,
        "rubric_assessment": best.get("full_rubric_assessment") or best.get("rubric_assessment") or {},
        "assignment": best.get("assignment"),
        "assignment_id": best.get("assignment_id"),
        "canvas_url": best.get("canvas_url"),
    }


def _relative(path: Path, user_root: Path) -> str:
    try:
        return path.relative_to(user_root).as_posix()
    except ValueError:
        return path.name


def _course_matches(data: dict, course: str, folder_name: str) -> bool:
    needle = (course or "").strip()
    if not needle:
        return True
    values = [
        data.get("course"),
        data.get("canvas_course_id"),
        data.get("gradescope_course_id"),
        folder_name,
    ]
    folded = needle.casefold()
    needle_codes = _codes(needle)
    for value in values:
        text = str(value or "")
        lowered = text.casefold()
        if not lowered:
            continue
        if folded == lowered or (len(folded) >= 6 and (folded in lowered or lowered in folded)):
            return True
        if needle.isdigit() and needle == text:
            return True
        if needle_codes and needle_codes & _codes(text):
            return True
    return False


def _assignment_matches(data: dict, assignment: str, canvas_assignment_id: str) -> bool:
    if canvas_assignment_id and str(data.get("canvas_assignment_id") or "") == str(canvas_assignment_id):
        return True
    needle = str(assignment or "").strip()
    if not needle:
        return not canvas_assignment_id
    if needle in {
        str(data.get("assignment_id") or ""),
        str(data.get("canvas_assignment_id") or ""),
        str(data.get("submission_id") or ""),
    }:
        return True
    return titles_match(needle, data.get("assignment") or "") or titles_match(needle, data.get("canvas_assignment") or "")


def _public_gradescope(data: dict, manifest: Path, user_root: Path) -> dict:
    folder = manifest.parent
    pdf = folder / "graded.pdf"
    summary = folder / "feedback.md"
    return {
        "found": True,
        "source": "gradescope",
        "course": data.get("course"),
        "canvas_course_id": data.get("canvas_course_id"),
        "gradescope_course_id": data.get("gradescope_course_id"),
        "assignment": data.get("assignment"),
        "assignment_id": data.get("assignment_id"),
        "canvas_assignment_id": data.get("canvas_assignment_id"),
        "canvas_assignment": data.get("canvas_assignment"),
        "canvas_url": data.get("canvas_url"),
        "submission_id": data.get("submission_id"),
        "score": data.get("score"),
        "points_possible": data.get("points_possible"),
        "updated_at": data.get("updated_at"),
        "questions": data.get("questions") or [],
        "comments": data.get("comments") or [],
        "gradescope_url": data.get("gradescope_url"),
        "summary": _relative(summary, user_root) if summary.exists() else None,
        "graded_pdf": _relative(pdf, user_root) if pdf.exists() else None,
    }


def load_gradescope_feedback(
    *,
    user_root: Path,
    course: str = "",
    assignment: str = "",
    canvas_assignment_id: str = "",
) -> dict:
    """Return one archived Gradescope submission for the signed-in student."""
    user_root = Path(user_root)
    matches = []
    for course_dir in course_dirs(user_root, course):
        base = course_dir / "gradescope"
        if not base.is_dir():
            continue
        for manifest in base.glob("*/*/*/submission.json"):
            data = _load_json(manifest)
            if not data:
                continue
            if not _course_matches(data, course, course_dir.name):
                continue
            if not _assignment_matches(data, assignment, canvas_assignment_id):
                continue
            matches.append((data, manifest))
    if not matches:
        return {"found": False, "course": course, "assignment": assignment}
    exact = [
        item for item in matches
        if canvas_assignment_id and str(item[0].get("canvas_assignment_id") or "") == str(canvas_assignment_id)
    ]
    chosen = exact or matches
    titles = {str(item[0].get("assignment") or "") for item in chosen}
    if not canvas_assignment_id and len(titles) > 1:
        return {
            "found": False,
            "error": "multiple Gradescope assignments match",
            "matches": [
                {"assignment": item[0].get("assignment"), "submission_id": item[0].get("submission_id")}
                for item in chosen[:8]
            ],
        }
    data, manifest = max(chosen, key=lambda item: (str(item[0].get("updated_at") or ""), str(item[0].get("submission_id") or "")))
    return _public_gradescope(data, manifest, user_root)
