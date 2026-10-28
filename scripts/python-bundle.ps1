# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# python-bundle.ps1 - Python and its libraries, prepared as folders that can travel inside the installer.
#
#   * The python.org installer of the Python that the GTK runtime's wheels are for, so that winget and the internet
#     are not needed to get Python.
#   * A "wheelhouse": every Python library KaramVT needs, built or downloaded once at build time, so that PyPI, the
#     llama-cpp-python wheel index and a C++ compiler are not needed when the application is installed.
#
# Dot-source this file to use its functions:
#
#     Get-GtkPythonMinor <gtk folder>
#     Find-BuildPython <x.y>
#     Get-PythonInstaller -Target <folder> -Version <x.y.z> [-Sha256 <hash>] [-InstallerPath <exe>]
#     Find-PythonInstaller <folder> <x.y>
#     Install-PythonFromInstaller <exe> [<log file>]
#     Build-Wheelhouse -Target <folder> -Python <exe> -ProjectRoot <dir> [-ExpectMinor <x.y>] [-PipArguments ...]
#     Test-Wheelhouse <folder> [<x.y>]
#
# The Get- and Build- functions never throw: they say why on the console when they return $false.
#
# Why Python itself is not unpacked into the installer
#   A virtual environment cannot be moved (it records the absolute path of the Python it was made from). The installer
#   therefore installs Python on the user's machine and the install script makes the environment there. Python's
#   installer is run per user (no administrator rights) and its result is left in place when KaramVT is removed,
#   because other programs may use it.
#
# Trust in the Python installer
#   It is a download of the exact version of the Python that builds the wheelhouse, checked against its Authenticode
#   signature (it must be signed by the Python Software Foundation) and, when -Sha256 is given, against that hash. The
#   hash that was seen is printed.
#
# Python after 3.14
#   Releases after 3.14 are planned to come through the Python install manager instead of a classic installer
#   (PEP 773). Moving to one will need this file revisited; until then the build says it could not get the installer
#   and the application falls back to winget.

$script:CpuLlamaIndex = 'https://abetlen.github.io/llama-cpp-python/whl/cpu'

# The Python code that prints a Python's version as "3.14". It has no double quote on purpose: Windows PowerShell 5.1
# (which the installer runs the install script with) drops the double quotes of an argument it hands to a program, so
# a snippet that had them ended in a SyntaxError, and the check "is this environment made with this Python?" said no
# every time.
$script:PythonMinorSnippet = 'import sys; print(str(sys.version_info[0]) + chr(46) + str(sys.version_info[1]))'

# Why the last Get-PythonInstaller or Build-Wheelhouse returned $false (they print it too); the build script can then
# stop with that reason (-RequirePython) instead of leaving it in the middle of a long log.
$script:BundleProblem = ''

# The file that says which Python a wheelhouse was verified for (written by Build-Wheelhouse once pip has shown that
# the project's libraries can be installed from the folder alone for that Python).
$script:WheelhouseMarker = 'for-python.txt'

# --- Finding a Python --------------------------------------------------------------------------------------------

# Runs a native command and returns its output lines; $null when it failed. Never throws.
function Invoke-QuietNative([string]$Exe, [string[]]$Arguments) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return $output
    } catch { return $null } finally { $ErrorActionPreference = $saved }
}

<#
.SYNOPSIS
    The newest Python version ("3.14") that the PyGObject wheels in <gtk folder>\wheels are for, or $null.
#>
function Get-GtkPythonMinor([string]$GtkFolder) {
    $best = $null
    $wheelFolder = Join-Path $GtkFolder 'wheels'
    foreach ($wheel in @(Get-ChildItem $wheelFolder -Filter 'pygobject*.whl' -ErrorAction SilentlyContinue)) {
        if ($wheel.Name -match '-cp(\d)(\d+)-cp\d+-win_amd64\.whl$') {
            $version = [version]("{0}.{1}" -f $Matches[1], $Matches[2])
            if (-not $best -or $version -gt $best) { $best = $version }
        }
    }
    if ($best) { return "$($best.Major).$($best.Minor)" }
    return $null
}

