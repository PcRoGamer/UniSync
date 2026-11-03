"""UniSync Core Storage and KB Management."""

from pathlib import Path
from typing import Any
from unisync.core.path_service import get_path_service


class KnowledgeBaseManager:
    """Standalone course directory and storage manager."""
    def get_knowledge_base_path(self, kb_name: str) -> Path:
        return get_path_service().get_knowledge_base_path(kb_name)


def current_kb_manager() -> KnowledgeBaseManager:
    return KnowledgeBaseManager()


def _ensure_kb_exists(kb_name: str, description: str = "") -> Path:
    p = get_path_service().get_knowledge_base_path(kb_name)
    (p / "raw").mkdir(parents=True, exist_ok=True)
    return p


def _mark_kb_queued_for_processing(*args: Any, **kwargs: Any) -> None:
    pass


def run_upload_processing_task(*args: Any, **kwargs: Any) -> None:
    pass
