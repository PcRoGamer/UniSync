"""Read-only EdStem API integration.

Ed's web pages are client-rendered, so the integration uses the documented JSON
API rather than scraping the HTML shell. The API token is accepted only by this
server-side client and is never included in returned models or logs.
"""
from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, urlparse
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_EDSTEM_API_BASE = "https://edstem.org/api"
_ALLOWED_RESOURCE_HOSTS = {"edstem.org"}


class EdStemError(RuntimeError):
    """A user-facing EdStem API or resource-download failure."""

    def __init__(self, message: str, *, status_code: int = 0, kind: str = "api") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


@dataclass(frozen=True)
class EdStemCourse:
    id: str
    name: str
    code: str = ""
    year: str = ""
    session: str = ""
    status: str = ""
    role: str = ""


@dataclass
class EdStemSnapshot:
    course: dict[str, Any]
    modules: list[dict[str, Any]]
    lessons: list[dict[str, Any]]
    resources: list[dict[str, Any]]
    threads: list[dict[str, Any]]
    announcements: list[dict[str, Any]]
    fetched_at: str
    errors: dict[str, str] | None = None


class _EdHtmlToMarkdown(HTMLParser):
    """HTML and EdStem XML document linearizer that converts rich lesson and slide content to standard Markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.pre_depth = 0
        self.link_href = ""
        self.list_depth = 0

    def block(self) -> None:
        if self.out and self.out[-1] not in {"\n", "> "}:
            self.out.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        attrs_map = dict(attrs)

        # Standard headings or EdStem <heading level="N">
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.block()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "heading":
            self.block()
            try:
                lvl = max(1, min(6, int(attrs_map.get("level", 2))))
            except Exception:
                lvl = 2
            self.out.append("#" * lvl + " ")
        elif tag in {"p", "div", "section", "article", "paragraph"}:
            self.block()
        elif tag in {"br", "break"}:
            self.out.append("  \n")
        elif tag in {"ul", "ol", "list"}:
            self.list_depth += 1
            self.block()
        elif tag in {"li", "list-item"}:
            self.block()
            self.out.append("  " * max(0, self.list_depth - 1))
            self.out.append("1. " if attrs_map.get("data-list-type") == "ordered" or attrs_map.get("style") == "numbered" else "- ")
        elif tag in {"blockquote", "callout"}:
            self.block()
            self.out.append("> ")
        elif tag in {"pre", "snippet-file", "web-snippet-file"}:
            self.pre_depth += 1
            self.block()
            lang = attrs_map.get("language") or ""
            fname = attrs_map.get("filename") or ""
            hdr = f":{fname}" if fname else ""
            self.out.append(f"```{lang}{hdr}\n")
        elif tag == "code" and not self.pre_depth:
            self.out.append("`")
        elif tag in {"strong", "b", "bold"}:
            self.out.append("**")
        elif tag in {"em", "i", "italic"}:
            self.out.append("*")
        elif tag in {"a", "link"}:
            self.link_href = attrs_map.get("href") or attrs_map.get("url") or ""
            self.out.append("[")
        elif tag in {"img", "image", "figure"}:
            alt = (attrs_map.get("alt") or attrs_map.get("caption") or "").strip()
            src = attrs_map.get("src") or attrs_map.get("url") or ""
            if src:
                self.out.append(f"![{alt}]({src})")
        elif tag == "math":
            self.out.append("$")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "heading", "p", "div", "section", "article", "paragraph", "li", "list-item", "blockquote", "callout"}:
            self.block()
        elif tag in {"ul", "ol", "list"}:
            self.list_depth = max(0, self.list_depth - 1)
            self.block()
        elif tag in {"pre", "snippet-file", "web-snippet-file"}:
            self.pre_depth = max(0, self.pre_depth - 1)
            self.out.append("\n```\n")
            self.block()
        elif tag == "code" and not self.pre_depth:
            self.out.append("`")
        elif tag in {"strong", "b", "bold"}:
            self.out.append("**")
        elif tag in {"em", "i", "italic"}:
            self.out.append("*")
        elif tag in {"a", "link"}:
            if self.link_href:
                self.out.append(f"]({self.link_href})")
            self.link_href = ""
        elif tag == "math":
            self.out.append("$")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.pre_depth:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if text:
            self.out.append(text)


def edstem_to_markdown(value: Any, limit: int = 300_000) -> str:
    """Convert Ed HTML/Markdown-ish content into bounded Markdown."""
    if value is None:
        return ""
    text = str(value).strip()[:limit]
    if not text:
        return ""
    if "<" not in text or ">" not in text:
        return text
    parser = _EdHtmlToMarkdown()
    try:
        parser.feed(text)
        parser.close()
        rendered = "".join(parser.out)
    except Exception:  # noqa: BLE001
        rendered = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\n{3,}", "\n\n", rendered).strip()


def _as_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _id(value: Any) -> str:
    return _string(value)


def _collection(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [_as_record(item) for item in payload if isinstance(item, dict)]
    record = _as_record(payload)
    for key in keys:
        value = record.get(key)
        if isinstance(value, list):
            return [_as_record(item) for item in value if isinstance(item, dict)]
    return []


def _course_from_enrollment(entry: dict[str, Any]) -> EdStemCourse | None:
    course = _as_record(entry.get("course")) or entry
    course_id = _id(course.get("id"))
    if not course_id:
        return None
    role = _as_record(entry.get("role"))
    return EdStemCourse(
        id=course_id,
        name=_string(course.get("name") or course.get("title") or course_id),
        code=_string(course.get("code") or course.get("course_code")),
        year=_string(course.get("year")),
        session=_string(course.get("session")),
        status=_string(course.get("status")),
        role=_string(role.get("role") or entry.get("role")),
    )


def course_as_dict(course: EdStemCourse) -> dict[str, str]:
    return {
        "id": course.id,
        "name": course.name,
        "code": course.code,
        "year": course.year,
        "session": course.session,
        "status": course.status,
        "role": course.role,
        "url": f"https://edstem.org/au/courses/{course.id}",
    }


def _find_resource_url(value: Any, depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(value, str) and value.startswith("https://"):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host == "edstem.org" or host.endswith(".edstem.org"):
            return value
    if isinstance(value, dict):
        for key in ("download_url", "file_url", "url", "href", "link"):
            found = _find_resource_url(value.get(key), depth + 1)
            if found:
                return found
        for child in value.values():
            found = _find_resource_url(child, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_resource_url(child, depth + 1)
            if found:
                return found
    return ""


def normalize_resource(resource: dict[str, Any], index: int) -> dict[str, Any]:
    resource_id = _id(resource.get("id") or resource.get("resource_id") or resource.get("file_id")) or f"resource-{index + 1}"
    title = _string(resource.get("title") or resource.get("name") or resource.get("filename") or resource_id)
    url = _find_resource_url(resource)
    if not url and resource_id and not str(resource_id).startswith("resource-"):
        # EdStem API resource download endpoint: POST /api/resources/{id}/download/{filename}?dl=1
        ext = _string(resource.get("extension") or "")
        name = _string(resource.get("name") or resource.get("title") or resource_id)
        fname = f"{name}{ext}" if ext and not name.endswith(ext) else name
        url = f"https://edstem.org/api/resources/{resource_id}/download/{quote(fname)}?dl=1"

    return {
        **resource,
        "id": resource_id,
        "title": title,
        "description": _string(resource.get("description") or resource.get("content")),
        "url": url,
        "download_url": url,
        "created_at": _string(resource.get("created_at") or resource.get("published_at")),
        "updated_at": _string(resource.get("updated_at")),
    }



class EdStemClient:
    """Small synchronous client intended to run in FastAPI worker threads."""

    def __init__(
        self,
        token: str,
        *,
        api_base_url: str = DEFAULT_EDSTEM_API_BASE,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/") or DEFAULT_EDSTEM_API_BASE
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise EdStemError("EdStem API token is not configured.", kind="auth")
        url = f"{self.api_base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                    if response.status_code == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "2.0"))
                        except Exception:
                            delay = 2.0
                        time.sleep(max(delay, 1.5 * (attempt + 1)))
                    else:
                        time.sleep(1.0 * (attempt + 1))
                    continue
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 3:
                    raise EdStemError(f"Could not reach EdStem: {exc}", kind="network") from exc
                time.sleep(1.0 * (attempt + 1))
        else:  # pragma: no cover - defensive
            raise EdStemError(f"Could not reach EdStem: {last_error}", kind="network")

        if response.status_code in {401, 403}:
            raise EdStemError("EdStem API token was rejected or has expired.", status_code=response.status_code, kind="auth")
        if response.status_code == 404:
            raise EdStemError(f"EdStem endpoint was not found: {path}", status_code=404, kind="not_found")
        if not response.ok:
            kind = "rate_limit" if response.status_code == 429 else "api"
            raise EdStemError(f"EdStem returned HTTP {response.status_code}.", status_code=response.status_code, kind=kind)
        try:
            return response.json()
        except ValueError as exc:
            raise EdStemError("EdStem returned a non-JSON response. Check the API base URL.", kind="base_url") from exc

    def validate(self) -> dict[str, Any]:
        payload = _as_record(self._request("user"))
        user = _as_record(payload.get("user"))
        courses = self.list_courses(payload)
        return {"authenticated": True, "user": {"id": _id(user.get("id")), "name": _string(user.get("name")), "email": _string(user.get("email"))}, "courses": [course_as_dict(c) for c in courses]}

    def list_courses(self, user_payload: dict[str, Any] | None = None) -> list[EdStemCourse]:
        payload = user_payload if user_payload is not None else _as_record(self._request("user"))
        raw_courses = _collection(payload, "courses", "enrollments", "units")
        courses: list[EdStemCourse] = []
        seen: set[str] = set()
        for entry in raw_courses:
            course = _course_from_enrollment(entry)
            if course and course.id not in seen:
                seen.add(course.id)
                courses.append(course)
        return sorted(courses, key=lambda item: item.name.lower())

    def fetch_lessons(self, course_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        payload = self._request(f"courses/{course_id}/lessons")
        modules = _collection(payload, "modules", "lesson_modules")
        module_names = {_id(m.get("id")): _string(m.get("name")) for m in modules}
        lessons: list[dict[str, Any]] = []
        for raw in _collection(payload, "lessons"):
            lesson = dict(raw)
            lesson_id = _id(lesson.get("id"))
            if lesson_id and not lesson.get("slides"):
                try:
                    time.sleep(0.08)
                    detail = _as_record(self._request(f"lessons/{lesson_id}", params={"view": "1"}))
                    lesson.update(_as_record(detail.get("lesson")) or detail)
                except EdStemError as exc:
                    if exc.kind == "rate_limit":
                        time.sleep(2.5)
                        try:
                            detail = _as_record(self._request(f"lessons/{lesson_id}", params={"view": "1"}))
                            lesson.update(_as_record(detail.get("lesson")) or detail)
                        except Exception as retry_err:
                            logger.warning("Retry failed for lesson %s: %s", lesson_id, retry_err)
                    elif exc.kind not in {"not_found", "api"}:
                        raise
            module_id = _id(lesson.get("module_id"))
            lesson["id"] = lesson_id
            lesson.setdefault("course_id", course_id)
            lesson["module_name"] = _string(lesson.get("module_name")) or module_names.get(module_id, "")

            # Fetch interactive questions for quiz slides so exercises, options & answers are fully ingested
            for slide in _as_list(lesson.get("slides")):
                if isinstance(slide, dict) and slide.get("type") == "quiz" and slide.get("id") and "questions" not in slide:
                    s_id = _id(slide.get("id"))
                    try:
                        q_resp = self._request(f"lessons/slides/{s_id}/questions")
                        slide["questions"] = _collection(q_resp, "questions")
                    except Exception as q_err:
                        logger.debug("Failed fetching questions for quiz slide %s: %s", s_id, q_err)

            lessons.append(lesson)
        return lessons, modules

    def fetch_resources(self, course_id: str) -> list[dict[str, Any]]:
        # Resources were exposed under this route in Ed's course API. Some
        # deployments use files instead; support that shape without masking
        # authentication failures.
        try:
            payload = self._request(f"courses/{course_id}/resources")
        except EdStemError as exc:
            if exc.kind != "not_found":
                raise
            payload = self._request(f"courses/{course_id}/files")
        return [normalize_resource(item, index) for index, item in enumerate(_collection(payload, "resources", "files", "items"))]

    def fetch_threads(self, course_id: str, *, max_items: int = 500) -> list[dict[str, Any]]:
        threads: list[dict[str, Any]] = []
        offset = 0
        while len(threads) < max_items:
            payload = self._request(
                f"courses/{course_id}/threads",
                params={"limit": min(100, max_items - len(threads)), "offset": offset, "sort": "new"},
            )
            page = _collection(payload, "threads", "items")
            if not page:
                break
            threads.extend(page)
            if len(page) < min(100, max_items - len(threads) + len(page)):
                break
            offset += len(page)
        normalized: list[dict[str, Any]] = []
        for raw in threads[:max_items]:
            thread = dict(raw)
            thread_id = _id(thread.get("id"))
            if thread_id and not thread.get("comments") and not thread.get("answers"):
                try:
                    detail = _as_record(self._request(f"threads/{thread_id}"))
                    thread.update(_as_record(detail.get("thread")) or detail)
                except EdStemError as exc:
                    if exc.kind not in {"not_found", "api"}:
                        raise
            thread["id"] = thread_id
            normalized.append(thread)
        return normalized

    def fetch_announcements(self, course_id: str) -> list[dict[str, Any]]:
        try:
            payload = self._request(f"courses/{course_id}/announcements")
        except EdStemError as exc:
            if exc.kind == "not_found":
                return []
            raise
        return _collection(payload, "announcements", "items")

    def snapshot(self, course_id: str, *, course: dict[str, Any] | None = None, max_threads: int = 500) -> EdStemSnapshot:
        """Fetch every section independently so one unavailable Ed endpoint
        does not discard the rest of a course. Authentication failures remain
        fatal; ordinary section failures are recorded in ``errors``."""
        from datetime import datetime, timezone

        errors: dict[str, str] = {}
        sections: dict[str, Any] = {
            "modules": [],
            "lessons": [],
            "resources": [],
            "threads": [],
            "announcements": [],
        }
        try:
            sections["lessons"], sections["modules"] = self.fetch_lessons(course_id)
        except EdStemError as exc:
            if exc.kind == "auth":
                raise
            errors["lessons"] = str(exc)
        for name, fetch in (
            ("resources", lambda: self.fetch_resources(course_id)),
            ("threads", lambda: self.fetch_threads(course_id, max_items=max_threads)),
            ("announcements", lambda: self.fetch_announcements(course_id)),
        ):
            try:
                sections[name] = fetch()
            except EdStemError as exc:
                if exc.kind == "auth":
                    raise
                errors[name] = str(exc)
        return EdStemSnapshot(
            course=course or {"id": course_id},
            modules=sections["modules"],
            lessons=sections["lessons"],
            resources=sections["resources"],
            threads=sections["threads"],
            announcements=sections["announcements"],
            fetched_at=datetime.now(timezone.utc).isoformat(),
            errors=errors,
        )

    def download_resource(self, url: str, destination: Path, *, max_bytes: int = 50 * 1024 * 1024) -> int:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in _ALLOWED_RESOURCE_HOSTS and not host.endswith(".edstem.org"):
            raise EdStemError("Refusing to download a resource from an unsupported host.", kind="security")
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "*/*"}
        method = "POST" if "/download" in url else "GET"
        try:
            response = self.session.request(method, url, headers=headers, timeout=(20, 120), stream=True)
            if response.status_code in {404, 405} and method == "POST":
                response.close()
                response = self.session.get(url, headers=headers, timeout=(20, 120), stream=True)
            with response:
                if response.status_code in {401, 403}:
                    raise EdStemError("EdStem resource access was rejected; the token may have expired.", status_code=response.status_code, kind="auth")
                response.raise_for_status()
                length = int(response.headers.get("Content-Length", "0") or 0)
                if length > max_bytes:
                    raise EdStemError("EdStem resource exceeds the 50 MB download limit.", kind="size")
                written = 0
                with destination.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > max_bytes:
                            raise EdStemError("EdStem resource exceeds the 50 MB download limit.", kind="size")
                        output.write(chunk)
        except requests.RequestException as exc:
            raise EdStemError(f"Could not download EdStem resource: {exc}", kind="network") from exc

        try:
            from unisync.core.download_registry import register_download
            register_download(
                platform="edstem",
                item_id=destination.stem,
                title=destination.name,
                direct_link=url,
                local_filepath=destination,
                size_bytes=written,
            )
        except Exception:
            pass
        return written


def _doc_path(section: str, index: int, title: str, course_code: str) -> str:
    slug = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "-").lower()[:70] or f"item-{index + 1}"
    prefix = re.sub(r"[^\w\-]+", "-", course_code).strip("-").lower() or "edstem"
    return f"edstem/{section}/{prefix}-{section}-{index + 1:04d}-{slug}.md"


def render_snapshot(snapshot: EdStemSnapshot, course_id: str) -> list[tuple[str, str]]:
    """Render all fetched Ed content as focused, source-labelled documents."""
    course = snapshot.course
    course_code = _string(course.get("code") or course.get("course_code")) or course_id
    course_name = _string(course.get("name") or course.get("title")) or course_id
    docs: list[tuple[str, str]] = []
    overview = [f"# {course_name}", "", "## EdStem course", "", f"- Course ID: `{course_id}`", f"- Course code: `{course_code}`"]
    for label, key in (("Year", "year"), ("Session", "session"), ("Status", "status")):
        if course.get(key):
            overview.append(f"- {label}: {course[key]}")
    docs.append(("edstem/00-course-overview.md", "\n".join(overview) + "\n"))

    if snapshot.modules:
        lines = ["# EdStem Lesson Modules", ""]
        for module in snapshot.modules:
            lines.extend([f"## {_string(module.get('name')) or 'Untitled module'}", ""])
        docs.append(("edstem/01-modules.md", "\n".join(lines).rstrip() + "\n"))

    for index, lesson in enumerate(snapshot.lessons):
        title = _string(lesson.get("title") or lesson.get("name")) or f"Lesson {index + 1}"
        lines = [f"# {title}", "", "- Source: EdStem lesson", f"- Lesson ID: `{_id(lesson.get('id'))}`"]
        for label, key in (("Module", "module_name"), ("Available", "available_at"), ("Due", "due_at"), ("Updated", "updated_at")):
            if lesson.get(key):
                lines.append(f"- {label}: {lesson[key]}")
        outline = edstem_to_markdown(lesson.get("outline") or lesson.get("content") or lesson.get("description"))
        if outline:
            lines.extend(["", outline])
        for slide_index, slide in enumerate(_as_list(lesson.get("slides"))):
            slide_record = _as_record(slide)
            slide_title = _string(slide_record.get("title")) or f"Slide {slide_index + 1}"
            slide_content = edstem_to_markdown(slide_record.get("content") or slide_record.get("passage") or slide_record.get("data"))
            lines.extend(["", f"## {slide_title}"])
            if slide_content:
                lines.extend(["", slide_content])
            # Render interactive quiz questions, options, solution markers, and explanations
            questions = _as_list(slide_record.get("questions"))
            if questions:
                lines.extend(["", "### Questions & Exercises"])
                for q_idx, q in enumerate(questions, 1):
                    q_rec = _as_record(q)
                    q_data = _as_record(q_rec.get("data"))
                    q_text = edstem_to_markdown(q_data.get("content") or q_data.get("text") or q_rec.get("content"))
                    if q_text:
                        lines.extend(["", f"**Question {q_idx}:** {q_text}"])
                    answers = _as_list(q_data.get("answers"))
                    solution = q_data.get("solution")
                    sol_set = set(solution) if isinstance(solution, list) else {solution} if solution is not None else set()
                    if answers:
                        lines.append("")
                        for a_idx, ans in enumerate(answers):
                            ans_text = edstem_to_markdown(ans)
                            is_correct = a_idx in sol_set
                            mark = "[x]" if is_correct else "[ ]"
                            lines.append(f"- {mark} {ans_text}")
                    explanation = edstem_to_markdown(q_data.get("explanation"))
                    if explanation:
                        lines.extend(["", f"> **Explanation:** {explanation}"])
        docs.append((_doc_path("lessons", index, title, course_code), "\n".join(lines).rstrip() + "\n"))

    if snapshot.resources:
        lines = ["# EdStem Resources", ""]
        for index, resource in enumerate(snapshot.resources, 1):
            title = _string(resource.get("title")) or f"Resource {index}"
            lines.append(f"## {title}")
            lines.append(f"- Resource ID: `{_id(resource.get('id'))}`")
            if resource.get("url"):
                lines.append(f"- URL: {resource['url']}")
            description = edstem_to_markdown(resource.get("description"))
            if description:
                lines.extend(["", description])
            lines.append("")
        docs.append(("edstem/20-resources-index.md", "\n".join(lines).rstrip() + "\n"))

    for index, thread in enumerate(snapshot.threads):
        title = _string(thread.get("title")) or f"Discussion thread {index + 1}"
        lines = [f"# {title}", "", "- Source: EdStem discussion", f"- Thread ID: `{_id(thread.get('id'))}`"]
        for label, key in (("Number", "number"), ("Category", "category"), ("Created", "created_at"), ("Updated", "updated_at")):
            if thread.get(key):
                lines.append(f"- {label}: {thread[key]}")
        body = edstem_to_markdown(thread.get("content") or thread.get("document") or thread.get("body"))
        if body:
            lines.extend(["", body])
        replies = _as_list(thread.get("answers")) + _as_list(thread.get("comments"))
        for reply_index, reply in enumerate(replies, 1):
            reply_record = _as_record(reply)
            reply_body = edstem_to_markdown(reply_record.get("content") or reply_record.get("document") or reply_record.get("body"))
            if reply_body:
                lines.extend(["", f"## Reply {reply_index}", "", reply_body])
        docs.append((_doc_path("discussions", index, title, course_code), "\n".join(lines).rstrip() + "\n"))

    if snapshot.errors:
        lines = ["# EdStem Sync Notes", "", "Some EdStem sections could not be read during this sync:", ""]
        for section, error in sorted(snapshot.errors.items()):
            lines.append(f"- **{section}:** {error}")
        docs.append(("edstem/99-sync-notes.md", "\n".join(lines) + "\n"))

    for index, announcement in enumerate(snapshot.announcements):
        title = _string(announcement.get("title") or announcement.get("name")) or f"Announcement {index + 1}"
        body = edstem_to_markdown(announcement.get("content") or announcement.get("body") or announcement.get("document"))
        docs.append((_doc_path("announcements", index, title, course_code), f"# {title}\n\n- Source: EdStem announcement\n\n{body}\n"))
    return docs


def extract_due_items(snapshot: EdStemSnapshot) -> list[dict[str, Any]]:
    """Return lesson/activity records with due dates and submission statuses for todo synchronization.

    Zero-LLM programmatic parser that checks interactive lesson completion states
    (e.g. 'completed', 'attempted', 'unattempted') and extracts points possible
    from titles and metadata to populate student progress tracking.
    """
    import re
    import time
    items: list[dict[str, Any]] = []
    for lesson in snapshot.lessons:
        due_at = _string(lesson.get("due_at") or lesson.get("effective_due_at") or lesson.get("deadline"))
        title = _string(lesson.get("title") or lesson.get("name"))
        if not (due_at and title):
            continue

        # Check completion and attempt states on the lesson
        raw_status = _string(lesson.get("status")).lower()
        submitted_at = lesson.get("submitted_at")
        is_completed = (raw_status == "completed") or (submitted_at is not None)
        is_attempted = (raw_status == "attempted") or (lesson.get("attempt_id") is not None)

        # Parse points possible from title e.g. 'More operators (2 points)' or lesson attributes
        pts = None
        m = re.search(r"\((\d+(?:\.\d+)?)\s*points?\)", title, re.IGNORECASE)
        if m:
            try:
                pts = float(m.group(1))
            except Exception:
                pass
        elif lesson.get("points") is not None:
            try:
                pts = float(lesson.get("points"))
            except Exception:
                pass

        # Map EdStem lifecycle to standard UniSync submission statuses
        if is_completed:
            task_status = "done"
            sub_status = "submitted"
        elif is_attempted:
            task_status = "open"
            sub_status = "pending_review"
        else:
            task_status = "open"
            sub_status = "unsubmitted"

        item_id = _id(lesson.get("id")) or title
        course_id = _id(lesson.get("course_id"))

        items.append({
            "id": item_id,
            "title": title,
            "due_at": due_at,
            "status": task_status,
            "submission_status": sub_status,
            "is_completed": is_completed,
            "is_graded": is_completed,  # Ed challenges auto-grade upon all slides completed
            "points_possible": pts,
            "score": pts if is_completed else None,
            "grade": str(pts) if (is_completed and pts is not None) else None,
            "grading_type": "points" if pts is not None else None,
            "submitted_at": submitted_at,
            "completed_at": time.time() if is_completed else None,
            "url": f"https://edstem.org/au/courses/{course_id}/lessons/{item_id}",
        })
    return items
