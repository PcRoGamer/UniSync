"""Authoritative course identity and transcript ownership for UniSync MCP.

Course-specific results are admitted only when their source provenance matches the
requested enrolled subject. Unknown course ownership is never inferred from text.
"""
from __future__ import annotations

import re
from pathlib import Path

CODE_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}\d{5})(?![A-Z0-9])", re.I)


def enrolled_courses() -> dict[str, dict[str, str]]:
    from unisync.api.lectures import _load_libraries

    courses: dict[str, dict[str, str]] = {}
    for canvas_id, entry in _load_libraries().items():
        title = str(entry.get("name") or "")
        found = CODE_RE.search(title)
        if not found:
            continue
        code = found.group(1).upper()
        name = re.sub(r"\([A-Z]{4}\d{5}[^)]*\)", "", title, flags=re.I).strip()
        courses[code] = {
            "code": code,
            "name": name,
            "full_title": title,
            "canvas_id": str(canvas_id),
            "section_id": str(entry.get("echo360_course_id") or "").lower(),
        }
    return courses



def archived_courses() -> dict[str, dict[str, str]]:
    """Explicitly verified Echo360 course identities, not LMS enrolments."""
    from unisync.core.path_service import get_path_service
    import json
    source = get_path_service().get_user_root() / "libraries" / "echo360_section_subjects.json"
    if not source.is_file():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    results: dict[str, dict[str, str]] = {}
    if payload.get("scope") != "archived_unlinked_not_canvas_enrolments":
        return {}
    for section_id, value in payload.get("sections", {}).items():
        if not isinstance(value, dict) or value.get("enrolled") is not False:
            continue
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", section_id):
            continue
        code = str(value.get("code", "")).upper()
        name = str(value.get("name", "")).strip()
        if not CODE_RE.fullmatch(code) or not name:
            continue
        if code in results:
            # Do not guess if separate archived sections conflict.
            raise ValueError(f"Duplicate archived course code {code}")
        results[code] = {
            "code": code,
            "name": name,
            "full_title": f"{name} ({code}_ARCHIVED_UNLINKED)",
            "canvas_id": "",
            "section_id": section_id.lower(),
            "enrolled": False,
        }
    return results


def known_courses() -> dict[str, dict[str, str]]:
    """Enrolments plus separately labelled, verified archived source subjects."""
    active = enrolled_courses()
    for code, entry in archived_courses().items():
        # A previously archived subject may later become a current enrolment.
        # Prefer the active course record without losing its archived VTT UUID.
        active.setdefault(code, entry)
    return active


def resolve_course(course: str | None, *, catalog=None) -> str | None:
    """Return enrolled course code; invalid/ambiguous filters are errors, not all-courses."""
    if course is None or not str(course).strip():
        return None
    catalog = catalog or known_courses()
    query = str(course).strip().casefold()
    if query.upper() in catalog:
        return query.upper()
    mentioned = [
        c for c in catalog
        if re.search(r"(?<![a-z0-9])" + re.escape(c.casefold()) + r"(?![a-z0-9])", query)
    ]
    if len(mentioned) == 1:
        return mentioned[0]
    if len(mentioned) > 1:
        raise ValueError("Ambiguous subject identifier; provide one course code")
    # Conservative name aliases: at least five characters and exactly one subject.
    matching = [
        c for c, item in catalog.items()
        if len(query) >= 5 and (
            query == item["name"].casefold()
            or query == item["full_title"].casefold()
            or query in item["name"].casefold()
        )
    ]
    if len(matching) == 1:
        return matching[0]
    raise ValueError("Unknown or ambiguous course; use an enrolled subject code")


def matches_course(source: object, code: str | None, *, catalog=None) -> bool:
    """Source ownership check. Mentions of a course in arbitrary text don't count."""
    if code is None:
        return True
    if not source:
        return False
    value = str(source).strip()
    codes = {m.group(1).upper() for m in CODE_RE.finditer(value)}
    if codes:
        return codes == {code}
    catalog = catalog or known_courses()
    item = catalog.get(code)
    return bool(item and value.casefold() in {item["name"].casefold(), item["full_title"].casefold()})


def lecture_course(filename: str, *, catalog=None) -> str | None:
    """Resolve ownership using Echo360 section UUID or well-defined legacy prefix."""
    catalog = catalog or known_courses()
    name = Path(filename).name.lower()
    owners = {
        code for code, item in catalog.items()
        if item["section_id"] and item["section_id"] in name
    }
    # Preserve old section UUIDs when an archived course becomes enrolled.
    owners.update(
        code for code, item in archived_courses().items()
        if item["section_id"] and item["section_id"] in name
    )
    # Historical BLAW transcript downloads used BLAW_EMBED_ source names.
    if name.startswith("blaw_embed_") and "BLAW10001" in catalog:
        owners.add("BLAW10001")
    return next(iter(owners)) if len(owners) == 1 else None


def course_dirs(root: Path, code: str | None, *, catalog=None) -> list[Path]:
    if not root.exists():
        return []
    if code is None:
        return [root]
    catalog = catalog or known_courses()
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and matches_course(d.name, code, catalog=catalog)
    )
