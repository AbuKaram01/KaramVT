# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# build-windows.ps1 - builds the Windows installer (packaging\dist\karamvt-<version>-windows-setup.exe).
# It needs Inno Setup 6 (winget install JRSoftware.InnoSetup) and can be run from anywhere.

<#
.SYNOPSIS
    Builds the Windows installer, with everything the application needs inside it.
.DESCRIPTION
    Prepares, in this order, the folders that the installer script picks up when they exist (each is made afresh every
    time, so that nothing old is shipped by accident), then compiles the installer:
      * packaging\tesseract   a portable Tesseract OCR
      * packaging\gtk         the GTK 4 + libadwaita runtime
      * packaging\python and packaging\wheelhouse
                              Python's installer and the Python libraries, for the Python the GTK runtime's wheels
                              are for (so they need the GTK runtime to be inside the installer)
      * packaging\vcruntime   the Visual C++ runtime DLLs
    A part that cannot be prepared is left out with a warning, and the installer fetches it when the application is
    installed. -RequirePython and -RequireVcRuntime turn that warning into the end of the build.
.PARAMETER NoTesseract
    Build the installer without a copy of Tesseract inside (it is then fetched, or installed with winget, when the
    application is installed).
.PARAMETER NoGtk
    Build the installer without the GTK runtime inside (it is then downloaded when the application is installed).
.PARAMETER GtkTag
    The gvsbuild release whose GTK 4 runtime goes inside: a tag such as 2026.8.0, or "latest". By default the release
    this project is tested with (scripts\gtk-portable.ps1), so that building again gives the same runtime and the
    Python of its wheels is one the other libraries support.
.PARAMETER GtkSha256
    The SHA256 of that release's GTK4_Gvsbuild_<tag>_x64.zip; the build stops if the download differs. Without it the
    digest that GitHub publishes for the file is used when there is one. The build prints the hash it saw.
.PARAMETER GtkZip
    A GTK4_Gvsbuild_*_x64.zip that is already on this machine, so that nothing is downloaded.
.PARAMETER NoPython
    Build the installer without Python's installer and without the Python libraries inside (Python is then installed
    with winget when it is missing, and the libraries are downloaded when the application is installed).
.PARAMETER RequirePython
    Stop the build, saying why, when Python's installer or its libraries could not be prepared, instead of building
    an installer without them (CI uses it: an installer that quietly lacks them would only fail much later).
.PARAMETER PythonExe
    The python.exe that builds the wheelhouse. It has to be the Python version that the GTK runtime's wheels are for
    (3.14 with the default runtime); by default one is looked for on PATH and with the py launcher.
.PARAMETER PythonInstaller
    A python-<version>-amd64.exe that is already on this machine, so that nothing is downloaded. It has to be the
    installer of the Python that -PythonExe is.
.PARAMETER PythonInstallerSha256
    The SHA256 that the Python installer must have. Without it only its signature is checked.
.PARAMETER NoVcRuntime
    Build the installer without the Visual C++ runtime DLLs (msvcp140.dll ...) inside. Without them ctranslate2,
    onnxruntime and llama.cpp need the Visual C++ Redistributable on the machine where the application is installed.
.PARAMETER RequireVcRuntime
    Stop the build, saying why, when those DLLs could not be collected, instead of building an installer without them
    (CI uses it).
.PARAMETER VcRuntimeDir
    A folder that holds msvcp140.dll, msvcp140_1.dll and vcomp140.dll (and any other msvcp140*.dll), used instead of
    the Redist folder of the newest Visual Studio on this machine.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoTesseract,
    [switch]$NoGtk,
    [string]$GtkTag = '',
    [string]$GtkSha256 = '',
    [string]$GtkZip = '',
    [switch]$NoPython,
    [switch]$RequirePython,
    [string]$PythonExe = '',
    [string]$PythonInstaller = '',
    [string]$PythonInstallerSha256 = '',
    [switch]$NoVcRuntime,
    [switch]$RequireVcRuntime,
    [string]$VcRuntimeDir = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot

# The helper scripts are dot-sourced, so that their functions are available below:
#   tesseract-portable.ps1   Get-PortableTesseract
#   gtk-portable.ps1         Get-PortableGtk
#   python-bundle.ps1        Get-PythonInstaller, Build-Wheelhouse
#   vcruntime-bundle.ps1     Get-VcRuntime
. (Join-Path $ProjectRoot 'scripts\tesseract-portable.ps1')
. (Join-Path $ProjectRoot 'scripts\gtk-portable.ps1')
. (Join-Path $ProjectRoot 'scripts\python-bundle.ps1')
. (Join-Path $ProjectRoot 'scripts\vcruntime-bundle.ps1')

$pyproject = Get-Content (Join-Path $ProjectRoot 'pyproject.toml') -Raw -Encoding UTF8
if ($pyproject -notmatch '(?m)^version\s*=\s*"([^"]+)"') { throw 'Could not read the version from pyproject.toml.' }
$Version = $Matches[1]

# --- The parts that go inside the installer ----------------------------------------------------------------------

# The portable Tesseract goes into packaging\tesseract, which the installer script picks up when it exists.
function Add-TesseractBundle {
    $bundle = Join-Path $PSScriptRoot 'tesseract'
    if (Get-PortableTesseract $bundle) {
        Write-Host "Tesseract will be inside the installer ($bundle)."
    } else {
        Write-Warning 'Building the installer WITHOUT Tesseract; it will be fetched when the application is installed.'
    }
}

