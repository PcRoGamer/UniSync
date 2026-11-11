"""Archive the current student's Gradescope copies linked from Canvas.

Gradescope is opened through the resident Canvas browser session (LTI launch).
No Gradescope password is read or stored. Logs and saved errors contain only a
host, a URL path, an HTTP status, or a short message written in this module.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

logger = logging.getLogger("unisync.gradescope")

CANVAS = "canvas.lms.unimelb.edu.au"
# Production LTI 1.3 sends the browser through these hosts before Gradescope.
# canvas.instructure.com is the legacy OIDC host Canvas still accepts.
# Beta and test hosts are intentionally absent.
LAUNCH_HOSTS = {
    CANVAS,
    "sso.canvaslms.com",
    "canvas.instructure.com",
    # Gradescope (Turnitin) LTI 1.3 OIDC login and launch host.
    "lti-gradescope.int.turnitin.com",
    # Turnitin platform OIDC proxy Gradescope posts its login request to.
    "lti.int.turnitin.com",
}
GRADESCOPE = {"www.gradescope.com", "gradescope.com"}
TURNITIN_LTI = "lti-gradescope.int.turnitin.com"
# Gradescope serves a JSON stub for submission URLs unless HTML is requested.
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
_SCRIPT_REDIRECT = re.compile(r"""\bredirect_url\s*=\s*new URL\(\s*"((?:[^"\\]|\\.)*)"\s*\)""")
HOSTS = GRADESCOPE | LAUNCH_HOSTS

SUBMISSION = re.compile(r"/courses/(\d+)/assignments/(\d+)/submissions/(\d+)")
QUESTION_ANCHOR = re.compile(r"^Question_(.+)-rubric$")
SCORE_PAIR = re.compile(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)")

REQUEST_PAUSE = 0.4
MAX_PAGES = 40
MAX_OWN_SUBMISSIONS = 8
_SECRET_HINTS = ("cookie", "token", "authorization", "password", "secret", "eyj", "set-cookie")


