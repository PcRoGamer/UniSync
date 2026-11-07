"""UniSync workstation MCP bridge, version 2.1.

One existing tunnel, six existing MCP tool names, one local opt-in grant.
YOLO is activated once *on the Windows desktop*, never by an MCP request.
No approval queue, popup worker, retry loop, or additional network listener.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

VERSION = "2.1.1"
GRANT_DIR = Path.home() / ".unisync-workstation"
YOLO_FILE = GRANT_DIR / "bridge-yolo.json"
DEFAULT_GRANT_FILE = GRANT_DIR / "grant.json"
PERMISSIONS = {"read", "write", "run"}
BASIC_PERMISSIONS = {"read", "write"}
MAX_TEXT_BYTES = 128_000
MAX_WRITE_BYTES = 1_000_000
MAX_CMD_SECONDS = 60
MAX_OUTPUT_BYTES = 24_000
MAX_EDIT_BYTES = 8_000_000
MUTATION_LOCK = threading.RLock()  # serialize edits across concurrent MCP calls

# Retain the original file-tool secret exclusions, even in YOLO mode. Host RUN
# is a separate, explicitly acknowledged capability with OS-user privileges.
SECRET_COMPONENTS = {
    ".ssh", ".aws", ".gnupg", "browser_profile", "secrets", "credentials",
    "token_store", "oauth", ".git", "node_modules", ".venv", "__pycache__",
}
SECRET_NAMES = {".env", ".netrc", "id_rsa", "id_ed25519", "key4.db", "cookies.sqlite"}
SECRET_PREFIXES = (".env.",)
SECRET_MARKERS = ("secret", "credential", "cookie", "password", "token", "private_key", "private-key", "auth_state")
SECRET_EXTENSIONS = {".pem", ".p12", ".pfx", ".key"}


class WorkstationError(ValueError):
    pass


def _grant_path() -> Path:
    return Path(os.environ.get("UNISYNC_WORKSTATION_GRANT_FILE", DEFAULT_GRANT_FILE)).expanduser()


def _yolo_path() -> Path:
    return Path(os.environ.get("UNISYNC_BRIDGE_YOLO_FILE", YOLO_FILE)).expanduser()


def _run_grant_path() -> Path:
    return _grant_path().with_name("run-grant.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError):
        return None


def _existing_directory(raw: Any) -> Path | None:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    try:
        path = Path(raw).resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _legacy_grant(path: Path, *, allow_run: bool = False) -> dict[str, Any] | None:
    data = _read_json(path)
    if not data or data.get("schema") != 1:
        return None
    try:
        perms = set(data.get("permissions") or [])
        if not perms or not perms <= ({"run"} if allow_run else BASIC_PERMISSIONS):
            return None
        if data.get("persistent") is True:
            if allow_run or data.get("expires_at") is not None:
                return None
            expiry = None
        else:
            expiry = float(data.get("expires_at", 0))
            if expiry <= time.time():
                return None
        root = _existing_directory(data.get("root"))
        if root is None:
            return None
        return {"root": root, "permissions": perms,
                "expires_at": expiry, "persistent": expiry is None}
    except (ValueError, TypeError, OverflowError):
        return None


def _load_grant() -> dict[str, Any] | None:
    data = _read_json(_yolo_path())
    if (data and data.get("schema") == 2 and data.get("mode") == "yolo"
            and data.get("acknowledgement") == "LOCAL_WINDOWS_CONFIRMATION"
            and data.get("permissions") == ["read", "write", "run"]):
        root = _existing_directory(data.get("root"))
        workspace = _existing_directory(data.get("workspace"))
        if root is not None and workspace is not None:
            try:
                workspace.relative_to(root)
            except ValueError:
                pass
            else:
                return {"root": root, "workspace": workspace,
                        "permissions": PERMISSIONS.copy(),
                        "expires_at": None, "persistent": True, "mode": "yolo",
                        "run_root": root, "run_expires_at": None}

    # Existing manually approved read/write access remains available when
    # switching installations or declining the new YOLO activation.
    ordinary = _legacy_grant(_grant_path())
    auto = [_legacy_grant(_grant_path().with_name(f"auto-{name}.json"))
            for name in ("read", "write")]
    candidates = [item for item in [ordinary, *auto] if item]
    primary = next(iter(candidates), None)
    matched = [item for item in candidates if item["root"] == primary["root"]] if primary else []
    perms = set().union(*(item["permissions"] for item in matched))
    expiring = [item["expires_at"] for item in matched if item["expires_at"] is not None]
    enduring = set().union(*(item["permissions"] for item in matched if item["persistent"]))
    run = _legacy_grant(_run_grant_path(), allow_run=True)
    if not perms and not run:
        return None
    if run:
        perms.add("run")
    root = primary["root"] if primary else run["root"]
    persistent = bool(primary and (perms - {"run"}) <= enduring)
    return {"root": root, "workspace": root, "permissions": perms,
            "persistent": persistent, "mode": "legacy", "expires_at": None if persistent else max(expiring, default=None),
            "run_root": run["root"] if run else None,
            "run_expires_at": run["expires_at"] if run else None}


def _require(permission: str) -> dict[str, Any]:
    grant = _load_grant()
    if not grant or permission not in grant["permissions"]:
        raise WorkstationError(
            "Workstation capability not locally approved. Enable UniSync YOLO on the Windows desktop."
        )
    return grant


def _resolve(grant: dict[str, Any], raw: str, *, existing: bool = False,
             for_run: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise WorkstationError("A non-empty path is required")
    root = grant["run_root"] if for_run else grant["root"]
    if root is None:
        raise WorkstationError("No approved command root")
    supplied = Path(raw)
    if not supplied.is_absolute() and (supplied.drive or supplied.root):
        raise WorkstationError("Drive-relative and rooted-relative paths are forbidden")
    if not supplied.is_absolute() and ":" in raw:
        raise WorkstationError("Alternate data streams and drive-relative paths are forbidden")
    # Relative paths keep the existing UniSync repository semantics. Explicit
    # absolute paths unlock the broader C:\ scope in YOLO without making every
    # existing agent suddenly resolve backend/... against the drive root.
    base = grant.get("workspace", root)
    target = (supplied if supplied.is_absolute() else base / supplied).resolve(strict=False)
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise WorkstationError("Path escapes approved root") from None
    if not for_run:
        for part in parts:
            name = part.lower()
            if (name in SECRET_COMPONENTS or name in SECRET_NAMES
                    or any(name.startswith(p) for p in SECRET_PREFIXES)
                    or any(marker in name for marker in SECRET_MARKERS)
                    or Path(part).suffix.lower() in SECRET_EXTENSIONS
                    or ":" in part):
                raise WorkstationError("File path is excluded from MCP file access")
    if existing and not target.exists():
        raise WorkstationError("File or directory does not exist")
    return target


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise WorkstationError("Parent directory does not exist")
    fd, tmp = tempfile.mkstemp(prefix=".unisync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def host_status() -> dict[str, Any]:
    grant = _load_grant()
    if not grant:
        return {"enabled": False, "reason": "local_activation_required",
                "bridge_version": VERSION, "new_tunnel_needed": False}
    result = {
        "enabled": True, "root": str(grant["root"]),
        "workspace": str(grant["workspace"]),
        "permissions": sorted(grant["permissions"]), "persistent": grant["persistent"],
        "expires_at": grant["expires_at"], "mode": grant["mode"],
        "bridge_version": VERSION, "new_tunnel_needed": False,
    }
    if "run" in grant["permissions"]:
        result["run_root"] = str(grant["run_root"])
        result["run_expires_at"] = grant["run_expires_at"]
    return result


def host_list(directory: str = ".", limit: int = 200) -> dict[str, Any]:
    grant = _require("read")
    folder = _resolve(grant, directory, existing=True)
    if not folder.is_dir():
        raise WorkstationError("Not a directory")
    limit = min(max(int(limit), 1), 500)
    entries = []
    for item in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        try:
            path = _resolve(grant, str(item), existing=True)
            entries.append({"name": item.name, "is_dir": path.is_dir(),
                            "bytes": path.stat().st_size if path.is_file() else None})
        except (WorkstationError, OSError, ValueError):
            continue
        if len(entries) >= limit:
            break
    return {"directory": directory, "entries": entries}


def host_read(path: str, offset: int = 0, max_bytes: int = 32000) -> dict[str, Any]:
    grant = _require("read")
    target = _resolve(grant, path, existing=True)
    if not target.is_file():
        raise WorkstationError("Not a file")
    size = target.stat().st_size
    if not isinstance(offset, int) or not 0 <= offset <= size:
        raise WorkstationError("Invalid byte offset")
    max_bytes = min(max(int(max_bytes), 1), MAX_TEXT_BYTES)
    with target.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(max_bytes)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return {"path": path, "sha256": digest.hexdigest(), "size": size,
            "offset": offset, "next_offset": offset + len(data),
            "has_more": offset + len(data) < size,
            "content": data.decode("utf-8", errors="replace")}


def host_write(path: str, content: str, expected_sha256: str = "") -> dict[str, Any]:
    with MUTATION_LOCK:
        grant = _require("write")
        target = _resolve(grant, path)
        if target.exists():
            raise WorkstationError("File exists; use SHA-protected replace")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkstationError("Invalid or oversized content")
        if expected_sha256:
            raise WorkstationError("File creation cannot use an existing-file precondition")
        payload = content.encode("utf-8")
        _atomic_write(target, payload)
        return {"path": path, "created": True, "sha256": _sha256(payload), "bytes": len(payload)}


def host_replace(path: str, old_text: str, new_text: str,
                 expected_sha256: str) -> dict[str, Any]:
    with MUTATION_LOCK:
        grant = _require("write")
        target = _resolve(grant, path, existing=True)
        if not target.is_file() or target.stat().st_size > MAX_EDIT_BYTES:
            raise WorkstationError("Only existing text files under 8 MB can be edited")
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise WorkstationError("Non-empty old text and new text must be strings")
        if len(old_text.encode("utf-8")) > MAX_EDIT_BYTES or len(new_text.encode("utf-8")) > MAX_EDIT_BYTES:
            raise WorkstationError("Replacement text is oversized")
        original = target.read_bytes()
        if not expected_sha256 or _sha256(original) != expected_sha256:
            raise WorkstationError("SHA-256 precondition failed; re-read the file")
        decoded = original.decode("utf-8")
        if decoded.count(old_text) != 1:
            raise WorkstationError("Exactly one matching text span is required")
        modified = decoded.replace(old_text, new_text, 1).encode("utf-8")
        if len(modified) > MAX_EDIT_BYTES:
            raise WorkstationError("Modified file exceeds size limit")
        backup = target.with_name(target.name + ".backup-" + str(time.time_ns()))
        _atomic_write(backup, original)
        _atomic_write(target, modified)
        try:
            backup_name = str(backup.relative_to(grant["workspace"]))
        except ValueError:
            backup_name = str(backup)
        return {"path": path, "backup": backup_name,
                "previous_sha256": _sha256(original), "sha256": _sha256(modified)}


def host_run(argv: list[str], cwd: str = ".", timeout_seconds: int = 30) -> dict[str, Any]:
    """Run one argv command with Windows-account privileges, no shell parsing."""
    grant = _require("run")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise WorkstationError("Provide a non-empty command argument list")
    if not all(isinstance(arg, str) and arg and "\x00" not in arg and len(arg) <= 4096
               for arg in argv):
        raise WorkstationError("Command contains invalid arguments")
    directory = _resolve(grant, cwd, existing=True, for_run=True)
    if not directory.is_dir():
        raise WorkstationError("Command working directory is not a directory")
    timeout_seconds = min(max(int(timeout_seconds), 1), MAX_CMD_SECONDS)
    # Preserve the original restricted environment. The caller can explicitly
    # supply an alternative command via argv when intentionally needed.
    keys = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
            "USERPROFILE", "LOCALAPPDATA", "APPDATA", "VIRTUAL_ENV", "PYTHONPATH",
            "LANG", "LC_ALL", "COMSPEC", "PROCESSOR_ARCHITECTURE"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keys}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(argv, cwd=directory, shell=False, env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=flags)
    except OSError as exc:
        return {"exit_code": None, "error": type(exc).__name__}
    outputs: dict[str, tuple[bytes, int]] = {}
    def drain(stream: Any, key: str) -> None:
        retained = bytearray()
        total = 0
        while True:
            data = stream.read(8192)
            if not data:
                break
            total += len(data)
            room = MAX_OUTPUT_BYTES - len(retained)
            if room > 0:
                retained.extend(data[:room])
        outputs[key] = (bytes(retained), total)

    threads = [threading.Thread(target=drain, args=(stream, label), daemon=True)
               for label, stream in (("stdout", process.stdout), ("stderr", process.stderr))]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            # Best-effort Windows process-tree cleanup, avoiding orphaned children.
            try:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=flags)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        process.wait()
        timed_out = True
    for thread in threads:
        thread.join(timeout=2)
    if timed_out:
        return {"exit_code": None, "timed_out": True, "timeout_seconds": timeout_seconds}
    out, out_length = outputs.get("stdout", (b"", 0))
    err, err_length = outputs.get("stderr", (b"", 0))
    return {"exit_code": process.returncode,
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "stdout_truncated": out_length > MAX_OUTPUT_BYTES,
            "stderr_truncated": err_length > MAX_OUTPUT_BYTES}


def register_workstation_tools(mcp: Any) -> None:
    """Keep the six established tool signatures and MCP names unchanged."""
    @mcp.tool()
    def unisync_host_status() -> str:
        """Return the active workstation bridge mode, scope, and permissions."""
        return json.dumps(host_status())

    @mcp.tool()
    def unisync_host_list(directory: str = ".", limit: int = 200) -> str:
        """List non-secret files under the approved filesystem root."""
        return json.dumps(host_list(directory, limit))

    @mcp.tool()
    def unisync_host_read(path: str, offset: int = 0, max_bytes: int = 32000) -> str:
        """Read a bounded file segment and SHA-256 under the approved root."""
        return json.dumps(host_read(path, offset, max_bytes))

    @mcp.tool()
    def unisync_host_write(path: str, content: str) -> str:
        """Create a file without overwriting an existing file."""
        return json.dumps(host_write(path, content))

    @mcp.tool()
    def unisync_host_replace(path: str, old_text: str, new_text: str,
                             expected_sha256: str) -> str:
        """Replace a single exact text span with SHA precondition and backup."""
        return json.dumps(host_replace(path, old_text, new_text, expected_sha256))

    @mcp.tool()
    def unisync_host_run(argv: list[str], cwd: str = ".", timeout_seconds: int = 30) -> str:
        """Run one local subprocess via enabled workstation YOLO bridge."""
        return json.dumps(host_run(argv, cwd, timeout_seconds))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="UniSync workstation bridge")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    sub.add_parser("revoke")
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(host_status(), indent=2))
    else:
        for item in (_yolo_path(), _grant_path(), _run_grant_path(),
                     _grant_path().with_name("auto-read.json"),
                     _grant_path().with_name("auto-write.json")):
            item.unlink(missing_ok=True)
        print("UniSync workstation capabilities revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
