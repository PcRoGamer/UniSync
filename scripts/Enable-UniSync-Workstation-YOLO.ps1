# One-time local YOLO opt-in for the existing UniSync MCP workstation tools.
# No network listener, startup task, or remote approval is created here.
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::UserInteractive) {
    throw 'Run this locally in your interactive Windows session.'
}
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$bridge = Join-Path $repo 'backend\unisync\mcp\workstation.py'
if (!(Test-Path -LiteralPath $bridge)) {
    throw 'The UniSync workstation bridge is not installed.'
}
$drive = [IO.Path]::GetPathRoot($repo)
$warning = @"
Allow persistent UniSync workstation YOLO access?

All clients connected through this UniSync MCP tunnel can:
- Read and edit accessible files on $drive
- Execute arbitrary programs with YOUR Windows account permissions
- Continue without asking again until you revoke access

The file tools exclude credential paths, but command execution can access
anything your Windows account can access. Only approve trusted clients.

Repository workspace: $repo
Permission duration: UNTIL REVOKED

APPROVE permanent YOLO access?
"@
Add-Type -AssemblyName System.Windows.Forms
$answer = [System.Windows.Forms.MessageBox]::Show(
    $warning, 'UniSync workstation YOLO authorization',
    [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Warning,
    [System.Windows.Forms.MessageBoxDefaultButton]::Button2
)
if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) {
    Write-Output 'Denied. Existing permissions were not changed.'
    exit 1
}
$folder = Join-Path $env:USERPROFILE '.unisync-workstation'
if (!(Test-Path -LiteralPath $folder)) {
    [void](New-Item -ItemType Directory -Path $folder -Force)
}
$target = Join-Path $folder 'bridge-yolo.json'
$payload = [ordered]@{
    schema = 2
    mode = 'yolo'
    root = $drive
    workspace = $repo
    permissions = @('read', 'write', 'run')
    acknowledgement = 'LOCAL_WINDOWS_CONFIRMATION'
}
$json = $payload | ConvertTo-Json -Compress
$temp = Join-Path $folder ('.bridge-yolo-' + [guid]::NewGuid().ToString('N') + '.tmp')
$backup = Join-Path $folder ('bridge-yolo.previous-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.json')
try {
    [IO.File]::WriteAllText($temp, $json, [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $target) {
        [IO.File]::Replace($temp, $target, $backup, $true)
    } else {
        [IO.File]::Move($temp, $target)
    }
} finally {
    if (Test-Path -LiteralPath $temp) {
        Remove-Item -LiteralPath $temp -Force
    }
}
Write-Output 'UniSync YOLO enabled locally. Revoke with python -m unisync.mcp.workstation revoke.'
