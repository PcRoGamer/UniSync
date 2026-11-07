# UniSync Workstation MCP Bridge (v2.1.1)

This optional module uses UniSync's **existing MCP tunnel**, without a second
network listener, desktop approval daemon, heartbeat queue, or another MCP
installation. It preserves the six tool names:

`unisync_host_status`, `unisync_host_list`, `unisync_host_read`,
`unisync_host_write`, `unisync_host_replace`, and `unisync_host_run`.

The academic UniSync tools are unaffected. The bridge is disabled until
explicitly authorized on the Windows machine.

## Installation and activation

1. Integrate `backend/unisync/mcp/workstation.py` and its registration in
   `backend/unisync/mcp/server.py`. Preserve all local uncommitted work.
2. Restart **only the existing UniSync tunnel/MCP client** when installing new
   code, then confirm all 27 tools appear. Do not create another tunnel.
3. On the Windows desktop, explicitly run
   `scripts/Enable-UniSync-Workstation-YOLO.ps1`. It displays a local Yes/No
   dialog with **No selected by default**. Choosing No makes no changes.
   Choosing Yes writes the persistent local-only grant at
   `%USERPROFILE%\.unisync-workstation\bridge-yolo.json`.

Grant changes are read by the bridge on each call. You do **not** need to
restart the tunnel merely to change/revoke the grant.

The authorized root is the **entire drive containing the repository** (normally
`C:\`). The default relative-path workspace remains the repository. Absolute
paths elsewhere on that drive are allowed. File tools still block known secret
paths, but RUN can execute any program available to the Windows user.

### Verification

```powershell
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m unisync.mcp.workstation status
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_mcp_workstation.py backend/tests/test_yolo_host_run_stdio.py -q
```

`unisync_host_status` should show `bridge_version: "2.1.1"`,
`mode: "yolo"`, `persistent: true`, `expires_at: null`, and the
`read`, `write`, `run` permissions.

## Security and limits

- **High privilege:** Any client already connected to this MCP tunnel can run
  arbitrary commands under the logged-in Windows account, without additional
  prompts, until YOLO is revoked. A drive path is a scope declaration, **not a
  sandbox for command execution**.
- All configuration is local; never commit grant files or host credentials.
- `write` refuses overwrites. `replace` requires the current SHA-256,
  changes exactly one text span, and backs up the original. Mutations are
  serialized, and symlink/path escapes are refused by the file tools.
- `run` uses argv without shell parsing, a bounded timeout and output cap,
  and `stdin=subprocess.DEVNULL` so child commands never inherit the MCP
  transport's input stream.
- The bridge is not a desktop GUI controller. It does not offer screenshots,
  mouse/keyboard events, or a long-lived interactive shell.

### Revocation

In the repository's Windows PowerShell session:

```powershell
$env:PYTHONPATH = "$PWD\backend"
& '.\.venv\Scripts\python.exe' -m unisync.mcp.workstation revoke
```

This removes the YOLO grant and any legacy UniSync workstation grants. The
existing tunnel and academic tools remain connected. `unisync_host_status`
continues to work without authorization.

## Maintenance

The workstation bridge is deliberately independent of the academic sync
implementation and the Windows tunnel startup scripts. Do not reintroduce
the retired approval listener, separate startup shortcut, temporary grant
scripts, or a second tunnel. Back up unrelated dirty-tree work before merging.

For regression testing run `backend/tests/test_mcp_workstation.py` and
`backend/tests/test_yolo_host_run_stdio.py`, then verify live MCP registration
and Windows command execution through the existing tunnel.
