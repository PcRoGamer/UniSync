"""UniSync FastMCP Server.

Exposes university coursework, deadlines, lecture transcripts, course materials,
and sync metadata to autonomous AI agents (Hermes, Cursor, Claude Desktop, Antigravity)
using the standard Model Context Protocol (MCP).

Zero LLM calls — 100% programmatic queries into UniSync's local databases,
manifests, and WebVTT subtitle caches.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

# Prevent HuggingFace/tqdm from writing progress bars to sys.stdout (corrupts MCP stdio JSON-RPC)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from mcp.server.fastmcp import FastMCP

from unisync.core.text_cleaner import (
    filter_academic_boilerplate,
    is_academic_boilerplate_section,
    filter_vtt_content,
    is_academic_boilerplate_cue,
)

# Initialize FastMCP application
mcp = FastMCP(
    "unisync",
    dependencies=["pypdf"],
)

logger = logging.getLogger("unisync.mcp")

# Auto-trigger on-disk academic boilerplate scrubber in background on MCP startup
try:
    from unisync.services.disk_cleaner import run_background_disk_scrubber
    run_background_disk_scrubber(delay_sec=0.5)
except Exception as scrub_err:
    logger.debug("Failed spawning background disk scrubber: %s", scrub_err)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_iso(val: Any) -> tuple[str, datetime | None]:
    """Returns ISO string and timezone-aware datetime object."""
    if not val:
        return "", None
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return dt.isoformat(), dt
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(), dt
    except Exception:
        return str(val), None


def _format_countdown(due_at: Any, is_done: bool) -> str:
    """Calculates human-readable relative time until or past deadline."""
    if is_done:
        return "Completed"
    iso_str, due_dt = _normalize_iso(due_at)
    if not due_dt:
        return iso_str[:10] if iso_str else "No deadline"
    try:
        now_dt = datetime.now(timezone.utc)
        diff_secs = int((due_dt - now_dt).total_seconds())
        if diff_secs < 0:
            past_secs = abs(diff_secs)
            if past_secs < 3600:
                return f"Overdue by {past_secs // 60}m"
            if past_secs < 86400:
                return f"Overdue by {past_secs // 3600}h"
            return f"Overdue by {past_secs // 86400}d"
        else:
            if diff_secs < 3600:
                return f"Due in {diff_secs // 60}m"
            if diff_secs < 86400:
                return f"Due in {diff_secs // 3600}h"
            return f"Due in {diff_secs // 86400}d"
    except Exception:
        return iso_str[:10] if iso_str else "Unknown"


def _clean_html_text(raw_html: str) -> str:
    """Strips HTML tags, unescapes entities, and cleans administrative boilerplate."""
    if not raw_html:
        return ""
    # Strip script and style tags
    clean = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw_html, flags=re.DOTALL | re.IGNORECASE)
    # Convert accordion triggers and headers to markdown headings so section filtering catches them
    clean = re.sub(
        r'<[^>]*class="[^"]*accordion-title[^"]*"[^>]*>(.*?)</[^>]+>',
        r"\n\n### \1\n",
        clean,
        flags=re.DOTALL | re.IGNORECASE,
    )
    clean = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", r"\n\n### \2\n", clean, flags=re.DOTALL | re.IGNORECASE)
    # Convert paragraph and break tags to newlines
    clean = re.sub(r"</?(p|div|tr|li)[^>]*>", "\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<br\s*/?>", "\n", clean, flags=re.IGNORECASE)
    # Strip all other HTML tags
    clean = re.sub(r"<[^>]+>", "", clean)
    # Decode basic entities
    clean = (
        clean.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    # Clean administrative headers and formatting boilerplate
    return filter_academic_boilerplate(clean)


def _detect_academic_context(libs: dict[str, Any], items: list[Any]) -> tuple[str, str, str]:
    """Dynamically infer institution name, academic term, and Canvas host from live course data."""
    canvas_host = "canvas.lms.unimelb.edu.au"
    for t in items:
        url = getattr(t, "url", "") or ""
        if "canvas" in url:
            m = re.search(r"https?://([^/]+)", url)
            if m:
                canvas_host = m.group(1)
                break

    institution = "University"
    if "unimelb" in canvas_host:
        institution = "The University of Melbourne"
    elif "monash" in canvas_host:
        institution = "Monash University"
    elif "usyd" in canvas_host or "sydney" in canvas_host:
        institution = "The University of Sydney"
    elif "unsw" in canvas_host:
        institution = "UNSW Sydney"
    else:
        parts = canvas_host.split(".")
        if len(parts) >= 2:
            institution = f"{parts[-2].capitalize()} University"

    detected_term = ""
    for data in libs.values():
        name = data.get("name", "")
        m_t = re.search(r"(\d{4})[_\s]*(?:SM|S|Sem|Semester)[_\s]*(\d)", name, re.IGNORECASE)
        if m_t:
            detected_term = f"{m_t.group(1)} Semester {m_t.group(2)}"
            break
    if not detected_term:
        detected_term = f"{datetime.now(timezone.utc).year} Semester"

    return institution, detected_term, canvas_host


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1: Academic Executive Overview (Zero-Context Briefing)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_get_overview() -> str:
    """Provide a comprehensive executive briefing of the student's current academic semester.

    Designed for agents that start a session with zero prior knowledge. Returns the enrolled subjects,
    urgent upcoming deadlines with relative countdowns, recent grades, latest announcements,
    and sync health in a single call.

    Returns:
        Structured JSON overview containing enrolled subjects, deadlines, grades, notices, and term metadata.
    """
    now_dt = datetime.now(timezone.utc)

    # 1. Enrolled academic subjects
    libs = {}
    try:
        from unisync.api.lectures import _load_libraries
        libs = _load_libraries()
    except Exception:
        pass

    # 2. Upcoming deadlines & recent grades
    upcoming: list[dict[str, Any]] = []
    graded: list[dict[str, Any]] = []
    items: list[Any] = []

    try:
        from unisync.services.todos import get_todo_service
        svc = get_todo_service()
        items = svc.list_items()
    except Exception as _t_err:
        logger.debug("Overview todo collection error: %s", _t_err)

    institution, term, canvas_host = _detect_academic_context(libs, items)

    subjects = []
    subject_deadline_counts: dict[str, int] = {}

    for cid, data in libs.items():
        name = data.get("name", "")
        m_code = re.search(r"([A-Z]{4}\d{5})", name)
        if not m_code:
            continue
        code = m_code.group(1)
        clean_name = re.sub(r"\([A-Z]{4}\d{5}[^\)]*\)", "", name).strip() or name
        subject_deadline_counts[code] = 0
        subjects.append({
            "code": code,
            "name": clean_name,
            "canvas_course_id": cid,
            "echo360_section_id": data.get("echo360_course_id"),
            "canvas_url": f"https://{canvas_host}/courses/{cid}",
        })

    for t in items:
        is_done = t.status == "done"
        t_due = t.due_at
        if t_due and not is_done:
            _, due_dt = _normalize_iso(t_due)
            if due_dt and due_dt > now_dt:
                diff_secs = int((due_dt - now_dt).total_seconds())
                countdown = _format_countdown(t_due, is_done)
                upcoming.append({
                    "id": str(t.id),
                    "title": t.title,
                    "course": t.course or "University Subject",
                    "due_at": due_dt.isoformat(),
                    "countdown": countdown,
                    "points_possible": t.points_possible,
                    "_secs": diff_secs,
                })
                for s in subjects:
                    if s["code"].lower() in (t.course or "").lower() or s["name"].lower() in (t.course or "").lower():
                        subject_deadline_counts[s["code"]] += 1

        if t.is_graded or t.score is not None:
            graded.append({
                "title": t.title,
                "course": t.course,
                "score": t.score,
                "points_possible": t.points_possible,
                "grade": t.grade,
            })

    upcoming.sort(key=lambda x: x["_secs"])
    for u in upcoming:
        del u["_secs"]

    for s in subjects:
        s["active_deadlines"] = subject_deadline_counts.get(s["code"], 0)

    # 3. Latest Canvas announcements
    recent_announcements: list[dict[str, Any]] = []
    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")

    courses_dir = user_root / "courses"
    if courses_dir.exists():
        for page_file in courses_dir.glob("*/pages/*announcement*.md"):
            try:
                raw = page_file.read_text(encoding="utf-8", errors="replace")
                lines = raw.splitlines()
                title = page_file.stem
                for line in lines[:5]:
                    if line.startswith("#"):
                        title = line.lstrip("#").strip()
                        break
                c_name = page_file.parent.parent.name
                mtime = page_file.stat().st_mtime
                recent_announcements.append({
                    "course": c_name.split("_")[0],
                    "title": title,
                    "date": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()[:10],
                    "snippet": raw[:220].replace("\n", " ").strip(),
                    "_mtime": mtime,
                })
            except Exception:
                pass

    recent_announcements.sort(key=lambda x: x["_mtime"], reverse=True)
    for a in recent_announcements:
        del a["_mtime"]

    # 4. Sync Health
    sync_summary: dict[str, Any] = {}
    try:
        from unisync.api.sync_engine import get_computed_sync_status
        st = get_computed_sync_status()
        sync_summary = {
            "state": st.get("state"),
            "state_label": st.get("state_label"),
            "last_synced": st.get("last_synced_at"),
            "total_assets_indexed": st.get("total_assets"),
            "latest_lecture_date": st.get("latest_lecture_date"),
        }
    except Exception:
        pass

    return json.dumps({
        "summary": "Academic Semester Overview & Executive Briefing",
        "institution": institution,
        "term": term,
        "current_time_utc": now_dt.isoformat()[:19] + "Z",
        "sync_health": sync_summary,
        "enrolled_subjects": subjects,
        "urgent_deadlines": upcoming[:6],
        "recent_announcements": recent_announcements[:4],
        "recent_graded_assignments": graded[:4],
    }, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: List Enrolled Courses & Subjects
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_list_courses() -> str:
    """List all enrolled university courses, subject codes, Canvas IDs, and current status.

    Use this tool to discover which subjects the student is taking (e.g. COMP10001, INFO10003, BLAW10001, CCDP10003)
    along with their Canvas course IDs, lecture capture sections, and links.

    Returns:
        Structured JSON list of enrolled courses with subject codes, names, semester,
        and Canvas course IDs.
    """
    try:
        from unisync.api.lectures import _load_libraries
        libs = _load_libraries()
    except Exception:
        libs = {}

    items = []
    try:
        from unisync.services.todos import get_todo_service
        items = get_todo_service().list_items()
    except Exception:
        pass

    _, _, canvas_host = _detect_academic_context(libs, items)

    courses = []
    for cid, data in libs.items():
        name = data.get("name", "")
        m_code = re.search(r"([A-Z]{4}\d{5})", name)
        code = m_code.group(1) if m_code else ""

        clean_name = re.sub(r"\([A-Z]{4}\d{5}[^\)]*\)", "", name).strip()
        if not clean_name:
            clean_name = name

        is_academic = bool(code)

        courses.append({
            "code": code or "COMMUNITY",
            "name": clean_name,
            "full_title": name,
            "canvas_course_id": cid,
            "is_academic": is_academic,
            "echo360_section_id": data.get("echo360_course_id"),
            "canvas_url": f"https://{canvas_host}/courses/{cid}",
        })

    courses.sort(key=lambda x: (not x["is_academic"], x["code"]))

    return json.dumps({
        "total_courses": len(courses),
        "academic_subjects": [c for c in courses if c["is_academic"]],
        "all_enrolled": courses,
    }, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: Academic Deadlines & Assignments
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_get_deadlines(
    filter: Literal["upcoming", "missed", "graded", "completed", "all"] = "upcoming",
    course: str | None = None,
    limit: int = 20,
) -> str:
    """List academic deadlines, assignments, and quizzes from Canvas LMS and EdStem.

    Args:
        filter: One of 'upcoming' (nearest future deadlines first), 'missed' (overdue items),
                'graded' (assignments with instructor marks/feedback), 'completed' (done), or 'all'.
        course: Optional filter by course name or subject code (e.g. 'COMP10001', 'Business Law').
        limit: Maximum number of deadlines to return (default 20).

    Returns:
        Structured JSON list of deadlines with due dates, countdowns, submission statuses,
        points possible, and grades.
    """
    try:
        from unisync.services.todos import get_todo_service
        svc = get_todo_service()
        items = svc.list_items()
    except Exception as exc:
        return json.dumps({"error": f"Failed to query todo service: {exc}"})

    now_dt = datetime.now(timezone.utc)
    results = []

    for t in items:
        # Course filter
        if course:
            c_target = course.lower()
            t_course = (t.course or "").lower()
            t_notes = (t.notes or "").lower()
            if c_target not in t_course and c_target not in t_notes:
                continue

        is_done = t.status == "done"
        due_iso, due_dt = _normalize_iso(t.due_at)
        is_past = bool(due_dt and due_dt < now_dt)
        is_graded = bool(t.is_graded or t.submission_status == "graded" or (t.score is not None))

        # Category filter
        if filter == "upcoming" and (is_done or not due_dt or is_past):
            continue
        if filter == "missed" and (is_done or not due_dt or not is_past):
            continue
        if filter == "graded" and not is_graded:
            continue
        if filter == "completed" and not is_done:
            continue

        results.append({
            "id": t.id,
            "title": t.title,
            "course": t.course or "Independent Task",
            "due_at": due_iso,
            "countdown": _format_countdown(t.due_at, is_done),
            "status": t.status,
            "submission_status": t.submission_status or ("completed" if is_done else "unsubmitted"),
            "is_graded": is_graded,
            "score": t.score,
            "grade": t.grade,
            "points_possible": t.points_possible,
            "priority": t.priority,
            "url": t.url,
            "source": t.source,
        })

    # Sort results
    if filter == "upcoming":
        results.sort(key=lambda x: x["due_at"] or "9999")
    elif filter == "missed":
        results.sort(key=lambda x: x["due_at"] or "", reverse=True)
    elif filter == "graded":
        results.sort(key=lambda x: (x["score"] or 0), reverse=True)

    return json.dumps({
        "filter": filter,
        "total_matches": len(results),
        "deadlines": results[:limit],
    }, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: Full Assignment Detail & Rubrics
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_get_assignment_detail(
    title_or_id: str | None = None,
    assignment_id: str | None = None,
    id: str | None = None,
    title: str | None = None,
    course: str | None = None,
) -> str:
    """Retrieve complete instructions, rubrics, point breakdowns, and submission status for an assignment.

    Args:
        title_or_id: Assignment title (e.g. 'Project 1', 'A1.2') or numeric database ID (e.g. '661942').
        assignment_id: Optional alias for title_or_id.
        id: Optional alias for title_or_id.
        title: Optional alias for title_or_id.
        course: Optional course name/code to disambiguate identical titles across subjects.

    Returns:
        Full assignment details including cleaned instructions, rubric criteria & rating tiers,
        score, points possible, submission comments, archived Canvas feedback, Gradescope
        marks when a local archive matches, and URLs.
    """
    try:
        from unisync.services.todos import get_todo_service
        svc = get_todo_service()
        items = svc.list_items()
    except Exception as exc:
        return json.dumps({"error": f"Failed to query todo service: {exc}"})

    target_raw = str(title_or_id or assignment_id or id or title or "").strip()
    if not target_raw:
        return json.dumps({
            "error": "Missing assignment identifier. Please provide 'title_or_id', 'assignment_id', or 'id'.",
            "suggestion": "Call unisync_get_deadlines to see available assignment titles and IDs.",
        })

    needle = target_raw.lower()
    matched = None

    # Normalization helper for abbreviations like A1.2 <-> Assignment 1.2
    def _normalize_title(t: str) -> str:
        s = t.lower()
        s = re.sub(r"\bassignment\s*", "a", s)
        return s.replace(" ", "").replace("_", "").replace("-", "")

    norm_needle = _normalize_title(needle)

    # 1. Exact ID match (item id, canvas id, or source_id)
    for t in items:
        t_id = str(t.id).lower()
        t_source = str(getattr(t, "source_id", "") or "").lower()
        if needle in (t_id, t_source) or t_source.endswith(f":{needle}"):
            matched = t
            break

    # 2. Direct title match or normalized abbreviation match (e.g. A1.2 <-> Assignment 1.2)
    if not matched:
        for t in items:
            if course and course.lower() not in (t.course or "").lower():
                continue
            t_title = (t.title or "").lower()
            if needle in t_title or t_title in needle:
                matched = t
                break
            if norm_needle and (norm_needle in _normalize_title(t_title) or _normalize_title(t_title) in norm_needle):
                matched = t
                break

    # 3. Token-based overlap
    if not matched:
        terms = [w for w in re.split(r"[\s_\-]+", needle) if len(w) > 1]
        best_score = 0
        for t in items:
            if course and course.lower() not in (t.course or "").lower():
                continue
            t_title = (t.title or "").lower()
            score = sum(1 for term in terms if term in t_title)
            if score > best_score and score >= max(1, len(terms) // 2):
                best_score = score
                matched = t

    if not matched:
        return json.dumps({
            "error": f"Assignment '{target_raw}' not found.",
            "suggestion": "Call unisync_get_deadlines to see available assignment titles and IDs.",
        })

    # Parse and clean description
    clean_desc = _clean_html_text(matched.description or "")
    if not clean_desc and matched.notes:
        clean_desc = filter_academic_boilerplate(matched.notes)

    # Automatically resolve authoritative EdStem lesson specification if available
    ed_lesson_file = None
    ed_lesson_id = None
    if matched.url:
        m_id = re.search(r"lessons/(\d+)", matched.url)
        if m_id:
            ed_lesson_id = m_id.group(1)

    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")

    courses_dir = user_root / "courses"
    if courses_dir.exists():
        candidates = []
        norm_title = re.sub(r"[^a-z0-9]+", "-", (matched.title or "").lower()).strip("-")
        for lesson_p in courses_dir.glob("**/edstem/lessons/*.md"):
            name_lower = lesson_p.name.lower()
            score = 0
            if ed_lesson_id and ed_lesson_id in name_lower:
                score += 15
            if norm_title and norm_title in name_lower:
                score += 10
            # Also check if lesson ID is inside the file header
            if ed_lesson_id and score < 15:
                try:
                    head = lesson_p.read_text(encoding="utf-8", errors="replace")[:600]
                    if ed_lesson_id in head:
                        score += 15
                except Exception:
                    pass
            if score > 0:
                candidates.append((score, lesson_p))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_lesson = candidates[0][1]
            try:
                raw_spec = best_lesson.read_text(encoding="utf-8", errors="replace")
                cleaned_spec = filter_academic_boilerplate(raw_spec)
                if cleaned_spec:
                    ed_lesson_file = str(best_lesson.relative_to(user_root)).replace("\\", "/")
                    # If description is a placeholder or very short, use authoritative Ed spec
                    is_placeholder = not clean_desc or len(clean_desc) < 350 or any(
                        ph in clean_desc.lower() for ph in (
                            "details will be published in ed",
                            "published in ed during the semester",
                            "submitted via ed",
                            "submission method: via ed",
                        )
                    )
                    if is_placeholder:
                        clean_desc = cleaned_spec
            except Exception as _e:
                logger.debug("Failed reading Ed lesson file: %s", _e)

    # Format rubrics cleanly
    formatted_rubric = []
    if isinstance(matched.rubric, list):
        for r in matched.rubric:
            criteria = {
                "description": r.get("description") or "Criterion",
                "points": r.get("points", 0),
                "long_description": filter_academic_boilerplate(r.get("long_description") or ""),
                "ratings": [],
            }
            if isinstance(r.get("ratings"), list):
                for tier in r["ratings"]:
                    criteria["ratings"].append({
                        "points": tier.get("points", 0),
                        "description": tier.get("description", ""),
                    })
            formatted_rubric.append(criteria)

    due_iso, _ = _normalize_iso(matched.due_at)
    sub_iso, _ = _normalize_iso(matched.submitted_at)
    grad_iso, _ = _normalize_iso(matched.graded_at)

    payload = {
        "id": str(matched.id),
        "title": matched.title,
        "course": matched.course or "University Subject",
        "due_at": due_iso,
        "status": matched.status,
        "submission_status": matched.submission_status,
        "is_graded": bool(matched.is_graded or (matched.score is not None)),
        "score": matched.score,
        "points_possible": matched.points_possible,
        "grade": matched.grade,
        "grading_type": matched.grading_type,
        "submitted_at": sub_iso,
        "graded_at": grad_iso,
        "url": matched.url,
        "instructions": clean_desc,
        "ed_lesson_file": ed_lesson_file,
        "ed_lesson_id": ed_lesson_id,
        "specification_source": "EdStem Lesson (Authoritative Written Specification)" if ed_lesson_file else "Canvas LMS",
        "rubric": formatted_rubric,
        "submission_comments": matched.submission_comments or [],
    }
    source_id = str(getattr(matched, "source_id", "") or "")
    source_match = re.search(r"assignment:(\d+)", source_id)
    if source_match:
        canvas_assignment_id = source_match.group(1)
    else:
        url_match = re.search(r"/assignments/(\d+)", matched.url or "")
        canvas_assignment_id = url_match.group(1) if url_match else ""
    try:
        from unisync.mcp.egress import scrub
        from unisync.mcp.submission_archive import load_gradescope_feedback, load_submission_archive

        archive = load_submission_archive(
            user_root=user_root,
            course=matched.course or "",
            canvas_url=matched.url or "",
            assignment_id=canvas_assignment_id,
            title=matched.title or "",
        )
        gradescope = load_gradescope_feedback(
            user_root=user_root,
            course=matched.course or "",
            assignment=matched.title or canvas_assignment_id,
            canvas_assignment_id=canvas_assignment_id,
        )
        payload["submission_archive"] = archive if archive.get("found") else None
        payload["gradescope"] = gradescope if gradescope.get("found") else None
        return json.dumps(scrub(payload), indent=2)
    except Exception:
        logger.debug("Assignment archive merge failed")
        return json.dumps(payload, indent=2)


@mcp.tool()
def unisync_get_gradescope_feedback(course: str, assignment: str) -> str:
    """Return the signed-in student's archived Gradescope score, rubric items, and marker comments.

    Args:
        course: Course code, Canvas course id, or course folder (for example 'COMP10001' or '241246').
        assignment: Assignment title, Canvas assignment id, or Gradescope assignment id.

    Returns:
        Structured feedback already stored on disk. This does not contact Gradescope.
    """
    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")
    from unisync.mcp.egress import scrub_json
    from unisync.mcp.submission_archive import load_gradescope_feedback

    try:
        payload = load_gradescope_feedback(user_root=user_root, course=course, assignment=assignment)
    except Exception as exc:
        payload = {"found": False, "error": type(exc).__name__}
    return scrub_json(payload)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 3: Course Materials Search
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_search_course_materials(
    query: str,
    course: str | None = None,
    limit: int = 10,
    search_content: bool = True,
) -> str:
    """Search downloaded course slides, PDF handouts, tutorial worksheets, and module files.

    Supports filename matching, academic abbreviations (e.g. 'Assignment 1' matches 'A1.1_Template.docx'),
    and full-text content search inside lecture slide decks and PDFs.

    Args:
        query: Keyword or phrase to search (e.g. 'week 3', 'A1.2', 'word count', 'lecture slides', 'sample exam').
        course: Optional course filter (e.g. 'COMP10001', 'INFO10003').
        limit: Maximum number of files to return (default 10).
        search_content: If True, searches text inside slides and documents when filename matches are sparse.

    Returns:
        List of matching files with filename, subject, folder path, size, file type, and matching text excerpts.
    """
    try:
        from unisync.core.download_registry import DownloadRegistry
        reg = DownloadRegistry.get_instance()
        items = list(reg._items.values())
    except Exception:
        items = []

    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")

    # 1. Abbreviation & Academic Synonym Expansion
    q_clean = query.lower().strip()
    raw_terms = [w for w in re.split(r"[\s_\-]+", q_clean) if w]
    expanded_terms = set(raw_terms)

    # A1.2 <-> Assignment 1.2
    m_a = re.search(r"\b(?:assignment|a)\s*([0-9]+(?:\.[0-9]+)?)\b", q_clean)
    if m_a:
        num = m_a.group(1)
        expanded_terms.update([f"a{num}", f"assignment{num}", f"a_{num}", f"a-{num}", f"a{num.replace('.', '')}"])
    # Week 2 <-> W2 / Wk2
    m_w = re.search(r"\b(?:week|wk|w)\s*([0-9]+)\b", q_clean)
    if m_w:
        num = m_w.group(1)
        expanded_terms.update([f"week{num}", f"wk{num}", f"w{num}", f"week{int(num):02d}", f"wk{int(num):02d}"])
    # Lecture 2 <-> Lec02 / L2
    m_l = re.search(r"\b(?:lecture|lec|l)\s*([0-9]+)\b", q_clean)
    if m_l:
        num = m_l.group(1)
        expanded_terms.update([f"lecture{num}", f"lec{num}", f"lecture{int(num):02d}", f"lec{int(num):02d}"])
    # Tutorial 2 <-> Tut02
    m_t = re.search(r"\b(?:tutorial|tut|t)\s*([0-9]+)\b", q_clean)
    if m_t:
        num = m_t.group(1)
        expanded_terms.update([f"tutorial{num}", f"tut{num}", f"tutorial{int(num):02d}", f"tut{int(num):02d}"])

    seen_paths = set()
    scored_matches: list[tuple[int, dict[str, Any]]] = []

    # 2. Match registry items
    for item in items:
        c_name = item.course_name or (item.metadata.get("course_name") if item.metadata else "") or ""
        if course and course.lower() not in c_name.lower():
            continue

        filename = Path(item.local_filepath).name if item.local_filepath else item.title
        local_p = item.local_filepath or ""
        folder = str(Path(local_p).parent) if local_p else ""
        file_ext = Path(filename).suffix.lstrip(".") if filename else ""
        searchable = f"{item.title} {filename} {local_p} {folder} {c_name}".lower()

        score = 0
        if q_clean in searchable:
            score += 100
        if any(term in searchable for term in expanded_terms):
            score += 40
        if all(term in searchable for term in raw_terms):
            score += 50
        score += sum(10 for term in raw_terms if term in searchable)

        if score >= 30:
            seen_paths.add(str(Path(local_p).resolve()) if local_p else filename)
            row = {
                "filename": filename,
                "title": item.title,
                "course": c_name or "University Subject",
                "folder": folder,
                "local_filepath": local_p,
                "size_bytes": item.size_bytes,
                "file_type": file_ext or item.content_type,
                "match_type": "filename_or_metadata",
                "downloaded_at": item.downloaded_at,
            }
            if (item.platform or "").lower() == "echo360":
                row["direct_link"] = item.direct_link
                row["recording_url"] = item.recording_url
                row["lesson_id"] = item.lesson_id
                row["recording_url_source"] = str((item.metadata or {}).get("recording_url_source") or "")
                row["recording_url_kind"] = item.recording_url_kind or str(
                    (item.metadata or {}).get("recording_url_kind") or ""
                )
            scored_matches.append((score, row))

    # 3. Direct disk crawl & exact-content grep across data/courses
    courses_dir = user_root / "courses"
    if courses_dir.exists():
        # Compile safe regex / substring matcher for exact code and text grep
        try:
            content_pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            content_pattern = re.compile(re.escape(query), re.IGNORECASE)

        if course:
            c_filter = course.lower().strip()
            target_dirs = [d for d in courses_dir.iterdir() if d.is_dir() and c_filter in d.name.lower()]
            if not target_dirs:
                target_dirs = [courses_dir]
        else:
            target_dirs = [courses_dir]

        for t_dir in target_dirs:
            for f in t_dir.glob("**/*"):
                if not f.is_file():
                    continue
                ext = f.suffix.lower()
                if ext not in (".pdf", ".docx", ".pptx", ".txt", ".md", ".py", ".json", ".sh", ".r", ".java", ".c", ".cpp"):
                    continue
                resolved_p = str(f.resolve())
                if resolved_p in seen_paths:
                    continue

                rel_p = str(f.relative_to(user_root)) if f.is_relative_to(user_root) else str(f)
                rel_courses = f.relative_to(courses_dir) if f.is_relative_to(courses_dir) else None
                c_name = rel_courses.parts[0] if rel_courses and rel_courses.parts else f.parent.name

                # (A) Filename / Path Matching
                searchable = f"{f.name} {rel_p} {c_name}".lower()
                score = 0
                if q_clean in searchable:
                    score += 100
                if any(term in searchable for term in expanded_terms):
                    score += 40
                if all(term in searchable for term in raw_terms):
                    score += 50
                score += sum(10 for term in raw_terms if term in searchable)

                # (B) Literal / Regex Content Grep for Code and Text Files
                content_match = None
                line_no = None
                if search_content and ext in (".py", ".md", ".txt", ".json", ".sh", ".r", ".java", ".c", ".cpp"):
                    try:
                        if f.stat().st_size <= 2 * 1024 * 1024:  # scan files up to 2MB
                            text_content = f.read_text(encoding="utf-8", errors="ignore")
                            if content_pattern.search(text_content):
                                # Locate matching line and line number
                                for idx, line in enumerate(text_content.splitlines()):
                                    if content_pattern.search(line):
                                        clean_line = filter_academic_boilerplate(line.strip())
                                        if clean_line:
                                            content_match = clean_line[:350]
                                            line_no = idx + 1
                                            # Boost score for exact content / code match
                                            score = max(score, 140 if ext == ".py" else 120)
                                            break
                    except Exception:
                        pass

                if score >= 30:
                    seen_paths.add(resolved_p)
                    match_data = {
                        "filename": f.name,
                        "title": f.name,
                        "course": c_name,
                        "folder": str(f.parent),
                        "local_filepath": rel_p,
                        "size_bytes": f.stat().st_size,
                        "file_type": ext.lstrip("."),
                        "match_type": "exact_content_match" if content_match else "filesystem_scan",
                    }
                    if content_match:
                        match_data["line_number"] = line_no
                        match_data["matched_excerpt"] = content_match
                    scored_matches.append((score, match_data))
                if len(scored_matches) >= 30:
                    break

    # 4. Full-text Content Search Fallback via Knowledge Base (only when exact hits < limit or conceptual query)
    has_exact_content = any(m[1].get("match_type") == "exact_content_match" for m in scored_matches)
    needs_kb = search_content and (
        (len(scored_matches) < limit and not has_exact_content)
        or any(w in q_clean for w in ("count", "word", "overview", "criteria", "rubric", "rule", "instruction"))
    )
    if needs_kb:
        try:
            from unisync.services.kb import get_kb_service
            kb = get_kb_service()
            kb_results = kb.search(query=query, course=course, limit=max(3, limit - len(scored_matches)))
            for kr in kb_results:
                f_name = kr.get("file_name") or Path(kr.get("file_path", "")).name
                f_path = kr.get("file_path") or f_name
                resolved_p = str(Path(f_path).resolve()) if f_path else f_name
                snippet = filter_academic_boilerplate((kr.get("text") or "").strip())
                if not snippet:
                    continue

                # Highlight or clean up section header
                sec_hdr = kr.get("section_title") or ""
                scored_matches.append((int(kr.get("score", 0.5) * 80), {
                    "filename": f_name,
                    "title": f_name,
                    "course": kr.get("course") or course or "University Subject",
                    "folder": str(Path(f_path).parent) if f_path else "",
                    "local_filepath": f_path,
                    "size_bytes": None,
                    "file_type": Path(f_name).suffix.lstrip(".") or "document",
                    "match_type": "full_text_content",
                    "section": sec_hdr,
                    "matched_excerpt": snippet[:350],
                }))
        except Exception as _kb_err:
            logger.debug("Content search fallback skipped: %s", _kb_err)

    # Sort descending by match score
    scored_matches.sort(key=lambda x: x[0], reverse=True)
    deduped = []
    seen_final = set()
    for _, item in scored_matches:
        k = item["filename"] + (item.get("matched_excerpt") or "")[:50]
        if k not in seen_final:
            seen_final.add(k)
            deduped.append(item)

    from unisync.mcp.egress import scrub_lecture_payload

    return json.dumps(scrub_lecture_payload({
        "query": query,
        "total_found": len(deduped),
        "files": deduped[:limit],
    }), indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 4: Read Course File Content
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_read_course_file(file_path: str, max_chars: int = 8000) -> str:
    """Read and extract text from a downloaded course document (PDF slides, Word docx, PowerPoint pptx, notes).

    Args:
        file_path: Relative or absolute path to the file (as returned by unisync_search_course_materials).
        max_chars: Maximum characters to return to avoid overflowing agent context (default 8000).

    Returns:
        Extracted text content from the file with metadata.
    """
    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")

    target = Path(file_path)
    if not target.is_absolute():
        if (user_root / file_path).exists():
            target = user_root / file_path
        elif Path(file_path).exists():
            target = Path(file_path)
        elif (user_root / "workspace" / file_path).exists():
            target = user_root / "workspace" / file_path
        elif (user_root / "courses" / file_path).exists():
            target = user_root / "courses" / file_path
        else:
            fname = Path(file_path).name
            found = list((user_root / "courses").glob(f"**/{fname}"))
            if found:
                target = found[0]
            else:
                target = user_root / file_path

    if not target.exists() or not target.is_file():
        return json.dumps({"error": f"File not found on disk: {file_path}"})

    suffix = target.suffix.lower()

    # 1. PDF Document Extraction
    if suffix == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(target))
            extracted = []
            for i, page in enumerate(reader.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    extracted.append(f"--- Page {i + 1} ---\n{txt.strip()}")
                if sum(len(p) for p in extracted) >= max_chars:
                    break
            full_text = filter_academic_boilerplate("\n\n".join(extracted))
            if not full_text:
                full_text = "(PDF contains scanned images or filtered boilerplate)"
            truncated = len(full_text) > max_chars
            return json.dumps({
                "file": target.name,
                "file_type": "pdf",
                "total_pages": len(reader.pages),
                "truncated": truncated,
                "content": full_text[:max_chars],
            }, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to extract PDF text: {exc}"})

    # 2. Word Document (.docx) Extraction
    if suffix == ".docx":
        try:
            import docx
            doc = docx.Document(str(target))
            lines: list[str] = []
            for p in doc.paragraphs:
                txt = p.text.strip()
                if not txt:
                    continue
                style_name = p.style.name.lower() if p.style else ""
                if "heading 1" in style_name:
                    lines.append(f"# {txt}")
                elif "heading 2" in style_name:
                    lines.append(f"## {txt}")
                elif "heading 3" in style_name:
                    lines.append(f"### {txt}")
                elif "title" in style_name:
                    lines.append(f"# {txt}")
                else:
                    lines.append(txt)
            for t in doc.tables:
                # Omit AI declaration tables/cover page boxes from template extraction
                table_text = " ".join(c.text.strip().lower() for row in t.rows for c in row.cells)
                if any(kw in table_text for kw in ("ai use declaration", "ai usage declaration", "generative ai declaration", "declaration of originality")):
                    continue
                for row in t.rows:
                    row_txt = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                    if row_txt:
                        lines.append(row_txt)
            full_text = filter_academic_boilerplate("\n\n".join(lines))
            if not full_text:
                full_text = "(Document appears to be empty or image-only)"
            truncated = len(full_text) > max_chars
            return json.dumps({
                "file": target.name,
                "file_type": "docx",
                "paragraphs_count": len(doc.paragraphs),
                "tables_count": len(doc.tables),
                "truncated": truncated,
                "content": full_text[:max_chars],
            }, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to extract DOCX text: {exc}"})

    # 3. PowerPoint (.pptx) Extraction
    if suffix == ".pptx":
        try:
            import pptx
            prs = pptx.Presentation(str(target))
            slides_text = []
            for i, slide in enumerate(prs.slides):
                slide_txts = [shape.text.strip() for shape in slide.shapes if shape.has_text_frame and shape.text.strip()]
                if slide_txts:
                    slides_text.append(f"--- Slide {i + 1} ---\n" + "\n".join(slide_txts))
                if sum(len(s) for s in slides_text) >= max_chars:
                    break
            full_text = filter_academic_boilerplate("\n\n".join(slides_text))
            if not full_text:
                full_text = "(Presentation contains shapes/images without text)"
            truncated = len(full_text) > max_chars
            return json.dumps({
                "file": target.name,
                "file_type": "pptx",
                "total_slides": len(prs.slides),
                "truncated": truncated,
                "content": full_text[:max_chars],
            }, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to extract PPTX text: {exc}"})

    # 4. Plain Text / Markdown / Code / Subtitle Files
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
        clean_text = filter_academic_boilerplate(raw)
        truncated = len(clean_text) > max_chars
        return json.dumps({
            "file": target.name,
            "file_type": suffix.lstrip(".") or "text",
            "truncated": truncated,
            "content": clean_text[:max_chars],
        }, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"Failed to read file: {exc}"})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 5: Search Lecture Transcripts
# ─────────────────────────────────────────────────────────────────────────────

def _lecture_recording(vtt_path: Path) -> dict[str, str]:
    """Watch page for one cached subtitle, after the egress allowlist."""
    from unisync.mcp.egress import scrub_recording_url
    from unisync.services.sync.echo360_links import recording_fields

    try:
        from unisync.core.path_service import get_path_service
        data_root = get_path_service().get_user_root()
    except Exception:
        data_root = vtt_path.parent.parent.parent
    fields = recording_fields(vtt_path.stem, data_root=data_root)
    fields["recording_url"] = scrub_recording_url(fields.get("recording_url"), allow_canvas=True)
    return fields


@mcp.tool()
def unisync_search_lectures(
    query: str,
    course: str | None = None,
    limit: int = 5,
) -> str:
    """Search recorded lecture transcripts across all subjects with timestamps and context cues.

    Args:
        query: Concept, topic, or keyword spoken by the professor (e.g. 'complexity', 'dijkstra', 'statute').
        course: Optional course filter (e.g. 'COMP10001', 'Principles of Business Law').
        limit: Maximum number of matching lectures to return (default 5).

    Returns:
        Matching lecture recordings with lecture date, Echo360 watch URL, and timestamp cues.
    """
    try:
        from unisync.core.path_service import get_path_service
        vtt_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
    except Exception:
        vtt_dir = Path("data/cache/echo360_subtitles")

    if not vtt_dir.exists():
        return json.dumps({
            "query": query,
            "total_lectures_matched": 0,
            "results": [],
            "note": "No lecture transcript cache exists yet.",
        })

    vtt_files = list(vtt_dir.glob("*.vtt"))
    if not vtt_files:
        return json.dumps({
            "query": query,
            "total_lectures_matched": 0,
            "results": [],
            "note": "No downloaded subtitle files found.",
        })

    terms = [t.lower() for t in query.split() if t.strip()]
    matches = []

    for f in vtt_files:
        # Extract date from filename: e.g. ..._2026-08-25T17_05_00...
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        lec_date = date_match.group(1) if date_match else "Unknown Date"

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Parse WebVTT blocks: timestamp --> timestamp \n text
        cues = re.findall(
            r"(\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?)\s*-->\s*(\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?)\s*\n([\s\S]*?)(?=\n\n|\Z)",
            content,
        )

        matched_cues = []
        for start_ts, end_ts, cue_text in cues:
            clean_cue = re.sub(r"<[^>]+>", "", cue_text).strip()
            if not clean_cue or is_academic_boilerplate_cue(clean_cue):
                continue
            lower_cue = clean_cue.lower()
            if all(term in lower_cue for term in terms):
                matched_cues.append({
                    "timestamp": start_ts.split(".")[0],
                    "text": clean_cue,
                })
                if len(matched_cues) >= 4:
                    break

        if matched_cues:
            recording = _lecture_recording(f)
            for cue in matched_cues:
                # Echo360 classroom URLs have no start-time parameter.
                # The cue timestamp stays beside the recording link.
                cue["recording_url"] = recording["recording_url"]
            matches.append({
                "lecture_date": lec_date,
                "file_id": f.stem,
                "lesson_id": recording["lesson_id"],
                "recording_url": recording["recording_url"],
                "recording_url_source": recording["recording_url_source"],
                "recording_url_kind": recording.get("recording_url_kind", ""),
                "hit_count": len(matched_cues),
                "cues": matched_cues,
            })

    # Sort most recent lectures first, pushing lectures with unknown dates to the end
    matches.sort(key=lambda x: x["lecture_date"] if x["lecture_date"] != "Unknown Date" else "", reverse=True)

    from unisync.mcp.egress import scrub_lecture_payload

    return json.dumps(scrub_lecture_payload({
        "query": query,
        "total_lectures_matched": len(matches),
        "results": matches[:limit],
    }), indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 6: Full Lecture Transcript Retrieval
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_get_lecture_transcript(
    lecture_date_or_id: str,
    max_chars: int = 12000,
) -> str:
    """Retrieve the full spoken transcript of a specific recorded university lecture.

    Args:
        lecture_date_or_id: Date of the lecture ('2026-08-25') or subtitle file ID.
        max_chars: Maximum characters to return (default 12000).

    Returns:
        Structured transcript text grouped into timestamped paragraphs, plus the Echo360 watch URL.
    """
    try:
        from unisync.core.path_service import get_path_service
        vtt_dir = get_path_service().get_user_root() / "cache" / "echo360_subtitles"
    except Exception:
        vtt_dir = Path("data/cache/echo360_subtitles")

    if not vtt_dir.exists():
        return json.dumps({"error": "No subtitles directory found."})

    target = lecture_date_or_id.strip()
    candidates = [f for f in vtt_dir.glob("*.vtt") if target in f.name]
    candidates.sort(key=lambda x: x.stat().st_size, reverse=True)
    matched_file = candidates[0] if candidates else None

    if matched_file is None:
        return json.dumps({
            "error": f"No transcript found for '{lecture_date_or_id}'.",
            "suggestion": "Call unisync_search_lectures to discover matching lecture dates.",
        })

    try:
        raw = matched_file.read_text(encoding="utf-8", errors="replace")
        # Sanitize VTT content dynamically (in case on-disk file was uncleaned)
        raw = filter_vtt_content(raw)
        # Parse into timestamped lines
        lines = []
        for match in re.finditer(r"(\d{2}:\d{2}(?::\d{2})?(?:\.\d{3})?)\s*-->\s*[^\n]+\n([\s\S]*?)(?=\n\n|\Z)", raw):
            start = match.group(1).split(".")[0]
            text = match.group(2)
            clean = re.sub(r"<[^>]+>", "", text).strip()
            if clean and not is_academic_boilerplate_cue(clean):
                lines.append(f"[{start}] {clean}")

        joined = filter_academic_boilerplate("\n".join(lines))
        truncated = len(joined) > max_chars
        recording = _lecture_recording(matched_file)
        from unisync.mcp.egress import scrub_lecture_payload

        return json.dumps(scrub_lecture_payload({
            "file": matched_file.name,
            "lesson_id": recording["lesson_id"],
            "recording_url": recording["recording_url"],
            "recording_url_source": recording["recording_url_source"],
            "recording_url_kind": recording.get("recording_url_kind", ""),
            "truncated": truncated,
            "transcript": joined[:max_chars],
        }), indent=2)
    except Exception as exc:
        return json.dumps({"error": f"Failed reading transcript: {exc}"})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 7: Subject Announcements & Email Updates
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_search_announcements(
    query: str,
    course: str | None = None,
    limit: int = 10,
) -> str:
    """Search university subject announcements, Canvas notices, email updates, and instructor posts.

    Searches across Canvas LMS announcements, course update pages, and incoming university emails.

    Args:
        query: Keywords or phrases to search (e.g. 'Assignment 1.2', 'exam release', 'quiz', 'word count', 'consultation').
        course: Optional course name/code filter (e.g. 'BLAW10001', 'INFO10003', 'COMP10001').
        limit: Maximum number of announcements to return (default 10).

    Returns:
        List of matching announcements with date, sender, course, subject, and relevant text snippet.
    """
    try:
        from unisync.core.path_service import get_path_service
        user_root = get_path_service().get_user_root()
    except Exception:
        user_root = Path("data")

    q_clean = query.lower().strip()
    raw_terms = [w for w in re.split(r"[\s_\-]+", q_clean) if w]
    expanded_terms = set(raw_terms)

    # Academic abbreviations: e.g. Assignment 1.2 <-> A1.2
    m_a = re.search(r"\b(?:assignment|a)\s*([0-9]+(?:\.[0-9]+)?)\b", q_clean)
    if m_a:
        num = m_a.group(1)
        expanded_terms.update([f"a{num}", f"assignment{num}", f"a_{num}", f"a-{num}", f"a{num.replace('.', '')}"])

    scored: list[tuple[int, dict[str, Any]]] = []
    seen_ids = set()

    def _extract_context_snippet(full_text: str, terms: list[str], max_len: int = 350) -> str:
        """Find the earliest occurrence of any term and return surrounding text window."""
        lower_txt = full_text.lower()
        earliest_idx = -1
        for t in terms:
            idx = lower_txt.find(t)
            if idx != -1 and (earliest_idx == -1 or idx < earliest_idx):
                earliest_idx = idx
        if earliest_idx == -1:
            return full_text[:max_len].strip()
        start = max(0, earliest_idx - 60)
        end = min(len(full_text), earliest_idx + max_len - 60)
        snippet = full_text[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(full_text):
            snippet = snippet + "..."
        return snippet

    # 1. Search Canvas Course Announcements stored in data/courses/*/pages/
    courses_dir = user_root / "courses"
    if courses_dir.exists():
        for page_file in courses_dir.glob("*/pages/*.md"):
            try:
                c_name = page_file.parent.parent.name
                if course and course.lower() not in c_name.lower():
                    continue

                raw = page_file.read_text(encoding="utf-8", errors="replace")
                raw = filter_academic_boilerplate(raw)
                if not raw.strip():
                    continue
                lines = raw.splitlines()
                # Extract title from first markdown header
                subject = page_file.stem
                for line in lines[:5]:
                    if line.startswith("#"):
                        subject = line.lstrip("#").strip()
                        break

                searchable = f"{subject} {page_file.name} {c_name} {raw}".lower()

                score = 0
                if q_clean in searchable:
                    score += 100
                if any(term in searchable for term in expanded_terms):
                    score += 40
                if all(term in searchable for term in raw_terms):
                    score += 40
                score += sum(10 for term in raw_terms if term in searchable)

                is_announcement = "announcement" in page_file.name.lower() or "announcement" in subject.lower()
                if is_announcement:
                    score += 25

                if score >= 35:
                    file_id = f"canvas_page_{page_file.stem}"
                    if file_id not in seen_ids:
                        seen_ids.add(file_id)
                        snippet = _extract_context_snippet(raw, list(expanded_terms))
                        scored.append((score, {
                            "id": file_id,
                            "date": datetime.fromtimestamp(page_file.stat().st_mtime, tz=timezone.utc).isoformat()[:10],
                            "sender": f"{c_name} (Canvas Announcement)",
                            "course": c_name,
                            "subject": subject,
                            "snippet": snippet,
                            "source": "canvas_announcement",
                        }))
            except Exception as _p_err:
                logger.debug("Error scanning page file %s: %s", page_file.name, _p_err)

    # 2. Search Email Monitor Store
    try:
        from unisync.services.email_monitor.store import EmailMonitorStore
        store = EmailMonitorStore()
        entries = store.get_ledger_entries(limit=250)
        for entry in entries:
            subject = entry.subject or ""
            searchable = f"{subject} {entry.ledger_id}".lower()
            sender_display = "University Email"
            full_body = ""

            if entry.archived_path and Path(entry.archived_path).exists():
                try:
                    p = Path(entry.archived_path)
                    if p.is_dir():
                        body_file = p / "body.txt"
                        meta_file = p / "metadata.json"
                        if body_file.exists():
                            full_body = body_file.read_text(encoding="utf-8", errors="replace")
                        if meta_file.exists():
                            try:
                                m_data = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
                                sender_display = m_data.get("from") or sender_display
                                if not subject:
                                    subject = m_data.get("subject", "")
                            except Exception:
                                pass
                    else:
                        full_body = p.read_text(encoding="utf-8", errors="replace")
                    searchable += f" {full_body.lower()} {sender_display.lower()}"
                except Exception:
                    pass

            if course and course.lower() not in searchable:
                continue

            score = 0
            if q_clean in searchable:
                score += 100
            if any(term in searchable for term in expanded_terms):
                score += 40
            if all(term in searchable for term in raw_terms):
                score += 40
            score += sum(10 for term in raw_terms if term in searchable)

            if score >= 35 and entry.ledger_id not in seen_ids:
                seen_ids.add(entry.ledger_id)
                snippet = _extract_context_snippet(full_body or subject, list(expanded_terms))
                scored.append((score, {
                    "id": entry.ledger_id,
                    "date": datetime.fromtimestamp(entry.processed_at, tz=timezone.utc).isoformat() if entry.processed_at else "",
                    "sender": sender_display,
                    "course": course or "University Notice",
                    "subject": subject,
                    "snippet": snippet or f"Processed email ({entry.status})",
                    "source": "email",
                }))
    except Exception:
        pass

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item for _, item in scored][:limit]

    return json.dumps({
        "query": query,
        "total_matched": len(scored),
        "announcements": results,
        "note": "No matching announcements or notifications found." if not results else "",
    }, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 8: Live UniSync Status & Sync Health
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_get_sync_status() -> str:
    """Check the real-time status of UniSync, connection to LMS, last update time, and pending lectures.

    Returns:
        Status object with current state, latest lecture date, total assets, and pending lecture count.
    """
    try:
        from unisync.api.sync_engine import get_computed_sync_status
        status = get_computed_sync_status()
        return json.dumps(status, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"Failed to get sync status: {exc}"})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 9: Manage Personal Study Tasks & Todos
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_manage_todo(
    action: Literal["create", "complete", "delete"],
    title: str,
    due_at: str | None = None,
    course: str | None = None,
    notes: str | None = None,
    todo_id: str | int | None = None,
) -> str:
    """Create, mark complete, or remove personal study milestones and academic tasks.

    Args:
        action: One of 'create', 'complete', or 'delete'.
        title: Title of the study task.
        due_at: Optional ISO 8601 deadline (e.g. '2026-09-12T23:59:00Z').
        course: Optional subject name or code (e.g. 'COMP10001').
        notes: Optional context or study notes.
        todo_id: Identifier of the task (required for 'complete' and 'delete' if title is not specified).

    Returns:
        Result confirmation object.
    """
    try:
        from unisync.services.todos import get_todo_service
        svc = get_todo_service()
    except Exception as exc:
        return json.dumps({"error": f"Failed to initialize todo service: {exc}"})

    if action == "create":
        item = svc.create_item({
            "title": title,
            "due_at": due_at,
            "course": course or "Personal Study",
            "notes": notes or "",
            "source": "manual",
            "priority": "normal",
        })
        return json.dumps({"status": "created", "todo_id": item.id, "title": item.title})

    elif action == "complete":
        target_id = str(todo_id) if todo_id is not None else None
        if not target_id:
            # Look up by title
            for t in svc.list_items():
                if t.title.lower() == title.lower():
                    target_id = str(t.id)
                    break
        if not target_id:
            return json.dumps({"error": "Task not found to mark complete. Please specify todo_id or exact title."})

        updated = svc.update_item(target_id, {"status": "done"})
        return json.dumps({"status": "completed", "todo_id": target_id, "title": updated.title if updated else title})

    elif action == "delete":
        target_id = str(todo_id) if todo_id is not None else None
        if not target_id:
            for t in svc.list_items():
                if t.title.lower() == title.lower():
                    target_id = str(t.id)
                    break
        if not target_id:
            return json.dumps({"error": "Task not found to delete. Please specify todo_id or exact title."})

        deleted = svc.delete_item(target_id)
        return json.dumps({"status": "deleted", "todo_id": target_id, "success": deleted})

    return json.dumps({"error": f"Unsupported action '{action}'."})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 10: Semantic Knowledge Base Query
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_query_knowledge_base(
    query: str,
    course: str | None = None,
    limit: int = 5,
) -> str:
    """Perform hybrid semantic vector & keyword search across university course materials.

    Searches across downloaded lecture slides, readings, Canvas pages (including BLAW syllabus
    and case studies), Echo360 lecture transcripts with timestamp windows, and official university notices.

    Args:
        query: Conceptual question or search phrase (e.g. 'Ermogenous intention to create legal relations',
               'design thinking crazy 8s', 'multiple choice question tips', 'binary search trees').
        course: Optional course filter (e.g. 'BLAW10001', 'CCDP10003', 'COMP10001', 'INFO10003').
        limit: Maximum number of relevant passages to retrieve (default 5).

    Returns:
        JSON string containing ranked passages with file sources, course codes, relevance scores,
        section headings, and timestamps for lecture transcripts.
    """
    try:
        from unisync.services.kb import get_kb_service
        with redirect_stdout(sys.stderr):
            kb = get_kb_service()
            results = kb.search(query=query, course=course, limit=limit)
        return json.dumps(
            {
                "query": query,
                "course_filter": course,
                "count": len(results),
                "results": results,
            },
            indent=2,
        )
    except Exception as exc:
        logger.exception("Error executing knowledge base query")
        return json.dumps({"error": f"Failed to query knowledge base: {exc}"})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 11: Reindex Knowledge Base
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_reindex_knowledge_base(force: bool = False) -> str:
    """Trigger indexing of university materials, slides, lecture transcripts, and Canvas pages.

    Args:
        force: If True, reindexes all files from scratch. If False, incrementally indexes new/modified files.

    Returns:
        JSON string with summary of indexed files, chunks, and database status.
    """
    try:
        from unisync.services.kb import get_kb_service
        with redirect_stdout(sys.stderr):
            kb = get_kb_service()
            stats = kb.index_all(force=force)
        return json.dumps({"status": "success", "indexing_summary": stats}, indent=2)
    except Exception as exc:
        logger.exception("Error reindexing knowledge base")
        return json.dumps({"error": f"Failed to reindex knowledge base: {exc}"})


# ─────────────────────────────────────────────────────────────────────────────
# Tool 12: Unified Universal Search
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool()
def unisync_search(
    query: str,
    course: str | None = None,
    limit: int = 5,
) -> str:
    """Universal search across all university context: assignments/deadlines, lecture transcripts, course materials/slides, announcements, and emails.

    Use this as the primary first-look search tool when unsure where information resides (e.g. finding mentions of 'word count', 'Assignment 1.2', 'rubric', 'group contract', or a specific lecture topic).

    Args:
        query: Concept, topic, assignment name, or keyword to search everywhere.
        course: Optional course filter (e.g. 'COMP10001', 'INFO10003', 'BLAW10001').
        limit: Maximum number of matches to return per category (default 5).

    Returns:
        Aggregated overview partitioned into:
        - assignments: Deadlines, rubrics, points, and submission statuses
        - materials: Downloaded slides, PDF worksheets, and document files
        - lectures: Recorded lecture transcript cues with timestamps
        - announcements: Canvas announcements and instructor emails
        - semantic_passages: High-confidence semantic matches across all indexed course files
    """
    q_clean = query.lower().strip()
    terms = [w for w in re.split(r"[\s_\-]+", q_clean) if w]

    # 1. Assignments and Deadlines
    matched_assignments: list[dict[str, Any]] = []
    try:
        from unisync.services.todos import get_todo_service
        svc = get_todo_service()
        items = svc.list_items()
        for t in items:
            if course and course.lower() not in (t.course or "").lower():
                continue
            t_title = (t.title or "").lower()
            t_notes = (t.notes or "").lower()
            t_url = (t.url or "").lower()
            t_full = f"{t_title} {t_notes} {t_url}"
            if q_clean in t_full or (terms and all(w in t_full for w in terms)):
                due_iso, _ = _normalize_iso(t.due_at)
                matched_assignments.append({
                    "id": str(t.id),
                    "title": t.title,
                    "course": t.course or "University Subject",
                    "due_at": due_iso,
                    "countdown": _format_countdown(t.due_at, t.status == "done"),
                    "status": t.status,
                    "points_possible": t.points_possible,
                    "score": t.score,
                    "url": t.url,
                })
                if len(matched_assignments) >= limit:
                    break
    except Exception as _e:
        logger.debug("unisync_search assignment lookup error: %s", _e)

    # 2. Course Materials & Slides
    matched_materials: list[dict[str, Any]] = []
    try:
        raw_mat = json.loads(unisync_search_course_materials(query=query, course=course, limit=limit, search_content=True))
        matched_materials = raw_mat.get("files", [])
    except Exception as _e:
        logger.debug("unisync_search materials lookup error: %s", _e)

    # 3. Spoken Lecture Transcripts
    matched_lectures: list[dict[str, Any]] = []
    try:
        raw_lec = json.loads(unisync_search_lectures(query=query, course=course, limit=limit))
        matched_lectures = raw_lec.get("results", [])
    except Exception as _e:
        logger.debug("unisync_search lectures lookup error: %s", _e)

    # 4. Canvas Announcements & Emails
    matched_announcements: list[dict[str, Any]] = []
    try:
        raw_ann = json.loads(unisync_search_announcements(query=query, course=course, limit=limit))
        matched_announcements = raw_ann.get("announcements", [])
    except Exception as _e:
        logger.debug("unisync_search announcements lookup error: %s", _e)

    # 5. Semantic Passages via Knowledge Base
    matched_passages: list[dict[str, Any]] = []
    try:
        from unisync.services.kb import get_kb_service
        with redirect_stdout(sys.stderr):
            kb = get_kb_service()
            kb_results = kb.search(query=query, course=course, limit=limit)
        for kr in kb_results:
            file_path = str(kr.get("file_path") or "")
            passage = {
                "file": kr.get("file_name") or Path(file_path).name,
                "course": kr.get("course"),
                "section": kr.get("section_title"),
                "timestamp": f"[{kr.get('timestamp_start')} - {kr.get('timestamp_end')}]" if kr.get("timestamp_start") else None,
                "score": kr.get("score"),
                "excerpt": (kr.get("text") or "")[:350].strip(),
            }
            if file_path.endswith(".vtt"):
                passage.update(_lecture_recording(Path(file_path)))
            matched_passages.append(passage)
    except Exception as _e:
        logger.debug("unisync_search kb lookup error: %s", _e)

    total_hits = (
        len(matched_assignments)
        + len(matched_materials)
        + len(matched_lectures)
        + len(matched_announcements)
        + len(matched_passages)
    )

    from unisync.mcp.egress import scrub_lecture_payload

    return json.dumps(scrub_lecture_payload({
        "query": query,
        "course_filter": course,
        "total_results": total_hits,
        "results": {
            "assignments": matched_assignments,
            "materials": matched_materials,
            "lectures": matched_lectures,
            "announcements": matched_announcements,
            "semantic_passages": matched_passages,
        },
    }), indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Tools 13-16: Google NotebookLM Cloud Studio & Grounded Synthesis
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_notebooklm_course(course: str) -> dict[str, Any]:
    """Resolve an enrolled course code to its provisioned Google NotebookLM notebook record."""
    from unisync.services.notebooklm.state import load_sync_state

    state = load_sync_state()
    notebooks = state.get("notebooks", {})
    code_upper = course.strip().upper()
    if code_upper in notebooks:
        return notebooks[code_upper]
    for k, v in notebooks.items():
        if code_upper in k.upper():
            return v
    available = list(notebooks.keys())
    raise ValueError(
        f"No provisioned NotebookLM notebook found for course '{course}'. "
        f"Available enrolled subject notebooks: {available}"
    )


@mcp.tool()
async def unisync_notebooklm_query(
    query: str,
    course: str,
    conversation_id: str | None = None,
) -> str:
    """Query Google NotebookLM for a deep, grounded answer with source citations.

    Synthesizes across the entire subject material (Echo360 lecture transcripts,
    Canvas slides/readings, and EdStem discussion digests) using Google Gemini 1.5 Pro.

    Args:
        query: Conceptual question or prompt (e.g. 'Explain the doctrine of privity with case citations',
               'What were the key takeaways from week 4?', 'Summarize the assignment guidelines').
        course: Academic course code (e.g. 'BLAW10001', 'COMP10001', 'INFO10003', 'CCDP10003').
        conversation_id: Optional conversation ID from a previous turn to continue a multi-turn conversation.

    Returns:
        JSON string containing:
        - answer: Grounded markdown answer with citation markers [1], [2]
        - references: Array of cited source excerpts with citation number, source ID, and text quotes
        - course_code: Course code queried
        - notebook_title: Subject notebook title
        - notebook_url: Direct web link to the subject notebook
        - conversation_id: ID for follow-up turns
    """
    from unisync.services.notebooklm.service import NotebookLMService

    svc = NotebookLMService()
    if not svc.is_authenticated():
        return json.dumps(
            {
                "error": "Google NotebookLM session is not authenticated. Run 'notebooklm login --browser chrome' in your terminal."
            },
            indent=2,
        )

    try:
        nb_record = _resolve_notebooklm_course(course)
        notebook_id = nb_record["notebook_id"]
        from notebooklm.client import NotebookLMClient

        with redirect_stdout(sys.stderr):
            async with NotebookLMClient.from_storage(allow_headless=True) as cl:
                res = await cl.chat.ask(
                    notebook_id=notebook_id,
                    question=query,
                    conversation_id=conversation_id,
                )

        refs = []
        if res.references:
            for r in res.references:
                refs.append({
                    "citation_number": getattr(r, "citation_number", None),
                    "source_id": getattr(r, "source_id", None),
                    "cited_text": getattr(r, "cited_text", None),
                    "score": getattr(r, "score", None),
                })

        return json.dumps(
            {
                "course_code": course.upper(),
                "notebook_title": nb_record.get("notebook_title"),
                "notebook_url": nb_record.get("notebook_url"),
                "answer": res.answer,
                "references": refs,
                "conversation_id": res.conversation_id,
                "turn_number": getattr(res, "turn_number", 1),
            },
            indent=2,
        )
    except Exception as exc:
        logger.exception("Failed querying NotebookLM for %s", course)
        return json.dumps({"error": f"NotebookLM query failed: {exc}"}, indent=2)


@mcp.tool()
def unisync_notebooklm_list_notebooks() -> str:
    """List all provisioned Google NotebookLM subject notebooks, web links, and sync status.

    Returns:
        JSON string with authentication status, list of courses, notebook IDs,
        direct notebook URLs, and count of synced sources.
    """
    try:
        from unisync.services.notebooklm.service import NotebookLMService

        svc = NotebookLMService()
        status = svc.get_sync_status()
        return json.dumps(status, indent=2)
    except Exception as exc:
        logger.exception("Failed listing NotebookLM notebooks")
        return json.dumps({"error": f"Failed listing notebooks: {exc}"}, indent=2)


@mcp.tool()
async def unisync_notebooklm_generate_artifact(
    course: str,
    artifact_type: Literal["study_guide", "audio", "quiz", "flashcards", "briefing"],
    instructions: str | None = None,
) -> str:
    """Generate an AI study artifact in the subject's Google NotebookLM notebook.

    Triggers Google Gemini to synthesize:
    - 'study_guide': Comprehensive exam revision guide with key terms, FAQs, and essay questions.
    - 'audio': Deep Dive Audio Overview (two-host conversational podcast).
    - 'quiz': Practice quiz questions with answer keys.
    - 'flashcards': Study flashcard deck.
    - 'briefing': Executive briefing document.

    Args:
        course: Academic course code (e.g. 'BLAW10001', 'COMP10001', 'INFO10003', 'CCDP10003').
        artifact_type: One of 'study_guide', 'audio', 'quiz', 'flashcards', 'briefing'.
        instructions: Optional custom focus prompt (e.g. 'Focus on contract formation and consideration').

    Returns:
        JSON string with generation task details, status, and artifact URL.
    """
    from unisync.services.notebooklm.service import NotebookLMService

    svc = NotebookLMService()
    if not svc.is_authenticated():
        return json.dumps(
            {
                "error": "Google NotebookLM session is not authenticated. Run 'notebooklm login --browser chrome' in your terminal."
            },
            indent=2,
        )

    try:
        nb_record = _resolve_notebooklm_course(course)
        notebook_id = nb_record["notebook_id"]
        from notebooklm.client import NotebookLMClient

        with redirect_stdout(sys.stderr):
            async with NotebookLMClient.from_storage(allow_headless=True) as cl:
                if artifact_type == "study_guide":
                    res = await cl.artifacts.generate_study_guide(
                        notebook_id=notebook_id,
                        extra_instructions=instructions,
                    )
                elif artifact_type == "audio":
                    res = await cl.artifacts.generate_audio(
                        notebook_id=notebook_id,
                        instructions=instructions,
                    )
                elif artifact_type == "quiz":
                    res = await cl.artifacts.generate_quiz(
                        notebook_id=notebook_id,
                        instructions=instructions,
                    )
                elif artifact_type == "flashcards":
                    res = await cl.artifacts.generate_flashcards(
                        notebook_id=notebook_id,
                        instructions=instructions,
                    )
                elif artifact_type == "briefing":
                    from notebooklm._types.artifacts import ReportFormat

                    res = await cl.artifacts.generate_report(
                        notebook_id=notebook_id,
                        report_format=ReportFormat.BRIEFING_DOC,
                        extra_instructions=instructions,
                    )
                else:
                    return json.dumps({"error": f"Unsupported artifact type: {artifact_type}"})

        return json.dumps(
            {
                "course_code": course.upper(),
                "artifact_type": artifact_type,
                "notebook_id": notebook_id,
                "task_id": getattr(res, "task_id", None),
                "status": getattr(res, "status", "generating"),
                "url": getattr(res, "url", nb_record.get("notebook_url")),
                "message": f"Artifact generation for {artifact_type} started in {course.upper()} notebook.",
            },
            indent=2,
        )
    except Exception as exc:
        logger.exception("Failed generating artifact for %s", course)
        return json.dumps({"error": f"Failed generating artifact: {exc}"}, indent=2)


@mcp.tool()
async def unisync_notebooklm_add_note(
    course: str,
    title: str,
    content: str,
) -> str:
    """Save a study note, solution, or revision summary directly into the subject's Google NotebookLM notebook.

    Args:
        course: Academic course code (e.g. 'BLAW10001', 'COMP10001', 'INFO10003', 'CCDP10003').
        title: Title of the note (e.g. 'Exam Revision Checklist - Week 3').
        content: Markdown formatted content of the note.

    Returns:
        JSON string confirming the note creation with note ID and URL.
    """
    from unisync.services.notebooklm.service import NotebookLMService

    svc = NotebookLMService()
    if not svc.is_authenticated():
        return json.dumps(
            {
                "error": "Google NotebookLM session is not authenticated. Run 'notebooklm login --browser chrome' in your terminal."
            },
            indent=2,
        )

    try:
        nb_record = _resolve_notebooklm_course(course)
        notebook_id = nb_record["notebook_id"]
        from notebooklm.client import NotebookLMClient

        with redirect_stdout(sys.stderr):
            async with NotebookLMClient.from_storage(allow_headless=True) as cl:
                note = await cl.notes.create(
                    notebook_id=notebook_id,
                    title=title,
                    content=content,
                )

        return json.dumps(
            {
                "status": "success",
                "course_code": course.upper(),
                "notebook_id": notebook_id,
                "note_id": getattr(note, "id", None),
                "note_title": title,
                "notebook_url": nb_record.get("notebook_url"),
                "message": f"Note '{title}' saved successfully to {course.upper()} notebook.",
            },
            indent=2,
        )
    except Exception as exc:
        logger.exception("Failed adding note to NotebookLM for %s", course)
        return json.dumps({"error": f"Failed adding note: {exc}"}, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Optional workstation tools: enabled only by explicit local YOLO opt-in.
from unisync.mcp.workstation import register_workstation_tools
register_workstation_tools(mcp)

# Server Execution & Protocol Negotiation Wrapper
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_CLIENT_REQUEST_METHODS = {
    "ping",
    "initialize",
    "completion/complete",
    "logging/setLevel",
    "prompts/get",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "tools/call",
    "tools/list",
}

KNOWN_CLIENT_NOTIFICATION_METHODS = {
    "notifications/cancelled",
    "notifications/progress",
    "notifications/initialized",
    "notifications/roots/list_changed",
}


async def run_stdio_async() -> None:
    """Run FastMCP server over stdio with protocol negotiation fallback.

    Intercepts unsupported extension or modern discovery probes (such as 'server/discover'
    from MCP 2026-07-28 clients) and returns a standard JSON-RPC 2.0 -32601 Method Not Found error
    instead of letting Pydantic ValidationError crash the server taskgroup. This allows modern MCP
    clients (including Antigravity) to probe and cleanly fall back to the standard initialize handshake.
    """
    import anyio
    from mcp.server.stdio import stdio_server
    import mcp.types as types

    async with stdio_server() as (read_stream, write_stream):
        proxy_writer, proxy_reader = anyio.create_memory_object_stream[
            types.JSONRPCMessage | Exception
        ](0)

        async def filter_incoming() -> None:
            try:
                async with proxy_writer:
                    async for message in read_stream:
                        if isinstance(message, types.JSONRPCMessage):
                            if isinstance(message.root, types.JSONRPCRequest):
                                method = message.root.method
                                if method not in KNOWN_CLIENT_REQUEST_METHODS:
                                    logger.info(
                                        "Unsupported MCP request method '%s'; returning -32601 Method Not Found",
                                        method,
                                    )
                                    err_msg = types.JSONRPCMessage(
                                        types.JSONRPCError(
                                            jsonrpc="2.0",
                                            id=message.root.id,
                                            error=types.ErrorData(
                                                code=-32601,
                                                message=f"Method not found: {method}",
                                            ),
                                        )
                                    )
                                    await write_stream.send(err_msg)
                                    continue
                            elif isinstance(message.root, types.JSONRPCNotification):
                                method = message.root.method
                                if method not in KNOWN_CLIENT_NOTIFICATION_METHODS:
                                    logger.debug(
                                        "Ignoring unsupported MCP notification method '%s'",
                                        method,
                                    )
                                    continue
                        await proxy_writer.send(message)
            except anyio.ClosedResourceError:
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(filter_incoming)
            try:
                await mcp._mcp_server.run(
                    proxy_reader,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
            finally:
                tg.cancel_scope.cancel()


# Attach custom run_stdio_async to FastMCP instance
mcp.run_stdio_async = run_stdio_async  # type: ignore[method-assign]


def main() -> None:
    """Run the FastMCP server over standard input/output (stdio)."""
    import anyio

    anyio.run(run_stdio_async)


if __name__ == "__main__":
    main()

