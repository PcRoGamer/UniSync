"""Safely overlay the email retrieval MCP tools onto a newer local UniSync checkout.

The GitHub branch may be older than the running UniSync code. NEVER overwrite
the target server wholesale. Copy only the new index module and add the new
standalone tool definitions, preserving every existing MCP tool.

Usage:
  python scripts/install_email_mcp_overlay.py --target C:\Users\Admin\UniSync-repo
  python scripts/install_email_mcp_overlay.py --target C:\Users\Admin\UniSync-repo --apply

No network calls, mailbox access or LLM requests. Restart the MCP server
separately after successful installation.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


TOOL_NAMES = (
    "unisync_search_emails",
    "unisync_get_email",
    "unisync_read_email_attachment",
    "unisync_get_email_sync_status",
)


def tool_names(code: str) -> set[str]:
    module = ast.parse(code)
    return {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def prepare(source: Path, target: Path) -> dict[str, object]:
    source_main = source / "backend/unisync/mcp/server.py"
    source_archive = source / "backend/unisync/services/email_monitor/archive_search.py"
    target_main = target / "backend/unisync/mcp/server.py"
    target_archive = target / "backend/unisync/services/email_monitor/archive_search.py"
    for required in (source_main, source_archive, target_main):
        if not required.is_file():
            raise FileNotFoundError(f"Missing required file: {required}")
    upstream = source_main.read_text(encoding="utf-8")
    original = target_main.read_text(encoding="utf-8")
    source_start = upstream.find("def _unisync_email_archive():")
    source_end = upstream.find("@mcp.tool()\ndef unisync_get_overview(", source_start)
    if source_start < 0 or source_end <= source_start:
        raise ValueError("Incompatible source branch: expected email tool definitions absent")
    block = upstream[source_start:source_end]
    new_tool_names = set(TOOL_NAMES)
    parsed_block = tool_names(block)
    if not new_tool_names.issubset(parsed_block):
        raise ValueError(f"Missing tool definitions in source: {sorted(new_tool_names - parsed_block)}")
    present = new_tool_names & tool_names(original)
    if present and present != new_tool_names:
        raise ValueError(f"Partial email MCP installation, refusing duplicate definitions: {sorted(present)}")
    installed = len(present) == len(new_tool_names)
    if installed:
        merged = original
    else:
        if "import json" not in original or "from pathlib import Path" not in original:
            raise ValueError("Target server lacks basic imports; review manually")
        anchor = original.find("@mcp.tool()\ndef unisync_get_overview(")
        if anchor < 0:
            # Keep generic compatibility for later UniSync versions by adding
            # tools before the first existing MCP declaration.
            anchor = original.find("@mcp.tool()")
        if anchor < 0:
            raise ValueError("No FastMCP tool definitions in target")
        merged = original[:anchor] + block + "\n" + original[anchor:]
    ast.parse(merged)
    original_names = tool_names(original)
    merged_names = tool_names(merged)
    if not original_names.issubset(merged_names):
        raise ValueError("Overlay would remove an existing function; refusing")
    if not new_tool_names.issubset(merged_names):
        raise ValueError("Overlay did not preserve all new email tools")

    archive_bytes = source_archive.read_bytes()
    compile(archive_bytes, str(target_archive), "exec")
    return {
        "server": str(target_main), "archive": str(target_archive),
        "original_server": original, "merged_server": merged,
        "archive_bytes": archive_bytes,
        "already_installed": installed and target_archive.is_file() and
        target_archive.read_bytes() == archive_bytes,
        "prior_function_count": len(original_names),
        "result_function_count": len(merged_names),
        "new_tools": sorted(new_tool_names),
    }


def install(prepared: dict[str, object]) -> dict[str, object]:
    server = Path(str(prepared["server"]))
    archive = Path(str(prepared["archive"]))
    old_server = server.read_bytes()
    old_archive = archive.read_bytes() if archive.is_file() else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups = server.parent / ("server.py.email-mcp-backup-" + stamp)
    backups.write_bytes(old_server)
    backup_archive = None
    if old_archive is not None:
        backup_archive = archive.parent / ("archive_search.py.email-mcp-backup-" + stamp)
        backup_archive.write_bytes(old_archive)
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(bytes(prepared["archive_bytes"]))
        server.write_text(str(prepared["merged_server"]), encoding="utf-8")
        compile(server.read_bytes(), str(server), "exec")
        compile(archive.read_bytes(), str(archive), "exec")
        if not set(TOOL_NAMES).issubset(tool_names(server.read_text(encoding="utf-8"))):
            raise RuntimeError("Post-write tool verification failed")
    except BaseException:
        server.write_bytes(old_server)
        if old_archive is not None:
            archive.write_bytes(old_archive)
        elif archive.exists():
            archive.unlink()
        raise
    return {
        "installed": True,
        "server_backup": str(backups),
        "archive_backup": str(backup_archive) if backup_archive else None,
        "new_tools": prepared["new_tools"],
        "requires_mcp_restart": True,
        "warning": "Verify live search/get/attachment and source completeness after restart.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="Apply after dry-run; creates backups")
    args = parser.parse_args()
    info = prepare(args.source.resolve(), args.target.resolve())
    summary = {k:v for k,v in info.items() if k not in
               ("original_server","merged_server","archive_bytes")}
    if args.apply:
        summary.update(install(info))
    else:
        summary["dry_run"] = True
        summary["message"] = "Target files unchanged. Add --apply after reviewing this preview."
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
