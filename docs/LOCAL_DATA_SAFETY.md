# UniSync local data safety

UniSync stores authenticated coursework, grades, uploaded responses, browser
sessions, and offline caches locally. These are **user data**, not source code.

## Git repository hygiene

`.gitignore` excludes the canonical `data/` root, migration directories,
browser-auth profiles, credential files, common database formats and backup
artifacts. A private GitHub repository is **not** a substitute for keeping
session secrets and downloaded academic records out of version control.

This protects new untracked artifacts. It does not remove information that was
already tracked or committed; perform a separate history review for that.

## Preserving original academic documents

The API supports an explicit environment switch:

```text
UNISYNC_SKIP_DISK_SCRUB=1
```

When enabled, the API does not schedule its automatic *in-place on-disk*
academic-boilerplate scrubber during startup. The academic MCP egress scrubber
continues filtering responses on the way to connected clients.

The default remains unchanged when the flag is absent. The setting is useful
when a researcher needs exact original academic downloads on disk for provenance
or repeatable source extraction while maintaining sanitized MCP responses.

Do not disable MCP credential redaction or expose the backend to unauthenticated
clients. This flag affects only the background file-rewriting operation, not
access-control permissions or authorization.

## Verification

- `python -m py_compile backend/unisync/api/main.py`
- `git check-ignore -v --no-index data.migration-staging/example.json`
- Check that synthetic credentials, browser profiles, and SQLite caches are
  ignored before creating any new source branch.
