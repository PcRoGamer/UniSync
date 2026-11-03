"""Persistent state management for Google NotebookLM synchronization."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from unisync.core.file_io import atomic_write_json

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("data/libraries/notebooklm_sync.json")


def _get_project_root() -> Path:
    """Resolve project root directory."""
    return Path(__file__).resolve().parents[4]


def get_state_file_path() -> Path:
    """Return absolute path to notebooklm_sync.json."""
    root = _get_project_root()
    return root / DEFAULT_STATE_FILE


def _default_state_structure() -> dict[str, Any]:
    """Return initial blank state schema."""
    return {
        "version": 1,
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": {
            "tier": "pro",
            "max_sources_per_notebook": 300,
            "max_notebooks": 500,
            "email": None,
        },
        "notebooks": {},  # course_code -> notebook details
    }


def load_sync_state() -> dict[str, Any]:
    """Load persistent sync state from disk, creating default if absent."""
    state_path = get_state_file_path()
    if not state_path.exists():
        initial = _default_state_structure()
        save_sync_state(initial)
        return initial

    try:
        import json
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Invalid sync state file format, resetting to default")
            return _default_state_structure()
        if "notebooks" not in data:
            data["notebooks"] = {}
        return data
    except Exception as exc:
        logger.error("Failed to read NotebookLM sync state: %s", exc)
        return _default_state_structure()


def save_sync_state(state: dict[str, Any]) -> None:
    """Persist sync state atomically to disk."""
    state_path = get_state_file_path()
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_path, state)
    except Exception as exc:
        logger.error("Failed to write NotebookLM sync state: %s", exc)


def get_course_sync_state(course_code: str) -> Optional[dict[str, Any]]:
    """Retrieve tracking record for a specific course code (e.g. 'BLAW10001')."""
    state = load_sync_state()
    return state.get("notebooks", {}).get(course_code.upper())


def update_course_notebook(
    course_code: str,
    notebook_id: str,
    notebook_title: str,
    course_name: str = "",
) -> dict[str, Any]:
    """Update or register a NotebookLM notebook associated with a subject."""
    state = load_sync_state()
    code = course_code.upper()
    current = state.get("notebooks", {}).get(code, {})

    current.update({
        "course_code": code,
        "course_name": course_name or current.get("course_name", code),
        "notebook_id": notebook_id,
        "notebook_title": notebook_title,
        "notebook_url": f"https://notebooklm.google.com/notebook/{notebook_id}",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    if "sources" not in current:
        current["sources"] = {}
    if "last_synced_at" not in current:
        current["last_synced_at"] = None

    state["notebooks"][code] = current
    save_sync_state(state)
    return current


def is_source_synced(course_code: str, file_rel_path: str, file_hash: str) -> bool:
    """Check if a specific file version has already been successfully uploaded."""
    course_state = get_course_sync_state(course_code)
    if not course_state:
        return False
    sources = course_state.get("sources", {})
    record = sources.get(file_rel_path)
    if not record:
        return False
    return record.get("file_hash") == file_hash and bool(record.get("source_id"))


def record_uploaded_source(
    course_code: str,
    file_rel_path: str,
    source_id: str,
    file_hash: str,
    title: str,
    size_bytes: int,
) -> None:
    """Record an uploaded file source to prevent redundant uploads."""
    state = load_sync_state()
    code = course_code.upper()
    if code not in state["notebooks"]:
        state["notebooks"][code] = {
            "course_code": code,
            "sources": {},
        }
    if "sources" not in state["notebooks"][code]:
        state["notebooks"][code]["sources"] = {}

    state["notebooks"][code]["sources"][file_rel_path] = {
        "source_id": source_id,
        "file_hash": file_hash,
        "title": title,
        "size_bytes": size_bytes,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    state["notebooks"][code]["source_count"] = len(state["notebooks"][code]["sources"])
    state["notebooks"][code]["last_synced_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_sync_state(state)
