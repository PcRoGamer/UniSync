"""Regression tests for consolidated UniSync data and existing KB index paths."""
from pathlib import Path

from unisync.core import path_service
from unisync.services.kb.service import KnowledgeBaseService


def test_default_kb_honours_runtime_data_root(tmp_path, monkeypatch):
    root = tmp_path / "academic-data"
    monkeypatch.setenv("UNISYNC_DATA_DIR", str(root))
    monkeypatch.setattr(path_service, "_INSTANCE", None)
    kb = KnowledgeBaseService()
    assert kb.kb_dir.resolve() == (root / "kb").resolve()
    assert kb.index_dir == root / "kb" / "index"


def test_legacy_index_paths_rebase_to_active_course_files(tmp_path):
    root = tmp_path / "academic-data"
    file = root / "courses" / "BLAW10001_2026_SM2" / "pages" / "examinable.md"
    file.parent.mkdir(parents=True)
    file.write_text("Study notes", encoding="utf-8")
    kb = KnowledgeBaseService(kb_dir=root / "kb")
    old = "C:/OldMachine/UniSync/data/courses/BLAW10001_2026_SM2/pages/examinable.md"
    assert kb._rebase_indexed_path(old) == str(file.resolve())
    assert kb._rebase_indexed_path("C:/not-a-valid-source.txt") == "C:/not-a-valid-source.txt"
