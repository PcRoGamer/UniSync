"""FastAPI router for Download Inventory and Provenance Resolution.

Allows AI agents, ingestion pipelines, and frontend UIs to query:
- Direct LMS URL given a local filepath
- Local filepath given a direct URL or ID
- Storage stats and asset lists
"""

from __future__ import annotations

import json
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from unisync.core.download_registry import get_registry, resolve_item


def _synced_canvas_courses() -> set[str]:
    """Return course names discovered by the browser sync, even before files download."""
    try:
        from unisync.core.path_service import get_path_service

        state_file = (
            get_path_service().get_user_root()
            / "sync_state"
            / "sync_delta_state.json"
        )
        if not state_file.exists():
            return set()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return {
            str(course.get("name", "")).strip()
            for course in (data.get("courses") or {}).values()
            if isinstance(course, dict) and str(course.get("name", "")).strip()
        }
    except (OSError, TypeError, ValueError):
        return set()

router = APIRouter(prefix="/inventory", tags=["inventory"])


class ResolveResponse(BaseModel):
    found: bool
    query: str
    item: Optional[dict[str, Any]] = None


@router.get("/resolve", response_model=ResolveResponse)
async def resolve_inventory_item(
    query: str = Query(..., description="Local filepath, filename, direct URL, or item ID")
) -> dict[str, Any]:
    """Instantly resolves provenance (direct link, local path, course) for any item."""
    item = resolve_item(query)
    if not item:
        return {"found": False, "query": query, "item": None}
    return {
        "found": True,
        "query": query,
        "item": item.to_dict(),
    }


@router.get("/list")
async def list_inventory_items(
    platform: Optional[str] = Query(None, description="Filter by platform (canvas, echo360, edstem, email)"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """List tracked downloaded assets with pagination."""
    reg = get_registry()
    items = reg.list_all(platform=platform)
    total = len(items)
    page_items = items[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [it.to_dict() for it in page_items],
    }


@router.get("/stats")
async def get_inventory_stats() -> dict[str, Any]:
    """Return storage and provenance statistics."""
    reg = get_registry()
    items = reg.list_all()
    by_platform: dict[str, int] = {}
    total_bytes = 0
    courses = set()

    for it in items:
        by_platform[it.platform] = by_platform.get(it.platform, 0) + 1
        total_bytes += it.size_bytes
        if it.course_name:
            courses.add(it.course_name)

    # A course can be fully synced before its first file or lecture is
    # downloaded. Include those browser-discovered courses in the dashboard
    # count instead of reporting zero enrolled courses during that interval.
    courses.update(_synced_canvas_courses())

    return {
        "total_assets": len(items),
        "total_courses": len(courses),
        "total_storage_bytes": total_bytes,
        "total_storage_mb": round(total_bytes / (1024 * 1024), 2),
        "by_platform": by_platform,
    }


@router.post("/refresh-markdown")
async def refresh_markdown_inventory() -> dict[str, Any]:
    """Regenerates DOWNLOADS_INVENTORY.md from current registry state."""
    reg = get_registry()
    content = reg.export_markdown()
    return {
        "ok": True,
        "message": f"Refreshed markdown inventory with {len(reg.list_all())} items.",
        "line_count": len(content.splitlines()),
    }
