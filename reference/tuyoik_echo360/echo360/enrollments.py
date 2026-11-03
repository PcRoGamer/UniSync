import requests

from pick import pick

from .utils import (
    load_saved_login_email,
    pick_with_line_highlight,
    save_login_email,
)


def session_from_driver(driver):
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"])
    return session


def fetch_enrollments(hostname, session):
    url = "{}/user/enrollments".format(hostname.rstrip("/"))
    r = session.get(url, headers={"Accept": "application/json"}, timeout=30)
    if not r.ok:
        raise Exception(
            "Failed to load enrollments from {} (HTTP {}). "
            "Log in at {}/courses first.".format(url, r.status_code, hostname.rstrip("/"))
        )
    payload = r.json()
    if payload.get("status") != "ok":
        raise Exception(
            "Enrollments API error: {}".format(payload.get("message") or payload.get("status"))
        )
    return payload["data"][0]["userSections"]


def group_sections_by_course(user_sections):
    by_course = {}
    for section in user_sections:
        code = section.get("courseCode") or section.get("courseId")
        if code not in by_course:
            by_course[code] = {
                "courseCode": section.get("courseCode", ""),
                "courseName": section.get("courseName", ""),
                "courseId": section.get("courseId"),
                "sections": [],
            }
        by_course[code]["sections"].append(section)
    return sorted(by_course.values(), key=lambda c: c["courseCode"])


def pick_enrolled_courses(user_sections):
    courses = group_sections_by_course(user_sections)
    if not courses:
        raise Exception("No enrolled courses found in enrollments response.")
    options = [
        "{code} - {name} ({streams} streams, {lessons} lessons)".format(
            code=c["courseCode"],
            name=c["courseName"],
            streams=len(c["sections"]),
            lessons=sum(s.get("lessonCount", 0) for s in c["sections"]),
        )
        for c in courses
    ]
    selected = pick(
        options,
        "Select course(s) to download (SPACE to mark, ENTER to continue):",
        multiselect=True,
        min_selection_count=1,
    )
    return [courses[s[1]] for s in selected]


def pick_enrolled_course(user_sections):
    """Pick a single course. Prefer pick_enrolled_courses for multiselect."""
    picked = pick_enrolled_courses(user_sections)
    return picked[0]


def resolve_echo360_email():
    """Confirm saved login email (pick TUI) or prompt for a new one."""
    from sys import version_info

    input_fn = input if version_info[0] > 2 else raw_input
    saved = load_saved_login_email()
    if saved:
        options = [
            "Use saved — {}".format(saved),
            "Enter a different email",
        ]
        selected, _ = pick_with_line_highlight(
            options,
            "Echo360 login (↑↓ move, ENTER to choose)\n"
            "Not sure which email works? Check Echo360 -> Settings "
            "(account/profile) for your exact login email:",
        )
        if selected == options[0]:
            return saved
    print(
        ">> Tip: find the email that works in Echo360 -> Settings "
        "(account/profile) — use that exact address."
    )
    email = input_fn(">> Echo360 login email: ").strip()
    if email:
        save_login_email(email)
    return email or None
