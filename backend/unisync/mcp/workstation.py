"""Optional, locally approved workstation operations over UniSync's existing MCP.

Security boundary:
- Never active without an expiring approval written on the host.
- Read/write operations are confined to the approved root, excluding secrets.
- Replacements require a SHA-256 precondition, with an adjacent backup.
- Commands need a separate, explicit high-privilege grant. No shell parsing.
- No long-running processes; timeouts and output caps are enforced.

The grant is created *locally*, never through an MCP tool. This module adds
zero additional network listeners, auth systems, or background services.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

GRANT_DIR = Path.home() / ".unisync-workstation"
DEFAULT_GRANT_FILE = GRANT_DIR / "grant.json"
PERMISSIONS = {"read", "write", "run"}
SECRET_COMPONENTS = {
    ".ssh", ".aws", ".gnupg", "browser_profile", "secrets", "credentials",
    "token_store", "oauth", ".git", "node_modules", ".venv", "__pycache__",
}
SECRET_NAMES = {".env", ".netrc", "id_rsa", "id_ed25519", "key4.db", "cookies.sqlite"}
SECRET_PREFIXES = (".env.",)
MAX_TEXT_BYTES = 128_000
MAX_WRITE_BYTES = 1_000_000
MAX_CMD_SECONDS = 60
MAX_OUTPUT_BYTES = 24_000


class WorkstationError(ValueError):
    pass


def _grant_path() -> Path:
    """Override is intended for isolated tests and locally configured hosts only."""
    return Path(os.environ.get("UNISYNC_WORKSTATION_GRANT_FILE", DEFAULT_GRANT_FILE)).expanduser()


def _load_grant() -> dict[str, Any] | None:
    try:
        grant = json.loads(_grant_path().read_text(encoding="utf-8"))
        if not isinstance(grant, dict):
            return None
        if grant.get("schema") != 1 or grant.get("expires_at", 0) <= time.time():
            return None
        root = Path(grant["root"]).resolve(strict=True)
        if not root.is_dir():
            return None
        permissions = set(grant.get("permissions") or [])
        if not permissions or not permissions <= PERMISSIONS:
            return None
        return {"root": root, "permissions": permissions, "expires_at": float(grant["expires_at"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _require(permission: str) -> dict[str, Any]:
    grant = _load_grant()
    if grant is None:
        raise WorkstationError("No active workstation approval. Approve locally using python -m unisync.mcp.workstation grant.")
    if permission not in grant["permissions"]:
        raise WorkstationError(f"Permission '{permission}' is not locally approved.")
    return grant


def _resolve(grant: dict[str, Any], raw: str, *, existing: bool = False) -> Path:
    if not raw or "\x00" in raw:
        raise WorkstationError("A valid relative path is required")
    incoming = Path(raw)
    if incoming.is_absolute():
        raise WorkstationError("Use a path relative to the locally approved root")
    path = (grant["root"] / incoming).resolve(strict=False)
    try:
        parts = path.relative_to(grant["root"]).parts
    except ValueError:
        raise WorkstationError("Path escapes the locally approved root") from None
    if any(p.lower() in SECRET_COMPONENTS or p.lower() in SECRET_NAMES or
           any(p.lower().startswith(prefix) for prefix in SECRET_PREFIXES) for p in parts):
        raise WorkstationError("This path is excluded from workstation access")
    if existing and not path.exists():
        raise WorkstationError("File or directory does not exist")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tempname = tempfile.mkstemp(prefix=".unisync-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tempname, path)
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)


def host_status() -> dict[str, Any]:
    grant = _load_grant()
    if not grant:
        return {"enabled": False, "reason": "local_grant_required", "new_tunnel_needed": False}
    return {"enabled": True, "root": str(grant["root"]),
            "permissions": sorted(grant["permissions"]),
            "expires_at": grant["expires_at"], "new_tunnel_needed": False}


def host_list(directory: str = ".", limit: int = 200) -> dict[str, Any]:
    grant = _require("read")
    path = _resolve(grant, directory, existing=True)
    if not path.is_dir():
        raise WorkstationError("Not a directory")
    limit = min(max(limit, 1), 500)
    contents = []
    # Denied entries are excluded even from the directory listing.
    for item in sorted(path.iterdir(), key=lambda p: p.name.lower()):
        try:
            resolved = _resolve(grant, str(item.relative_to(grant["root"])))
            if resolved.exists():
                contents.append({"name": item.name, "is_dir": resolved.is_dir(),
                                 "bytes": resolved.stat().st_size if resolved.is_file() else None})
        except (WorkstationError, OSError, ValueError):
            continue
        if len(contents) == limit:
            break
    return {"directory": directory, "entries": contents}


def host_read(path: str, offset: int = 0, max_bytes: int = 32000) -> dict[str, Any]:
    grant = _require("read")
    target = _resolve(grant, path, existing=True)
    if not target.is_file():
        raise WorkstationError("Not a file")
    if not 0 <= offset <= target.stat().st_size:
        raise WorkstationError("Invalid byte offset")
    max_bytes = min(max(max_bytes, 1), MAX_TEXT_BYTES)
    with target.open("rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    # File hash is computed without copying the full file into the response.
    digest = hashlib.sha256()
    with target.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": path, "sha256": digest.hexdigest(), "size": target.stat().st_size,
            "offset": offset, "next_offset": offset + len(data),
            "has_more": offset + len(data) < target.stat().st_size,
            "content": data.decode("utf-8", errors="replace")}


def host_write(path: str, content: str, expected_sha256: str = "") -> dict[str, Any]:
    grant = _require("write")
    target = _resolve(grant, path)
    if target.exists():
        raise WorkstationError("File exists. Use replace with its current SHA-256 instead")
    if not target.parent.exists():
        raise WorkstationError("Parent directory does not exist")
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        raise WorkstationError("File exceeds the write size limit")
    if expected_sha256:
        raise WorkstationError("Creation does not accept an existing-file precondition")
    _atomic_write(target, payload)
    return {"path": path, "created": True, "sha256": _sha256(payload), "bytes": len(payload)}


def host_replace(path: str, old_text: str, new_text: str, expected_sha256: str) -> dict[str, Any]:
    grant = _require("write")
    target = _resolve(grant, path, existing=True)
    if not target.is_file() or target.stat().st_size > 8_000_000:
        raise WorkstationError("Only existing text files up to 8MB can be edited")
    content = target.read_bytes()
    if not expected_sha256 or _sha256(content) != expected_sha256:
        raise WorkstationError("SHA-256 precondition failed; re-read the file before editing")
    if not old_text:
        raise WorkstationError("An exact non-empty search string is required")
    original = content.decode("utf-8")
    if original.count(old_text) != 1:
        raise WorkstationError("Expected exactly one matching text block")
    changed = original.replace(old_text, new_text, 1).encode("utf-8")
    if len(changed) > 8_000_000:
        raise WorkstationError("Changed text exceeds size limit")
    # Never overwrite an existing backup. A timestamped backup is created
    # under the same directory, inside the already approved root.
    backup = target.with_name(target.name + ".backup-" + str(time.time_ns()))
    _atomic_write(backup, content)
    _atomic_write(target, changed)
    return {"path": path, "backup": str(backup.relative_to(grant["root"])),
            "previous_sha256": _sha256(content), "sha256": _sha256(changed)}


def host_run(argv: list[str], cwd: str = ".", timeout_seconds: int = 30) -> dict[str, Any]:
    """Run an explicitly locally approved subprocess; never invoke a shell."""
    grant = _require("run")
    if not argv or len(argv) > 64 or not all(isinstance(v, str) and v for v in argv):
        raise WorkstationError("Provide a non-empty argv list")
    if any("\x00" in v or len(v) > 4096 for v in argv):
        raise WorkstationError("Command contains invalid arguments")
    directory = _resolve(grant, cwd, existing=True)
    if not directory.is_dir():
        raise WorkstationError("Command working directory is not a directory")
    timeout_seconds = min(max(int(timeout_seconds), 1), MAX_CMD_SECONDS)
    # Run has the effective privileges of the UniSync process. Explicit local
    # approval is mandatory. Inherited environment is trimmed to reduce leaks.
    allowed_env = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "VIRTUAL_ENV", "PYTHONPATH",
        "LANG", "LC_ALL", "COMSPEC", "PROCESSOR_ARCHITECTURE",
    }
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed_env}
    try:
        process = subprocess.run(
            argv, cwd=directory, shell=False, env=env,
            capture_output=True, timeout=timeout_seconds, check=False,
        )
        stdout = process.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        stderr = process.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr,
                "stdout_truncated": len(process.stdout) > MAX_OUTPUT_BYTES,
                "stderr_truncated": len(process.stderr) > MAX_OUTPUT_BYTES}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timed_out": True, "timeout_seconds": timeout_seconds}
    except OSError as exc:
        return {"exit_code": None, "error": type(exc).__name__}


def register_workstation_tools(mcp: Any) -> None:
    """Register a disabled-by-default extension on the existing MCP server."""
    @mcp.tool()
    def unisync_host_status() -> str:
        """Check whether the user locally approved workstation access and its expiry."""
        return json.dumps(host_status())

    @mcp.tool()
    def unisync_host_list(directory: str = ".", limit: int = 200) -> str:
        """List ordinary files within the locally approved root (never secrets)."""
        return json.dumps(host_list(directory, limit))

    @mcp.tool()
    def unisync_host_read(path: str, offset: int = 0, max_bytes: int = 32000) -> str:
        """Read a bounded text segment and SHA-256 of an approved local file."""
        return json.dumps(host_read(path, offset, max_bytes))

    @mcp.tool()
    def unisync_host_write(path: str, content: str) -> str:
        """Create a new file under the approved root; refuse overwrites."""
        return json.dumps(host_write(path, content))

    @mcp.tool()
    def unisync_host_replace(path: str, old_text: str, new_text: str, expected_sha256: str) -> str:
        """Surgically edit a file with SHA-256 concurrency check and automatic backup."""
        return json.dumps(host_replace(path, old_text, new_text, expected_sha256))

    @mcp.tool()
    def unisync_host_run(argv: list[str], cwd: str = ".", timeout_seconds: int = 30) -> str:
        """Run one subprocess using a temporary, explicitly approved RUN grant."""
        return json.dumps(host_run(argv, cwd, timeout_seconds))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Local approval for UniSync's existing MCP tunnel")
    sub = parser.add_subparsers(dest="action", required=True)
    grant = sub.add_parser("grant", help="Grant temporary access by confirming on this computer")
    grant.add_argument("--root", type=Path, required=True)
    grant.add_argument("--minutes", type=int, default=15)
    grant.add_argument("--permissions", default="read,write")
    sub.add_parser("revoke", help="Revoke all current workstation access")
    sub.add_parser("status", help="Show current grant without revealing credentials")
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(host_status(), indent=2))
        return 0
    if args.action == "revoke":
        _grant_path().unlink(missing_ok=True)
        print("Revoked workstation permissions")
        return 0
    if not args.root.is_dir():
        parser.error("--root must name an existing directory")
    if not 1 <= args.minutes <= 120:
        parser.error("--minutes must be between 1 and 120")
    requested = {p.strip().lower() for p in args.permissions.split(",")}
    if not requested or not requested <= PERMISSIONS:
        parser.error("--permissions must contain read,write,run")
    print("This grants ChatGPT-connected MCP agents access to your laptop.")
    print("ROOT:", args.root.resolve())
    print("PERMISSIONS:", ", ".join(sorted(requested)))
    print("EXPIRES IN:", args.minutes, "minutes")
    if "run" in requested:
        print("WARNING: RUN permits code execution with your Windows account permissions.")
        print("A RUN grant can access files beyond the file-tool root. Approve only when trusted.")
    answer = input("Type APPROVE WORKSTATION to proceed: ")
    if answer != "APPROVE WORKSTATION":
        print("No changes made")
        return 1
    path = _grant_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps({"schema": 1, "root": str(args.root.resolve()),
                                    "permissions": sorted(requested),
                                    "expires_at": time.time() + args.minutes * 60}).encode("utf-8"))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print("Approved. Existing UniSync MCP tunnel can now use workstation tools until expiry.")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
