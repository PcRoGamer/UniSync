"""No-attempt, metadata-only Canvas/Respondus assessment link coverage."""
import json
from unisync.services.sync.canvas_assessment_catalog import (
    build_assessment_catalog, safe_course_url, write_assessment_catalog,
)


def test_respondus_exam_link_preserved_no_access_attempt(tmp_path):
    entries = build_assessment_catalog(
        "241246",
        pages=[{
            "url": "mst-and-exam",
            "body": (
                '<p>2025 past exams require Respondus LockDown Browser</p>'
                '<a href="/courses/241246/quizzes/123">2025 S1 exam</a>'
                '<a href="/courses/241246/quizzes/123">2025 S1 exam</a>'
                '<a href="https://other.example/courses/241246/quizzes/999">Other</a>'
            ),
        }],
        quizzes=[{
            "id": 123, "title": "2025 S1 exam",
            "description": "Requires Respondus LockDown Browser",
        }],
    )
    assert len(entries) == 1
    assert entries[0]["url"] == "https://canvas.lms.unimelb.edu.au/courses/241246/quizzes/123"
    assert entries[0]["access"] == "requires_respondus"
    assert set(entries[0]["sources"]) == {"page:mst-and-exam", "quiz"}
    md = tmp_path / "assessment-links.md"
    assert write_assessment_catalog(md, "241246", entries) is True
    assert write_assessment_catalog(md, "241246", entries) is False
    saved = json.loads(md.with_suffix(".json").read_text(encoding="utf-8"))
    assert saved["entries"] == entries
    assert "requires_respondus" in md.read_text(encoding="utf-8")


def test_unknown_quiz_not_incorrectly_labelled_respondus():
    entries = build_assessment_catalog(
        "241246", quizzes=[{"id": 99, "title": "Practice coding quiz"}]
    )
    assert len(entries) == 1
    assert entries[0]["access"] == "canvas_assessment_link"


def test_respondus_not_required_practice_link_is_not_misclassified():
    entries = build_assessment_catalog(
        "241246",
        pages=[{
            "url": "mst-and-exam",
            "body": (
                '<a href="/courses/241246/quizzes/328259">'
                '2025 S1 exam - Respondus not required - ONLY FOR PRACTICE</a>'
            ),
        }],
    )
    assert len(entries) == 1
    assert entries[0]["access"] == "canvas_assessment_link"


def test_explicit_quiz_lockdown_requirement_is_retained():
    entries = build_assessment_catalog(
        "241246",
        quizzes=[{
            "id": 328250,
            "title": "2025 S1 final exam",
            "require_lockdown_browser": True,
        }],
    )
    assert len(entries) == 1
    assert entries[0]["access"] == "requires_respondus"


def test_link_title_wins_over_unrelated_no_respondus_practice_note():
    entries = build_assessment_catalog(
        "241246",
        pages=[{
            "url": "mst-and-exam",
            "body": (
                '<li><a title="Practice Exam - Requires Respondus LockDown Browser" '
                'href="/courses/241246/quizzes/328258">Additional Practice Exam</a> '
                '<span>(requires Respondus LockDown Browser)<br/>'
                '<strong>Additional Practice Exams with no respondus lockdown browser</strong>'
                '</span></li>'
            ),
        }],
    )
    assert len(entries) == 1
    assert entries[0]["access"] == "requires_respondus"


def test_no_respondus_phrase_is_not_classified_as_required():
    entries = build_assessment_catalog(
        "241246",
        pages=[{
            "url": "mst-and-exam",
            "body": (
                '<a href="/courses/241246/quizzes/328257">'
                'Practice exam with no Respondus LockDown Browser</a>'
            ),
        }],
    )
    assert len(entries) == 1
    assert entries[0]["access"] == "canvas_assessment_link"


def test_module_and_assignment_links_preserved():
    entries = build_assessment_catalog(
        "241246",
        modules=[{"items": [{"type": "Quiz", "content_id": 4, "title": "Sample exam"}]}],
        assignments=[{
            "id": 5, "name": "2025 Practice Exam",
            "description": "Respondus required",
        }],
    )
    assert len(entries) == 2
    assert {x["url"].split("/")[-1] for x in entries} == {"4", "5"}
    assert any(x["access"] == "requires_respondus" for x in entries)


def test_off_course_and_sensitive_query_urls_not_archived():
    assert safe_course_url("241246", "https://evil.example/courses/241246/quizzes/1") is None
    assert safe_course_url("241246", "/courses/555/quizzes/1") is None
    assert safe_course_url("241246", "/courses/241246/quizzes/1?access_code=SECRET") == (
        "https://canvas.lms.unimelb.edu.au/courses/241246/quizzes/1"
    )
    assert safe_course_url("241246", "/courses/241246/quizzes/1/attempts") is None
