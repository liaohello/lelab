<#
.SYNOPSIS
  Make the local LeRobot checkout available at the canonical sibling path.

.DESCRIPTION
  LeLab depends on LeRobot as a local, editable source at `..\lerobot`
  (see the `[tool.uv.sources]` block in pyproject.toml), so that Jetson,
  Windows and the training server all execute the same 0.6.2 tree instead of
  a GitHub or PyPI release.

  In the development layout the checkout is called `lerobot-main`, so this
  script creates the directory junction `..\lerobot -> ..\lerobot-main`.
  Junctions do not require administrator rights and `-Remove` deletes only the
  junction, never the target tree.

  On the deployed stack the path already exists (`~/Desktop/lelab/lerobot`),
  so this script is only needed on development workstations.

.EXAMPLE
  .\deploy\windows\link-lerobot.ps1
  .\deploy\windows\link-lerobot.ps1 -LerobotRoot ..\lerobot-main
  .\deploy\windows\link-lerobot.ps1 -Remove
#>
[CmdletBinding()]
param(
  [string]$StackRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
  [string]$LerobotRoot = "",
  [switch]$Remove
)

$ErrorActionPreference = "Stop"

$linkPath = Join-Path $StackRoot "lerobot"

if ($Remove) {
  if (Test-Path -LiteralPath $linkPath) {
    $item = Get-Item -LiteralPath $linkPath -Force
    if ($item.LinkType -in @("Junction", "SymbolicLink")) {
      [System.IO.Directory]::Delete($linkPath, $false)
      Write-Host "Removed junction $linkPath"
    } else {
      throw "$linkPath is a real directory, refusing to delete it."
    }
  } else {
    Write-Host "Nothing to remove at $linkPath"
  }
  return
}

if (Test-Path -LiteralPath $linkPath) {
  $item = Get-Item -LiteralPath $linkPath -Force
  Write-Host "$linkPath already exists ($($item.LinkType)) - nothing to do."
  return
}

if (-not $LerobotRoot) {
  $candidate = Join-Path $StackRoot "lerobot-main"
  if (Test-Path -LiteralPath $candidate) {
    $LerobotRoot = $candidate
  } else {
    throw "No LeRobot checkout found. Pass -LerobotRoot <path to a LeRobot 0.6.2 checkout>."
  }
}

$resolved = (Resolve-Path -LiteralPath $LerobotRoot).Path
$version = (Select-String -Path (Join-Path $resolved "pyproject.toml") -Pattern '^version\s*=' |
  Select-Object -First 1).Line
if ($version -notmatch '0\.6\.2') {
  Write-Warning "LeRobot at $resolved reports '$version' - LeLab expects 0.6.2."
}

New-Item -ItemType Junction -Path $linkPath -Target $resolved | Out-Null
Write-Host "Linked $linkPath -> $resolved"
Write-Host "Verify with: (Get-Item $linkPath).Target"