<#
.SYNOPSIS
    A 64-bit python.exe of version <x.y> on this machine, or $null.
.DESCRIPTION
    The one on PATH (not the Microsoft Store stub), else the one the py launcher knows.
#>
function Find-BuildPython([string]$Minor) {
    $candidates = @()
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and $onPath.Source -notlike '*\WindowsApps\*') { $candidates += $onPath.Source }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $viaLauncher = Invoke-QuietNative 'py' @("-$Minor-64", '-c', 'import sys; print(sys.executable)')
        if ($viaLauncher) { $candidates += ([string]($viaLauncher | Select-Object -First 1)).Trim() }
    }
    foreach ($candidate in $candidates) {
        $reported = Invoke-QuietNative $candidate @('-c', $script:PythonMinorSnippet)
        if ($reported -and ([string]($reported | Select-Object -First 1)).Trim() -eq $Minor) { return $candidate }
    }
    return $null
}

# --- The Python installer ----------------------------------------------------------------------------------------

<#
.SYNOPSIS
    The path of the newest python-<x.y>.*-amd64.exe in a folder, or $null.
#>
function Find-PythonInstaller([string]$Folder, [string]$Minor) {
    if (-not $Folder -or -not (Test-Path -LiteralPath $Folder)) { return $null }
    $found = Get-ChildItem -LiteralPath $Folder -Filter "python-$Minor.*-amd64.exe" -ErrorAction SilentlyContinue |
             Sort-Object Name -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

# Throws unless the file is validly signed (Authenticode) by the Python Software Foundation. Only Windows can check a
# signature, and the installer is only run there, so elsewhere there is nothing to check.
function Assert-PythonInstallerSignature([string]$File) {
    if ($env:OS -ne 'Windows_NT') { return }
    $signature = Get-AuthenticodeSignature -LiteralPath $File
    if ($signature.Status -ne 'Valid') { throw "The installer's signature is not valid ($($signature.Status))." }
    $signer = [string]$signature.SignerCertificate.Subject
    if ($signer -notmatch 'Python Software Foundation') {
        throw "The installer is not signed by the Python Software Foundation (signed by $signer)."
    }
    Write-Host 'OK  The installer is signed by the Python Software Foundation.' -ForegroundColor Green
}

# The installer file to use: the one given, or the exact version downloaded from python.org into $WorkDir.
function Get-PythonInstallerFile([string]$Version, [string]$InstallerPath, [string]$WorkDir) {
    if ($InstallerPath) {
        if (-not (Test-Path -LiteralPath $InstallerPath)) { throw "$InstallerPath does not exist." }
        return (Resolve-Path -LiteralPath $InstallerPath).Path
    }
    $name = "python-$Version-amd64.exe"
    $directory = $Version -replace '^(\d+\.\d+\.\d+).*$', '$1'
    $file = Join-Path $WorkDir $name
    Write-Host "==> Downloading Python $Version (python.org)..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$directory/$name" -OutFile $file -UseBasicParsing
    return $file
}

# Checks the file against the expected SHA256 (when there is one) and prints the hash that was seen; throws when
# they differ.
function Confirm-PythonInstallerHash([string]$File, [string]$Sha256) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $File).Hash
    if ($Sha256 -and ($hash -ne $Sha256.ToUpper())) {
        throw "The file does not match the expected SHA256 (got $hash, expected $($Sha256.ToUpper()))."
    }
    if ($Sha256) {
        Write-Host "OK  Python installer SHA256 $hash matches." -ForegroundColor Green
    } else {
        Write-Host "!!  No SHA256 to check the Python installer against. Its SHA256 is $hash." -ForegroundColor Yellow
    }
}

<#
.SYNOPSIS
    Puts the python.org installer of one Python version into a folder, as python-<version>-amd64.exe.
