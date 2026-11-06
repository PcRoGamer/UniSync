# UniSync Workstation MCP (Desktop Commander alternative)

This optional extension reuses UniSync's **existing MCP tunnel**. It introduces
**no new network server**, authentication system, subscription, or LLM calls.

It adds six tools: `unisync_host_status`, `unisync_host_list`, `unisync_host_read`,
`unisync_host_write`, `unisync_host_replace`, and `unisync_host_run`.

## Install / deploy

1. Merge the feature branch. On the authorized Windows PC, pull or copy the three
   additions, preserving existing uncommitted UniSync work. **Do not reset the repo.**
2. Restart the existing UniSync MCP process/tunnel once so tool discovery exposes
   the new workstation tools. Academic tools remain unchanged.
3. Locally approve a short-lived grant:

```powershell
Set-Location "$env:USERPROFILE\UniSync-repo"
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m unisync.mcp.workstation grant --root "$PWD" --minutes 20 --permissions read,write,run
```

The CLI requires typing `APPROVE WORKSTATION` locally. For read-only tasks, use
`--permissions read`. For edits without executing commands, use `read,write`.
To revoke immediately:

```powershell
& '.\.venv\Scripts\python.exe' -m unisync.mcp.workstation revoke
```

`status` is always available in MCP and shows when permission expires. Permission
is **not automatically renewed**; an expiring grant survives an MCP process restart
only until its original expiration.

## Security

- Default: no local grant, **no host operations possible**.
- File operations are confined to a local approval root. Secret directories and
  `.env` files are deliberately excluded, including through symlinks.
- `replace` checks the prior SHA-256 before editing and writes a backup before
  changing the file. `write` never overwrites files.
- `run` has a separate opt-in permission, uses argv (no shell expansion), a
  60-second timeout, and bounded stdout/stderr. **It still executes with the
  privileges of the UniSync OS account and can access files outside the root**.
  Only grant `run` when you trust every agent connected to this MCP tunnel.
- No tokens, browser cookies, or private content are written to a separate
  remote service. Tool outputs may include content you choose to read or print.
- This is not full Desktop Commander parity: there are no screenshots, GUI
  mouse/keyboard operations, persistent interactive processes, or device
  discovery. Those can be developed as separate, equally permissioned modules.

## Testing

```powershell
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_mcp_workstation.py -q
```

For the Canvas past-paper bug, start with a `read,run` grant, inspect the live
Canvas HTTP status code, and only request `write` after reviewing the patch.
