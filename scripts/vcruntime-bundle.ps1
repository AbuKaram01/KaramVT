# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# vcruntime-bundle.ps1 - the Visual C++ runtime DLLs that the libraries of KaramVT import and Windows does not have,
# collected so that packaging\build-windows.ps1 can put them inside the installer.
#
# Why: a computer that never had the Visual C++ Redistributable has no msvcp140.dll, and without it ctranslate2
# (faster-whisper), onnxruntime and llama.cpp do not load. The installer therefore carries its own copies and the
# program loads them first (src\karamvt\winsupport.py). packaging\windows\dlls.py says which DLLs the installed program
# needs from the machine.
#
# Dot-sourced by packaging\build-windows.ps1; use:
#
#     Get-VcRuntime -Target <folder> [-From <folder>] [-VisualStudioRoot <folder>, ...]
#
# Works in Windows PowerShell 5.1 and PowerShell 7.

# Why the last Get-VcRuntime returned $false.
$script:VcRuntimeProblem = ''

# What is copied: the C++ standard library (msvcp140.dll, msvcp140_1.dll, ...), the parallel runtime (concrt140.dll)
# and OpenMP (vcomp140.dll). Not vcruntime140.dll and vcruntime140_1.dll: Python's own installer puts them beside
# python.exe, and a second copy in another folder would be a second copy of the C runtime in one process.
$script:VcRuntimePatterns = @('msvcp140*.dll', 'concrt140.dll', 'vcomp140.dll')

# Without these the libraries do not load on a machine that lacks the Redistributable (what packaging\windows\dlls.py
# found).
$script:VcRuntimeRequired = @('msvcp140.dll', 'msvcp140_1.dll', 'vcomp140.dll')

# --- Finding the Redistributable ---------------------------------------------------------------------------------

# Join-Path with several segments, written so that it also works in Windows PowerShell 5.1.
function Join-Segments([string]$Base, [string[]]$Parts) {
    foreach ($part in $Parts) { $Base = Join-Path $Base $part }
    return $Base
}

# The Visual Studio installations that vswhere and the usual folders show on this machine.
function Find-VisualStudioRoots {
    $roots = @()
    $programFilesX86 = ${env:ProgramFiles(x86)}
    if ($programFilesX86) {
        $vswhere = Join-Segments $programFilesX86 @('Microsoft Visual Studio', 'Installer', 'vswhere.exe')
        if (Test-Path -LiteralPath $vswhere) {
            $roots += @(& $vswhere -all -products '*' -property installationPath 2>$null | Where-Object { $_ })
        }
    }
    foreach ($base in @($env:ProgramFiles, $programFilesX86)) {
        if (-not $base) { continue }
        $studio = Join-Path $base 'Microsoft Visual Studio'
        foreach ($release in @(Get-ChildItem -LiteralPath $studio -Directory -ErrorAction SilentlyContinue)) {
            $roots += @(Get-ChildItem -LiteralPath $release.FullName -Directory -ErrorAction SilentlyContinue |
                        ForEach-Object { $_.FullName })
        }
    }
    return $roots
}

