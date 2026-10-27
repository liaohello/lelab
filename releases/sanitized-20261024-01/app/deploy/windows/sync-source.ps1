<#
.SYNOPSIS
Creates an immutable source release on a Linux target over SSH.

.DESCRIPTION
Dry-run is the default. Use -Apply to package the current working files, excluding datasets,
calibration, virtual environments, Git metadata, and secret-bearing files. The remote release
is extracted under releases/<id> and current is switched atomically only after SHA-256 checks.
No password is accepted; configure SSH keys and verify the host fingerprint first.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,

    [string]$RemoteHost = $env:LELAB_JETSON_HOST,
    [string]$RemoteUser = "nvidia",
    [ValidateRange(1, 65535)]
    [int]$RemotePort = 22,
    [string]$IdentityFile = "",
    [string]$RemoteRoot = "Desktop/lelab",
    [string]$ReleaseId = "",
    [string]$ExcludeFile = (Join-Path (Split-Path $PSScriptRoot -Parent) "source-excludes.txt"),
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-SafeValue {
    param([string]$Name, [string]$Value, [string]$Pattern)
    if ($Value -notmatch $Pattern) {
        throw "Invalid $Name value: $Value"
    }
}

function Test-ExcludedPath {
    param([string]$RelativePath, [string[]]$Patterns)
    $normalized = $RelativePath.Replace("\", "/").TrimStart("/")
    if ($normalized.StartsWith("./")) { $normalized = $normalized.Substring(2) }
    $segments = $normalized.Split("/", [System.StringSplitOptions]::RemoveEmptyEntries)
    if ($segments -contains "__pycache__" -or $normalized.EndsWith(".pyc")) {
        return $true
    }
    foreach ($rawPattern in $Patterns) {
        $pattern = $rawPattern.Replace("\", "/").TrimStart("/")
        if ($pattern.StartsWith("./")) { $pattern = $pattern.Substring(2) }
        if ($pattern.StartsWith("**/")) {
            $pattern = $pattern.Substring(3)
            if ($normalized -like "*/$pattern" -or $normalized -like "*/$pattern/*") {
                return $true
            }
        }
        if ($normalized -like $pattern -or $normalized -like "$pattern/*") {
            return $true
        }
        # Runtime directories are root-relative; src/lerobot/datasets is source code.
        $rootOnly = @("data", "dataset", "datasets", "outputs", "calibration")
        if ($pattern -notin $rootOnly -and $pattern -notlike "*/*" -and
            ($segments | Where-Object { $_ -like $pattern })) {
            return $true
        }
    }
    return $false
}

Assert-SafeValue -Name "RemoteHost" -Value $RemoteHost -Pattern "^[A-Za-z0-9.-]+$"
Assert-SafeValue -Name "RemoteUser" -Value $RemoteUser -Pattern "^[A-Za-z0-9._-]+$"
Assert-SafeValue -Name "RemoteRoot" -Value $RemoteRoot -Pattern "^/?[A-Za-z0-9._/-]+$"

$resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path
if ($IdentityFile) {
    $IdentityFile = (Resolve-Path -LiteralPath $IdentityFile).Path
    if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
        throw "Identity file is not a regular file."
    }
}
if (-not (Test-Path -LiteralPath $ExcludeFile -PathType Leaf)) {
    throw "Exclude file not found: $ExcludeFile"
}
$patterns = @(
    Get-Content -LiteralPath $ExcludeFile |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith("#") }
)

if (-not $ReleaseId) {
    $projectName = (Split-Path -Leaf $resolvedSource) -replace "[^A-Za-z0-9._-]", "-"
    $ReleaseId = "{0}-{1}" -f $projectName, (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
}
Assert-SafeValue -Name "ReleaseId" -Value $ReleaseId -Pattern "^[A-Za-z0-9._-]+$"

$files = @(
    Get-ChildItem -LiteralPath $resolvedSource -Recurse -File -Force |
        Where-Object {
            $relative = [System.IO.Path]::GetRelativePath($resolvedSource, $_.FullName)
            -not (Test-ExcludedPath -RelativePath $relative -Patterns $patterns)
        }
)
$totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
if ($null -eq $totalBytes) { $totalBytes = 0 }

Write-Host "Source release plan"
Write-Host "  mode:        $(if ($Apply) { 'APPLY' } else { 'DRY-RUN' })"
Write-Host "  source:      $resolvedSource"
Write-Host "  files:       $($files.Count)"
Write-Host "  bytes:       $totalBytes"
Write-Host "  destination: $RemoteUser@$RemoteHost`:$RemotePort $RemoteRoot/releases/$ReleaseId"
Write-Host "  excluded:    datasets, calibration, outputs, environments, Git metadata, credentials"

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry-run only. First included paths:"
    $files | Select-Object -First 20 | ForEach-Object {
        Write-Host "  $([System.IO.Path]::GetRelativePath($resolvedSource, $_.FullName))"
    }
    Write-Host "Re-run with -Apply after reviewing the target and exclusions."
    return
}

