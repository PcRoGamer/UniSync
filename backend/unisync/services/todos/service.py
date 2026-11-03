"""TodoService — JSON CRUD for personal todo items.

Stored under ``data/user/workspace/todos/todos.json``.
Mirrors the ``NotebookManager`` pattern from ``unisync/services/notebook/service.py``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .models import TodoItem

# ── helpers ──────────────────────────────────────────────────────────


def _default_serializer(o: object) -> str:
    """``json.dumps`` default — delegates datetimes to ISO format."""
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def _parse_datetime(value: object) -> datetime | None:
    """Rebuild a ``datetime`` from an ISO string or pass through an existing datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def _to_comparable_utc(dt: datetime | None) -> datetime | None:
    """Convert naive or aware datetimes to UTC for safe comparisons."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _migrate_item(raw: dict) -> dict:
    """Normalise a raw dict so it round-trips through ``TodoItem.model_validate``."""
    for field in ("due_at", "source_received_at", "submitted_at", "graded_at"):
        val = raw.get(field)
        if isinstance(val, str):
            raw[field] = _parse_datetime(val)
    return raw


# ── service ──────────────────────────────────────────────────────────


class TodoService:
    """Manage todo items in a single JSON file.

    Thread-safe for single-process use; no locking is performed.
    """

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.todo_file = self.base_dir / "todos.json"

    # ── JSON I/O ───────────────────────────────────────────────────

    def _load_items(self) -> list[dict]:
        if not self.todo_file.exists():
            return []
        try:
            with open(self.todo_file, encoding="utf-8") as f:
                data = json.load(f)
            items = data if isinstance(data, list) else data.get("items", [])
            return [_migrate_item(it) for it in items]
        except Exception:
            return []

    def _save_items(self, items: list[dict]) -> None:
        tmp = self.todo_file.with_suffix(f".tmp.{uuid.uuid4().hex}")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"items": items},
                    f,
                    indent=2,
                    ensure_ascii=False,
                    default=_default_serializer,
                )
            for attempt in range(10):
                try:
                    os.replace(str(tmp), str(self.todo_file))
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:
                    pass

    # ── CRUD ───────────────────────────────────────────────────────

    def create_item(self, data: dict) -> TodoItem:
        item: dict = {
            "id": str(uuid.uuid4())[:8],
            "title": data.get("title", ""),
            "status": data.get("status", "open"),
            "due_at": data.get("due_at"),
            "due_at_confidence": data.get("due_at_confidence"),
            "kind": data.get("kind", "manual"),
            "course": data.get("course"),
            "priority": data.get("priority", "normal"),
            "notes": data.get("notes", ""),
            "source": data.get("source", "manual"),
            "source_message_id": data.get("source_message_id"),
            "source_subject": data.get("source_subject"),
            "source_from": data.get("source_from"),
            "source_received_at": data.get("source_received_at"),
            "created_at": time.time(),
            "updated_at": time.time(),
            "completed_at": data.get("completed_at"),
            "submission_status": data.get("submission_status"),
            "is_graded": bool(data.get("is_graded", False)),
            "score": data.get("score"),
            "points_possible": data.get("points_possible"),
            "grade": data.get("grade"),
            "grading_type": data.get("grading_type"),
            "submitted_at": data.get("submitted_at"),
            "graded_at": data.get("graded_at"),
            "description": data.get("description"),
            "submission_types": data.get("submission_types"),
            "allowed_extensions": data.get("allowed_extensions"),
            "rubric": data.get("rubric"),
            "submission_comments": data.get("submission_comments"),
            "url": data.get("url"),
            "source_id": data.get("source_id"),
        }
        # If status is "done" but no completed_at, set it now.
        if item["status"] == "done" and item["completed_at"] is None:
            item["completed_at"] = time.time()

        items = self._load_items()
        source_id = item.get("source_id")
        source = item.get("source")
        if source_id:
            for idx, existing in enumerate(items):
                if existing.get("source_id") == source_id and existing.get("source") == source:
                    # Idempotent update of existing item
                    for k, v in item.items():
                        if v is not None and k not in ("id", "created_at"):
                            existing[k] = v
                    existing["updated_at"] = time.time()
                    self._save_items(items)
                    return TodoItem.model_validate(existing)

        items.append(item)
        self._save_items(items)
        return TodoItem.model_validate(item)

    def list_items(
        self,
        status: str | None = None,
        kind: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
    ) -> list[TodoItem]:
        items = self._load_items()
        result: list[dict] = []
        before_utc = _to_comparable_utc(due_before)
        after_utc = _to_comparable_utc(due_after)

        for item in items:
            if status is not None and item.get("status") != status:
                continue
            if kind is not None and item.get("kind") != kind:
                continue
            due_utc = _to_comparable_utc(_parse_datetime(item.get("due_at")))
            if before_utc is not None and (due_utc is None or due_utc > before_utc):
                continue
            if after_utc is not None and (due_utc is None or due_utc < after_utc):
                continue
            result.append(item)

        return [TodoItem.model_validate(it) for it in result]

    def get_item(self, item_id: str) -> TodoItem | None:
        items = self._load_items()
        for item in items:
            if item.get("id") == item_id:
                return TodoItem.model_validate(item)
        return None

    def update_item(self, item_id: str, updates: dict) -> TodoItem:
        items = self._load_items()
        for item in items:
            if item.get("id") != item_id:
                continue

            for key, value in updates.items():
                if key in ("id", "created_at"):
                    continue  # immutable fields
                # If setting status to done, auto-set completed_at
                if key == "status" and value == "done":
                    if not item.get("completed_at"):
                        item["completed_at"] = time.time()
                # If status is changed away from done, clear completed_at
                if key == "status" and value != "done" and item.get("status") == "done":
                    item["completed_at"] = None
                item[key] = value

            item["updated_at"] = time.time()
            self._save_items(items)
            return TodoItem.model_validate(item)

        raise KeyError(f"TodoItem with id {item_id!r} not found")

    def delete_item(self, item_id: str) -> bool:
        items = self._load_items()
        before = len(items)
        items = [it for it in items if it.get("id") != item_id]
        if len(items) == before:
            return False
        self._save_items(items)
        return True

    def get_upcoming_items(self) -> list[TodoItem]:
        items = self._load_items()
        open_items = [it for it in items if it.get("status") == "open"]
        max_dt = datetime.max.replace(tzinfo=timezone.utc)
        open_items.sort(
            key=lambda it: (
                0 if it.get("due_at") else 1,
                _to_comparable_utc(_parse_datetime(it.get("due_at"))) or max_dt,
            )
        )
        return [TodoItem.model_validate(it) for it in open_items]

    # ── dedup ──────────────────────────────────────────────────────

    def find_by_source(
        self,
        source_message_id: str,
        normalized_title: str,
        source_subject: str | None = None,
    ) -> list[TodoItem]:
        """Return items matching the same email message id *or* subject, and same title.

        Used as a secondary guard against double-insertion when the
        ledger may have been wiped but the email monitor re-processes the
        same message.
        """
        items = self._load_items()
        matches: list[dict] = []
        for item in items:
            title_matches = item.get("title") == normalized_title
            id_matches = item.get("source_message_id") == source_message_id
            subj_matches = source_subject and item.get("source_subject") == source_subject

            if title_matches and (id_matches or subj_matches):
                matches.append(item)
        return [TodoItem.model_validate(it) for it in matches]


# ── singleton factory ──────────────────────────────────────────────


_instances: dict[str, TodoService] = {}


def get_todo_service(base_dir: str | None = None) -> TodoService:
    if base_dir is None:
        from unisync.core.path_service import get_path_service

        base_dir = str(get_path_service().get_workspace_feature_dir("todos"))
    key = str(Path(base_dir).resolve())
    if key not in _instances:
        _instances[key] = TodoService(base_dir=base_dir)
    return _instances[key]


class _TodoServiceProxy:
    """Convenience proxy so callers can write ``todo_service.create_item(...)``."""

    def __getattr__(self, name: str):
        return getattr(get_todo_service(), name)


todo_service: TodoService = _TodoServiceProxy()  # type: ignore[assignment]