# The Redist\MSVC\<version>\x64 folders of one Visual Studio installation, each as an object with Version, Crt (the
# C++ runtime folder) and OpenMp (the OpenMP runtime folder, or $null). Versions without a C++ runtime are left out.
function Get-VcRedistFolders([string]$Studio) {
    $redist = Join-Segments $Studio @('VC', 'Redist', 'MSVC')
    foreach ($folder in @(Get-ChildItem -LiteralPath $redist -Directory -ErrorAction SilentlyContinue)) {
        # Folders are named by version (14.44.35112); others (v143) are links that carry no version.
        if ($folder.Name -notmatch '^\d+\.\d+\.\d+') { continue }
        $version = [version]($Matches[0])
        $x64 = Join-Path $folder.FullName 'x64'
        $crt = @(Get-ChildItem -LiteralPath $x64 -Directory -Filter 'Microsoft.VC*.CRT' -ErrorAction SilentlyContinue |
                 Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'msvcp140.dll') } |
                 Select-Object -First 1)
        if (-not $crt) { continue }
        $openMp = @(Get-ChildItem -LiteralPath $x64 -Directory -Filter 'Microsoft.VC*.OpenMP' `
                                  -ErrorAction SilentlyContinue | Select-Object -First 1)
        [pscustomobject]@{
            Version = $version
            Crt     = $crt[0].FullName
            OpenMp  = $(if ($openMp) { $openMp[0].FullName } else { $null })
        }
    }
}

<#
.SYNOPSIS
    The newest Redist\MSVC\<version>\x64 folders (the C++ runtime and the OpenMP runtime) of the Visual Studio
    installations in -Root.
.DESCRIPTION
    Without -Root, the installations that vswhere and the usual folders show on this machine.
.OUTPUTS
    $null, or an object with Version, Crt and OpenMp.
#>
function Find-VcRedist {
    param([string[]]$Root = @())

    $roots = @($Root | Where-Object { $_ })
    if (-not $roots) { $roots = @(Find-VisualStudioRoots) }

    $best = $null
    foreach ($studio in $roots) {
        foreach ($candidate in @(Get-VcRedistFolders $studio)) {
            if (-not $best -or $candidate.Version -gt $best.Version) { $best = $candidate }
        }
    }
    return $best
}

# --- Collecting the runtime --------------------------------------------------------------------------------------

# Where the DLLs come from: an object with Folders (to take them from) and Origin (words for the note beside them), or
# $null with the reason in $script:VcRuntimeProblem.
function Resolve-VcRuntimeSource([string]$From, [string[]]$VisualStudioRoot) {
    if ($From) {
        if (-not (Test-Path -LiteralPath $From -PathType Container)) {
            $script:VcRuntimeProblem = "The folder $From does not exist."
            return $null
        }
        return [pscustomobject]@{ Folders = @($From); Origin = $From }
    }
    $found = Find-VcRedist -Root $VisualStudioRoot
    if (-not $found) {
        $script:VcRuntimeProblem = ('No Visual Studio with a Visual C++ Redistributable folder (VC\Redist\MSVC) was ' +
            'found on this machine. Install the Visual Studio Build Tools with the C++ workload, or pass ' +
            '-VcRuntimeDir <a folder that holds msvcp140.dll, msvcp140_1.dll and vcomp140.dll>.')
        return $null
    }
    $folders = @($found.Crt, $found.OpenMp) | Where-Object { $_ }
    return [pscustomobject]@{
        Folders = @($folders)
        Origin  = "Visual Studio's Redistributable $($found.Version) ($($found.Crt))"
    }
}

# The runtime files found in the folders, by lower-case name; when two folders have the same file, the first wins.
function Get-VcRuntimeFiles([string[]]$Folders) {
    $byName = @{}
    foreach ($folder in $Folders) {
        foreach ($pattern in $script:VcRuntimePatterns) {
            $matching = Get-ChildItem -LiteralPath $folder -Filter $pattern -File -ErrorAction SilentlyContinue
            foreach ($file in @($matching)) {
                $key = $file.Name.ToLowerInvariant()
                if (-not $byName.ContainsKey($key)) { $byName[$key] = $file }
            }
        }
    }
    return $byName
}

# Copies the files (a hashtable of name to file) into $Folder, unchanged, with versions.txt (where they came from, and
# the version and SHA256 of each) and the NOTICE that says what they are.
function Write-VcRuntimeFolder([string]$Folder, [hashtable]$Files, [string]$Origin) {
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    $notes = @("Taken from: $Origin")
    foreach ($name in @($Files.Keys | Sort-Object)) {
        $file = $Files[$name]
        Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $Folder $file.Name)
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $file.FullName).Hash
        $notes += ('{0}  version {1}  sha256 {2}' -f $file.Name, $file.VersionInfo.FileVersion, $hash)
    }
    Set-Content -LiteralPath (Join-Path $Folder 'versions.txt') -Value $notes -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $Folder 'NOTICE.txt') -Encoding UTF8 -Value @(
        'These are the Microsoft Visual C++ runtime libraries (msvcp140*, concrt140, vcomp140), copied',
        'unchanged from the Redistributable files of Visual Studio on the machine that built this installer.',
        'KaramVT carries them so that the libraries it uses (ctranslate2, onnxruntime, llama.cpp) load on a',
        'computer that does not have the Visual C++ Redistributable. They remain Microsoft software and are',
        'redistributed under Microsoft''s terms for the Visual C++ Redistributable files; versions.txt says',
        'which ones these are.'
    )
}

<#
.SYNOPSIS
    Puts the Visual C++ runtime DLLs into the folder -Target, with a note of where they came from.
.DESCRIPTION
    They are taken from -From (a folder holding them) or, without it, from the Redist folder of the newest Visual
    Studio found. Microsoft allows these files to be redistributed with an application; they are copied unchanged.
    The folder is built beside -Target and moved into place at the end, so that a failure leaves nothing half-made.
.OUTPUTS
    $true, or $false with the reason in $script:VcRuntimeProblem.
#>
function Get-VcRuntime {
    param(
        [Parameter(Mandatory)][string]$Target,
        [string]$From = '',
        [string[]]$VisualStudioRoot = @()
    )

    $script:VcRuntimeProblem = ''
    $source = Resolve-VcRuntimeSource $From $VisualStudioRoot
    if (-not $source) { return $false }

    $byName = Get-VcRuntimeFiles $source.Folders
    $missing = @($script:VcRuntimeRequired | Where-Object { -not $byName.ContainsKey($_) })
    if ($missing) {
        $script:VcRuntimeProblem = "$($source.Origin) lacks $($missing -join ', ')."
        return $false
    }

    $partial = "$Target.partial"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
    Write-VcRuntimeFolder -Folder $partial -Files $byName -Origin $source.Origin
    if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
    Move-Item -LiteralPath $partial -Destination $Target
    return $true
}
