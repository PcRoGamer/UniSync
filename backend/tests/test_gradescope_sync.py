"""Offline Gradescope launch, crawl, rubric, incremental archive, and MCP tests."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from unisync.mcp.egress import scrub
from unisync.mcp.submission_archive import load_gradescope_feedback, load_submission_archive
from unisync.services.sync.gradescope import (
    archive_gradescope,
    belongs_to_viewer,
    collect_scope,
    link_canvas_assignments,
    parse_submission_document,
    trusted,
)
from unisync.services.sync.submissions import SubmissionSync

FIXTURES = Path(__file__).parent / "fixtures" / "gradescope"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
SECRET = "login-hint-SHOULD-NOT-LOG"
TOKEN = "id-token-SHOULD-NOT-LOG"
CANVAS = "canvas.lms.unimelb.edu.au"
TOOL = "/courses/241246/external_tools/5945"


@pytest.fixture(autouse=True)
def no_gradescope_pause(monkeypatch):
    monkeypatch.setattr("unisync.services.sync.gradescope.REQUEST_PAUSE", 0)


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_launch_hosts_are_explicit_and_https_only():
    assert trusted(f"https://{CANVAS}{TOOL}")
    assert trusted("https://sso.canvaslms.com/api/lti/authorize_redirect")
    assert trusted("https://canvas.instructure.com/api/lti/authorize_redirect")
    assert trusted("https://www.gradescope.com/courses/1375834")
    assert not trusted("http://sso.canvaslms.com/api/lti/authorize_redirect")
    assert not trusted("https://sso.beta.canvaslms.com/api/lti/authorize_redirect")
    assert not trusted("https://sso.canvaslms.com.evil.example/api/lti/authorize_redirect")
    assert not trusted("https://user:secret@sso.canvaslms.com/api/lti/authorize_redirect")
    assert not trusted("https://evil.example/courses/1375834")


def test_outline_and_react_rubrics_keep_applied_items_only():
    outline = parse_submission_document(BeautifulSoup(_html("submission_outline.html"), "html.parser"))
    assert outline["assignment"] == "Mid-semester Test"
    assert outline["score"] == 36.5
    assert outline["points_possible"] == 50
    assert outline["updated_at"] == "2026-10-20T04:12:00Z"
    first, sixth = outline["questions"]
    assert first["id"] == "1.1"
    assert first["score"] == 8
    assert first["points_possible"] == 10
    assert first["rubric_items"] == [{
        "description": "Off-by-one in the loop bound",
        "points": -2,
        "applied": True,
        "comment": "Watch the range.",
    }]
    assert sixth["id"] == "6"
    assert sixth["rubric_items"][0]["description"] == "Incomplete explanation"
    assert "No attempt" not in json.dumps(outline["questions"])

    props = parse_submission_document(BeautifulSoup(_html("submission_react.html"), "html.parser"))
    assert props["submission_id"] == "422550001"
    assert props["score"] == 36.5
    assert belongs_to_viewer(props)
    assert props["questions"][0]["rubric_items"][0]["comment"] == "Watch the range."
    assert "No attempt" not in json.dumps(props["questions"])
    props["owner_id"] = 1
    props["current_user_id"] = 2
    assert not belongs_to_viewer(props)


def test_crawl_scope_ignores_rosters_and_other_courses():
    home = collect_scope(BeautifulSoup(_html("course_home.html"), "html.parser"), "https://www.gradescope.com/courses/1375834", "1375834")
    assert [item["id"] for item in home["assignments"]] == ["8511948"]
    assert home["submissions"] == []

    assignment = collect_scope(
        BeautifulSoup(_html("assignment_student.html"), "html.parser"),
        "https://www.gradescope.com/courses/1375834/assignments/8511948",
        "1375834",
    )
    assert assignment["submissions"] == [("1375834", "8511948", "422550001")]
    assert assignment["roster"] is False

    roster = collect_scope(
        BeautifulSoup(_html("roster.html"), "html.parser"),
        "https://www.gradescope.com/courses/1375834/assignments/8511948",
        "1375834",
    )
    assert roster["submissions"] == []
    assert roster["roster"] is True

    many = "".join(
        f'<a href="/courses/1375834/assignments/8511948/submissions/{index}">Copy {index}</a>'
        for index in range(9)
    )
    crowded = collect_scope(BeautifulSoup(many, "html.parser"), "https://www.gradescope.com/courses/1", "1375834")
    assert crowded["submissions"] == []
    assert crowded["roster"] is True


class LaunchSite:
    def __init__(self, outline: str):
        self.outline = outline
        self.urls = []
        self.bodies = []
        self.pdf_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append((request.method, request.url.host, request.url.path))
        self.bodies.append(request.content.decode())
        path = request.url.path
        if path == TOOL:
            return httpx.Response(200, text=(
                f'<iframe id="tool_content" src="https://canvas.instructure.com/api/lti/authorize_redirect'
                f'?login_hint={SECRET}"></iframe>'
            ))
        if request.url.host == "canvas.instructure.com":
            return httpx.Response(302, headers={
                "location": f"https://sso.canvaslms.com/api/lti/authorize_redirect?nonce={SECRET}",
            })
        if request.url.host == "sso.canvaslms.com":
            return httpx.Response(200, text=(
                '<form id="tool_form" action="https://www.gradescope.com/lti/launch" method="post">'
                f'<input type="hidden" name="id_token" value="{TOKEN}">'
                f'<input type="hidden" name="state" value="{SECRET}">'
                "</form>"
            ))
        if path == "/lti/launch":
            assert request.method == "POST"
            assert TOKEN in request.content.decode()
            return httpx.Response(303, headers={"location": "https://www.gradescope.com/courses/1375834"})
        if path == "/courses/1375834":
            return httpx.Response(200, text=_html("course_home.html"))
        if path == "/courses/1375834/assignments/8511948":
            return httpx.Response(200, text=_html("assignment_student.html"))
        if path == "/courses/1375834/assignments/8511948/submissions/422550001":
            return httpx.Response(200, text=self.outline)
        if path == "/courses/1375834/assignments/8511948/submissions/422550001.pdf":
            self.pdf_requests += 1
            return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        return httpx.Response(404)


def _course() -> dict:
    return {"id": 241246, "course_code": "COMP10001_2026_SM1", "name": "Foundations of Computing"}


def _tab() -> dict:
    return {"label": "Gradescope", "html_url": TOOL}


async def _archive(tmp_path: Path, site: LaunchSite):
    async with httpx.AsyncClient(transport=httpx.MockTransport(site)) as client:
        sync = SubmissionSync(client, tmp_path / "courses")
        result = await archive_gradescope(sync, _course(), _tab())
    return result


def _saved(tmp_path: Path) -> Path:
    return (
        tmp_path / "courses" / "COMP10001_2026_SM1" / "gradescope"
        / "1375834" / "8511948" / "422550001"
    )


@pytest.mark.asyncio
async def test_launch_chain_archives_only_the_student_pdf_and_marks(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    site = LaunchSite(_html("submission_outline.html"))
    result = await _archive(tmp_path, site)
    assert result["downloaded"] == 1
    assert result["failed"] == 0
    assert result["errors"] == []
    saved = _saved(tmp_path)
    assert saved.joinpath("graded.pdf").read_bytes().startswith(b"%PDF-")
    assert not list(saved.parent.rglob("*.part"))
    assert not (saved.parent / "100").exists()
    assert not (saved.parent / "101").exists()
    manifest = json.loads((saved / "submission.json").read_text(encoding="utf-8"))
    assert manifest["score"] == 36.5
    assert manifest["points_possible"] == 50
    assert manifest["canvas_course_id"] == "241246"
    assert manifest["questions"][0]["id"] == "1.1"
    assert manifest["questions"][0]["rubric_items"][0]["comment"] == "Watch the range."
    summary = (saved / "feedback.md").read_text(encoding="utf-8")
    assert "Gradescope feedback: Mid-semester Test" in summary
    assert "Off-by-one in the loop bound" in summary
    assert "Question 6" in summary
    assert "No attempt" not in summary
    assert SECRET not in caplog.text
    assert TOKEN not in caplog.text
    assert "sso.canvaslms.com" in caplog.text
    assert "canvas.instructure.com" in caplog.text
    hosts = {host for _method, host, _path in site.urls}
    assert "evil.example" not in hosts
    assert site.pdf_requests == 1

    linked = link_canvas_assignments(tmp_path / "courses", _course(), [{
        "id": 661924,
        "name": "Mid-semester Test",
        "html_url": "https://canvas.lms.unimelb.edu.au/courses/241246/assignments/661924",
    }])
    assert linked == 1
    manifest = json.loads((saved / "submission.json").read_text(encoding="utf-8"))
    assert manifest["canvas_assignment_id"] == "661924"
    assert manifest["fingerprint"]["content_hash"]

    again = await _archive(tmp_path, site)
    assert again["cached"] == 1
    assert again["downloaded"] == 0
    assert again["regraded"] == 0
    assert site.pdf_requests == 1

    site.outline = _html("submission_outline.html").replace("36.5 / 50.0", "40.0 / 50.0")
    regraded = await _archive(tmp_path, site)
    assert regraded["regraded"] == 1
    assert site.pdf_requests == 2
    updated = json.loads((saved / "submission.json").read_text(encoding="utf-8"))
    assert updated["score"] == 40
    assert updated["fingerprint"]["content_hash"] != manifest["fingerprint"]["content_hash"]


@pytest.mark.asyncio
async def test_non_pdf_and_other_student_are_not_stored(tmp_path):
    class Other(LaunchSite):
        def __call__(self, request):
            if request.url.path.endswith("/422550001"):
                self.urls.append((request.method, request.url.host, request.url.path))
                html = _html("submission_react.html").replace('"owner_id":9001', '"owner_id":1')
                return httpx.Response(200, text=html)
            if request.url.path.endswith(".pdf"):
                self.pdf_requests += 1
                return httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "application/octet-stream"})
            return super().__call__(request)

    site = Other(_html("submission_outline.html"))
    result = await _archive(tmp_path, site)
    assert result["skipped"] == 1
    assert result["downloaded"] == 0
    assert site.pdf_requests == 0
    assert not _saved(tmp_path).exists()
    assert any("another student" in message for message in result["errors"])

    class BadPdf(LaunchSite):
        def __call__(self, request):
            if request.url.path.endswith(".pdf"):
                self.pdf_requests += 1
                return httpx.Response(200, content=b"<html>not a pdf</html>", headers={"content-type": "application/octet-stream"})
            return super().__call__(request)

    bad = BadPdf(_html("submission_outline.html"))
    failed = await _archive(tmp_path, bad)
    assert failed["failed"] == 1
    assert failed["downloaded"] == 0
    assert "Gradescope file was not a PDF" in failed["errors"]
    assert not (_saved(tmp_path) / "graded.pdf").exists()
    assert (_saved(tmp_path) / "submission.json").exists()
    assert not list((tmp_path / "courses").rglob("*.download"))


@pytest.mark.asyncio
async def test_untrusted_redirect_does_not_log_the_query(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"https://evil.example/steal?login_hint={SECRET}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sync = SubmissionSync(client, tmp_path / "courses")
        result = await archive_gradescope(sync, _course(), _tab())
    assert result["error"] == "Gradescope launch redirected to an untrusted host"
    assert SECRET not in result["error"]
    assert SECRET not in caplog.text
    assert "evil.example" in caplog.text


@pytest.mark.asyncio
async def test_gradescope_status_uses_a_safe_message(tmp_path):
    class Client:
        async def get(self, url, params=None):
            class Response:
                links = {}

                def raise_for_status(self):
                    return None

                def json(self):
                    if "enrollment_type=student" in url:
                        return [{"id": 241246, "course_code": "COMP10001_2026_SM1"}]
                    if "/tabs" in url:
                        return [{"label": "Gradescope", "html_url": "https://evil.example/courses/241246/external_tools/5945?token=SECRET"}]
                    return []

            return Response()

    result = await SubmissionSync(Client(), tmp_path).run(include_gradescope=True)
    info = result["gradescope"]["241246"]
    assert info["error"] == "Unexpected Canvas Gradescope tab"
    assert "SECRET" not in json.dumps(info)
    assert "token" not in json.dumps(info).lower()


def test_mcp_merges_gradescope_feedback_and_scrubs_secrets(tmp_path):
    saved = _saved(tmp_path)
    saved.mkdir(parents=True)
    record = {
        "source": "gradescope",
        "course": "COMP10001_2026_SM1",
        "canvas_course_id": "241246",
        "gradescope_course_id": "1375834",
        "assignment_id": "8511948",
        "assignment": "Mid-semester Test",
        "canvas_assignment_id": "661924",
        "canvas_assignment": "Mid-semester Test",
        "submission_id": "422550001",
        "score": 36.5,
        "points_possible": 50,
        "updated_at": "2026-10-20T04:12:00Z",
        "questions": [{
            "id": "1.1",
            "title": "Question 1.1",
            "score": 8,
            "points_possible": 10,
            "rubric_items": [{
                "description": "Off-by-one in the loop bound",
                "points": -2,
                "applied": True,
                "comment": "Watch the range.",
            }],
            "comments": ["Watch the range."],
        }],
        "comments": [],
        "gradescope_url": "https://www.gradescope.com/courses/1375834/assignments/8511948/submissions/422550001",
        "id_token": TOKEN,
    }
    (saved / "submission.json").write_text(json.dumps(record), encoding="utf-8")
    (saved / "feedback.md").write_text(
        "# Gradescope feedback: Mid-semester Test\n\nScore: 36.5 / 50.0\n\n"
        "## Question 1.1\n\nOff-by-one in the loop bound. Watch the range.\n",
        encoding="utf-8",
    )
    (saved / "graded.pdf").write_bytes(PDF)
    feedback = load_gradescope_feedback(
        user_root=tmp_path, course="COMP10001", assignment="Mid-semester Test", canvas_assignment_id="661924",
    )
    assert feedback["found"] is True
    assert feedback["score"] == 36.5
    assert feedback["questions"][0]["rubric_items"][0]["comment"] == "Watch the range."
    assert "id_token" not in feedback
    scrubbed = scrub({"id_token": TOKEN, "comment": "Watch the range.", "url": f"https://sso.canvaslms.com/launch?login_hint={SECRET}"})
    assert "id_token" not in scrubbed
    assert SECRET not in scrubbed["url"]
    assert scrubbed["comment"] == "Watch the range."

    from unisync.mcp.server import unisync_get_assignment_detail, unisync_get_gradescope_feedback
    from unisync.services.todos.models import TodoItem

    item = TodoItem(
        id="todo-mst",
        title="Mid-semester Test",
        course="COMP10001 Foundations of Computing",
        source="canvas",
        source_id="canvas:assignment:661924",
        created_at=1,
        updated_at=1,
        url="https://canvas.lms.unimelb.edu.au/courses/241246/assignments/661924",
        score=36.5,
        points_possible=50,
        is_graded=True,
    )

    class Service:
        def list_items(self):
            return [item]

    class Paths:
        def get_user_root(self):
            return tmp_path

    with patch("unisync.services.todos.get_todo_service", return_value=Service()), \
         patch("unisync.core.path_service.get_path_service", return_value=Paths()):
        detail = json.loads(unisync_get_assignment_detail("661924"))
        tool = json.loads(unisync_get_gradescope_feedback("241246", "8511948"))
    assert detail["gradescope"]["submission_id"] == "422550001"
    assert detail["gradescope"]["questions"][0]["title"] == "Question 1.1"
    assert "id_token" not in json.dumps(detail)
    assert tool["found"] is True
    assert tool["score"] == 36.5
    assert TOKEN not in json.dumps(tool)

    canvas_only = tmp_path / "courses" / "BLAW10001_2026_SM2" / "submissions" / "123" / "attempt_1"
    canvas_only.mkdir(parents=True)
    (canvas_only / "submission.json").write_text(json.dumps({
        "record_kind": "grade_or_feedback_only",
        "score": 0,
        "assignment_id": 123,
        "assignment": "Essay Feedback",
        "submission_comments": [{"comment": "Please cite cases."}],
        "canvas_url": "https://canvas.lms.unimelb.edu.au/courses/77/assignments/123",
    }), encoding="utf-8")
    loaded = load_submission_archive(
        user_root=tmp_path,
        course="BLAW10001_2026_SM2",
        canvas_url="https://canvas.lms.unimelb.edu.au/courses/77/assignments/123",
    )
    assert loaded["submission_record_kind"] == "grade_or_feedback_only"
    assert loaded["archived_score"] == 0


def test_gradescope_markdown_is_indexed(tmp_path, monkeypatch):
    course = tmp_path / "courses" / "COMP10001_2026_SM1" / "gradescope" / "1375834" / "8511948" / "422550001"
    course.mkdir(parents=True)
    (course / "feedback.md").write_text(
        "# Gradescope feedback: Mid-semester Test\n\nScore: 36.5 / 50.0\n\n"
        "## Question 1.1\n\nOff-by-one in the loop bound. Watch the range carefully.\n",
        encoding="utf-8",
    )
    from unisync.services.kb.service import KnowledgeBaseService

    service = KnowledgeBaseService(kb_dir=tmp_path / "kb")
    captured = []

    class FakeEmbeddings:
        def index(self, stream):
            captured.extend(list(stream))

        def upsert(self, stream):
            captured.extend(list(stream))

        def save(self, path):
            return None

        def count(self):
            return len(captured)

    monkeypatch.setattr(service, "_get_embeddings", lambda: FakeEmbeddings())
    result = service._index_all_inner(
        courses_dir=tmp_path / "courses",
        subtitles_dir=tmp_path / "no-subtitles",
        emails_dir=tmp_path / "no-emails",
    )
    assert result["files_indexed"] == 1
    assert any("gradescope" in item["file_path"] and "Off-by-one" in item["text"] for item in captured)


@pytest.mark.asyncio
async def test_native_scheduler_archives_submissions():
    from unisync.api import sync_engine

    calls = []

    async def pipeline(*_args, **_kwargs):
        calls.append("pipeline")

    async def submissions(course_id=None):
        calls.append(("submissions", course_id))
        return {"state": "done", "gradescope": {"241246": {"downloaded": 1}}}

    class State:
        def due(self):
            return True

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] > 1:
            raise asyncio.CancelledError()

    with patch.object(sync_engine, "_run_sync_pipeline", pipeline), \
         patch("unisync.services.sync.submissions.sync_submissions", submissions), \
         patch("unisync.services.sync.native_sync.NativeSyncState", State), \
         patch.object(sync_engine.asyncio, "sleep", fake_sleep), \
         patch.object(sync_engine, "log_sync"):
        previous = sync_engine.sync_status.get("is_syncing")
        sync_engine.sync_status["is_syncing"] = False
        try:
            with pytest.raises(asyncio.CancelledError):
                await sync_engine.native_sync_scheduler()
        finally:
            sync_engine.sync_status["is_syncing"] = previous
    assert calls == ["pipeline", ("submissions", None)]


@pytest.mark.asyncio
async def test_sync_submissions_requests_gradescope():
    from unisync.services.sync.submissions import sync_submissions

    with patch.object(SubmissionSync, "run", new_callable=AsyncMock, return_value={"attempts": 0, "gradescope": {}}) as run, \
         patch("unisync.services.kb.get_kb_service") as kb:
        kb.return_value.index_all.return_value = {"files_indexed": 0}
        result = await sync_submissions("241246")
    assert result["state"] == "done"
    assert run.await_args.kwargs["include_gradescope"] is True
    assert run.await_args.args == ("241246",)
