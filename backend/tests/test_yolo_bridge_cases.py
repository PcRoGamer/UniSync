from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import time
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "unisync" / "mcp" / "workstation.py"
SPEC = importlib.util.spec_from_file_location("bridge_yolo_under_test", SOURCE)
ws = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ws)


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "safe-root"
    root.mkdir()
    parent = tmp_path / "private"
    parent.mkdir()
    monkeypatch.setenv("UNISYNC_BRIDGE_YOLO_FILE", str(parent / "yolo.json"))
    monkeypatch.setenv("UNISYNC_WORKSTATION_GRANT_FILE", str(parent / "grant.json"))
    return root, parent


def enable(state):
    root, parent = state
    (parent / "yolo.json").write_text(json.dumps({
        "schema": 2, "mode": "yolo", "root": str(root),
        "workspace": str(root), "permissions": ["read", "write", "run"],
        "acknowledgement": "LOCAL_WINDOWS_CONFIRMATION",
    }), encoding="utf-8")
    return root


def test_unapproved_is_fail_closed(state):
    assert ws.host_status()["enabled"] is False
    for action in (lambda: ws.host_list(), lambda: ws.host_read("anything"),
                   lambda: ws.host_write("x", "test"),
                   lambda: ws.host_run([sys.executable, "--version"])):
        with pytest.raises(ws.WorkstationError):
            action()


def test_yolo_requires_all_config_fields(state):
    _, parent = state
    valid = {"schema": 2, "mode": "yolo", "root": str(state[0]),
             "workspace": str(state[0]), "permissions": ["read", "write", "run"],
             "acknowledgement": "LOCAL_WINDOWS_CONFIRMATION"}
    for key, value in [("schema", 3), ("mode", "legacy"), ("root", "not-there"),
                       ("permissions", ["run"]), ("acknowledgement", "remote")]:
        payload = {**valid, key: value}
        (parent / "yolo.json").write_text(json.dumps(payload))
        assert not ws.host_status()["enabled"], key
    (parent / "yolo.json").write_text("{broken json")
    assert not ws.host_status()["enabled"]
    (parent / "yolo.json").write_text("[]")
    assert not ws.host_status()["enabled"]


def test_enabled_yolo_is_persistent(state):
    root = enable(state)
    result = ws.host_status()
    assert result["bridge_version"] == "2.1.1"
    assert result["mode"] == "yolo"
    assert result["persistent"] is True
    assert result["expires_at"] is None
    assert result["run_expires_at"] is None
    assert set(result["permissions"]) == {"read", "write", "run"}
    assert Path(result["root"]) == root
    assert Path(result["workspace"]) == root
    assert Path(result["run_root"]) == root


def test_yolo_read_write_replace_and_backup(state):
    root = enable(state)
    target = root / "data.txt"
    created = ws.host_write("data.txt", "hello β\nold=2\n")
    assert created["bytes"] == len("hello β\nold=2\n".encode("utf-8"))
    before = ws.host_read(str(target))  # absolute path inside approved root
    assert before["content"].endswith("old=2\n")
    assert before["sha256"] == created["sha256"]
    with pytest.raises(ws.WorkstationError, match="SHA"):
        ws.host_replace("data.txt", "old=2", "old=3", "wrong")
    after_edit = ws.host_replace("data.txt", "old=2", "old=3", before["sha256"])
    after = ws.host_read("data.txt")
    assert after["sha256"] == after_edit["sha256"]
    assert after["content"].endswith("old=3\n")
    original = ws.host_read(after_edit["backup"])
    assert original["sha256"] == before["sha256"]
    with pytest.raises(ws.WorkstationError):
        ws.host_write("data.txt", "destroy")
    with pytest.raises(ws.WorkstationError):
        ws.host_replace("data.txt", "old=3", "bad", before["sha256"])
    assert target.read_text(encoding="utf-8").endswith("old=3\n")


def test_yolo_pagination_and_bounded_read(state):
    root = enable(state)
    (root / "a.txt").write_text("abcdefgh")
    first = ws.host_read("a.txt", 1, 3)
    assert (first["content"], first["next_offset"], first["has_more"]) == ("bcd", 4, True)
    assert ws.host_read("a.txt", 4, 20)["content"] == "efgh"
    for bad in (-1, 10):
        with pytest.raises(ws.WorkstationError):
            ws.host_read("a.txt", bad)
    assert ws.host_read("a.txt", 0, 10**8)["has_more"] is False


def test_list_secret_exclusion_and_nested_paths(state):
    root = enable(state)
    (root / "ordinary.txt").write_text("ok")
    (root / ".env").write_text("hidden")
    (root / "credentials").mkdir()
    (root / "credentials" / "value.txt").write_text("hidden")
    (root / "group").mkdir()
    assert [x["name"] for x in ws.host_list()["entries"]] == ["group", "ordinary.txt"]
    for raw in [".env", "credentials/value.txt", "id_rsa", "../outside", "data.txt:stream", "C:foo"]:
        with pytest.raises(ws.WorkstationError):
            ws.host_read(raw)


def test_symbolic_links_cannot_escape(state, tmp_path):
    root = enable(state)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    try:
        (root / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not permitted on this OS")
    with pytest.raises(ws.WorkstationError):
        ws.host_read("escape.txt")
    with pytest.raises(ws.WorkstationError):
        ws.host_write("escape.txt", "overwrite")
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"], cwd="escape.txt")


def test_run_is_automatic_and_preserves_argument_boundaries(state):
    enable(state)
    result = ws.host_run([sys.executable, "-c", "import sys; print(repr(sys.argv[1]))", "a;b && echo hacked"], timeout_seconds=6)
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == repr("a;b && echo hacked")
    assert result["stderr"] == ""


def test_run_proper_cwd_and_errors(state):
    root = enable(state)
    sub = root / "some-folder"
    sub.mkdir()
    result = ws.host_run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd="some-folder")
    assert Path(result["stdout"].strip()).resolve() == sub.resolve()
    assert ws.host_run(["command-that-does-not-exist---12497"])["exit_code"] is None
    with pytest.raises(ws.WorkstationError):
        ws.host_run([], cwd=".")
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"], cwd="../private")
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"], cwd="missing")


