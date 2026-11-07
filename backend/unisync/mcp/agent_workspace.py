"""Durable, private agent handoffs and drafts backed by UniSync's data directory.

This is an exchange, never a Canvas submission destination.
Only approved text formats and folders are accessible via MCP. Reads are scrubbed
before pagination. Writes are atomic and cannot overwrite silently: callers
must supply the content SHA-256 returned by list/read when editing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any

from unisync.core.path_service import get_path_service
from unisync.mcp.egress import scrub_text, redact_secrets

CATEGORIES = ("drafts", "handoffs", "notes", "reports", "artifacts")
EXTENSIONS = frozenset((".md", ".txt", ".json", ".csv"))
MAX_WRITE_CHARS = 150_000
MAX_BYTES = 1_500_000
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,90}$")
INIT_HANDOFF = """# UniSync shared agent workspace

This folder belongs to the user and is not a Canvas submission destination.

## For the next chat or agent

1. Call `unisync_agent_workspace_list` to discover recent drafts and handoffs.
2. Call `unisync_agent_workspace_read` and follow `next_offset` until `has_more=false`.
3. On edit, call `unisync_agent_workspace_save` with `expected_sha256`
   from the read result. It refuses conflicting overwrites.
4. After meaningful work, save a concise dated handoff in `handoffs/`
   with source links, pending decisions, tests run, next steps, and draft paths.
5. Keep user-owned drafts in `drafts/<course-code>/`; put research notes
   in `notes/`, written reports in `reports/`, and export instructions in
   `artifacts/`. Versions are retained automatically when overwriting.
6. All files are private local state ignored by Git. Don't store credentials,
   access tokens, cookies, or authentication exports here.
7. Saving a draft does NOT submit an assignment, publish it, send an email,
   or sync it into NotebookLM. Those actions need their own explicit workflow.

Original local files are retained as written; content returned via ChatGPT MCP
is filtered through the academic and credential scrubber.
"""


def workspace_root() -> Path:
    base = get_path_service().get_workspace_dir().resolve()
    root = (base / "agent_shared").resolve()
    if not root.is_relative_to(base):
        raise PermissionError("Workspace root escaped the UniSync data directory.")
    root.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        (root / category).mkdir(exist_ok=True)
    init_path = root / "handoffs" / "START_HERE.md"
    if not init_path.exists():
        # First-run only, never overwrite a user or agent edited handoff.
        try:
            with init_path.open("x", encoding="utf-8") as stream:
                stream.write(INIT_HANDOFF)
        except FileExistsError:
            pass
    return root


def _path(name: str) -> tuple[Path, Path]:
    if not isinstance(name, str) or len(name) > 235 or "\\" in name or "\x00" in name:
        raise ValueError("Use a relative POSIX-style workspace path.")
    p = PurePosixPath(name)
    parts = p.parts
    if (p.is_absolute() or len(parts) < 2 or parts[0] not in CATEGORIES
            or not all(SEGMENT.fullmatch(part) and part not in (".", "..") for part in parts)
            or Path(parts[-1]).suffix.lower() not in EXTENSIONS
            or any(part.startswith(".") for part in parts)):
        raise ValueError("Path must be inside drafts/, handoffs/, notes/, reports/, or artifacts/ and end in .md, .txt, .json, or .csv.")
    root = workspace_root()
    dest = root.joinpath(*parts)
    resolved = dest.resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError("A symlink or junction would escape the shared workspace.")
    if dest.is_symlink():
        raise PermissionError("Symlink files are not writable or readable via MCP.")
    return root, dest


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _meta(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": stat.st_size,
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": _sha(data),
    }


def list_files(category: str = "all", offset: int = 0, limit: int = 50) -> dict[str, Any]:
    if category != "all" and category not in CATEGORIES:
        raise ValueError("Unknown category.")
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("offset must be >=0 and limit must be 1..100.")
    root = workspace_root()
    containers = [root / category] if category != "all" else [root / item for item in CATEGORIES]
    files: list[Path] = []
    for folder in containers:
        for p in folder.rglob("*"):
            if not p.is_file() or p.is_symlink() or p.suffix.lower() not in EXTENSIONS:
                continue
            try:
                _path(p.relative_to(root).as_posix())
            except (ValueError, PermissionError):
                continue
            files.append(p)
    files.sort(key=lambda p: (p.stat().st_mtime_ns, p.as_posix()), reverse=True)
    out = []
    for p in files[offset:offset + limit]:
        try:
            out.append(_meta(p, root))
        except OSError:
            continue
    return {
        "category": category,
        "total": len(files),
        "offset": offset,
        "next_offset": offset + limit if offset + limit < len(files) else None,
        "entries": out,
        "workspace": "private UniSync data/workspace/agent_shared",
        "scrubber": "academic_boilerplate+credential_redaction",
    }


def read_file(path: str, offset: int = 0, max_chars: int = 8000) -> dict[str, Any]:
    if offset < 0 or not 128 <= max_chars <= 40000:
        raise ValueError("offset must be >=0, max_chars must be 128..40000.")
    root, file = _path(path)
    if not file.is_file():
        raise FileNotFoundError("Agent workspace document does not exist.")
    if file.stat().st_size > MAX_BYTES:
        raise ValueError("File exceeds the MCP text reader's limit.")
    raw = file.read_bytes()
    text = scrub_text(raw.decode("utf-8-sig"))
    if offset > len(text):
        raise ValueError("offset exceeds scrubbed text length.")
    end = min(len(text), offset + max_chars)
    return {
        **_meta(file, root),
        "content": text[offset:end],
        "offset": offset,
        "total_chars": len(text),
        "returned_chars": end - offset,
        "has_more": end < len(text),
        "next_offset": end if end < len(text) else None,
        "scrubber": "academic_boilerplate+credential_redaction",
        "local_original_unchanged": True,
    }


def save_file(path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
    if not isinstance(content, str) or len(content) > MAX_WRITE_CHARS:
        raise ValueError(f"content must be text with at most {MAX_WRITE_CHARS} characters.")
    if redact_secrets(content) != content:
        raise ValueError("Potential credential detected in content. Remove it before saving.")
    root, dest = _path(path)
    if dest.suffix.lower() == ".json":
        json.loads(content)
    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_BYTES:
        raise ValueError("Content is too large.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    root_check, target_check = _path(path)
    if target_check != dest or root_check != root:
        raise PermissionError("Workspace path changed during write.")
    # Atomic create and edit with optimistic concurrency. The lock serializes
    # writers across multiple MCP processes on Windows and other platforms.
    lock = root / ".agent-write.lock"
    with lock.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            previous = dest.read_bytes() if dest.exists() else None
            if previous is not None:
                old_sha = _sha(previous)
                if expected_sha256 != old_sha:
                    raise FileExistsError("Existing file changed or no expected_sha256 supplied. Read the latest file and retry with its sha256.")
                if previous == content_bytes:
                    return {"status": "unchanged", **_meta(dest, root)}
                history_root = root / ".history" / dest.relative_to(root)
                history_root.mkdir(parents=True, exist_ok=True)
                # The old revision is preserved before the atomic replacement.
                backup = history_root / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{old_sha[:12]}.bak"
                backup.write_bytes(previous)
            elif expected_sha256:
                raise FileNotFoundError("Expected a prior version, but this file does not exist.")
            fd, tmp = tempfile.mkstemp(prefix=".agent-write-", suffix=".tmp", dir=dest.parent)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content_bytes)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return {"status": "created" if previous is None else "updated", **_meta(dest, root)}
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
