"""Archive the current student's Gradescope copies linked from Canvas."""
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

CANVAS = "canvas.lms.unimelb.edu.au"
GRADESCOPE = {"www.gradescope.com", "gradescope.com"}
HOSTS = GRADESCOPE | {CANVAS}
SUBMISSION = re.compile(r"/courses/(\d+)/assignments/(\d+)/submissions/(\d+)")
COURSE = re.compile(r"/courses/(\d+)")


def trusted(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in HOSTS


def links(document, base):
    for anchor in document.select("a[href]"):
        url = urljoin(base, anchor["href"])
        if trusted(url) and urlsplit(url).netloc in GRADESCOPE:
            yield url


async def page(client, url):
    """Follow the Canvas tool iframe and hidden LTI launch forms."""
    method, fields = "GET", None
    for _ in range(12):
        if not trusted(url):
            raise ValueError("Gradescope launch needs an authenticated session")
        response = await client.request(method, url, data=fields, follow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            if response.status_code not in (307, 308):
                method, fields = "GET", None
            continue
        response.raise_for_status()
        document = BeautifulSoup(response.text, "html.parser")
        frame = document.select_one('iframe[id^="tool_content"][src], iframe[title="Gradescope"][src]')
        if frame:
            url = urljoin(url, frame["src"])
            method, fields = "GET", None
            continue
        form = document.select_one('form#tool_form, form:has(input[name="id_token"]), form:has(input[name="lti_message_type"])')
        if form and form.get("method", "").lower() == "post":
            url = urljoin(url, form.get("action") or url)
            fields = {field["name"]: field.get("value", "") for field in form.select('input[type="hidden"][name]')}
            method = "POST"
            continue
        if urlsplit(url).netloc not in GRADESCOPE or urlsplit(url).path.startswith(("/login", "/auth")):
            raise ValueError("Gradescope launch did not reach a student course")
        return url, document
    raise ValueError("Gradescope launch exceeded redirect limit")


async def archive_gradescope(sync, course, tab):
    from unisync.services.sync.submissions import safe_name

    canvas_id = str(course["id"])
    launch = urljoin(f"https://{CANVAS}", tab.get("html_url") or tab.get("url") or "")
    if not trusted(launch) or urlsplit(launch).netloc != CANVAS or not re.fullmatch(
        rf"/courses/{re.escape(canvas_id)}/external_tools/\d+", urlsplit(launch).path
    ):
        raise ValueError("Unexpected Canvas Gradescope tab")
    url, document = await page(sync.client, launch)
    match = COURSE.match(urlsplit(url).path)
    if not match:
        raise ValueError("Gradescope course could not be identified")
    scope = match[1]
    folder = sync.root / safe_name(course.get("course_code") or course.get("name") or canvas_id)
    pending = [(url, document)]
    seen, submissions = set(), set()
    counts = {"downloaded": 0, "cached": 0, "failed": 0}
    while pending:
        current, doc = pending.pop()
        current_path = urlsplit(current).path.rstrip("/")
        if current_path in seen:
            continue
        seen.add(current_path)
        for candidate in [current, *links(doc, current)]:
            parts = urlsplit(candidate)
            path = parts.path.rstrip("/")
            submission = SUBMISSION.fullmatch(path.removesuffix(".pdf"))
            if submission and submission[1] == scope:
                submissions.add(submission.groups())
                continue
            if path not in seen and re.fullmatch(rf"/courses/{scope}(?:/assignments/\d+)?", path):
                try:
                    pending.append(await page(sync.client, f"https://{parts.netloc}{path}"))
                except Exception:
                    counts["failed"] += 1
    for gs_course, assignment, submission in sorted(submissions):
        target = folder / "gradescope" / gs_course / assignment / submission
        target.mkdir(parents=True, exist_ok=True)
        pdf = target / "graded.pdf"
        if pdf.exists():
            with pdf.open("rb") as existing:
                if existing.read(5) == b"%PDF-":
                    counts["cached"] += 1
                    continue
        endpoint = f"https://www.gradescope.com/courses/{gs_course}/assignments/{assignment}/submissions/{submission}.pdf"
        staged = target / "graded.download"
        try:
            result = await sync.download({"url": endpoint}, staged)
            if result["status"] == "failed":
                counts["failed"] += 1
                continue
            with staged.open("rb") as downloaded:
                valid = downloaded.read(5) == b"%PDF-"
            if valid:
                staged.replace(pdf)
                counts["downloaded"] += 1
            else:
                counts["failed"] += 1
        finally:
            staged.unlink(missing_ok=True)
    return counts
