"""FastAPI router for EdStem integration in UniSync."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from unisync.services.edstem import EdStemClient, EdStemError
from unisync.core.file_io import atomic_write_json
from unisync.core.text_cleaner import filter_academic_boilerplate, is_boilerplate_page

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/edstem", tags=["edstem"])


class EdStemSettingsUpdate(BaseModel):
    """EdStem connection settings."""
    api_token: Optional[str] = None
    api_base_url: str = "https://edstem.org/api"


class EdStemImportRequest(BaseModel):
    """Request to import an EdStem course."""
    course_id: str
    output_dir: Optional[str] = None


def _settings_file() -> Path:
    """Return path to integrations settings file."""
    path = Path.home() / ".unisync" / "settings" / "integrations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_settings() -> dict[str, Any]:
    f = _settings_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_settings(data: dict[str, Any]) -> None:
    atomic_write_json(_settings_file(), data)


@router.get("/settings")
async def get_edstem_settings() -> dict[str, Any]:
    """Return EdStem settings with token redacted."""
    settings = _load_settings()
    token = str(settings.get("edstem_api_token") or "")
    return {
        "settings": {
            "api_base_url": settings.get("edstem_api_base_url", "https://edstem.org/api"),
        },
        "api_token_set": bool(token),
    }


@router.put("/settings")
async def update_edstem_settings(payload: EdStemSettingsUpdate) -> dict[str, Any]:
    """Persist EdStem token and API base URL without echoing token back."""
    current = _load_settings()
    if payload.api_token is not None:
        current["edstem_api_token"] = payload.api_token.strip()
    current["edstem_api_base_url"] = payload.api_base_url.strip().rstrip("/") or "https://edstem.org/api"
    _save_settings(current)
    return await get_edstem_settings()


@router.post("/test")
async def test_edstem_connection(payload: EdStemSettingsUpdate) -> dict[str, Any]:
    """Validate a draft or stored EdStem token against the user endpoint."""
    stored = _load_settings()
    token = stored.get("edstem_api_token", "") if payload.api_token is None else payload.api_token.strip()
    api_base_url = payload.api_base_url.strip().rstrip("/") or stored.get(
        "edstem_api_base_url", "https://edstem.org/api"
    )
    if not token:
        return {"ok": False, "kind": "auth", "message": "No API token provided."}

    client = EdStemClient(token, api_base_url=api_base_url)
    try:
        result = await asyncio.to_thread(client.validate)
    except EdStemError as exc:
        return {"ok": False, "kind": exc.kind, "message": str(exc)}
    except Exception as exc:
        logger.exception("EdStem connectivity test failed")
        return {"ok": False, "kind": "error", "message": f"Unexpected error: {exc}"}

    courses = result.get("courses", [])
    return {
        "ok": True,
        "message": f"EdStem connected; found {len(courses)} enrolled course(s).",
        "user": result.get("user", {}),
        "course_count": len(courses),
        "courses": courses,
    }


@router.get("/courses")
async def list_edstem_courses() -> dict[str, Any]:
    """Return enrolled EdStem courses for current user."""
    stored = _load_settings()
    token = stored.get("edstem_api_token", "")
    api_base_url = stored.get("edstem_api_base_url", "https://edstem.org/api")
    if not token:
        return {"courses": [], "message": "EdStem token not configured."}

    client = EdStemClient(token, api_base_url=api_base_url)
    try:
        result = await asyncio.to_thread(client.validate)
        return {"courses": result.get("courses", [])}
    except Exception as exc:
        logger.error("Failed to list EdStem courses: %s", exc)
        return {"courses": [], "error": str(exc)}


def sync_edstem_course_todos(
    client: EdStemClient,
    course_id: str,
    course_name: str | None = None,
    snapshot: Any = None,
    verified_grades: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync lessons for an EdStem course into TodoService with submission statuses.

    Zero-LLM deterministic ingestion: parses completion flags,
    and updates existing items with completed/submitted states, points possible, and scores.
    """
    from unisync.services.edstem import extract_due_items, EdStemSnapshot
    from unisync.services.todos import get_todo_service

    if snapshot is None:
        lessons, _ = client.fetch_lessons(course_id)
        snapshot = EdStemSnapshot(
            course={"id": course_id, "title": course_name or ""},
            modules=[],
            lessons=lessons,
            resources=[],
            threads=[],
            announcements=[],
            fetched_at="",
        )
    due_items = extract_due_items(snapshot, verified_grades=verified_grades)

    todo_svc = get_todo_service()
    c_name = course_name or str(course_id)
    existing_todos = todo_svc.list_items()

    created_count = 0
    updated_count = 0

    for d in due_items:
        s_id = f"edstem:{course_id}:{d['id']}"
        matches = [
            t for t in existing_todos
            if (getattr(t, "source", None) == "edstem" and (
                getattr(t, "source_id", None) == s_id
                or (t.title == d["title"] and t.course == c_name)
            ))
        ]

        todo_payload = {
            "title": d["title"],
            "due_at": d["due_at"],
            "kind": "assignment",
            "course": c_name,
            "priority": "normal",
            "notes": f"EdStem lesson: {d['url']}",
            "source": "edstem",
            "source_id": s_id,
            "submission_status": d["submission_status"],
            "is_graded": d["is_graded"],
            "score": d["score"],
            "points_possible": d["points_possible"],
            "grade": d["grade"],
            "grading_type": d["grading_type"],
            "submitted_at": d["submitted_at"],
            "url": d["url"],
        }
        if d["status"] == "done":
            todo_payload["status"] = "done"
            todo_payload["completed_at"] = d["completed_at"]

        if not matches:
            todo_svc.create_item(todo_payload)
            created_count += 1
        else:
            for m in matches:
                update_fields = {
                    "submission_status": d["submission_status"],
                    "is_graded": d["is_graded"],
                    "score": d["score"],
                    "points_possible": d["points_possible"],
                    "grade": d["grade"],
                    "grading_type": d["grading_type"],
                    "submitted_at": d["submitted_at"],
                    "url": d["url"],
                    "status": d["status"],
                }
                if d["status"] == "done":
                    update_fields["status"] = "done"
                    if not m.completed_at:
                        update_fields["completed_at"] = d["completed_at"]
                elif m.status == "open" and d["status"] == "open":
                    update_fields["status"] = "open"

                try:
                    todo_svc.update_item(m.id, update_fields)
                    updated_count += 1
                except KeyError:
                    # Item was concurrently removed or replaced; create new item
                    try:
                        todo_svc.create_item(todo_payload)
                        created_count += 1
                    except Exception as create_err:
                        logger.debug("Failed creating fallback todo item: %s", create_err)

    return {
        "course_id": course_id,
        "due_items_count": len(due_items),
        "created_count": created_count,
        "updated_count": updated_count,
    }


