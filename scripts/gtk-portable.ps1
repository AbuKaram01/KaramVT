# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# gtk-portable.ps1 - the GTK 4 + libadwaita runtime for Windows, as a folder that can travel inside the installer.
#
# The runtime is the prebuilt gvsbuild release. Nothing is installed and nothing is written outside the target
# folder. Dot-source this file to use its functions:
#
#     Get-PortableGtk   -Target <folder> [-Tag <tag>|latest] [-Sha256 <hash>] [-ZipPath <zip>]
#     Test-GtkRuntime   <folder>
#     Resolve-GtkChoice -Explicit <dir> -ProjectRoot <dir> -DefaultDir <dir>
#
# Release
#   The release is the one named by the default tag below unless -Tag says otherwise ("latest" asks GitHub for the
#   newest). It is not "the newest" by default because the release decides which Python the PyGObject and pycairo
#   wheels in it are for, and the application's other libraries (faster-whisper, PyAV, llama-cpp-python) have to exist
#   for that Python: gvsbuild 2026.10.0 moved its wheels to Python 3.15 while 2026.8.0 has them for 3.14. To move to a
#   newer release, change the tag here and the Python version of the jobs in .github/workflows/ci.yml together, and
#   run the manual jobs.
#
# Integrity
#   The download is checked against the SHA256 given with -Sha256 or, failing that, against the digest that GitHub
#   publishes for that release file, when it publishes one. The hash that was used is printed and kept in
#   <folder>\gvsbuild-release.txt, so that a release build can pin it with -Sha256.

$script:GtkDefaultTag = '2026.8.0'

# --- Choosing a runtime ------------------------------------------------------------------------------------------

<#
.SYNOPSIS
    Whether a folder holds a complete GTK 4 + libadwaita runtime.
.DESCRIPTION
    A runtime has the DLLs, the GTK and libadwaita typelibs, and libadwaita itself (its DLL is adwaita-1-0.dll,
    not libadwaita-...).
#>
function Test-GtkRuntime([string]$Folder) {
    if (-not $Folder) { return $false }
    return (Test-Path (Join-Path $Folder 'bin')) -and
           (Test-Path (Join-Path $Folder 'lib\girepository-1.0\Gtk-4.0.typelib')) -and
           (Test-Path (Join-Path $Folder 'lib\girepository-1.0\Adw-1.typelib')) -and
           [bool](Get-ChildItem (Join-Path $Folder 'bin') -Filter '*adwaita-1-*.dll' -ErrorAction SilentlyContinue)
}

<#
.SYNOPSIS
    Which runtime install-windows.ps1 should use.
.DESCRIPTION
    In order of preference: the folder named with -Explicit (-GtkDir); the copy that came inside the installer
    (<application folder>\gtk, which the application finds by itself); the default folder, where one is downloaded
    when it is missing.
.OUTPUTS
    An object with Dir (the folder), Custom (the application has to be told about it, through KARAMVT_GTK_DIR) and
    Bundled (it is the copy from inside the installer).
#>
function Resolve-GtkChoice([string]$Explicit, [string]$ProjectRoot, [string]$DefaultDir) {
    if ($Explicit) {
        $folder = (Resolve-Path $Explicit).Path
        return [pscustomobject]@{ Dir = $folder; Custom = ($folder -ne $DefaultDir); Bundled = $false }
    }
    $bundled = Join-Path $ProjectRoot 'gtk'
    if (Test-GtkRuntime $bundled) {
        return [pscustomobject]@{ Dir = $bundled; Custom = $false; Bundled = $true }
    }
    return [pscustomobject]@{ Dir = $DefaultDir; Custom = $false; Bundled = $false }
}

# --- Talking to GitHub -------------------------------------------------------------------------------------------

function Get-GitHubHeaders {
    $headers = @{ 'User-Agent' = 'karamvt-build' }
    # A token lifts the API's rate limit.
    if ($env:GITHUB_TOKEN) { $headers['Authorization'] = "Bearer $($env:GITHUB_TOKEN)" }
    return $headers
}

# The tag of a gvsbuild release: the one given, the tested one when none is, or the newest for "latest". The "latest"
# redirect is tried first because the GitHub API allows only 60 anonymous requests per hour per address, which a shared
# connection can exhaust.
function Resolve-GtkTag([string]$Tag) {
    if (-not $Tag) { return $script:GtkDefaultTag }
    if ($Tag -ne 'latest') { return $Tag }
    try {
        $request = [Net.HttpWebRequest]::Create('https://github.com/wingtk/gvsbuild/releases/latest')
        $request.Method = 'HEAD'
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'karamvt-build'
        $response = $request.GetResponse()
        $location = [string]$response.Headers['Location']
        $response.Close()
        if ($location -match '/tag/([^/]+)$') { return $Matches[1] }
    } catch { }
    $api = 'https://api.github.com/repos/wingtk/gvsbuild/releases/latest'
    $release = Invoke-RestMethod -Uri $api -Headers (Get-GitHubHeaders)
    return [string]$release.tag_name
}

# The SHA256 that GitHub publishes for a release file, or $null (older files have none, and the API may be out of
# reach).
function Get-GtkPublishedDigest([string]$Tag, [string]$AssetName) {
    try {
        $api = "https://api.github.com/repos/wingtk/gvsbuild/releases/tags/$Tag"
        $release = Invoke-RestMethod -Uri $api -Headers (Get-GitHubHeaders)
        $asset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
        $digest = ''
        if ($asset -and ($asset.PSObject.Properties.Name -contains 'digest')) { $digest = [string]$asset.digest }
        if ($digest -match '^sha256:([0-9a-fA-F]{64})$') { return $Matches[1].ToUpper() }
    } catch { }
    return $null
}

