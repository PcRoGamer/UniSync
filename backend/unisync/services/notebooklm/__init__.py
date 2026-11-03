"""Google NotebookLM integration service package for UniSync."""

from __future__ import annotations

from unisync.services.notebooklm.state import (
    load_sync_state,
    save_sync_state,
    get_course_sync_state,
    update_course_notebook,
    record_uploaded_source,
    is_source_synced,
)
from unisync.services.notebooklm.gatherer import CourseFileGatherer, CourseFile
from unisync.services.notebooklm.service import NotebookLMService

__all__ = [
    "load_sync_state",
    "save_sync_state",
    "get_course_sync_state",
    "update_course_notebook",
    "record_uploaded_source",
    "is_source_synced",
    "CourseFileGatherer",
    "CourseFile",
    "NotebookLMService",
]
