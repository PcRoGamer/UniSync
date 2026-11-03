from pathlib import Path

from unisync.core.path_service import PathService


def test_path_service_honors_explicit_runtime_data_root(monkeypatch, tmp_path: Path) -> None:
    runtime_data = tmp_path / "guardian-user-data" / "unisync" / "data"
    monkeypatch.setenv("UNISYNC_DATA_DIR", str(runtime_data))

    service = PathService(project_root=tmp_path / "packaged-unisync")

    assert service.get_user_root() == runtime_data.resolve()
    assert service.get_workspace_dir() == runtime_data.resolve() / "workspace"
    assert (runtime_data / "libraries").is_dir()
    assert (runtime_data / "sync_state").is_dir()


def test_path_service_defaults_to_project_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("UNISYNC_DATA_DIR", raising=False)
    service = PathService(project_root=tmp_path)
    assert service.get_user_root() == tmp_path / "data"