.DESCRIPTION
    Downloads it (or takes the file given), checks its SHA256 and its signature, and leaves it alone in -Target (an
    older installer there is removed). When it fails it says why on the console and in $script:BundleProblem.
.PARAMETER Version
    3.14.9, or 3.15.0rc2 for a pre-release.
.PARAMETER Sha256
    The hash the installer must have; without one, only the signature is checked.
.PARAMETER InstallerPath
    A python-*-amd64.exe that is already on this machine: nothing is downloaded (offline builds).
.OUTPUTS
    $true when -Target holds the installer, else $false.
#>
function Get-PythonInstaller {
    param(
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$Version,
        [string]$Sha256 = '',
        [string]$InstallerPath = ''
    )

    $name = "python-$Version-amd64.exe"
    $work = Join-Path ([IO.Path]::GetTempPath()) ('python-installer-' + [guid]::NewGuid().ToString('N'))
    $script:BundleProblem = ''
    try {
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }
        New-Item -ItemType Directory -Force -Path $work | Out-Null

        $file = Get-PythonInstallerFile -Version $Version -InstallerPath $InstallerPath -WorkDir $work
        Confirm-PythonInstallerHash $file $Sha256
        Assert-PythonInstallerSignature $file

        New-Item -ItemType Directory -Force -Path $Target | Out-Null
        # An older installer is never left beside it.
        Get-ChildItem -LiteralPath $Target -Filter 'python-*-amd64.exe' -ErrorAction SilentlyContinue |
            Remove-Item -Force
        Copy-Item -LiteralPath $file -Destination (Join-Path $Target $name) -Force
        return $true
    } catch {
        $script:BundleProblem = "The Python installer could not be prepared: $($_.Exception.Message)"
        Write-Host "!!  $script:BundleProblem" -ForegroundColor Yellow
        return $false
    } finally {
        if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

<#
.SYNOPSIS
    Runs a python.org installer silently for the current user; $true when it succeeded.
.DESCRIPTION
    No administrator rights, nothing added to PATH, no launcher, tests, documentation, shortcuts or file
    associations. Python lands in %LOCALAPPDATA%\Programs\Python\Python3xx, where the install script looks first. The
    installer is ended after 15 minutes.
#>
function Install-PythonFromInstaller([string]$Installer, [string]$LogFile = '') {
    $arguments = @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0', 'Include_test=0',
                   'Include_doc=0', 'Shortcuts=0', 'AssociateFiles=0')
    if ($LogFile) { $arguments += @('/log', "`"$LogFile`"") }
    try {
        $process = Start-Process -FilePath $Installer -ArgumentList $arguments -PassThru
        if (-not $process.WaitForExit(15 * 60 * 1000)) {
            if ($env:OS -eq 'Windows_NT') {
                & taskkill /PID $process.Id /T /F 2>&1 | Out-Null
            } else {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
            $global:LASTEXITCODE = 0
            Write-Host '!!  The Python installer did not finish in 15 minutes and was stopped.' -ForegroundColor Yellow
            return $false
        }
        # 3010: installed, a restart is advisable (not needed to use it).
        return (@(0, 3010) -contains $process.ExitCode)
    } catch {
        Write-Host "!!  The Python installer could not be run: $($_.Exception.Message)" -ForegroundColor Yellow
        return $false
    }
}

# --- The wheelhouse ----------------------------------------------------------------------------------------------

# Why a folder cannot be used as the wheelhouse for Python <x.y> ('' when it can). A wheelhouse has the libraries whose
# absence would send the installation back to the internet (and, for llama-cpp-python, to a compiler), and, when a
# Python version is given, was verified for that version.
function Get-WheelhouseProblem([string]$Folder, [string]$Minor = '') {
    if (-not $Folder -or -not (Test-Path -LiteralPath $Folder)) { return "there is no folder $Folder" }
    $names = @(Get-ChildItem -LiteralPath $Folder -Filter '*.whl' -ErrorAction SilentlyContinue |
               ForEach-Object { $_.Name.ToLower() })
    foreach ($needed in 'llama_cpp_python-', 'faster_whisper-', 'setuptools-', 'wheel-') {
        if (-not @($names | Where-Object { $_.StartsWith($needed) })) {
            return "it has no $($needed.TrimEnd('-')) wheel (it has $($names.Count) wheels)"
        }
    }
    if ($Minor) {
        $marker = Join-Path $Folder $script:WheelhouseMarker
        $verified = ''
        if (Test-Path -LiteralPath $marker) { $verified = (Get-Content -LiteralPath $marker -TotalCount 1).Trim() }
        if ($verified -ne $Minor) {
            $what = if ($verified) { "Python $verified" } else { 'no Python version' }
            return "it was verified for $what, not for Python $Minor"
        }
    }
    return ''
}

<#
.SYNOPSIS
    Whether a folder is a complete wheelhouse (and, when a version is given, one verified for that Python).
#>
function Test-Wheelhouse([string]$Folder, [string]$Minor = '') {
    return -not (Get-WheelhouseProblem $Folder $Minor)
}

# Runs `python -m pip <arguments>` with its output on the console only (what a function prints is part of what it
# returns) and gives back the exit code and the last lines of what pip said, for the reason of a failure. Its error
# lines are made plain text first: Windows PowerShell 5.1 turns a native command's stderr into errors under 'Stop'.
function Invoke-Pip([string]$Python, [string[]]$Arguments) {
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Tee-Object leaves the variable unset when pip prints nothing, which strict mode does not allow.
    $pipLines = @()
    try { & $Python -m pip @Arguments 2>&1 | ForEach-Object { "$_" } | Tee-Object -Variable pipLines | Out-Host }
    finally { $ErrorActionPreference = $savedPreference }
    $code = $LASTEXITCODE
    $last = @($pipLines | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 4)
    $tail = ($last | ForEach-Object { $_.Trim() }) -join ' | '
    return [pscustomobject]@{ ExitCode = $code; Tail = $tail }
}

# The version ("3.14") of the Python that builds the wheelhouse. Throws when it cannot be run, or when it is not the
# Python the wheels have to be for.
function Get-BuildPythonMinor([string]$Python, [string]$ExpectMinor) {
    $reported = Invoke-QuietNative $Python @('-c', $script:PythonMinorSnippet)
    if (-not $reported) { throw "$Python could not be run." }
    $minor = ([string]($reported | Select-Object -First 1)).Trim()
    if ($ExpectMinor -and $minor -ne $ExpectMinor) {
        throw ("$Python is Python $minor, but the wheels have to be for Python $ExpectMinor " +
               "(the one the GTK runtime's wheels are for).")
    }
    return $minor
}

# What the project asks for (and what building it needs), from its own pyproject.toml, written to
# $WorkDir\requirements.txt, whose path is returned. Reading the toml takes tomllib: Python 3.11 or newer.
function Write-ProjectRequirements([string]$Python, [string]$ProjectRoot, [string]$WorkDir) {
    $reader = Join-Path $WorkDir 'read-requirements.py'
    Set-Content -LiteralPath $reader -Encoding ASCII -Value @(
        'import sys, tomllib',
        'with open(sys.argv[1], "rb") as handle:',
        '    data = tomllib.load(handle)',
        'for line in data.get("build-system", {}).get("requires", []) + data["project"].get("dependencies", []):',
        '    print(line)'
    )
    $lines = Invoke-QuietNative $Python @($reader, (Join-Path $ProjectRoot 'pyproject.toml'))
    if (-not $lines) {
        throw 'The requirements could not be read from pyproject.toml (this needs Python 3.11 or newer).'
    }
    $requirements = Join-Path $WorkDir 'requirements.txt'
    Set-Content -LiteralPath $requirements -Encoding ASCII -Value $lines
    return $requirements
}

# Fills $Staging with the wheels that $Requirements asks for, then lets pip itself say whether they are enough. A
# package that only comes as source (langdetect) is built into a wheel here; llama-cpp-python has to be a prebuilt
# wheel (from the index it is installed from), because compiling it takes many minutes and a C++ compiler. Throws, with
# what pip said, when a step fails.
function Invoke-WheelhouseBuild([string]$Python, [string]$Staging, [string]$Requirements, [string]$LlamaIndex,
                                [string[]]$PipArguments) {
    $wheelArguments = @('wheel', '--wheel-dir', $Staging, '--only-binary=llama-cpp-python', '--prefer-binary',
                        '--extra-index-url', $LlamaIndex, '-r', $Requirements)
    $wheel = Invoke-Pip $Python ($wheelArguments + $PipArguments)
    if ($wheel.ExitCode -ne 0) { throw "pip wheel failed (exit code $($wheel.ExitCode)): $($wheel.Tail)" }

    # Pip has the last word on whether these wheels are enough: everything the project asks for (and what building
    # it needs), for this Python, from this folder alone, with nothing to build and nothing to download.
    Write-Host '==> Checking that pip can install everything from the wheelhouse alone...' -ForegroundColor Cyan
    $verify = Invoke-Pip $Python @('install', '--dry-run', '--no-index', '--find-links', $Staging,
                                   '--only-binary=:all:', '--quiet', '-r', $Requirements)
    if ($verify.ExitCode -ne 0) {
        throw ("pip cannot install the libraries from the wheelhouse alone " +
               "(exit code $($verify.ExitCode)): $($verify.Tail)")
    }
    $problem = Get-WheelhouseProblem $Staging
    if ($problem) { throw "pip finished, but the wheelhouse is not usable: $problem." }
}

<#
.SYNOPSIS
    Builds the wheelhouse with pip: wheels for everything the project needs, for one Python.
.DESCRIPTION
    The wheels are for the project's dependencies and for what building the project needs (its pyproject.toml says
    which). The wheelhouse is verified with pip before it is moved to -Target, and the Python version it was verified
    for is written beside the wheels. When it fails it says why on the console and in $script:BundleProblem.
.PARAMETER Python
    The python.exe that pip runs with: the wheels are for this Python.
.PARAMETER ExpectMinor
    The Python version the wheels have to be for (the one the GTK runtime's wheels are for); -Python must be that.
.PARAMETER PipArguments
    More arguments for pip (a test gives a local index).
.OUTPUTS
    $true when -Target is a complete wheelhouse, else $false.
#>
function Build-Wheelhouse {
    param(
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$ProjectRoot,
        [string]$ExpectMinor = '',
        [string]$LlamaIndex = $script:CpuLlamaIndex,
        [string[]]$PipArguments = @()
    )

    $work = Join-Path ([IO.Path]::GetTempPath()) ('wheelhouse-' + [guid]::NewGuid().ToString('N'))
    $staging = $Target + '.partial'
    $script:BundleProblem = ''
    try {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $minor = Get-BuildPythonMinor $Python $ExpectMinor
        $requirements = Write-ProjectRequirements $Python $ProjectRoot $work

        if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $staging | Out-Null
        Write-Host ("==> Building the wheelhouse for Python $minor " +
                    '(the Python libraries that go inside the installer)...') -ForegroundColor Cyan
        Invoke-WheelhouseBuild -Python $Python -Staging $staging -Requirements $requirements `
            -LlamaIndex $LlamaIndex -PipArguments $PipArguments
        Set-Content -LiteralPath (Join-Path $staging $script:WheelhouseMarker) -Value $minor -Encoding ASCII

        if (Test-Path $Target) { Remove-Item $Target -Recurse -Force }
        Move-Item -LiteralPath $staging -Destination $Target
        $count = @(Get-ChildItem -LiteralPath $Target -Filter '*.whl').Count
        Write-Host "OK  $count wheels for Python $minor." -ForegroundColor Green
        return $true
    } catch {
        $script:BundleProblem = "The wheelhouse could not be built: $($_.Exception.Message)"
        Write-Host "!!  $script:BundleProblem" -ForegroundColor Yellow
        return $false
    } finally {
        if (Test-Path $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