def trusted(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and not parts.password and host in HOSTS


def safe_public_error(exc: BaseException) -> str:
    """Describe a failure without cookies, tokens, or query strings."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    if status and url is not None:
        return f"HTTP {status} {getattr(url, 'host', '')} {getattr(url, 'path', '')}"[:300]
    text = str(exc).strip().replace("\n", " ")
    lowered = text.lower()
    if (
        isinstance(exc, ValueError)
        and text
        and len(text) <= 240
        and "://" not in text
        and "?" not in text
        and not any(hint in lowered for hint in _SECRET_HINTS)
    ):
        return text
    return type(exc).__name__


def normalize_title(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _title_tokens(value: object) -> list[str]:
    """Letter runs and digit runs. '10' stays one token so it does not match '1'."""
    return re.findall(r"[a-z]+|\d+", normalize_title(value))


def _token_span(shorter: list[str], longer: list[str]) -> bool:
    span = len(shorter)
    if not span or span > len(longer):
        return False
    for start in range(len(longer) - span + 1):
        if longer[start:start + span] != shorter:
            continue
        extra = longer[start + span:start + span + 1]
        # "Lab 2026" must not swallow "Lab 2026 10", and "1" must not swallow "10".
        if extra and extra[0].isdigit():
            continue
        return True
    return False


def titles_match(left: object, right: object) -> bool:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return False
    if a == b:
        return True
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    shorter, longer = (left_tokens, right_tokens) if len(left_tokens) <= len(right_tokens) else (right_tokens, left_tokens)
    if not _token_span(shorter, longer):
        return False
    # "MST 2026S2" matches "MST2026S2-VerA" (same digit runs, letter suffix).
    # A lone short word does not match every longer title that contains it.
    has_digit = any(token.isdigit() for token in shorter)
    return has_digit or len("".join(shorter)) >= 8


def _remember(counts: dict, message: str) -> None:
    errors = counts.setdefault("errors", [])
    if message and message not in errors and len(errors) < 8:
        errors.append(message)


def _public_url(value: object) -> object:
    text = str(value)
    if not text.startswith(("http://", "https://")):
        return value
    parts = urlsplit(text)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


class _RedactUrlFilter(logging.Filter):
    """Drop query strings and userinfo from HTTP client logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = {key: _public_url(item) for key, item in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_public_url(item) for item in record.args)
        if isinstance(record.msg, str) and "://" in record.msg:
            record.msg = re.sub(r"https?://[^\s\"']+", lambda match: str(_public_url(match.group(0))), record.msg)
        return True


@contextmanager
def quiet_http_logs():
    """Keep httpx from writing launch URLs that carry one-time secrets."""
    filt = _RedactUrlFilter()
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    for item in loggers:
        item.addFilter(filt)
    try:
        yield
    finally:
        for item in loggers:
            item.removeFilter(filt)


def _log_hop(status: int, url: str, dest: str = "") -> None:
    parts = urlsplit(url)
    if not dest:
        logger.info("gradescope hop status=%s host=%s path=%s", status, parts.hostname, parts.path)
        return
    other = urlsplit(dest)
    logger.info(
        "gradescope redirect status=%s host=%s path=%s dest_host=%s dest_path=%s",
        status, parts.hostname, parts.path, other.hostname, other.path,
    )


async def _pause() -> None:
    if REQUEST_PAUSE:
        import asyncio
        await asyncio.sleep(REQUEST_PAUSE)


def _number(value: object):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else number


def _classes(tag) -> list[str]:
    if tag is None or not hasattr(tag, "get"):
        return []
    raw = tag.get("class") or []
    if isinstance(raw, str):
        return raw.split()
    return [str(item) for item in raw]


def _applied(tag) -> bool:
    for token in (item.casefold() for item in _classes(tag)):
        if token in {"applied", "selected"} or token.endswith("-applied") or token.endswith("-selected"):
            return True
    if str(tag.get("aria-pressed") or "").casefold() == "true":
        return True
    return str(tag.get("data-applied") or "").casefold() in {"true", "1", "yes"}


def _score_pair(text: str):
    match = SCORE_PAIR.search(text or "")
    if not match:
        return None, None
    return _number(match.group(1)), _number(match.group(2))


def page_title(document: BeautifulSoup) -> str:
    heading = document.select_one("h1")
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return text
    if not document.title:
        return ""
    return re.split(r"\s+(?:\||-|–)\s+", document.title.get_text(" ", strip=True), maxsplit=1)[0].strip()


def _react_payloads(document: BeautifulSoup) -> list[dict]:
    payloads = []
    for node in document.select("[data-react-props]"):
        try:
            parsed = json.loads(node.get("data-react-props") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    for script in document.select('script[type="application/json"]'):
        try:
            parsed = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def _question_from_props(question: dict) -> dict:
    items = []
    comments = []
    for item in question.get("rubric_items") or question.get("content") or []:
        if not isinstance(item, dict) or not (item.get("applied") or item.get("selected")):
            continue
        points = item.get("weight") if "weight" in item else item.get("points")
        record = {
            "description": str(item.get("description") or item.get("text") or "").strip(),
            "points": _number(points),
            "applied": True,
        }
        comment = str(item.get("comment") or "").strip()
        if comment:
            record["comment"] = comment
            comments.append(comment)
        items.append(record)
    for comment in question.get("comments") or []:
        text = comment if isinstance(comment, str) else ""
        if isinstance(comment, dict):
            text = str(comment.get("text") or comment.get("comment") or "")
        text = text.strip()
        if text and text not in comments:
            comments.append(text)
    weight = question.get("weight") if question.get("weight") is not None else question.get("points_possible")
    _sign_rubric_items(question, items, _number(question.get("score")), _number(weight))
    return {
        "id": str(question.get("index") or question.get("id") or ""),
        "title": str(question.get("title") or question.get("name") or ""),
        "score": _number(question.get("score")),
        "points_possible": _number(weight),
        "rubric_items": items,
        "comments": comments,
    }


def _scoring_mode(source: dict) -> str | None:
    if not isinstance(source, dict):
        return None
    for key in ("scoring_type", "scoring", "rubric_scoring", "scoring_method"):
        value = str(source.get(key) or "").casefold()
        if not value:
            continue
        if "neg" in value or "deduct" in value:
            return "negative"
        if "pos" in value:
            return "positive"
    if source.get("negative_scoring") is True:
        return "negative"
    if source.get("positive_scoring") is True:
        return "positive"
    if source.get("positive_scoring") is False:
        return "negative"
    return None


def _sign_rubric_items(question: dict, items: list[dict], score, maximum) -> None:
    """Store deduction items as negative points and earned items as positive points.

    Gradescope's viewer often sends a positive weight for a deduction ("2" meaning
    minus 2). The sign is taken from an explicit scoring mode when present, and
    otherwise from whether the weights add up to the score or to max-minus-score.
    """
    numeric = [
        item for item in items
        if isinstance(item.get("points"), (int, float)) and not isinstance(item.get("points"), bool)
    ]
    if not numeric or any(item["points"] < 0 for item in numeric):
        return
    mode = _scoring_mode(question)
    total = sum(item["points"] for item in numeric)
    if mode is None and maximum is not None and score is not None:
        deduction_fits = abs((maximum - total) - score) <= 1e-6
        positive_fits = abs(total - score) <= 1e-6
        if deduction_fits and not positive_fits:
            mode = "negative"
        elif positive_fits and not deduction_fits:
            mode = "positive"
    if mode == "negative":
        for item in numeric:
            item["points"] = -item["points"]


def _points_suffix(points) -> str:
    if points is None or isinstance(points, bool):
        return ""
    if isinstance(points, float) and points.is_integer():
        points = int(points)
    if isinstance(points, (int, float)) and points > 0:
        return f" (+{points} points)"
    return f" ({points} points)"


def _text_of(entry: object) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("comments", "comment", "text", "content", "body"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _from_viewer(props: dict) -> dict:
    """Gradescope's live AssignmentSubmissionViewer: scores and rubric are keyed by question id."""
    submission = props.get("assignment_submission") or {}
    assignment = props.get("assignment") if isinstance(props.get("assignment"), dict) else {}
    questions = {str(q.get("id")): q for q in props.get("questions") or [] if isinstance(q, dict)}
    answers = {str(q.get("question_id")): q for q in props.get("question_submissions") or [] if isinstance(q, dict)}
    groups = {str(g.get("id")): g for g in props.get("rubric_item_groups") or [] if isinstance(g, dict)}
    order = [str(i) for i in props.get("inorder_leaf_question_ids") or []]
    order += [k for k, q in questions.items() if k not in order and q.get("type") != "QuestionGroup"]
    leaves = []
    for qid in order:
        question = questions.get(qid) or {}
        answer = answers.get(qid) or {}
        items, comments = [], []
        for item in props.get("rubric_items") or []:
            if not isinstance(item, dict) or str(item.get("question_id")) != qid or not item.get("present"):
                continue
            record = {"description": str(item.get("description") or "").strip(),
                      "points": _number(item.get("weight")), "applied": True}
            group = groups.get(str(item.get("group_id")))
            if group and str(group.get("description") or "").strip():
                record["group"] = str(group["description"]).strip()
            items.append(record)
        for entry in list(answer.get("evaluations") or []) + list(answer.get("annotations") or []):
            text = _text_of(entry)
            if text and text not in comments:
                comments.append(text)
        _sign_rubric_items(question, items, _number(answer.get("score")), _number(question.get("weight")))
        number = str(question.get("full_index") or question.get("index") or qid)
        title = str(question.get("numbered_title") or question.get("title") or "").strip()
        leaves.append({
            "id": number,
            "title": title or f"Question {number}",
            "score": _number(answer.get("score")),
            "points_possible": _number(question.get("weight")),
            "rubric_items": items,
            "comments": comments,
        })
    current = props.get("current_user") if isinstance(props.get("current_user"), dict) else {}
    owners = [o.get("user_id") for o in props.get("ownerships") or [] if isinstance(o, dict) and o.get("active", True)]
    owner = current.get("id") if current.get("id") in owners else (owners[0] if owners else submission.get("uploaded_by_user_id"))
    return {
        "assignment": str(assignment.get("title") or ""),
        "score": _number(submission.get("score")),
        "points_possible": _number(assignment.get("total_points")),
        "questions": leaves,
        "comments": [],
        "updated_at": submission.get("updated_at") or submission.get("created_at"),
        "owner_id": owner,
        "current_user_id": current.get("id"),
        "submission_id": None if submission.get("id") is None else str(submission.get("id")),
    }


def _from_props(props: dict) -> dict | None:
    if isinstance(props.get("assignment_submission"), dict):
        return _from_viewer(props)
    submission = props.get("submission") if isinstance(props.get("submission"), dict) else {}
    assignment = props.get("assignment") if isinstance(props.get("assignment"), dict) else {}
    questions_raw = props.get("questions")
    if not submission and not isinstance(questions_raw, list):
        return None
    current = props.get("current_user") if isinstance(props.get("current_user"), dict) else {}
    total = assignment.get("total_points")
    if total is None:
        total = assignment.get("points_possible")
    return {
        "assignment": str(assignment.get("title") or assignment.get("name") or ""),
        "score": _number(submission.get("score")),
        "points_possible": _number(total),
        "questions": [_question_from_props(item) for item in (questions_raw or []) if isinstance(item, dict)],
        "comments": [],
        "updated_at": submission.get("updated_at") or submission.get("graded_at"),
        "owner_id": submission.get("owner_id") if submission.get("owner_id") is not None else submission.get("user_id"),
        "current_user_id": current.get("id") if current.get("id") is not None else props.get("current_user_id"),
        "submission_id": None if submission.get("id") is None else str(submission.get("id")),
    }


def _from_outline(document: BeautifulSoup) -> dict:
    questions = []
    for rubric in document.select("[id$='-rubric']"):
        anchor_id = rubric.get("id") or ""
        match = QUESTION_ANCHOR.fullmatch(anchor_id)
        if not match:
            continue
        index = match.group(1)
        container = rubric.find_parent(class_=re.compile(r"question", re.I)) or rubric.parent
        link = container.select_one(f'a[href="#{anchor_id}"]') if container else None
        title = link.get_text(" ", strip=True) if link else ""
        score_node = container.select_one('[class*="score"]') if container else None
        score, maximum = _score_pair(score_node.get_text(" ", strip=True) if score_node else "")
        items = []
        comments = []
        for item in rubric.select(".rubricItem"):
            if not _applied(item):
                continue
            description = ""
            described = item.select_one(".rubricItem--description")
            if described:
                description = described.get_text(" ", strip=True)
            points_node = item.select_one(".rubricItem--points")
            comment_node = item.select_one(".rubricItem--comment")
            comment = comment_node.get_text(" ", strip=True) if comment_node else ""
            record = {"description": description, "points": _number(points_node.get_text(" ", strip=True) if points_node else None), "applied": True}
            if comment:
                record["comment"] = comment
                comments.append(comment)
            items.append(record)
        questions.append({
            "id": index,
            "title": title or f"Question {index}",
            "score": score,
            "points_possible": maximum,
            "rubric_items": items,
            "comments": comments,
        })
    total_node = document.select_one(".submissionOutlineHeader--score, .submissionOutline--score")
    total, total_max = _score_pair(total_node.get_text(" ", strip=True) if total_node else "")
    stamped = document.select_one("time[datetime]")
    return {
        "assignment": page_title(document),
        "score": total,
        "points_possible": total_max,
        "questions": questions,
        "comments": [],
        "updated_at": stamped.get("datetime") if stamped else None,
        "owner_id": None,
        "current_user_id": None,
        "submission_id": None,
    }


def parse_submission_document(document: BeautifulSoup) -> dict:
    for props in _react_payloads(document):
        parsed = _from_props(props)
        if parsed and (parsed.get("questions") or parsed.get("score") is not None or parsed.get("submission_id")):
            if not parsed.get("assignment"):
                parsed["assignment"] = page_title(document)
            return parsed
    return _from_outline(document)


def belongs_to_viewer(parsed: dict) -> bool:
    owner = parsed.get("owner_id")
    current = parsed.get("current_user_id")
    if owner in (None, "") or current in (None, ""):
        return True
    return str(owner) == str(current)


def collect_scope(document: BeautifulSoup, base: str, scope: str) -> dict:
    """Assignment pages and submission links that belong to the signed-in student.

    Links inside a roster-sized table, or a page with too many bare submission
    links, are left alone. This does not open Gradescope's grading dashboard.
    """
    assignments = []
    seen_assignments = set()
    candidates = []
    for anchor in document.select("a[href]"):
        raw = urljoin(base, anchor.get("href") or "")
        parts = urlsplit(raw)
        if parts.scheme != "https" or (parts.hostname or "").lower() not in GRADESCOPE:
            continue
        path = parts.path.rstrip("/")
        assignment = re.fullmatch(rf"/courses/{re.escape(scope)}/assignments/(\d+)", path)
        if assignment:
            if assignment.group(1) not in seen_assignments:
                seen_assignments.add(assignment.group(1))
                assignments.append({
                    "id": assignment.group(1),
                    "title": anchor.get_text(" ", strip=True),
                    "url": f"https://{parts.hostname}{path}",
                })
            continue
        submission = SUBMISSION.fullmatch(path.removesuffix(".pdf"))
        if submission and submission.group(1) == scope:
            candidates.append((anchor, submission.groups()))

    blocked = set()
    by_table: dict[int, list] = {}
    for anchor, groups in candidates:
        table = anchor.find_parent("table")
        if table is not None:
            by_table.setdefault(id(table), []).append(groups)
        for parent in anchor.parents:
            if not hasattr(parent, "get"):
                continue
            blob = " ".join(token.casefold() for token in _classes(parent))
            if any(mark in blob for mark in ("roster", "submissionstable", "submissions-table", "gradingdashboard")):
                blocked.add(groups)
                break
    for groups in by_table.values():
        if len({item for item in groups}) > 3:
            blocked.update(groups)
    own = []
    for _anchor, groups in candidates:
        if groups in blocked or groups in own:
            continue
        own.append(groups)
    roster = (bool(candidates) and not own) or len(own) > MAX_OWN_SUBMISSIONS
    if len(own) > MAX_OWN_SUBMISSIONS:
        own = []
    return {"assignments": assignments, "submissions": own, "roster": roster}


def content_fingerprint(parsed: dict, submission_id: str) -> dict:
    payload = {
        "assignment": parsed.get("assignment") or "",
        "score": parsed.get("score"),
        "points_possible": parsed.get("points_possible"),
        "questions": parsed.get("questions") or [],
        "comments": parsed.get("comments") or [],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return {
        "submission_id": str(submission_id),
        "updated_at": parsed.get("updated_at") or "",
        "content_hash": digest,
    }


def render_markdown(record: dict) -> str:
    score = record.get("score")
    maximum = record.get("points_possible")
    score_text = "not graded" if score is None else str(score)
    if maximum is not None:
        score_text += f" / {maximum}"
    lines = [
        f'# Gradescope feedback: {record.get("assignment") or "Assignment"}',
        f'Course: {record.get("course") or ""}',
        f'Canvas course: {record.get("canvas_course_id") or ""}',
        f'Gradescope submission: {record.get("submission_id") or ""}',
        f'Score: {score_text}',
        f'Updated: {record.get("updated_at") or "unknown"}',
        "",
    ]
    for question in record.get("questions") or []:
        qscore = question.get("score")
        qmax = question.get("points_possible")
        qtext = "ungraded" if qscore is None else str(qscore)
        if qmax is not None:
            qtext += f" / {qmax}"
        lines.append(f'## {question.get("title") or question.get("id") or "Question"}')
        lines.append(f"Score: {qtext}")
        for item in question.get("rubric_items") or []:
            lines.append(f'- {item.get("description") or "Rubric item"}{_points_suffix(item.get("points"))}')
            if item.get("comment"):
                lines.append(f'  Feedback: {item["comment"]}')
        item_comments = {item.get("comment") for item in question.get("rubric_items") or []}
        for comment in question.get("comments") or []:
            if comment and comment not in item_comments:
                lines.append(f"- Comment: {comment}")
        lines.append("")
    for comment in record.get("comments") or []:
        lines.append(f"- {comment}")
    return "\n".join(lines).rstrip() + "\n"


def _pdf_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 5:
        return False
    with path.open("rb") as handle:
        return handle.read(5) == b"%PDF-"


def _match_canvas_assignment(title: str, assignments: list[dict]) -> dict | None:
    named = [item for item in assignments if isinstance(item, dict) and item.get("name")]
    exact = [item for item in named if normalize_title(item.get("name")) == normalize_title(title)]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    partial = [item for item in named if titles_match(item.get("name"), title)]
    if len(partial) == 1:
        return partial[0]
    return None


def link_canvas_assignments(root: Path, course: dict, assignments: list[dict]) -> int:
    """Record a Canvas assignment id when the Gradescope title matches one assignment."""
    from unisync.services.sync.submissions import safe_name, write_changed

    course_name = safe_name(course.get("course_code") or course.get("name") or course.get("id"))
    folder = Path(root) / course_name / "gradescope"
    if not folder.exists():
        return 0
    updated = 0
    for manifest in folder.glob("*/*/*/submission.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        match = _match_canvas_assignment(str(data.get("assignment") or ""), assignments)
        canvas_id = str(match.get("id")) if match else ""
        canvas_url = str((match or {}).get("html_url") or "")
        canvas_name = str((match or {}).get("name") or "")
        if (
            str(data.get("canvas_assignment_id") or "") == canvas_id
            and str(data.get("canvas_url") or "") == canvas_url
            and str(data.get("canvas_assignment") or "") == canvas_name
            and str(data.get("canvas_course_id") or "") == str(course.get("id") or "")
        ):
            continue
        data["canvas_course_id"] = str(course.get("id") or "")
        data["canvas_assignment_id"] = canvas_id or None
        data["canvas_assignment"] = canvas_name or None
        data["canvas_url"] = canvas_url or None
        write_changed(manifest, json.dumps(data, indent=2, ensure_ascii=False))
        updated += 1
    return updated


def _script_redirect(text: str, base: str) -> str | None:
    """Return the trusted Gradescope URL a Turnitin launch page redirects to in JS."""
    match = _SCRIPT_REDIRECT.search(text or "")
    if not match:
        return None
    try:
        target = json.loads('"' + match.group(1) + '"')
    except ValueError:
        return None
    target = urljoin(base, target)
    return target if trusted(target) and (urlsplit(target).hostname or "") in GRADESCOPE else None


async def page(client, url: str):
    """Follow the Canvas tool iframe, redirects, and hidden LTI launch forms."""
    method, fields = "GET", None
    for _ in range(12):
        if not trusted(url):
            host = urlsplit(url).hostname or ""
            path = urlsplit(url).path
            logger.warning("gradescope launch rejected host=%s path=%s", host, path)
            raise ValueError("Gradescope launch redirected to an untrusted host")
        await _pause()
        with quiet_http_logs():
            response = await client.request(method, url, data=fields, follow_redirects=False,
                                            headers={"Accept": HTML_ACCEPT})
        parts = urlsplit(url)
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                _log_hop(response.status_code, url)
                raise ValueError(f"HTTP {response.status_code} {parts.hostname} {parts.path}")
            dest = urljoin(url, location)
            _log_hop(response.status_code, url, dest)
            if not trusted(dest):
                logger.warning(
                    "gradescope launch rejected host=%s path=%s status=%s",
                    urlsplit(dest).hostname, urlsplit(dest).path, response.status_code,
                )
                raise ValueError("Gradescope launch redirected to an untrusted host")
            url = dest
            if response.status_code not in (307, 308):
                method, fields = "GET", None
            continue
        if response.status_code >= 400:
            _log_hop(response.status_code, url)
            raise ValueError(f"HTTP {response.status_code} {parts.hostname} {parts.path}")
        _log_hop(response.status_code, url)
        body = response.content or b""
        if body[:5] == b"%PDF-" or "pdf" in response.headers.get("content-type", "").lower():
            raise ValueError("Gradescope launch returned a file instead of a page")
        document = BeautifulSoup(response.text, "html.parser")
        frame = document.select_one('iframe[id^="tool_content"][src], iframe[title="Gradescope"][src]')
        if frame and frame.get("src"):
            nxt = urljoin(url, frame["src"])
            if trusted(nxt):
                url = nxt
                method, fields = "GET", None
                continue
        form = document.select_one(
            'form#tool_form, form[id^="tool_form"], form#no-thirdparty-cookies, form.js-ltiv1p3Return, form:has(input[name="id_token"]), form:has(input[name="lti_message_type"])'
        )
        if form and form.get("method", "get").lower() == "post":
            action = urljoin(url, form.get("action") or url)
            if not trusted(action):
                raise ValueError("Gradescope launch form targets an untrusted host")
            fields = {
                field["name"]: field.get("value", "")
                for field in form.select('input[type="hidden"][name]')
                if field.get("name")
            }
            url = action
            method = "POST"
            continue
        if parts.hostname == TURNITIN_LTI:
            # Turnitin's /launch page continues with `window.location = redirect_url`
            # (a Gradescope OIDC login URL) once its state cookie is present.
            script_redirect = _script_redirect(response.text, url)
            if script_redirect:
                url, method, fields = script_redirect, "GET", None
                continue
        landed = urlsplit(url)
        if landed.hostname not in GRADESCOPE or landed.path.startswith(("/login", "/auth")):
            raise ValueError("Gradescope launch did not reach a student course")
        return url, document
    raise ValueError("Gradescope launch exceeded redirect limit")


async def _save_pdf(sync, endpoint: str, target: Path, counts: dict, regrade: bool) -> dict:
    staged = target / "graded.download"
    pdf = target / "graded.pdf"
    staged.unlink(missing_ok=True)
    try:
        await _pause()
        result = await sync.download({"url": endpoint}, staged)
        if result.get("status") == "failed":
            counts["failed"] += 1
            message = str(result.get("error") or "Gradescope PDF download failed")
            _remember(counts, message)
            return {"filename": "graded.pdf", "status": "failed", "error": message}
        with staged.open("rb") as handle:
            valid = handle.read(5) == b"%PDF-"
        if not valid:
            counts["failed"] += 1
            _remember(counts, "Gradescope file was not a PDF")
            return {"filename": "graded.pdf", "status": "failed", "error": "Gradescope file was not a PDF"}
        staged.replace(pdf)
        counts["regraded" if regrade else "downloaded"] += 1
        saved = {"filename": "graded.pdf", "status": "available"}
        if result.get("sha256"):
            saved["sha256"] = result["sha256"]
        return saved
    finally:
        staged.unlink(missing_ok=True)


def _submission_record(course_name: str, canvas_id: str, parsed: dict, groups: tuple[str, str, str], fingerprint: dict, files: list[dict], previous: dict) -> dict:
    gs_course, assignment_id, submission_id = groups
    record = {
        "source": "gradescope",
        "record_kind": "gradescope_submission",
        "course": course_name,
        "canvas_course_id": canvas_id,
        "gradescope_course_id": gs_course,
        "assignment_id": assignment_id,
        "assignment": parsed.get("assignment") or previous.get("assignment") or "",
        "submission_id": submission_id,
        "score": parsed.get("score"),
        "points_possible": parsed.get("points_possible"),
        "updated_at": parsed.get("updated_at"),
        "questions": parsed.get("questions") or [],
        "comments": parsed.get("comments") or [],
        "gradescope_url": (
            f"https://www.gradescope.com/courses/{gs_course}/assignments/{assignment_id}/submissions/{submission_id}"
        ),
        "fingerprint": fingerprint,
        "files": files,
    }
    for key in ("canvas_assignment_id", "canvas_assignment", "canvas_url"):
        if previous.get(key) and key not in record:
            record[key] = previous[key]
    if files and files[0].get("sha256"):
        record["pdf_sha256"] = files[0]["sha256"]
    elif previous.get("pdf_sha256"):
        record["pdf_sha256"] = previous["pdf_sha256"]
    return record


async def _archive_one(sync, folder: Path, course_name: str, canvas_id: str, host: str, parsed: dict, groups: tuple[str, str, str], counts: dict) -> None:
    from unisync.services.sync.submissions import write_changed

    gs_course, assignment_id, submission_id = groups
    target = folder / "gradescope" / gs_course / assignment_id / submission_id
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / "submission.json"
    previous = {}
    if manifest.exists():
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except ValueError:
            previous = {}
    fingerprint = content_fingerprint(parsed, submission_id)
    pdf = target / "graded.pdf"
    old = previous.get("fingerprint") or {}
    if (
        old.get("submission_id") == fingerprint["submission_id"]
        and old.get("updated_at") == fingerprint["updated_at"]
        and old.get("content_hash") == fingerprint["content_hash"]
        and _pdf_ok(pdf)
    ):
        counts["cached"] += 1
        return
    regrade = bool(previous) and _pdf_ok(pdf)
    endpoint = f"https://{host}/courses/{gs_course}/assignments/{assignment_id}/submissions/{submission_id}.pdf"
    saved = await _save_pdf(sync, endpoint, target, counts, regrade)
    if saved.get("status") != "available" and previous.get("pdf_sha256") and _pdf_ok(pdf):
        saved = {"filename": "graded.pdf", "status": "available", "sha256": previous.get("pdf_sha256")}
    record = _submission_record(course_name, canvas_id, parsed, groups, fingerprint, [saved], previous)
    write_changed(manifest, json.dumps(record, indent=2, ensure_ascii=False))
    write_changed(target / "feedback.md", render_markdown(record))


async def archive_gradescope(sync, course, tab) -> dict:
    from unisync.services.sync.submissions import safe_name

    counts = {"downloaded": 0, "cached": 0, "regraded": 0, "failed": 0, "skipped": 0, "errors": []}
    if not isinstance(tab, dict):
        raise ValueError("Unexpected Canvas Gradescope tab")
    canvas_id = str(course["id"])
    launch = urljoin(f"https://{CANVAS}", str(tab.get("html_url") or tab.get("url") or ""))
    launch_parts = urlsplit(launch)
    if (
        not trusted(launch)
        or launch_parts.hostname != CANVAS
        or not re.fullmatch(rf"/courses/{re.escape(canvas_id)}/external_tools/\d+", launch_parts.path)
    ):
        raise ValueError("Unexpected Canvas Gradescope tab")
    try:
        landing_url, document = await page(sync.client, launch)
    except Exception as exc:
        message = safe_public_error(exc)
        counts["failed"] += 1
        counts["error"] = message
        _remember(counts, message)
        return counts
    match = re.match(r"/courses/(\d+)", urlsplit(landing_url).path)
    if not match:
        counts["failed"] += 1
        counts["error"] = "Gradescope course could not be identified"
        _remember(counts, counts["error"])
        return counts
    scope = match.group(1)
    host = urlsplit(landing_url).hostname or "www.gradescope.com"
    course_name = safe_name(course.get("course_code") or course.get("name") or canvas_id)
    folder = sync.root / course_name
    titles: dict[str, str] = {}
    submissions: dict[tuple[str, str, str], dict] = {}
    seen: set[str] = set()
    opened = {urlsplit(landing_url).path.rstrip("/")}
    pending: list[tuple[str, BeautifulSoup | None]] = [(landing_url, document)]

    while pending:
        current, doc = pending.pop()
        current_path = urlsplit(current).path.rstrip("/")
        if doc is None:
            if len(opened) >= MAX_PAGES:
                _remember(counts, f"Stopped after {MAX_PAGES} Gradescope pages")
                break
            try:
                current, doc = await page(sync.client, current)
                current_path = urlsplit(current).path.rstrip("/")
                opened.add(current_path)
            except Exception as exc:
                counts["failed"] += 1
                _remember(counts, safe_public_error(exc))
                continue
        if current_path in seen:
            continue
        seen.add(current_path)
        assign_match = re.fullmatch(rf"/courses/{re.escape(scope)}/assignments/(\d+)", current_path)
        if assign_match:
            opened_title = page_title(doc)
            if opened_title:
                titles[assign_match.group(1)] = opened_title
        found = collect_scope(doc, current, scope)
        if found["roster"] and not found["submissions"]:
            counts["skipped"] += 1
            _remember(counts, "Skipped a Gradescope roster page")
        for assignment in found["assignments"]:
            if assignment["title"]:
                titles.setdefault(assignment["id"], assignment["title"])
            path = f"/courses/{scope}/assignments/{assignment['id']}"
            if path in opened:
                continue
            opened.add(path)
            pending.append((f"https://{host}{path}", None))
        for groups in found["submissions"]:
            submissions.setdefault(groups, {})

    for groups in sorted(submissions):
        gs_course, assignment_id, submission_id = groups
        if len(opened) >= MAX_PAGES:
            _remember(counts, f"Stopped after {MAX_PAGES} Gradescope pages")
            break
        page_url = f"https://{host}/courses/{gs_course}/assignments/{assignment_id}/submissions/{submission_id}"
        opened.add(urlsplit(page_url).path.rstrip("/"))
        try:
            final_url, sub_doc = await page(sync.client, page_url)
        except Exception as exc:
            counts["failed"] += 1
            _remember(counts, safe_public_error(exc))
            continue
        final = SUBMISSION.fullmatch(urlsplit(final_url).path.rstrip("/").removesuffix(".pdf"))
        if not final or final.group(1) != scope:
            counts["skipped"] += 1
            _remember(counts, "Skipped a Gradescope page outside this course")
            continue
        resolved = final.groups()
        parsed = parse_submission_document(sub_doc)
        if not parsed.get("assignment"):
            parsed["assignment"] = titles.get(resolved[1]) or titles.get(assignment_id) or ""
        if not belongs_to_viewer(parsed):
            counts["skipped"] += 1
            _remember(counts, "Skipped another student's Gradescope submission")
            continue
        await _archive_one(sync, folder, course_name, canvas_id, host, parsed, resolved, counts)
    return counts