foreach ($command in @("python", "ssh.exe", "scp.exe")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $command"
    }
}

$archiveName = "$ReleaseId.tar.gz"
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) ("lelab-source-{0}.tar.gz" -f [guid]::NewGuid())
$fileListPath = "$archivePath.files"
$sshTarget = "$RemoteUser@$RemoteHost"
$sshArgs = @(
    "-T", "-p", "$RemotePort",
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes"
)
if ($IdentityFile) { $sshArgs += @("-i", $IdentityFile, "-o", "IdentitiesOnly=yes") }

try {
    # Package exactly the reviewed file list; do not traverse the source a second time.
    $relativePaths = @($files | ForEach-Object {
        [System.IO.Path]::GetRelativePath($resolvedSource, $_.FullName).Replace("\", "/")
    })
    $fileListBytes = [System.Text.Encoding]::UTF8.GetBytes(($relativePaths -join "`0") + "`0")
    [System.IO.File]::WriteAllBytes($fileListPath, $fileListBytes)
    # Windows bsdtar decodes -T using the system code page, corrupting Unicode names.
    $archiveScript = @'
from pathlib import Path
import sys, tarfile
root, archive, listing = map(Path, sys.argv[1:])
paths = listing.read_bytes().decode("utf-8").split("\0")
with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as output:
    for name in paths:
        if name:
            output.add(root / name, arcname=name, recursive=False)
'@
    & python -c $archiveScript $resolvedSource $archivePath $fileListPath
    if ($LASTEXITCODE -ne 0) { throw "source packaging failed with exit code $LASTEXITCODE" }

    $archiveSha = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $prepareScript = @'
set -euo pipefail
root=$1
release=$2
archive=$3
case "$root" in
  /*) ;;
  *) root="$HOME/$root" ;;
esac
[[ ! -e "$root/releases/$release" ]] || { echo "release already exists" >&2; exit 73; }
mkdir -p "$root/.incoming" "$root/releases"
printf "%s\n" "$root/.incoming/$archive"
'@
    $prepareScript | & ssh.exe @sshArgs $sshTarget bash -s -- $RemoteRoot $ReleaseId $archiveName
    if ($LASTEXITCODE -ne 0) { throw "remote prepare failed with exit code $LASTEXITCODE" }

    $remoteArchive = "$sshTarget`:$RemoteRoot/.incoming/$archiveName"
    $scpArgs = @("-P", "$RemotePort", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes")
    if ($IdentityFile) { $scpArgs += @("-i", $IdentityFile, "-o", "IdentitiesOnly=yes") }
    & scp.exe @scpArgs -- $archivePath $remoteArchive
    if ($LASTEXITCODE -ne 0) { throw "scp failed with exit code $LASTEXITCODE" }

    $finalizeScript = @'
set -euo pipefail
root=$1
release=$2
archive=$3
expected_sha=$4
case "$root" in
  /*) ;;
  *) root="$HOME/$root" ;;
esac
incoming="$root/.incoming/$archive"
target="$root/releases/$release"
read -r actual_sha _ < <(sha256sum "$incoming")
[[ "$actual_sha" == "$expected_sha" ]] || { echo "source archive checksum mismatch" >&2; exit 65; }
[[ ! -e "$target" ]] || { echo "release already exists" >&2; exit 73; }
mkdir -p "$target"
tar -xzf "$incoming" -C "$target"
if [[ -f "$target/SOURCE_MANIFEST.sha256" ]]; then
  (cd "$target" && sha256sum --check --quiet SOURCE_MANIFEST.sha256)
fi
printf "%s  %s\n" "$actual_sha" "$archive" >"$target/DEPLOYED_SOURCE_SHA256"
link="$root/.current-$release"
ln -s "releases/$release" "$link"
mv -Tf "$link" "$root/current"
rm -- "$incoming"
printf "CURRENT %s\n" "$target"
'@
    $finalizeScript | & ssh.exe @sshArgs $sshTarget bash -s -- $RemoteRoot $ReleaseId $archiveName $archiveSha
    if ($LASTEXITCODE -ne 0) { throw "remote finalize failed with exit code $LASTEXITCODE" }

    Write-Host "Source release verified and activated: $RemoteRoot/releases/$ReleaseId"
}
finally {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
    if (Test-Path -LiteralPath $fileListPath) {
        Remove-Item -LiteralPath $fileListPath -Force
    }
}
