# UniSync private agent workspace

## Purpose

A single durable workspace shared by local agents and ChatGPT through UniSync's MCP connection. Files survive chat boundaries, connector restarts and Windows reboots as long as the canonical data directory is preserved. **This is not a Canvas submission folder.**

Canonical local path:

`<UNISYNC_DATA_DIR>\workspace\agent_shared\`

The entire `data/` folder is Git-ignored. Do not push these files or secrets to GitHub.

## Folder protocol

| Folder | Use |
|---|---|
| `drafts/<course>/` | Working assignment drafts, outlines, exam-prep materials |
| `handoffs/` | Current state, decisions, next actions and paths for the next chat |
| `notes/` | Research and non-submission notes |
| `reports/` | Completed internal analyses |
| `artifacts/` | Text manifests for generated binary or external outputs |
| `.history/` | Automatically retained prior versions, not exposed via MCP |

Read `handoffs/START_HERE.md` at the beginning of a new project/chat.

## Tools

- `unisync_agent_workspace_list(category='all', limit=50, offset=0)`: discover files and their SHA-256 versions.
- `unisync_agent_workspace_read(path, offset=0, max_chars=8000)`: retrieve scrubbed text; continue using `next_offset`. Raw local files are unchanged.
- `unisync_agent_workspace_save(path, content, expected_sha256=None)`: create a new document or edit an existing one when given the SHA-256 of the version read previously. Rejects unversioned overwrites, credential patterns and directory escapes. Previous versions are backed up locally.

Only Markdown, plain text, JSON and CSV are writable over the MCP interface. For PDF, Word, zip or other binary artifacts, local agents may save files directly in the same directory but should include a `*.md` manifest so cross-chat discovery works. Those binary files are not served through this text-only MCP interface.

## Recommended handoff

Create a dated `handoffs/YYYY-MM-DD-project.md` with:

1. Goal, scope and current state.
2. Canonical draft paths and relevant original course materials.
3. Decisions, evidence, tests and source references.
4. Blockers and verified facts vs assumptions.
5. Next action an agent can take autonomously.
6. Nothing that should be submitted, sent or published without explicit approval.

**Submission distinction:** `data/courses/*/submissions/` stores captured Canvas submission attempts and feedback; `data/workspace/agent_shared/drafts/` stores user/agent working material. Drafts are never automatically marked submitted or sent anywhere.

## Confidentiality

All MCP tool outputs pass the existing academic/credential scrubber. The initial safe-text scrub occurs before pagination to avoid unintentional chunk truncation. Saving a file does not upload it externally. Do not store tokens, passwords, cookies or login exports. Agents with authorized local filesystem access can still see original files; configure their access appropriately.

## Acceptance tests

`python -m pytest backend/tests/test_agent_workspace.py backend/tests/test_mcp_security.py -q`

Use the canonical `.venv` to avoid mismatched MCP versions. After a tunnel restart, verify the new tools are listed, then use MCP `list`, `save`, and `read` through an actual stdio connection.
