"""FastAPI router for Google NotebookLM integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from unisync.services.notebooklm.service import NotebookLMService, NotebookLMAuthError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notebooklm", tags=["notebooklm"])

_SERVICE_INSTANCE: Optional[NotebookLMService] = None
_ACTIVE_SYNC_JOB: dict[str, Any] = {
    "status": "idle",
    "last_run": None,
    "result": None,
    "error": None,
}


def get_notebooklm_service() -> NotebookLMService:
    """Singleton getter for NotebookLMService."""
    global _SERVICE_INSTANCE
    if _SERVICE_INSTANCE is None:
        _SERVICE_INSTANCE = NotebookLMService()
    return _SERVICE_INSTANCE


class NotebookLMSyncRequest(BaseModel):
    """Request payload for syncing files to NotebookLM."""
    course_code: Optional[str] = None  # None means sync all courses
    force: bool = False
    max_sources: int = 290


class NotebookLMProvisionRequest(BaseModel):
    """Request payload for creating/linking notebooks without uploading."""
    pass


@router.get("/status")
async def get_notebooklm_status() -> dict[str, Any]:
    """Return Google session auth state, account tier, and linked subject notebooks."""
    service = get_notebooklm_service()
    status = service.get_sync_status()
    status["sync_job"] = _ACTIVE_SYNC_JOB
    return status


@router.get("/courses")
async def get_notebooklm_courses() -> dict[str, Any]:
    """List enrolled academic subjects and inspect gatherable course files."""
    service = get_notebooklm_service()
    enrolled = service.gatherer.get_enrolled_academic_courses()
    course_list = []
    for code, info in enrolled.items():
        try:
            files = service.gatherer.gather_all_sources(code)
            breakdown: dict[str, int] = {}
            for f in files:
                breakdown[f.kind] = breakdown.get(f.kind, 0) + 1
            course_list.append({
                "course_code": code,
                "course_name": info.get("course_name", code),
                "total_sources": len(files),
                "breakdown": breakdown,
            })
        except Exception as exc:
            logger.warning("Error inspecting files for %s: %s", code, exc)
            course_list.append({
                "course_code": code,
                "course_name": info.get("course_name", code),
                "total_sources": 0,
                "error": str(exc),
            })
    return {"courses": course_list, "count": len(course_list)}


@router.post("/login")
async def trigger_notebooklm_login() -> dict[str, Any]:
    """Return interactive terminal sign-in instructions for Google Pro account."""
    service = get_notebooklm_service()
    service.clean_stale_browser_locks()
    return {
        "ok": True,
        "command": "notebooklm login --browser chrome",
        "cookie_command": "notebooklm login --browser-cookies chrome",
        "message": "Run 'notebooklm login --browser chrome' in your PowerShell terminal to open Google Chrome for sign-in.",
    }


@router.post("/provision")
async def provision_subject_notebooks() -> dict[str, Any]:
    """Find or create dedicated NotebookLM notebooks for all enrolled courses."""
    service = get_notebooklm_service()
    if not service.is_authenticated():
        raise HTTPException(
            status_code=401,
            detail="NotebookLM session not authenticated. Run 'notebooklm login' first.",
        )
    try:
        provisioned = await service.get_or_create_subject_notebooks()
        return {"ok": True, "notebooks": provisioned}
    except NotebookLMAuthError as auth_err:
        raise HTTPException(status_code=401, detail=str(auth_err))
    except Exception as exc:
        logger.exception("Failed provisioning subject notebooks")
        raise HTTPException(status_code=500, detail=str(exc))


async def _run_sync_job(course_code: Optional[str], force: bool, max_sources: int) -> None:
    """Background task runner for file synchronization."""
    global _ACTIVE_SYNC_JOB
    service = get_notebooklm_service()
    import datetime

    _ACTIVE_SYNC_JOB = {
        "status": "running",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "course_code": course_code or "ALL",
        "result": None,
        "error": None,
    }
    try:
        if course_code:
            res = await service.sync_course(course_code, force=force, max_sources=max_sources)
            _ACTIVE_SYNC_JOB["result"] = res
        else:
            res = await service.sync_all_courses(force=force, max_sources=max_sources)
            _ACTIVE_SYNC_JOB["result"] = res
        _ACTIVE_SYNC_JOB["status"] = "completed"
    except Exception as exc:
        logger.exception("NotebookLM background sync failed")
        _ACTIVE_SYNC_JOB["status"] = "failed"
        _ACTIVE_SYNC_JOB["error"] = str(exc)
    finally:
        _ACTIVE_SYNC_JOB["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()


@router.post("/sync")
async def sync_notebooklm_sources(
    payload: NotebookLMSyncRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Queue background upload of course materials into NotebookLM notebooks."""
    global _ACTIVE_SYNC_JOB
    service = get_notebooklm_service()
    if not service.is_authenticated():
        raise HTTPException(
            status_code=401,
            detail="NotebookLM session not authenticated. Run 'notebooklm login' or trigger sign-in first.",
        )

    if _ACTIVE_SYNC_JOB.get("status") == "running" and not payload.force:
        return {
            "ok": False,
            "message": "A NotebookLM sync job is already in progress.",
            "job": _ACTIVE_SYNC_JOB,
        }

    background_tasks.add_task(
        _run_sync_job,
        course_code=payload.course_code,
        force=payload.force,
        max_sources=payload.max_sources,
    )

    return {
        "ok": True,
        "message": f"Sync queued for {payload.course_code or 'all enrolled subjects'}.",
        "course_code": payload.course_code,
        "force": payload.force,
    }


@router.post("/sync/cancel")
async def cancel_notebooklm_sync() -> dict[str, Any]:
    """Reset or cancel any currently tracked NotebookLM sync job."""
    global _ACTIVE_SYNC_JOB
    import datetime
    prev = _ACTIVE_SYNC_JOB.get("status")
    _ACTIVE_SYNC_JOB = {
        "status": "idle",
        "last_run": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "result": None,
        "error": "Cancelled or reset by user.",
    }
    return {
        "ok": True,
        "message": f"NotebookLM sync job state reset from '{prev}' to 'idle'.",
    }
