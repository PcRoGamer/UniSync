# CODEX HANDOFF | UniSync workstation tunnel + COMP10001 past-paper repair
Date: 2026-11-06 (Melbourne)
Priority: High. This is an **execution handoff**, not a request for another plan.

## Mission
1. Deploy, validate, and activate the opt-in workstation tools on the **existing UniSync MCP tunnel** so remote ChatGPT/Codex can safely inspect/edit/run code without Desktop Commander.
2. Diagnose and repair Canvas COMP10001 course 241246 page/file retrieval; verify the genuine downloadable past exam PDFs on disk, or report their precise access restrictions.
3. Report tested results, not assumptions. Preserve existing academic data and working source.

## Machine / repository
- Windows on ARM64 laptop, repository: `C:\Users\Admin\UniSync-repo`.
- Python environment: `.venv\Scripts\python.exe`. Read repo `AGENTS.md` first.
- Existing UniSync FastMCP server: `backend/unisync/mcp/server.py`; existing tunnel must be reused. Do not stand up a new unauthenticated or competing remote shell.
- Existing 21 tools include academic search and `unisync_agent_workspace_{list,read,save}`. Current MCP has **no** arbitrary filesystem/terminal tool.
- Private workspace handoff: `handoffs/2026-11-06-workstation-mcp.md`; this handoff is `handoffs/2026-11-06-codex-workstation-and-comp.md`. Workspace usually lives at `data/workspace/agent_shared/`.
- Desktop Commander explicitly returned **monthly quota exhausted**, so do not keep retrying it.
- IMPORTANT: local repo has **numerous unrelated uncommitted modifications** (Canvas crawler, authentication, course work and other projects). Do NOT run `git restore`, `git checkout --`, `git reset --hard`, `git clean`, or blindly merge/overwrite existing source. Capture `git status` and protect changed files.

## Ready implementation for Phase 1
- Private GitHub draft PR: https://github.com/PcRoGamer/UniSync/pull/2
- Branch: `feat/approved-workstation-mcp`, based on `main`; **not merged or deployed**.
- Adds `backend/unisync/mcp/workstation.py`, `backend/tests/test_mcp_workstation.py`, `docs/UNISYNC_WORKSTATION_MCP.md`, and a small registration in `backend/unisync/mcp/server.py`.
- Tools: `unisync_host_status`, `unisync_host_list`, `unisync_host_read`, `unisync_host_write`, `unisync_host_replace`, `unisync_host_run`.
- Prior work reported 10 isolated tests passing and two clean simulated installations; **retest on this laptop**.
- To retrieve safely: `git fetch origin feat/approved-workstation-mcp`; inspect the PR diff and copy/apply *only additive* files + the registration lines to the current local source. Prefer the non-destructive installer if the ZIP `unisync-workstation-mcp.zip` is present in Downloads; do not assume it is. Never replace local `server.py` wholesale.
- Review security carefully. Host reads/writes must stay within an explicitly granted root, disallow secrets and symlink escapes; replacement uses SHA-256 precondition and backups; bounded subprocess execution requires a **separate** short-lived local `run` grant. The subprocess is high privilege (same Windows account), so ask for the user's on-machine confirmation if not already granted. No auto-granting, sharing credentials or silent irreversible actions.
- Grant command after install, from repo root:
  `$env:PYTHONPATH = "$PWD\backend"; & '.\.venv\Scripts\python.exe' -m unisync.mcp.workstation grant --root "$PWD" --minutes 20 --permissions read,write,run`
  Local CLI asks user to type `APPROVE WORKSTATION`. Read-only or read/write grants preferable until run actually needed.
- Run `python -m pytest backend/tests/test_mcp_workstation.py -q`, verify registration with actual MCP `tools/list`, restart *only* the existing MCP process/tunnel without disrupting the academic backend. Prove tools are discoverable and functional. Don't claim success solely because files were copied.
- Diagnose process mismatch: at one point UniSync MCP status said `waiting_for_tab`, 0 in-memory cookies/no resident browser, while local HTTP `127.0.0.1:8000/api/v1/sync/status` reported `authenticated`, resident Camoufox and `camoufox_ready`. Check whether two different Python processes/working directories or a stale MCP backend are involved. Don't delete duplicate installs until identifying authoritative paths and data.

## Phase 2: COMP10001 Canvas root-cause repair
- Canvas course ID `241246`, official course exam page:
  `https://canvas.lms.unimelb.edu.au/courses/241246/pages/mst-and-exam`
- Latest verified local crawl report: `data/courses/COMP10001_2026_SM2/pages/canvas-241246-crawl-report.json` was `status=incomplete`, `total_pages=0`, `file_catalog_count=0`, `error=HTTPStatusError`, `file_catalog_error=HTTPStatusError`, `attachment_candidates=1`.
- Canvas authentication in local web backend was `authenticated`; targeted `POST /api/v1/sync/courses/241246/refresh` returned started, then completed, but real page/file catalogue still failed. **Waiting another cycle won't fix this.**
- At last check: **no 2023 or 2024 past exam PDFs verified on disk** in COMP10001 archive or download registry. Do not represent them as downloaded.
- Relevant code: `backend/unisync/services/browser/canvas_transport.py` (resident browser/HTTPX API transport), `backend/unisync/services/sync/canvas_client.py` (API, breadth-first linked page discovery), `backend/unisync/api/sync_engine.py` (course orchestration, candidate extraction, manifest), `backend/unisync/services/sync/canvas_snapshot.py`, `backend/unisync/services/browser/camoufox_service.py`. Local code has uncommitted enhancements beyond older GitHub main.
- First collect exact failure facts: status codes, response headers (safe subset), content type, redirect path/host, whether a 403/401 is transient or per-endpoint. Log ONLY safe host/path/status, **never cookies, tokens, query secrets or raw response with credentials**.
- Compare authenticated Canvas page view and API endpoints for pages/file listing and individual pages/files; permission denied on catalog doesn't mean individual exam page is inaccessible. Recover from official homepage and linked exam page. Do not invent or brute-force resource IDs.
- Preserve breadth-first link discovery without artificial depth cutoff, cached-page delta, downloaded-file manifest checks and Respondus metadata-only catalogue. Fix failure pathways honestly; distinguish inaccessible endpoints from empty ones.
- End-to-end verify actual exam resources: inspect 2023/2024 PDFs by name and file signature, record accurate local paths, size and manifest. If genuine PDFs cannot be accessed, report precisely which link/status blocked it. 2025 Respondus content: retain official link/accessible metadata only, no attempts or bypasses.
- Run focused + appropriate regression tests, and a live targeted refresh. Check final crawl report and filesystem. Prevent false `up_to_date` when crawl is incomplete.

## Output / finish criteria
Return a concise report: exact auth/API root cause, code modifications, tests with counts, whether a fresh MCP `tools/list` exposes new tools, desktop/tunnel instance mapping, authenticated source URLs (without secrets), and each 2023/24 exam file's **verified actual path** or documented reason unavailable. Do not declare done until those checks succeed; otherwise clearly state blocker and partial progress.