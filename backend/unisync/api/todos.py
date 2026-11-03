"""Todos API Router — CRUD for personal todo items."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from unisync.services.todos import get_todo_service

router = APIRouter(prefix="/api/v1/todos", tags=["todos"])


# === Request Models ===


class CreateTodoRequest(BaseModel):
    title: str = Field(min_length=1)
    due_at: datetime | None = None
    kind: Literal["assignment", "exam", "meeting", "admin", "other", "manual"] = "manual"
    course: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    notes: str = ""
    source: str = "manual"
    source_id: str | None = None
    submission_status: Literal["unsubmitted", "submitted", "graded", "pending_review"] | str | None = None
    is_graded: bool = False
    score: float | None = None
    points_possible: float | None = None
    grade: str | None = None
    grading_type: str | None = None
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    description: str | None = None
    submission_types: list[str] | None = None
    allowed_extensions: list[str] | None = None
    rubric: list[dict[str, Any]] | None = None
    submission_comments: list[dict[str, Any]] | None = None
    url: str | None = None


class UpdateTodoRequest(BaseModel):
    title: str | None = None
    status: Literal["open", "done", "dismissed"] | None = None
    due_at: datetime | None = None
    kind: Literal["assignment", "exam", "meeting", "admin", "other", "manual"] | None = None
    course: str | None = None
    priority: Literal["low", "normal", "high"] | None = None
    notes: str | None = None
    submission_status: Literal["unsubmitted", "submitted", "graded", "pending_review"] | str | None = None
    is_graded: bool | None = None
    score: float | None = None
    points_possible: float | None = None
    grade: str | None = None
    grading_type: str | None = None
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    description: str | None = None
    submission_types: list[str] | None = None
    allowed_extensions: list[str] | None = None
    rubric: list[dict[str, Any]] | None = None
    submission_comments: list[dict[str, Any]] | None = None
    url: str | None = None


# === Endpoints ===

# NOTE: /upcoming MUST be defined before /{todo_id} so FastAPI does not
# match "upcoming" as a todo_id path parameter.


@router.get("")
async def list_todos(
    status: str | None = None,
    kind: str | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
):
    """List todo items with optional filters."""
    try:
        service = get_todo_service()
        items = service.list_items(
            status=status, kind=kind, due_before=due_before, due_after=due_after
        )
        return {"success": True, "todos": [item.model_dump(mode="json") for item in items]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_todo(data: CreateTodoRequest):
    """Create a manual todo item."""
    try:
        service = get_todo_service()
        item = service.create_item(data.model_dump())
        return {"success": True, "todo": item.model_dump(mode="json")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/upcoming")
async def get_upcoming():
    """List open todo items sorted by due date (ascending, null last)."""
    try:
        service = get_todo_service()
        items = service.get_upcoming_items()
        return {"success": True, "todos": [item.model_dump(mode="json") for item in items]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{todo_id}")
async def get_todo(todo_id: str):
    """Get single todo item by ID with all details."""
    try:
        service = get_todo_service()
        item = service.get_item(todo_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"Todo {todo_id} not found")
        return {"success": True, "todo": item.model_dump(mode="json")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{todo_id}")
async def update_todo(todo_id: str, data: UpdateTodoRequest):
    """Update (or complete/dismiss) a todo item."""
    try:
        service = get_todo_service()
        updates = {k: v for k, v in data.model_dump(exclude_unset=True).items()}
        item = service.update_item(todo_id, updates)
        return {"success": True, "todo": item.model_dump(mode="json")}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Todo {todo_id} not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{todo_id}")
async def delete_todo(todo_id: str):
    """Delete a todo item."""
    try:
        service = get_todo_service()
        deleted = service.delete_item(todo_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Todo {todo_id} not found")
        return {"success": True, "message": f"Todo {todo_id} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