# The GTK 4 + libadwaita runtime goes into packaging\gtk, which the installer script picks up when it exists. It is
# prepared afresh every time, so that an old runtime is never shipped by accident.
function Add-GtkBundle {
    $gtkBundle = Join-Path $PSScriptRoot 'gtk'
    if (Test-Path $gtkBundle) { Remove-Item $gtkBundle -Recurse -Force }
    if (Get-PortableGtk -Target $gtkBundle -Tag $GtkTag -Sha256 $GtkSha256 -ZipPath $GtkZip) {
        Write-Host "GTK 4 + libadwaita will be inside the installer ($gtkBundle)."
        Get-Content (Join-Path $gtkBundle 'gvsbuild-release.txt') | ForEach-Object { Write-Host "  $_" }
    } else {
        Write-Warning 'Building the installer WITHOUT GTK; it will be downloaded when the application is installed.'
    }
}

# A reason the Python parts are missing: a warning, or with -RequirePython the end of the build.
function Skip-PythonPart([string]$Message) {
    if ($RequirePython) { throw "-RequirePython: $Message" }
    Write-Warning $Message
}

# Python's installer goes into packaging\python and the Python libraries into packaging\wheelhouse; the installer script
# picks them up when they exist. Both are for the Python version that the GTK runtime's wheels are for, so they are
# only prepared when that runtime is inside the installer.
function Add-PythonBundle {
    $minor = $null
    if (-not $NoGtk -and (Test-Path (Join-Path $PSScriptRoot 'gtk'))) {
        $minor = Get-GtkPythonMinor (Join-Path $PSScriptRoot 'gtk')
    }
    $buildPython = $null
    if ($minor) { $buildPython = if ($PythonExe) { $PythonExe } else { Find-BuildPython $minor } }

    if (-not $minor) {
        Skip-PythonPart ('Building the installer WITHOUT Python and its libraries: without the GTK runtime inside ' +
                         'it, the Python version is not known yet.')
        return
    }
    if (-not $buildPython) {
        Skip-PythonPart ("Building the installer WITHOUT Python and its libraries: no Python $minor was found on " +
                         "this machine (the GTK runtime's wheels are for it). Install it, or pass -PythonExe.")
        return
    }

    $full = (& $buildPython -c 'import platform; print(platform.python_version())' | Select-Object -First 1).Trim()
    $installerReady = Get-PythonInstaller -Target (Join-Path $PSScriptRoot 'python') -Version $full `
        -Sha256 $PythonInstallerSha256 -InstallerPath $PythonInstaller
    if ($installerReady) {
        Write-Host "Python $full's installer will be inside the installer."
    } else {
        Skip-PythonPart ('Building the installer WITHOUT the Python installer; Python is installed with winget ' +
                         "when it is missing. $script:BundleProblem")
    }
    $wheelhouseReady = Build-Wheelhouse -Target (Join-Path $PSScriptRoot 'wheelhouse') -Python $buildPython `
        -ProjectRoot $ProjectRoot -ExpectMinor $minor
    if ($wheelhouseReady) {
        Write-Host 'The Python libraries will be inside the installer.'
    } else {
        Skip-PythonPart ('Building the installer WITHOUT the Python libraries; they are downloaded when the ' +
                         "application is installed. $script:BundleProblem")
    }
}

# The Visual C++ runtime DLLs go into packaging\vcruntime; the installer script picks them up when they exist, and the
# program loads them before anything else (src\karamvt\winsupport.py). Made afresh every time.
function Add-VcRuntimeBundle {
    if (Test-Path (Join-Path $PSScriptRoot 'vcruntime')) {
        Remove-Item (Join-Path $PSScriptRoot 'vcruntime') -Recurse -Force
    }
    if ($NoVcRuntime) {
        Write-Warning ('Building the installer WITHOUT the Visual C++ runtime DLLs; ctranslate2, onnxruntime and ' +
                       'llama.cpp need the Visual C++ Redistributable on the machine where it is installed.')
    } elseif (Get-VcRuntime -Target (Join-Path $PSScriptRoot 'vcruntime') -From $VcRuntimeDir) {
        Write-Host 'The Visual C++ runtime DLLs will be inside the installer.'
        Get-Content (Join-Path $PSScriptRoot 'vcruntime\versions.txt') | ForEach-Object { Write-Host "  $_" }
    } else {
        $reason = ('Building the installer WITHOUT the Visual C++ runtime DLLs; the libraries then need the Visual ' +
                   'C++ Redistributable on the machine where it is installed. ' + $script:VcRuntimeProblem)
        if ($RequireVcRuntime) { throw "-RequireVcRuntime: $reason" }
        Write-Warning $reason
    }
}

# --- Compiling the installer -------------------------------------------------------------------------------------

# The path of Inno Setup's command-line compiler; throws when Inno Setup 6 is not installed.
function Find-InnoSetup {
    $candidates = @(
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    ) | Where-Object { $_ -and (Test-Path $_) }
    if (-not $candidates) { throw 'Inno Setup 6 was not found. Install it with: winget install JRSoftware.InnoSetup' }
    return @($candidates)[0]
}

# --- Build -------------------------------------------------------------------------------------------------------

if (-not $NoTesseract) { Add-TesseractBundle }
if (-not $NoGtk) { Add-GtkBundle }

foreach ($stale in 'python', 'wheelhouse') {
    if (Test-Path (Join-Path $PSScriptRoot $stale)) { Remove-Item (Join-Path $PSScriptRoot $stale) -Recurse -Force }
}
if ($NoPython) {
    Write-Warning ('Building the installer WITHOUT Python and its libraries; Python is installed with winget and the ' +
                   'libraries are downloaded when the application is installed.')
} else {
    Add-PythonBundle
}
Add-VcRuntimeBundle

$compiler = Find-InnoSetup
& $compiler "/DAppVersion=$Version" (Join-Path $PSScriptRoot 'windows\karamvt.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE." }
Write-Host "Built: $(Join-Path $PSScriptRoot "dist\karamvt-$Version-windows-setup.exe")"
