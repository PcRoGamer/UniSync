"""Regression test: workstation commands must not inherit MCP transport stdin."""
from __future__ import annotations

import json
import subprocess
import sys

from unisync.mcp import workstation as ws


def test_yolo_command_closes_transport_stdin(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    config = tmp_path / "bridge-yolo.json"
    config.write_text(json.dumps({
        "schema": 2,
        "mode": "yolo",
        "root": str(root),
        "workspace": str(root),
        "permissions": ["read", "write", "run"],
        "acknowledgement": "LOCAL_WINDOWS_CONFIRMATION",
    }), encoding="utf-8")
    monkeypatch.setenv("UNISYNC_BRIDGE_YOLO_FILE", str(config))
    original_popen = subprocess.Popen
    seen = []

    def checked_popen(*args, **kwargs):
        seen.append(kwargs.get("stdin"))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(ws.subprocess, "Popen", checked_popen)
    result = ws.host_run(
        [sys.executable, "-E", "-S", "-c", "print('stdin-isolated')"],
        cwd=str(root), timeout_seconds=12,
    )
    assert seen == [subprocess.DEVNULL], "Child must never inherit UniSync MCP stdin"
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == "stdin-isolated"
