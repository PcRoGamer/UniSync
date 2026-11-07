"""Metadata-only catalog of Canvas assessment and Respondus entry links.

Do not fetch quiz questions, attempts, Respondus launches, or external resources.
All URLs are official same-course Canvas entry points and use no query secrets.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from unisync.core.file_io import atomic_write_json, atomic_write_text

ASSESSMENT_PATH = re.compile(
    r"^/courses/(?P<course_id>[0-9]+)/(?P<kind>quizzes|assignments)/(?P<id>[0-9]+)(?:/)?$"
)
EXAM_WORDS = re.compile(r"\b(?:exam|practice\s+test|mock\s+test|past\s+paper|past\s+exam)\b", re.I)
LOCKDOWN_WORDS = re.compile(r"\b(?:respondus|lock\s*down\s*browser)\b", re.I)
NOT_REQUIRED_LOCKDOWN = re.compile(
    r"\b(?:respondus|lock\s*down\s*browser)\b.{0,60}\bnot\s+(?:required|needed|necessary)\b"
    r"|\bnot\s+(?:required|needed|necessary)\b.{0,60}\b(?:respondus|lock\s*down\s*browser)\b",
    re.I,
)
NO_LOCKDOWN = re.compile(
    r"\b(?:no|without)\s+(?:the\s+)?(?:respondus|lock\s*down\s*browser)\b",
    re.I,
)
SECRET_WORDS = re.compile(r"(?:token|secret|password|access_code|key)", re.I)


def _requires_lockdown(value: object) -> bool:
    text = str(value or "")
    return (
        bool(LOCKDOWN_WORDS.search(text))
        and not bool(NOT_REQUIRED_LOCKDOWN.search(text))
        and not bool(NO_LOCKDOWN.search(text))
    )


def safe_course_url(course_id: str, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    url = urlparse(urljoin("https://canvas.lms.unimelb.edu.au/", value))
    if url.scheme != "https" or url.hostname != "canvas.lms.unimelb.edu.au":
        return None
    if not ASSESSMENT_PATH.fullmatch(url.path):
        return None
    if ASSESSMENT_PATH.fullmatch(url.path).group("course_id") != str(course_id):
        return None
    return f"https://canvas.lms.unimelb.edu.au{url.path.rstrip('/')}"


def build_assessment_catalog(
    course_id: str, *, pages=(), modules=(), assignments=(), quizzes=(),
    announcements=(), discussions=(),
) -> list[dict]:
    """Inventory directly exposed official assessment links and metadata only."""
    found: dict[str, dict] = {}

    def register(url: object, *, title: object = "", source: str = "",
                 context: str = "", lockdown: bool = False) -> None:
        canonical = safe_course_url(course_id, url)
        if not canonical:
            return
        label = str(title or "").strip()[:180]
        # Do not label all quizzes Respondus. Only explicit evidence qualifies.
        protected = lockdown or _requires_lockdown(context)
        existing = found.get(canonical)
        if existing is None:
            found[canonical] = {
                "title": label or canonical.rsplit("/", 1)[-1],
                "url": canonical,
                "access": "requires_respondus" if protected else "canvas_assessment_link",
                "sources": [source] if source else [],
            }
        else:
            if label and (not existing["title"] or existing["title"].isdigit()):
                existing["title"] = label
            if protected:
                existing["access"] = "requires_respondus"
            if source and source not in existing["sources"]:
                existing["sources"].append(source)

    def scan_html(html: object, *, source: str, context: str = "") -> None:
        if not isinstance(html, str) or not html.strip():
            return
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            label = a.get_text(" ", strip=True)
            nearby = a.parent.get_text(" ", strip=True) if a.parent else label
            anchor_context = " ".join(
                part for part in (label, str(a.get("title") or "")) if part
            )
            # Canvas often puts a specific browser requirement in the link's
            # title while the surrounding list item mentions a different
            # practice quiz. Prefer link-local evidence when it is explicit.
            link_context = (
                anchor_context
                if LOCKDOWN_WORDS.search(anchor_context)
                else " ".join(part for part in (label, nearby, context) if part)
            )
            register(
                href, title=label, source=source, context=link_context,
                lockdown=_requires_lockdown(link_context),
            )

    for page in pages:
        if isinstance(page, dict):
            scan_html(page.get("body"), source="page:" + str(page.get("url") or ""))

    for module in modules:
        if not isinstance(module, dict):
            continue
        for item in module.get("items") or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            title = str(item.get("title") or "")
            if kind in ("Quiz", "Assignment"):
                target = item.get("html_url") or item.get("url")
                if not target and item.get("content_id"):
                    endpoint = "quizzes" if kind == "Quiz" else "assignments"
                    target = f"/courses/{course_id}/{endpoint}/{item['content_id']}"
                register(target, title=title, source="module", context=title)
            scan_html(item.get("description"), source="module:" + title)

    for quiz in quizzes:
        if not isinstance(quiz, dict):
            continue
        title = str(quiz.get("title") or quiz.get("name") or "")
        url = quiz.get("html_url")
        if not url and quiz.get("id"):
            url = f"/courses/{course_id}/quizzes/{quiz['id']}"
        context = " ".join(str(quiz.get(k) or "") for k in ("description", "instructions", "title"))
        lockdown = any(quiz.get(k) is True for k in (
            "require_lockdown_browser", "requires_lockdown_browser",
        ))
        register(url, title=title, source="quiz", context=context, lockdown=lockdown)
        scan_html(quiz.get("description"), source="quiz:" + title)

    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        title = str(assignment.get("name") or "")
        context = " ".join(str(assignment.get(k) or "") for k in ("description", "name"))
        if EXAM_WORDS.search(title) or LOCKDOWN_WORDS.search(context):
            url = assignment.get("html_url")
            if not url and assignment.get("id"):
                url = f"/courses/{course_id}/assignments/{assignment['id']}"
            register(url, title=title, source="assignment", context=context)
        scan_html(assignment.get("description"), source="assignment:" + title)

    for kind, collection in (("announcement", announcements), ("discussion", discussions)):
        for item in collection:
            if isinstance(item, dict):
                scan_html(
                    item.get("message") or item.get("body") or item.get("description"),
                    source=kind + ":" + str(item.get("id") or ""),
                )
    return sorted(found.values(), key=lambda v: (v["access"] != "requires_respondus", v["title"].lower()))


def write_assessment_catalog(path: Path, course_id: str, entries: list[dict]) -> bool:
    """Save a predictable machine-readable catalog and searchable Markdown view."""
    path.parent.mkdir(parents=True, exist_ok=True)
    json_path = path.with_suffix(".json")
    data = {"course_id": str(course_id), "entries": entries}
    try:
        old = __import__("json").loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old != data:
        atomic_write_json(json_path, data)

    lines = [
        "# Canvas assessments and Respondus entry links",
        "",
        "Official metadata and entry links only. No protected exam questions,",
        "attempts, or Respondus content are fetched.",
        "",
    ]
    for entry in entries:
        title = entry["title"].replace("[", r"\[").replace("]", r"\]")
        lines += [
            "## " + title,
            "- Access: " + entry["access"],
            "- Official link: " + entry["url"],
            "- Discovered via: " + ", ".join(entry["sources"]),
            "",
        ]
    content = "\n".join(lines).rstrip() + "\n"
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except OSError:
        pass
    atomic_write_text(path, content)
    return True
