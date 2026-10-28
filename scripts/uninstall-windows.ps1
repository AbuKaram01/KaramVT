# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uninstall-windows.ps1 - removes what scripts\install-windows.ps1 created.

<#
.SYNOPSIS
    Removes what scripts\install-windows.ps1 created.
.DESCRIPTION
    Your downloaded models, OCR language data, translation history and settings are kept unless you ask for them to be
    deleted.
.PARAMETER RemoveGtk
    Also delete the GTK runtime the installer downloaded (%LOCALAPPDATA%\karamvt\gtk).
.PARAMETER RemoveData
    Also delete downloaded models, OCR language data, translation history and settings. Cannot be undone.
.PARAMETER Force
    Do not ask for confirmation before -RemoveData deletes anything.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1 -RemoveGtk -RemoveData
#>
[CmdletBinding()]
param(
    [switch]$RemoveGtk,
    [switch]$RemoveData,
    [switch]$Force
)

Set-StrictMode -Version 2.0

# This script is for Windows only; say so clearly instead of failing on a missing %LOCALAPPDATA%.
if ($env:OS -ne 'Windows_NT') {
    Write-Host ('ERR This script is for Windows. On Linux use scripts/install-debian.sh or ' +
                'scripts/install-arch.sh.') -ForegroundColor Red
    exit 1
}
$ErrorActionPreference = 'Stop'

$AppId       = 'io.github.AbuKaram01.KaramVT'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DataRoot    = Join-Path $env:LOCALAPPDATA 'karamvt'
$ConfigRoot  = Join-Path $env:APPDATA 'karamvt'

function Info($Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Remove-IfPresent([string]$Path) {
    if (Test-Path $Path) { Remove-Item $Path -Recurse -Force; Info "Removed $Path" }
}

if ($RemoveData -and -not $Force) {
    $answer = Read-Host ('This deletes your downloaded models, OCR language data, translation history and ' +
                         'settings. Type YES to continue')
    if ($answer -ne 'YES') { Write-Host 'Nothing was deleted.'; exit 1 }
}

Remove-IfPresent (Join-Path ([Environment]::GetFolderPath('Programs')) 'KaramVT.lnk')
Remove-IfPresent (Join-Path $ProjectRoot '.venv')
# The portable Tesseract the installer fetched; it is not your data.
Remove-IfPresent (Join-Path $DataRoot 'tesseract')
# The GTK runtime the installer carried (the note beside it says it was prepared by the build, not by you).
$carriedGtk = Join-Path $ProjectRoot 'gtk'
if (Test-Path (Join-Path $carriedGtk 'gvsbuild-release.txt')) { Remove-IfPresent $carriedGtk }
Remove-IfPresent "HKCU:\Software\Classes\AppUserModelId\$AppId"
[Environment]::SetEnvironmentVariable('KARAMVT_GTK_DIR', $null, 'User')

if ($RemoveGtk) { Remove-IfPresent (Join-Path $DataRoot 'gtk') }
if ($RemoveData) {
    Remove-IfPresent $ConfigRoot
    Get-ChildItem $DataRoot -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne 'gtk' -or $RemoveGtk } |
        ForEach-Object { Remove-IfPresent $_.FullName }
    if ((Test-Path $DataRoot) -and -not (Get-ChildItem $DataRoot -Force)) { Remove-Item $DataRoot -Force }
}

Write-Host 'Done.'
if (-not $RemoveData) { Write-Host "Your data is still in $DataRoot and $ConfigRoot (use -RemoveData to delete it)." }