def sync_edstem_course_full(
    client: EdStemClient,
    course_id: str,
    course_name: str | None = None,
    output_dir: str | Path | None = None,
    trigger_kb_index: bool = True,
    persist_snapshot: bool = True,
) -> dict[str, Any]:
    """Perform complete synchronization for an EdStem course:
    1. Fetches lessons (with slide content & code blocks), modules, discussion threads, resources, announcements.
    2. Renders and writes all markdown documents to data/courses/{kb_name}/edstem/.
    3. Downloads all attached files and worksheets to data/courses/{kb_name}/edstem/files/ and registers them in DownloadRegistry.
    4. Synchronizes deadlines, submission statuses, points possible, and scores into TodoService.
    5. Records snapshot state in data/libraries/edstem_snapshots.json.
    6. Triggers background incremental knowledge base vector indexing.
    """
    import re
    from datetime import datetime, timezone
    from unisync.services.edstem import render_snapshot

    c_id_str = str(course_id)
    repo_root = Path(__file__).resolve().parents[3]
    libs_file = repo_root / "data" / "libraries" / "libraries.json"
    libs: dict[str, Any] = {}
    if libs_file.exists():
        try:
            libs = json.loads(libs_file.read_text(encoding="utf-8"))
        except Exception:
            libs = {}

    kb_name = None
    # 1. Match against configured libraries by edstem_course_id
    for lib in libs.values():
        if str(lib.get("edstem_course_id", "")) == c_id_str:
            kb_name = lib.get("kb_name") or lib.get("name")
            break

    # 2. Match by course code (e.g. COMP10001, BLAW10001)
    if not kb_name:
        code_match = re.search(r"([A-Za-z]{3,4}\s*\d{4,5})", f"{course_name or ''} {c_id_str}")
        code_norm = re.sub(r"\s+", "", code_match.group(1)).lower() if code_match else ""
        if code_norm:
            for lib in libs.values():
                lib_name_lower = (lib.get("name") or "").lower()
                lib_kb_lower = (lib.get("kb_name") or "").lower()
                if code_norm in lib_kb_lower or code_norm in lib_name_lower:
                    kb_name = lib.get("kb_name") or lib.get("name")
                    lib["edstem_course_id"] = c_id_str
                    try:
                        atomic_write_json(libs_file, libs)
                        logger.info("Auto-linked EdStem course %s to library %s", c_id_str, kb_name)
                    except Exception as save_err:
                        logger.debug("Failed saving auto-linked library: %s", save_err)
                    break

    # 3. Match by name token overlap (e.g. "Foundations of Computing")
    if not kb_name and course_name:
        c_tokens = set(re.findall(r"\w{4,}", course_name.lower()))
        for lib in libs.values():
            lib_tokens = set(re.findall(r"\w{4,}", (lib.get("name") or "").lower()))
            if len(c_tokens & lib_tokens) >= 2:
                kb_name = lib.get("kb_name") or lib.get("name")
                lib["edstem_course_id"] = c_id_str
                try:
                    atomic_write_json(libs_file, libs)
                except Exception:
                    pass
                break

    # 4. If no library matched, auto-provision a library so it automatically appears in the UI
    if not kb_name:
        clean_name = re.sub(r"[^\w\-]+", "_", course_name or c_id_str).strip("_")
        kb_name = f"edstem_{clean_name}"
        new_lib_id = c_id_str
        libs[new_lib_id] = {
            "id": new_lib_id,
            "name": course_name or f"EdStem Course {c_id_str}",
            "kb_name": kb_name,
            "edstem_course_id": c_id_str,
            "auto_sync": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            atomic_write_json(libs_file, libs)
            logger.info("Auto-provisioned library %s for EdStem course %s", kb_name, c_id_str)
        except Exception as prov_err:
            logger.debug("Failed auto-provisioning library: %s", prov_err)

    if output_dir:
        course_base = Path(output_dir)
    else:
        course_base = repo_root / "data" / "courses" / kb_name

    course_base.mkdir(parents=True, exist_ok=True)
    edstem_dir = course_base / "edstem"
    files_dir = edstem_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    # 1. Take full course snapshot (lessons, modules, discussions, announcements, resources)
    snapshot = client.snapshot(c_id_str, max_threads=500)
    c_title = snapshot.course.get("title") or snapshot.course.get("name") or course_name or c_id_str

    # 2. Render and write markdown documents
    docs = render_snapshot(snapshot, c_id_str)
    written_docs = 0
    for rel_path, content in docs:
        dest_file = course_base / rel_path
        # Automatically skip standalone academic integrity & policy stubs
        if is_boilerplate_page(dest_file.name):
            continue
        # Automatically strip AI declarations and Turnitin notices before writing to disk
        cleaned = filter_academic_boilerplate(content)
        if not cleaned.strip():
            continue
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(cleaned, encoding="utf-8")
        written_docs += 1

    # 3. Download resources / files
    downloaded_count = 0
    skipped_count = 0
    for res in snapshot.resources:
        dl_url = res.get("download_url") or res.get("url")
        if not dl_url:
            continue
        res_name = res.get("name") or res.get("title") or f"resource_{res.get('id')}"
        ext = res.get("extension") or ""
        fname = f"{res_name}{ext}" if ext and not res_name.endswith(ext) else res_name
        clean_fname = re.sub(r'[\\/*?:"<>|]', "_", fname)
        dest_path = files_dir / clean_fname

        expected_size = res.get("size")
        if dest_path.exists() and expected_size and dest_path.stat().st_size == expected_size:
            skipped_count += 1
            continue

        try:
            client.download_resource(dl_url, dest_path)
            downloaded_count += 1
        except Exception as dl_err:
            logger.warning("Failed downloading EdStem resource %s: %s", clean_fname, dl_err)

    # 4. Retrieve the account owner's submitted attempts and actual grades.
    # A completed Ed lesson alone is NOT evidence of a grade or submission.
    from unisync.services.edstem.submissions import archive_edstem_assessments
    try:
        verified = archive_edstem_assessments(client, snapshot.lessons, course_base)
    except EdStemError as exc:
        logger.warning("Ed assessments unavailable (%s); grades remain unverified", exc.kind)
        verified = {"grades_by_lesson": {}, "summary": {"error_kind": exc.kind}}
    due_res = sync_edstem_course_todos(
        client, c_id_str, c_title, snapshot=snapshot,
        verified_grades=verified["grades_by_lesson"],
    )

    # 5. Persist snapshot state in data/libraries/edstem_snapshots.json
    if persist_snapshot:
        try:
            snapshots_file = repo_root / "data" / "libraries" / "edstem_snapshots.json"
            snapshots_dict: dict[str, Any] = {}
            if snapshots_file.exists():
                try:
                    snapshots_dict = json.loads(snapshots_file.read_text(encoding="utf-8"))
                except Exception:
                    snapshots_dict = {}

            snapshots_dict[c_id_str] = {
                "course_id": c_id_str,
                "course": snapshot.course,
                "kb_name": kb_name,
                "doc_count": written_docs,
                "assessment_archive": verified["summary"],
                "counts": {
                    "modules": len(snapshot.modules),
                    "lessons": len(snapshot.lessons),
                    "resources": len(snapshot.resources),
                    "threads": len(snapshot.threads),
                    "announcements": len(snapshot.announcements),
                    "downloaded_files": downloaded_count,
                },
                "fetched_at": snapshot.fetched_at,
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "errors": snapshot.errors or {},
            }
            atomic_write_json(snapshots_file, snapshots_dict)
        except Exception as snap_err:
            logger.warning("Failed saving EdStem snapshot state: %s", snap_err)

    # 6. Trigger incremental knowledge base indexing (FAISS + BM25)
    if trigger_kb_index:
        try:
            from unisync.services.kb import get_kb_service
            kb_service = get_kb_service()
            import threading
            threading.Thread(target=kb_service.index_all, kwargs={"force": False}, daemon=True).start()
        except Exception as kb_err:
            logger.debug("Failed triggering background KB index after EdStem sync: %s", kb_err)

    # Keep original Ed submissions and grading unmodified on disk when the
    # operator selected response-level MCP scrubbing.
    import os
    if os.environ.get("UNISYNC_SKIP_DISK_SCRUB") != "1":
        try:
            from unisync.services.disk_cleaner import run_background_disk_scrubber
            run_background_disk_scrubber(target_path=course_base, delay_sec=1.0)
        except Exception as scrub_err:
            logger.debug("Failed triggering background disk scrubber after EdStem sync: %s", scrub_err)


    return {
        "course_id": c_id_str,
        "kb_name": kb_name,
        "doc_count": written_docs,
        "downloaded_files": downloaded_count,
        "cached_files": skipped_count,
        "due_items_count": due_res.get("due_items_count", 0),
        "created_todos": due_res.get("created_count", 0),
        "updated_todos": due_res.get("updated_count", 0),
        "assessment_archive": verified["summary"],
        "counts": {
            "modules": len(snapshot.modules),
            "lessons": len(snapshot.lessons),
            "resources": len(snapshot.resources),
            "threads": len(snapshot.threads),
            "announcements": len(snapshot.announcements),
        },
    }


_LAST_EDSTEM_AUTO_SYNC_TS: float = 0.0
_LAST_EDSTEM_AUTO_SYNC_RESULTS: list[dict[str, Any]] = []
EDSTEM_AUTO_SYNC_COOLDOWN_SECONDS: float = 300.0  # 5-minute debounce


def sync_all_configured_edstem_courses(force: bool = False) -> list[dict[str, Any]]:
    """Helper to perform full sync for all enrolled EdStem courses if API token is configured.

    Includes a 5-minute debounce cooldown to prevent HTTP 429 rate-limiting from rapid sync triggers.
    """
    global _LAST_EDSTEM_AUTO_SYNC_TS, _LAST_EDSTEM_AUTO_SYNC_RESULTS
    now = time.time()
    if not force and (now - _LAST_EDSTEM_AUTO_SYNC_TS) < EDSTEM_AUTO_SYNC_COOLDOWN_SECONDS:
        logger.info(
            "Skipping automatic EdStem sync: within %.1fs cooldown (last run %.1fs ago)",
            EDSTEM_AUTO_SYNC_COOLDOWN_SECONDS,
            now - _LAST_EDSTEM_AUTO_SYNC_TS,
        )
        return _LAST_EDSTEM_AUTO_SYNC_RESULTS

    stored = _load_settings()
    token = stored.get("edstem_api_token", "")
    api_base_url = stored.get("edstem_api_base_url", "https://edstem.org/api")
    if not token:
        return []

    try:
        client = EdStemClient(token, api_base_url=api_base_url)
        courses = client.list_courses()
        results = []
        for c in courses:
            try:
                res = sync_edstem_course_full(client, c.id, c.name)
                results.append(res)
            except Exception as exc:
                logger.warning("Failed to sync EdStem course %s: %s", c.id, exc)

        _LAST_EDSTEM_AUTO_SYNC_TS = now
        _LAST_EDSTEM_AUTO_SYNC_RESULTS = results
        return results
    except Exception as top_err:
        logger.warning("EdStem auto-sync skipped due to client error: %s", top_err)
        return []


@router.post("/sync")
async def sync_edstem_courses_endpoint() -> dict[str, Any]:
    """Trigger complete EdStem sync: lessons, slides, discussions, resources, announcements, and todos."""
    try:
        results = await asyncio.to_thread(sync_all_configured_edstem_courses, force=True)
        return {"ok": True, "synced_courses": results}
    except Exception as exc:
        logger.exception("Failed to sync EdStem courses")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/auto-register")
async def auto_register_edstem_courses() -> dict[str, Any]:
    """Discover enrolled EdStem courses, link/provision libraries, and sync all materials."""
    try:
        results = await asyncio.to_thread(sync_all_configured_edstem_courses, force=True)
        return {"ok": True, "courses": results, "count": len(results)}
    except Exception as exc:
        logger.exception("Failed to auto-register EdStem courses")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/import")
async def import_edstem_course(payload: EdStemImportRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Fetch and render course documents, download files, and update todos."""
    stored = _load_settings()
    token = stored.get("edstem_api_token", "")
    api_base_url = stored.get("edstem_api_base_url", "https://edstem.org/api")
    if not token:
        raise HTTPException(status_code=400, detail="EdStem token not configured.")

    client = EdStemClient(token, api_base_url=api_base_url)
    try:
        sync_res = await asyncio.to_thread(
            sync_edstem_course_full,
            client,
            payload.course_id,
            None,
            payload.output_dir,
        )
        return {
            "ok": True,
            **sync_res,
        }
    except Exception as exc:
        logger.exception("Failed to import EdStem course: %s", payload.course_id)
        raise HTTPException(status_code=500, detail=str(exc))


# Compatibility routers for frontend / legacy routes
settings_compat_router = APIRouter(prefix="/settings/edstem", tags=["edstem-compat"])
lectures_compat_router = APIRouter(prefix="/lectures/edstem", tags=["edstem-compat"])


@settings_compat_router.get("")
async def compat_get_settings() -> dict[str, Any]:
    return await get_edstem_settings()


@settings_compat_router.put("")
async def compat_update_settings(payload: EdStemSettingsUpdate) -> dict[str, Any]:
    return await update_edstem_settings(payload)


@settings_compat_router.post("/test")
async def compat_test_settings(payload: EdStemSettingsUpdate) -> dict[str, Any]:
    return await test_edstem_connection(payload)


@lectures_compat_router.get("/courses")
async def compat_get_courses() -> dict[str, Any]:
    return await list_edstem_courses()


@lectures_compat_router.post("/auto-register")
async def compat_auto_register() -> dict[str, Any]:
    return await auto_register_edstem_courses()

