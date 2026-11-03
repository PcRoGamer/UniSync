"""Persistent state for the Camoufox-native Canvas/Echo360 sync.

The scheduler must survive a restart: pending work, the last run outcome and
per-course freshness live in ``sync_state/native_sync.json`` instead of memory.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from unisync.core.file_io import atomic_write_json
from unisync.services.sync.echo360_ingest import (
    build_coverage,
    canonical_embed_key,
    embed_transcript_present,
)

DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
_RETRY_AFTER_SECONDS = 15 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_path() -> Path:
    from unisync.core.path_service import get_path_service

    path = get_path_service().get_user_root() / "sync_state" / "native_sync.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class NativeSyncState:
    """Read/write the resumable queue and per-course coverage."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_state_path()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema": 1,
            "enabled": True,
            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "next_run_at": 0.0,
            "last_run_started_at": "",
            "last_run_finished_at": "",
            "last_run_status": "never",
            "last_error": "",
            "login_required": False,
            "pending": {},
            "results": {},
            "courses": {},
        }
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if isinstance(loaded, dict):
            base.update({key: value for key, value in loaded.items() if value is not None})
        if not isinstance(base.get("pending"), dict):
            base["pending"] = {}
        if not isinstance(base.get("results"), dict):
            base["results"] = {}
        if not isinstance(base.get("courses"), dict):
            base["courses"] = {}
        return base

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    # ── scheduling ──────────────────────────────────────────────────────────
    def configure(self, *, enabled: bool | None = None, interval_seconds: int | None = None) -> None:
        if enabled is not None:
            self.data["enabled"] = bool(enabled)
        if interval_seconds is not None:
            self.data["interval_seconds"] = max(300, int(interval_seconds))
        self.save()

    def due(self, now: float | None = None) -> bool:
        return bool(self.data.get("enabled")) and (now or time.time()) >= float(self.data.get("next_run_at") or 0)

    def begin_run(self) -> None:
        self.data["last_run_started_at"] = _now()
        self.data["last_run_status"] = "running"
        self.data["last_error"] = ""
        self.save()

    def finish_run(self, status: str, *, error: str = "", login_required: bool = False) -> None:
        self.data["last_run_finished_at"] = _now()
        self.data["last_run_status"] = status
        self.data["last_error"] = error
        self.data["login_required"] = bool(login_required)
        self.data["next_run_at"] = time.time() + int(self.data.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS)
        self.save()

    # ── queue ───────────────────────────────────────────────────────────────
    @staticmethod
    def key_for(record: Mapping[str, Any]) -> str:
        return canonical_embed_key(
            record.get("canvas_course_id"),
            record.get("uuid") or record.get("media_id"),
            record.get("link_id"),
        )

    def pending_keys(self) -> list[str]:
        return list(self.data.get("pending", {}).keys())

    def enqueue(self, records: Iterable[Mapping[str, Any]], *, cache_dir: Path, thumbnail_dir: Path | None = None) -> list[dict[str, Any]]:
        """Add every embed that still lacks a cue-valid transcript.

        A previously failed key is retried after ``_RETRY_AFTER_SECONDS`` so a
        transient auth/network failure never permanently hides an item.
        """
        pending = self.data.setdefault("pending", {})
        now = time.time()
        queued: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            key = self.key_for(record)
            if not key or key.endswith("::"):
                continue
            if embed_transcript_present(cache_dir, record):
                pending.pop(key, None)
                continue
            entry = pending.get(key)
            if entry is None:
                entry = {"record": dict(record), "queued_at": _now(), "attempts": 0, "last_error": ""}
                pending[key] = entry
            else:
                entry["record"] = {**entry.get("record", {}), **dict(record)}
            if float(entry.get("retry_after") or 0) > now:
                continue
            queued.append(entry["record"])
        self.save()
        return queued

    def note_outcome(self, key: str, outcome: Mapping[str, Any], *, transcript_present: bool) -> None:
        pending = self.data.setdefault("pending", {})
        results = self.data.setdefault("results", {})
        results[key] = {**dict(outcome), "recorded_at": _now()}
        if transcript_present:
            pending.pop(key, None)
        else:
            entry = pending.get(key)
            if entry is not None:
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                entry["last_error"] = str(outcome.get("reason") or outcome.get("status") or "")
                entry["last_attempt_at"] = _now()
                entry["retry_after"] = time.time() + _RETRY_AFTER_SECONDS
        self.save()

    # ── coverage ────────────────────────────────────────────────────────────
    def record_coverage(
        self,
        course_id: str,
        records: Sequence[Mapping[str, Any]],
        *,
        cache_dir: Path,
        thumbnail_dir: Path | None = None,
        catalog_updated_at: str = "",
        syllabus_fetched_at: str = "",
        course_name: str = "",
        direct: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        coverage = build_coverage(
            course_id,
            records,
            cache_dir=cache_dir,
            thumbnail_dir=thumbnail_dir,
            catalog_updated_at=catalog_updated_at,
            syllabus_fetched_at=syllabus_fetched_at,
        )
        coverage["course_name"] = course_name or coverage.get("course_name", "")
        if direct is not None:
            coverage["direct"] = dict(direct)
        self.data.setdefault("courses", {})[str(course_id)] = coverage
        self.save()
        return coverage

    def summary(self) -> dict[str, Any]:
        courses = self.data.get("courses", {})
        return {
            "enabled": bool(self.data.get("enabled")),
            "interval_seconds": int(self.data.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS),
            "next_run_at": self.data.get("next_run_at") or 0.0,
            "last_run_started_at": self.data.get("last_run_started_at") or "",
            "last_run_finished_at": self.data.get("last_run_finished_at") or "",
            "last_run_status": self.data.get("last_run_status") or "never",
            "last_error": self.data.get("last_error") or "",
            "login_required": bool(self.data.get("login_required")),
            "pending_count": len(self.data.get("pending", {})),
            "pending": [
                {
                    "key": key,
                    "title": (entry.get("record") or {}).get("title") or (entry.get("record") or {}).get("source_title") or "",
                    "attempts": entry.get("attempts", 0),
                    "last_error": entry.get("last_error", ""),
                }
                for key, entry in (self.data.get("pending") or {}).items()
            ],
            "courses": courses,
            "totals": {
                "discovered_links": sum(int(c.get("discovered_links") or 0) for c in courses.values()),
                "cue_valid_transcripts": sum(int(c.get("cue_valid_transcripts") or 0) for c in courses.values()),
                "missing_transcripts": sum(int(c.get("missing_transcripts") or 0) for c in courses.values()),
                "direct_lessons": sum(
                    int((c.get("direct") or {}).get("discovered_lessons") or 0)
                    for c in courses.values()
                ),
                "direct_transcripts": sum(
                    int((c.get("direct") or {}).get("cue_valid_transcripts") or 0)
                    for c in courses.values()
                ),
                "direct_missing": sum(
                    int((c.get("direct") or {}).get("missing_transcripts") or 0)
                    for c in courses.values()
                ),
            },
        }
