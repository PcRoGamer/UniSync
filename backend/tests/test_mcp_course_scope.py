"""Synthetic regression tests for active and archived course provenance.

Never reads the user's actual enrollment, cache or Echo360 identifiers.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from unisync.core import path_service
from unisync.mcp import course_scope as scope


ARCHIVED_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CURRENT_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OLD_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    libraries = tmp_path / "libraries"
    libraries.mkdir()
    monkeypatch.setattr(path_service, "get_path_service",
                        lambda: SimpleNamespace(get_user_root=lambda: tmp_path))
    active = {"LIVE12345": {
        "code": "LIVE12345", "name": "Current Course",
        "full_title": "Current Course (LIVE12345_2026_SM2)",
        "canvas_id": "100", "section_id": CURRENT_UUID,
    }}
    monkeypatch.setattr(scope, "enrolled_courses", lambda: dict(active))
    record = {"scope": "archived_unlinked_not_canvas_enrolments",
              "sections": {ARCHIVED_UUID: {
                  "code": "ARCH12345", "name": "Historical Course", "enrolled": False,
              }}}
    (libraries / "echo360_section_subjects.json").write_text(
        json.dumps(record), encoding="utf-8")
    return tmp_path, active


def test_archived_section_separate_from_current_enrolments(catalog):
    root, _ = catalog
    archived = scope.archived_courses()
    assert set(archived) == {"ARCH12345"}
    assert archived["ARCH12345"]["enrolled"] is False
    known = scope.known_courses()
    assert set(known) == {"LIVE12345", "ARCH12345"}
    assert scope.resolve_course("Historical Course") == "ARCH12345"
    assert scope.lecture_course("G_lecture_" + ARCHIVED_UUID + ".vtt") == "ARCH12345"
    assert scope.lecture_course("G_lecture_" + CURRENT_UUID + ".vtt") == "LIVE12345"
    assert scope.lecture_course("G_lecture_unknown.vtt") is None
    assert scope.matches_course("ARCH12345_2026_SM1", "ARCH12345")
    assert not scope.matches_course("LIVE12345", "ARCH12345")


def test_unverified_archived_catalog_is_ignored(catalog):
    root, active = catalog
    path = root / "libraries" / "echo360_section_subjects.json"
    for data in (
        {"scope": "wrong", "sections": {ARCHIVED_UUID: {
            "code": "ARCH12345", "name": "Historical Course", "enrolled": False}}},
        {"scope": "archived_unlinked_not_canvas_enrolments",
         "sections": {ARCHIVED_UUID: {
             "code": "ARCH12345", "name": "Historical Course", "enrolled": True}}},
        {"scope": "archived_unlinked_not_canvas_enrolments",
         "sections": {"not-a-uuid": {
             "code": "ARCH12345", "name": "Historical Course", "enrolled": False}}},
    ):
        path.write_text(json.dumps(data), encoding="utf-8")
        assert scope.archived_courses() == {}
        assert set(scope.known_courses()) == set(active)


def test_conflicting_archive_owner_is_rejected(catalog):
    root, _ = catalog
    path = root / "libraries" / "echo360_section_subjects.json"
    path.write_text(json.dumps({
        "scope": "archived_unlinked_not_canvas_enrolments",
        "sections": {
            ARCHIVED_UUID: {"code": "ARCH12345", "name": "Historical", "enrolled": False},
            OLD_UUID: {"code": "ARCH12345", "name": "Historical", "enrolled": False},
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate archived"):
        scope.archived_courses()


def test_explicit_scope_and_directory_boundaries(catalog):
    root, _ = catalog
    courses = root / "courses"
    courses.mkdir()
    (courses / "LIVE12345_2026_SM2").mkdir()
    (courses / "ARCH12345_ARCHIVED").mkdir()
    assert [p.name for p in scope.course_dirs(courses, "ARCH12345")] == [
        "ARCH12345_ARCHIVED"
    ]
    with pytest.raises(ValueError):
        scope.resolve_course("ARCH12345 LIVE12345")
    with pytest.raises(ValueError):
        scope.resolve_course("Unknown course")