# --- Preparing the runtime folder --------------------------------------------------------------------------------

# Where the zip is, which release it is, and the SHA256 it has to have ('' when there is nothing to check it against).
# The zip is the one given, or it is downloaded into $WorkDir.
function Get-GtkZip([string]$Tag, [string]$Sha256, [string]$ZipPath, [string]$WorkDir) {
    if ($ZipPath) {
        if (-not (Test-Path -LiteralPath $ZipPath)) { throw "$ZipPath does not exist." }
        $zip = (Resolve-Path -LiteralPath $ZipPath).Path
        return [pscustomobject]@{
            Zip = $zip; Release = '(local file) ' + (Split-Path -Leaf $zip); Expected = $Sha256.ToUpper()
        }
    }
    $resolved = Resolve-GtkTag $Tag
    $asset = "GTK4_Gvsbuild_${resolved}_x64.zip"
    $zip = Join-Path $WorkDir $asset
    Write-Host "==> Downloading GTK 4 + libadwaita (gvsbuild $resolved)..." -ForegroundColor Cyan
    $url = "https://github.com/wingtk/gvsbuild/releases/download/$resolved/$asset"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $expected = if ($Sha256) { $Sha256.ToUpper() } else { [string](Get-GtkPublishedDigest $resolved $asset) }
    return [pscustomobject]@{ Zip = $zip; Release = $resolved; Expected = $expected }
}

# The SHA256 of the zip, after checking it against the expected one (when there is one); throws when they differ.
function Confirm-GtkZipHash([string]$Zip, [string]$Expected) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Zip).Hash
    if ($Expected -and ($hash -ne $Expected)) {
        throw "The file does not match the expected SHA256 (got $hash, expected $Expected)."
    }
    if ($Expected) {
        Write-Host "OK  GTK zip SHA256 $hash matches." -ForegroundColor Green
    } else {
        Write-Host "!!  No SHA256 to check the GTK zip against (none given, none published). Its SHA256 is $hash." `
            -ForegroundColor Yellow
    }
    return $hash
}

# Unpacks the zip inside $WorkDir and returns the folder that holds the runtime: the archive may have it at its top
# level or inside one folder.
function Expand-GtkZip([string]$Zip, [string]$WorkDir) {
    Write-Host '==> Unpacking GTK...' -ForegroundColor Cyan
    $unpacked = Join-Path $WorkDir 'unpacked'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($Zip, $unpacked)
    if (Test-Path (Join-Path $unpacked 'bin')) { return $unpacked }
    $inner = Get-ChildItem $unpacked -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'bin') } |
        Select-Object -First 1
    if ($inner) { return $inner.FullName }
    return $unpacked
}

# Moves the unpacked runtime to $Target once it has been checked, and leaves a note beside it saying where it came
# from (for whoever opens the folder later, and for a release build to pin). It is built next to the target first, so
# that a failure never leaves a half-made folder.
function Publish-GtkRuntime([string]$Root, [string]$Target, [string]$Release, [string]$Hash) {
    $staging = $Target + '.partial'
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $staging) | Out-Null
    Move-Item -LiteralPath $Root -Destination $staging
    if (-not (Test-GtkRuntime $staging)) {
        throw ('The archive is not a complete GTK 4 + libadwaita runtime ' +
               '(it needs bin, the Gtk-4.0 and Adw-1 typelibs, and adwaita-1-*.dll).')
    }
    Set-Content -LiteralPath (Join-Path $staging 'gvsbuild-release.txt') -Encoding UTF8 -Value @(
        "gvsbuild release: $Release",
        "SHA256 of the zip: $Hash",
        "Prepared: $([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm')) UTC"
    )
    if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
    Move-Item -LiteralPath $staging -Destination $Target
}

<#
.SYNOPSIS
    Prepares the GTK 4 + libadwaita runtime as a folder.
.DESCRIPTION
    Downloads the gvsbuild release (or takes the zip given), checks its SHA256, unpacks it and moves it to -Target.
    Does nothing when -Target already holds a complete runtime. It never throws: when it fails it says why on the
    console.
.PARAMETER Target
    The folder to prepare.
.PARAMETER Tag
    The gvsbuild release (a tag such as 2026.8.0, or "latest"); by default the tested one.
.PARAMETER Sha256
    The hash the zip must have; by default the one GitHub publishes for it, when it publishes one.
.PARAMETER ZipPath
    A GTK4_Gvsbuild_*_x64.zip that is already on this machine: nothing is downloaded (offline builds).
.OUTPUTS
    $true when -Target is a complete runtime, else $false.
#>
function Get-PortableGtk {
    param(
        [Parameter(Mandatory)][string]$Target,
        [string]$Tag = '',
        [string]$Sha256 = '',
        [string]$ZipPath = ''
    )

    if (Test-GtkRuntime $Target) { return $true }
    $work = Join-Path ([IO.Path]::GetTempPath()) ('gtk-portable-' + [guid]::NewGuid().ToString('N'))
    try {
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
        New-Item -ItemType Directory -Force -Path $work | Out-Null

        $source = Get-GtkZip -Tag $Tag -Sha256 $Sha256 -ZipPath $ZipPath -WorkDir $work
        $hash = Confirm-GtkZipHash $source.Zip $source.Expected
        $root = Expand-GtkZip $source.Zip $work
        Publish-GtkRuntime -Root $root -Target $Target -Release $source.Release -Hash $hash
        return $true
    } catch {
        Write-Host "!!  The GTK runtime could not be prepared: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    } finally {
        if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
        if ($Target -and (Test-Path ($Target + '.partial'))) {
            Remove-Item ($Target + '.partial') -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
