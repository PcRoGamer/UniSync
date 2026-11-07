"""Deterministic local snapshots of Canvas course content.

The native Camoufox sync owns authentication. This module owns the plain,
non-secret artifacts that make Canvas pages, assessment briefs and rubrics
searchable after a sync has completed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from unisync.core.file_io import atomic_write_text


_FILE_ID = re.compile(r"/files/(\d+)(?:[/?#'\"]|$)")
_SAFE_NAME = re.compile(r"[^a-z0-9]+")


def extract_canvas_file_ids(*values: object) -> set[str]:
    """Return Canvas file IDs linked from HTML or plain-text course content."""
    ids = {
        match.group(1)
        for value in values
        if isinstance(value, str)
        for match in _FILE_ID.finditer(value)
    }
    for value in values:
        if not isinstance(value, str):
            continue
        for match in re.finditer(r"/files\?[^\"'\s<>]*?preview=(\d+)", value):
            ids.add(match.group(1))
    return ids



def extract_canvas_page_slugs(course_id: str, *values: object) -> set[str]:
    """Discover same-course page links embedded in a Canvas rich-text page."""
    from urllib.parse import unquote, urljoin, urlparse

    prefix = f"/courses/{course_id}/pages/"
    origin = f"https://canvas.lms.unimelb.edu.au{prefix}"
    slugs: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        for link in BeautifulSoup(value, "html.parser").select("a[href]"):
            resolved = urlparse(urljoin(origin, str(link.get("href") or "")))
            if resolved.hostname != "canvas.lms.unimelb.edu.au":
                continue
            if not resolved.path.startswith(prefix):
                continue
            slug = unquote(resolved.path[len(prefix):])
            if slug and "/" not in slug and slug not in {".", ".."}:
                slugs.add(slug)
    return slugs


def _safe_name(value: object, fallback: str) -> str:
    clean = _SAFE_NAME.sub("-", str(value or "").lower()).strip("-")
    return (clean or fallback)[:110]


def _render_html(value: object) -> str:
    """Keep meaningful links and images while reducing Canvas HTML to Markdown."""
    if not isinstance(value, str) or not value.strip():
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for image in soup.find_all("img"):
        alt = " ".join(image.get("alt", "").split())
        src = image.get("src", "")
        image.replace_with(f"![{alt}]({src})" if src else alt)
    for link in soup.find_all("a"):
        label = " ".join(link.get_text(" ", strip=True).split())
        href = link.get("href", "")
        link.replace_with(f"[{label}]({href})" if label and href else label or href)
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass
    atomic_write_text(path, content)
    return True


def _canvas_url(course_id: str, path: str) -> str:
    return f"https://canvas.lms.unimelb.edu.au/courses/{course_id}{path}"


class CanvasSnapshotWriter:
    """Write idempotent Markdown artifacts for one Canvas course."""

    def __init__(self, course_dir: Path, course_id: str) -> None:
        self.course_id = str(course_id)
        self.pages_dir = Path(course_dir) / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)

    def write_overview(self, course: dict[str, Any]) -> bool:
        name = str(course.get("name") or f"Course {self.course_id}")
        lines = [
            f"# {name}",
            "",
            f"Canvas URL: {_canvas_url(self.course_id, '')}",
        ]
        syllabus = _render_html(course.get("syllabus_body"))
        if syllabus:
            lines.extend(["", "## Syllabus", "", syllabus])
        return _write_if_changed(
            self.pages_dir / f"canvas-{self.course_id}-course-overview.md",
            "\n".join(lines).rstrip() + "\n",
        )

    def write_modules(self, modules: list[dict[str, Any]]) -> bool:
        lines = ["# Modules", "", f"Canvas URL: {_canvas_url(self.course_id, '/modules')}"]
        for module in modules:
            title = str(module.get("name") or "Module")
            lines.extend(["", f"## {title}"])
            for item in module.get("items") or []:
                item_title = str(item.get("title") or "Item")
                item_type = str(item.get("type") or "Item")
                due = str(item.get("due_at") or "")
                suffix = f" — due {due}" if due else ""
                lines.append(f"- {item_title} ({item_type}){suffix}")
        return _write_if_changed(
            self.pages_dir / f"canvas-{self.course_id}-modules.md",
            "\n".join(lines).rstrip() + "\n",
        )

    def write_page(self, page: dict[str, Any]) -> bool:
        slug = _safe_name(page.get("url") or page.get("page_id") or page.get("title"), "page")
        title = str(page.get("title") or slug)
        url = str(page.get("html_url") or _canvas_url(self.course_id, f"/pages/{page.get('url') or slug}"))
        lines = [f"# {title}", "", f"Canvas URL: {url}"]
        updated_at = page.get("updated_at") or page.get("last_edited_at")
        if updated_at:
            lines.append(f"Last updated: {updated_at}")
        body = _render_html(page.get("body"))
        if body:
            lines.extend(["", body])
        return _write_if_changed(
            self.pages_dir / f"canvas-{self.course_id}-pages-{slug}.md",
            "\n".join(lines).rstrip() + "\n",
        )

    def write_assignment(self, assignment: dict[str, Any]) -> bool:
        assignment_id = str(assignment.get("id") or "assignment")
        title = str(assignment.get("name") or "Assignment")
        lines = [f"# {title}", ""]
        if assignment.get("due_at"):
            lines.append(f"Due: {assignment['due_at']}")
        if assignment.get("points_possible") is not None:
            lines.append(f"Points: {assignment['points_possible']}")
        if assignment.get("submission_types"):
            lines.append("Submission types: " + ", ".join(map(str, assignment["submission_types"])))
        lines.append("Canvas URL: " + str(assignment.get("html_url") or _canvas_url(self.course_id, f"/assignments/{assignment_id}")))
        description = _render_html(assignment.get("description"))
        if description:
            lines.extend(["", description])
        rubric = assignment.get("rubric")
        if isinstance(rubric, list):
            lines.extend(["", "## Rubric"])
            for criterion in rubric:
                if not isinstance(criterion, dict):
                    continue
                heading = str(criterion.get("description") or "Criterion")
                points = criterion.get("points")
                lines.extend(["", f"### {heading}" + (f" ({points} pts)" if points is not None else "")])
                details = _render_html(criterion.get("long_description"))
                if details:
                    lines.extend(["", details])
                ratings = criterion.get("ratings")
                if isinstance(ratings, list) and ratings:
                    lines.extend(["", "| Rating | Points | Description |", "| --- | ---: | --- |"])
                    for rating in ratings:
                        if not isinstance(rating, dict):
                            continue
                        label = str(rating.get("description") or "").replace("|", "\\|")
                        rating_points = str(rating.get("points", ""))
                        long = _render_html(rating.get("long_description")).replace("|", "\\|").replace("\n", " ")
                        lines.append(f"| {label} | {rating_points} | {long} |")
        return _write_if_changed(
            self.pages_dir / f"canvas-{self.course_id}-assignments-{assignment_id}.md",
            "\n".join(lines).rstrip() + "\n",
        )

    def write_collection(self, kind: str, entries: list[dict[str, Any]]) -> int:
        """Snapshot announcements, discussions or quizzes that expose a body/message."""
        changed = 0
        for index, entry in enumerate(entries):
            entry_id = str(entry.get("id") or entry.get("quiz_id") or index)
            title = str(entry.get("title") or entry.get("name") or kind.title())
            content = _render_html(entry.get("message") or entry.get("description") or entry.get("body"))
            if not content:
                continue
            url = str(entry.get("html_url") or entry.get("url") or "")
            lines = [f"# {title}", ""]
            if url:
                lines.append(f"Canvas URL: {url}")
            posted = entry.get("posted_at") or entry.get("updated_at") or entry.get("created_at")
            if posted:
                lines.append(f"Published: {posted}")
            lines.extend(["", content])
            filename = f"canvas-{self.course_id}-{kind}-{_safe_name(entry_id, str(index))}.md"
            changed += _write_if_changed(self.pages_dir / filename, "\n".join(lines).rstrip() + "\n")
        return changed

