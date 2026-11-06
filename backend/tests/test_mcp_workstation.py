"""Tests for the opt-in UniSync workstation MCP extension."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

from unisync.mcp import workstation as ws


@pytest.fixture
def approved(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    grant = tmp_path / "private" / "grant.json"
    monkeypatch.setenv("UNISYNC_WORKSTATION_GRANT_FILE", str(grant))
    grant.parent.mkdir()
    grant.write_text(json.dumps({"schema": 1, "root": str(root),
                                 "permissions": ["read", "write", "run"],
                                 "expires_at": time.time() + 600}))
    return root, grant


def test_grant_required(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISYNC_WORKSTATION_GRANT_FILE", str(tmp_path / "nope.json"))
    assert not ws.host_status()["enabled"]
    with pytest.raises(ws.WorkstationError):
        ws.host_list(".")
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"])


def test_listing_read_and_safe_write(approved):
    root, _ = approved
    root.joinpath("notes.txt").write_text("hello")
    root.joinpath(".env").write_text("secret")
    root.joinpath("browser_profile").mkdir()
    root.joinpath("browser_profile", "cookies.sqlite").write_bytes(b"secret")
    listed = ws.host_list()
    assert [x["name"] for x in listed["entries"]] == ["notes.txt"]
    result = ws.host_read("notes.txt")
    assert result["content"] == "hello"
    assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert ws.host_write("new.txt", "created")["created"]
    with pytest.raises(ws.WorkstationError):
        ws.host_write("new.txt", "overwrite")
    for name in [".env", "browser_profile/cookies.sqlite", "../private/grant.json"]:
        with pytest.raises(ws.WorkstationError):
            ws.host_read(name)


def test_replace_requires_sha_and_keeps_backup(approved):
    root, _ = approved
    (root / "file.py").write_text("a = 1\n")
    with pytest.raises(ws.WorkstationError):
        ws.host_replace("file.py", "1", "2", "bad")
    digest = ws.host_read("file.py")["sha256"]
    result = ws.host_replace("file.py", "a = 1", "a = 2", digest)
    assert (root / "file.py").read_text() == "a = 2\n"
    assert (root / result["backup"]).read_text() == "a = 1\n"
    with pytest.raises(ws.WorkstationError):
        ws.host_replace("file.py", "a = 2", "a = 3", digest)


def test_expired_and_scoped_grants(approved):
    root, grant = approved
    grant.write_text(json.dumps({"schema": 1, "root": str(root),
                                 "permissions": ["read"],
                                 "expires_at": time.time() - 1}))
    with pytest.raises(ws.WorkstationError):
        ws.host_list()
    grant.write_text(json.dumps({"schema": 1, "root": str(root),
                                 "permissions": ["read"],
                                 "expires_at": time.time() + 60}))
    with pytest.raises(ws.WorkstationError):
        ws.host_write("x", "hi")
    with pytest.raises(ws.WorkstationError):
        ws.host_run([sys.executable, "--version"])


def test_run_argv_without_shell(approved):
    result = ws.host_run([sys.executable, "-c", "print('mcp-ok')"], timeout_seconds=5)
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "mcp-ok"
    assert result["stderr"] == ""


def test_symlink_outside_root_denied(approved, tmp_path):
    root, _ = approved
    target = tmp_path / "elsewhere.txt"
    target.write_text("x")
    try:
        (root / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks unavailable on this platform")
    with pytest.raises(ws.WorkstationError):
        ws.host_read("link.txt")


def test_mcp_registration_keeps_tools_off_without_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISYNC_WORKSTATION_GRANT_FILE", str(tmp_path / "no.json"))
    class FakeMCP:
        def __init__(self):
            self.tools = {}
        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco
    mcp = FakeMCP()
    ws.register_workstation_tools(mcp)
    assert len(mcp.tools) == 6
    assert json.loads(mcp.tools["unisync_host_status"]())["enabled"] is False
    with pytest.raises(ws.WorkstationError):
        mcp.tools["unisync_host_read"]("README.md")


def test_file_read_pagination(approved):
    root, _ = approved
    (root / "log.txt").write_text("abcdefgh")
    first = ws.host_read("log.txt", offset=0, max_bytes=3)
    assert (first["content"], first["next_offset"], first["has_more"]) == ("abc", 3, True)
    second = ws.host_read("log.txt", offset=first["next_offset"], max_bytes=5)
    assert (second["content"], second["has_more"]) == ("defgh", False)


def test_only_one_text_match_may_be_replaced(approved):
    root, _ = approved
    (root / "app.py").write_text("same same")
    digest = ws.host_read("app.py")["sha256"]
    with pytest.raises(ws.WorkstationError):
        ws.host_replace("app.py", "same", "other", digest)
    assert (root / "app.py").read_text() == "same same"


def test_expired_grant_does_not_reactivate(approved):
    _, grant = approved
    data = json.loads(grant.read_text())
    data["expires_at"] = time.time() - 20
    grant.write_text(json.dumps(data))
    assert not ws.host_status()["enabled"]
    with pytest.raises(ws.WorkstationError):
        ws.host_read("anything.txt")
