"""PathService - Centralized runtime storage layout for UniSync.

Stores all data inside the UniSync data directory:
  UniSync/data/
  ├── cache/
  │   └── echo360_subtitles/
  ├── libraries/
  │   ├── canvas_files.json
  │   └── libraries.json
  ├── sync_state/
  │   └── sync_delta_state.json
  ├── settings/
  │   ├── integrations.json
  │   └── email_monitor.json
  └── workspace/
      ├── email_monitor/
      │   ├── processed.jsonl
      │   └── cursor.json
      ├── todos/
      │   └── todos.json
      └── courses/
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class PathService:
    """Centralized path resolver for UniSync."""

    def __init__(self, project_root: Optional[Path] = None):
        if project_root is None:
            # Fallback: find UniSync root by walking up from this file
            current = Path(__file__).resolve()
            # current is backend/unisync/core/path_service.py -> root is 4 parents up
            cand = current.parents[3]
            if (cand / "backend" / "unisync").exists():
                self.project_root = cand
            else:
                self.project_root = Path(r"c:\Users\Admin\Desktop\UniSync")
        else:
            self.project_root = Path(project_root)

        self.data_root = self.project_root / "data"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        """Create standard data directories."""
        dirs = [
            self.data_root / "cache" / "echo360_subtitles",
            self.data_root / "libraries",
            self.data_root / "sync_state",
            self.data_root / "settings",
            self.data_root / "workspace" / "email_monitor",
            self.data_root / "workspace" / "todos",
            self.data_root / "workspace" / "courses",
            self.data_root / "courses",
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    def get_user_root(self) -> Path:
        """Returns the data root where sync_state, libraries, settings live."""
        return self.data_root

    def get_workspace_dir(self) -> Path:
        """Returns the workspace directory."""
        return self.data_root / "workspace"

    def get_workspace_feature_dir(self, feature: str) -> Path:
        """Returns feature-specific directory (e.g., 'email_monitor', 'todos')."""
        path = self.data_root / "workspace" / feature
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_settings_dir(self) -> Path:
        """Returns settings directory."""
        path = self.data_root / "settings"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_runtime_cache_root(self) -> Path:
        """Returns cache directory."""
        path = self.data_root / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_knowledge_base_path(self, kb_name: str) -> Path:
        """Returns local directory for a course's knowledge base."""
        path = self.data_root / "courses" / kb_name
        path.mkdir(parents=True, exist_ok=True)
        return path


_INSTANCE: Optional[PathService] = None


def get_path_service() -> PathService:
    """Singleton getter for PathService."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = PathService()
    return _INSTANCE