def test_run_output_cap_and_timeout(state):
    enable(state)
    result = ws.host_run([sys.executable, "-c", "print('A'*100000)"], timeout_seconds=5)
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert len(result["stdout"].encode("utf-8")) <= ws.MAX_OUTPUT_BYTES
    result = ws.host_run([sys.executable, "-c", "import time;time.sleep(8)"], timeout_seconds=1)
    assert result["timed_out"] is True
    assert result["exit_code"] is None


def test_large_write_rejected_and_exact_match(state):
    root = enable(state)
    with pytest.raises(ws.WorkstationError):
        ws.host_write("large.txt", "x" * (ws.MAX_WRITE_BYTES + 1))
    (root / "two.txt").write_text("same same")
    sha = ws.host_read("two.txt")["sha256"]
    with pytest.raises(ws.WorkstationError):
        ws.host_replace("two.txt", "same", "different", sha)
    assert (root / "two.txt").read_text() == "same same"


def test_legacy_persistent_file_grant_supported(state):
    root, private = state
    (private / "grant.json").write_text(json.dumps({
        "schema": 1, "root": str(root), "permissions": ["read", "write"],
        "persistent": True, "expires_at": None,
    }))
    status = ws.host_status()
    assert status["enabled"]
    assert status["mode"] == "legacy"
    assert status["permissions"] == ["read", "write"]
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"])


def test_legacy_run_grant_requires_expiry(state):
    root, private = state
    (private / "run-grant.json").write_text(json.dumps({
        "schema": 1, "root": str(root), "permissions": ["run"],
        "expires_at": time.time() + 60,
    }))
    assert "run" in ws.host_status()["permissions"]
    assert ws.host_run([sys.executable, "-c", "print('legacy-ok')"])["stdout"].strip() == "legacy-ok"
    (private / "run-grant.json").write_text(json.dumps({
        "schema": 1, "root": str(root), "permissions": ["run"],
        "persistent": True, "expires_at": None,
    }))
    assert ws.host_status()["enabled"] is False


def test_expired_legacy_file_grant_is_denied(state):
    root, private = state
    (private / "grant.json").write_text(json.dumps({
        "schema": 1, "root": str(root), "permissions": ["read"],
        "expires_at": time.time() - 1,
    }))
    assert ws.host_status()["enabled"] is False


def test_revoke_disables_yolo_and_legacy(state, monkeypatch):
    enable(state)
    monkeypatch.setattr(sys, "argv", ["unisync-workstation", "revoke"])
    assert ws._cli() == 0
    assert ws.host_status()["enabled"] is False


def test_six_mcp_tool_signatures_remain(state):
    class MCP:
        tools = None
        def __init__(self):
            self.tools = {}
        def tool(self):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator
    m = MCP()
    ws.register_workstation_tools(m)
    assert set(m.tools) == {
        "unisync_host_status", "unisync_host_list", "unisync_host_read",
        "unisync_host_write", "unisync_host_replace", "unisync_host_run"
    }
    assert not json.loads(m.tools["unisync_host_status"]())["enabled"]
    enable(state)
    assert json.loads(m.tools["unisync_host_status"]())["mode"] == "yolo"
    result = json.loads(m.tools["unisync_host_run"]([sys.executable, "-c", "print(99)"]))
    assert result["stdout"].strip() == "99"


def test_no_background_listeners_or_network_services():
    source = SOURCE.read_text(encoding="utf-8")
    for banned in ("approval_agent", "approval_requests", "ThreadingHTTPServer", "uvicorn.run", "MessageBoxW", "Start-ScheduledTask"):
        assert banned not in source


def test_yolo_keeps_repo_relative_paths_and_explicit_absolute_scope(state):
    root = enable(state)
    repo = root / "Users" / "Admin" / "UniSync-repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("repo-relative-ok")
    (root / "public.txt").write_text("drive-absolute-ok")
    data = json.loads((state[1] / "yolo.json").read_text())
    data["workspace"] = str(repo)
    (state[1] / "yolo.json").write_text(json.dumps(data))
    assert ws.host_read("README.md")["content"] == "repo-relative-ok"
    assert ws.host_read(str(root / "public.txt"))["content"] == "drive-absolute-ok"
    assert ws.host_list()["directory"] == "."
    executed = ws.host_run([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert Path(executed["stdout"].strip()).resolve() == repo.resolve()


def test_concurrent_create_cannot_overwrite_other_tool_call(state):
    import concurrent.futures
    enable(state)
    def create(i):
        try:
            return ws.host_write("race.txt", f"written-by={i}")["created"]
        except ws.WorkstationError:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(create, range(32)))
    assert sum(results) == 1
    content = ws.host_read("race.txt")["content"]
    assert content.startswith("written-by=")


def test_concurrent_sha_edits_only_one_wins(state):
    import concurrent.futures
    root = enable(state)
    (root / "edit.txt").write_text("revision=0")
    sha = ws.host_read("edit.txt")["sha256"]
    def edit(i):
        try:
            return ws.host_replace("edit.txt", "revision=0", f"revision={i}", sha)["sha256"]
        except ws.WorkstationError:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(edit, range(1, 25)))
    assert sum(r is not None for r in results) == 1
    assert ws.host_read("edit.txt")["content"].startswith("revision=")
    assert len(list(root.glob("edit.txt.backup-*"))) == 1
