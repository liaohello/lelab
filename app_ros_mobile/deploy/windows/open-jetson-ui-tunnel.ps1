<# Opens a loopback-only Windows tunnel to the LeLab UI running on Jetson. #>
[CmdletBinding()]
param(
    [string]$JetsonHost = $env:LELAB_JETSON_HOST,
    [string]$JetsonUser = $env:LELAB_JETSON_USER,
    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,
    [ValidateRange(1, 65535)]
    [int]$LocalPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 8000,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($JetsonHost -notmatch "^[A-Za-z0-9.-]+$") { throw "Invalid JetsonHost" }
if ($JetsonUser -notmatch "^[A-Za-z0-9._-]+$") { throw "Invalid JetsonUser" }

Write-Host "Tunnel plan: http://127.0.0.1:$LocalPort -> $JetsonUser@$JetsonHost`:$SshPort -> 127.0.0.1:$RemotePort"
if (-not $Apply) {
    Write-Host "Dry-run only. Re-run with -Apply; Ctrl+C closes the tunnel."
    return
}

if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
    throw "Windows OpenSSH client (ssh.exe) is required"
}

& ssh.exe -N -T `
    -p $SshPort `
    -o BatchMode=yes `
    -o StrictHostKeyChecking=yes `
    -o ExitOnForwardFailure=yes `
    -o ServerAliveInterval=30 `
    -o ServerAliveCountMax=3 `
    -L "127.0.0.1`:$LocalPort`:127.0.0.1`:$RemotePort" `
    "$JetsonUser@$JetsonHost"
if ($LASTEXITCODE -ne 0) {
    throw "SSH tunnel exited with code $LASTEXITCODE"
}
