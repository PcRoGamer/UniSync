"""TodoItem Pydantic model for the personal todos system."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Any

from pydantic import BaseModel


class TodoItem(BaseModel):
    """A single todo item stored in ``data/user/workspace/todos/todos.json``."""

    id: str
    title: str
    status: Literal["open", "done", "dismissed"] = "open"
    due_at: datetime | None = None
    due_at_confidence: Literal["high", "medium", "low"] | None = None
    kind: Literal["assignment", "exam", "meeting", "admin", "other", "manual"] = "manual"
    course: str | None = None
    priority: Literal["low", "normal", "high"] = "normal"
    notes: str = ""
    # Pragmatic source: Canvas LMS assignments/quizzes, EdStem tasks, or manual entries
    source: Literal["canvas", "edstem", "manual", "echo360", "email"] | str = "manual"
    source_id: str | None = None  # e.g. canvas:assignment:123 or edstem:36892:114327
    source_message_id: str | None = None  # legacy / fallback
    source_subject: str | None = None
    source_from: str | None = None
    source_received_at: datetime | None = None
    created_at: float
    updated_at: float
    completed_at: float | None = None

    # Rich LMS Assessment & Progression Attributes (Zero-LLM Programmatic)
    submission_status: Literal["unsubmitted", "submitted", "graded", "pending_review"] | str | None = None
    is_graded: bool = False
    score: float | None = None  # Numeric points earned, e.g. 18.5
    points_possible: float | None = None  # Max available points, e.g. 20.0
    grade: str | None = None  # Display grade string, e.g. "18.5", "92.5%", "A"
    grading_type: str | None = None  # "points", "percent", "letter_grade", "pass_fail"
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    description: str | None = None  # Full assignment prompt / instructions (HTML or text)
    submission_types: list[str] | None = None  # Allowed submission methods: e.g. ['online_upload']
    allowed_extensions: list[str] | None = None  # e.g. ['pdf', 'docx', 'zip']
    rubric: list[dict[str, Any]] | None = None  # Rubric criteria definition with ratings
    submission_comments: list[dict[str, Any]] | None = None  # Marker/instructor feedback comments
    url: str | None = None  # Direct link to assignment in Canvas or EdStem

