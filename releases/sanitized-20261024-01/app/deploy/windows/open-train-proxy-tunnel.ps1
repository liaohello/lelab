<#
.SYNOPSIS
Reverses the local Mihomo mixed port to the train server's loopback.

.DESCRIPTION
The train server (192.0.2.10) cannot reach huggingface.co directly. Windows
runs Mihomo/Clash on its mixed port (7890 by default). This script opens

    Windows:7890  --ssh -R-->  train server 127.0.0.1:17890

so a remote lerobot run can use `LELAB_TRAIN_PROXY=http://127.0.0.1:17890`
for pip/HF traffic without ever exposing the proxy on a public interface
(`ssh -R` binds loopback unless GatewayPorts is enabled server-side).

Dry-run is the default; pass -Apply to actually hold the tunnel open
(Ctrl+C closes it). Keep it running in a spare terminal while the train
server downloads models or packages from the Hub.

.EXAMPLE
.\open-train-proxy-tunnel.ps1 -Apply
.\open-train-proxy-tunnel.ps1 -Apply -MihomoPort 7890 -RemotePort 17890
#>
[CmdletBinding()]
param(
    [string]$TrainHost = $env:LELAB_TRAIN_HOST,
    [ValidateRange(1, 65535)]
    [int]$TrainPort = $(if ($env:LELAB_TRAIN_PORT) { [int]$env:LELAB_TRAIN_PORT } else { 22 }),
    [string]$TrainUser = $env:LELAB_TRAIN_USER,
    [string]$SshKey = "$HOME\.ssh\id_ed25519_lelab_train",
    [ValidateRange(1, 65535)]
    [int]$MihomoPort = 7890,
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 17890,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($TrainHost -notmatch "^[A-Za-z0-9.-]+$") { throw "Invalid -TrainHost: $TrainHost" }
if ($TrainUser -notmatch "^[A-Za-z0-9._-]+$") { throw "Invalid -TrainUser: $TrainUser" }

Write-Host "Reverse Mihomo tunnel plan"
Write-Host "  local proxy:  127.0.0.1:$MihomoPort (Mihomo mixed port)"
Write-Host "  remote bind:  $TrainUser@$TrainHost`:$TrainPort -> 127.0.0.1:$RemotePort"
Write-Host "  ssh key:      $SshKey"

$keyArgs = @()
if (Test-Path -LiteralPath $SshKey) {
    $keyArgs = @("-i", $SshKey)
}
else {
    Write-Warning "SSH key not found at $SshKey; falling back to the default key/agent."
}

$sshArgs = @(
    "-N", "-T",
    "-p", "$TrainPort",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-R", "127.0.0.1:${RemotePort}:127.0.0.1:${MihomoPort}"
) + $keyArgs + @("$TrainUser@$TrainHost")

Write-Host ""
Write-Host ("ssh " + ($sshArgs -join " "))

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry-run only. Re-run with -Apply to hold the tunnel open."
    return
}

Write-Host ""
Write-Host "Tunnel open. On the train side use: LELAB_TRAIN_PROXY=http://127.0.0.1:$RemotePort"
Write-Host "Ctrl+C to close."
& ssh.exe @sshArgs
