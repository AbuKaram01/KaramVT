# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Windows scripts and CI agree on how the bundled Python and wheelhouse are used.

Checks that the install script, the Inno Setup script, the build and CI use the bundled Python and wheelhouse the way
the tests of scripts/python-bundle.ps1 assume.
"""

import re

from packaging_support import BUNDLE, ROOT, WINDOWS


def test_the_install_script_prefers_what_the_installer_carries_and_falls_back_to_the_internet():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert ". (Join-Path $PSScriptRoot 'python-bundle.ps1')" in install
    # Python comes from the installer that ships with KaramVT; winget is only the fallback.
    python_step = install.split("function Install-Python")[1].split("$wheelDir =")[0]
    assert (
        python_step.index("Find-PythonInstaller")
        < python_step.index("Install-PythonFromInstaller")
        < python_step.index("winget install")
    )
    assert "installer that came with KaramVT" in python_step
    # The libraries are installed from the wheelhouse, without an index, when it was built for this Python; if that
    # fails, they are downloaded as before.
    assert "$offline = Test-Wheelhouse $wheelhouse $chosen" in install
    assert "if (-not $offline) { Pip @('install', '--upgrade', 'pip', '--quiet') }" in install
    # The wheelhouse is used twice: for llama-cpp-python, and for the project with its other libraries.
    assert install.count("'--no-index', '--find-links', $wheelhouse") == 2
    assert "installed from the wheelhouse (no internet needed)" in install
    assert "downloading them instead" in install and "Pip @('install', '--quiet', '-e', $ProjectRoot)" in install
    # A llama-cpp-python that the user asked for with -Cuda, -BuildLlamaFromSource or -LlamaIndexUrl is never replaced
    # by the bundled one.
    llama = install.split("if ($BuildLlamaFromSource")[1].split("# Tesseract is a program")[0]
    assert "-not $LlamaIndexUrl" in llama and llama.index("Install-LlamaFromSource") < llama.index("--no-index")


def test_the_two_places_that_name_the_llama_cpp_wheel_index_agree():
    index = re.search(
        r"CpuLlamaIndex = '([^']+)'", (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    )
    bundled = re.search(r"CpuLlamaIndex = '([^']+)'", BUNDLE.read_text(encoding="utf-8"))
    assert index and bundled and index.group(1) == bundled.group(1)


def test_the_build_prepares_python_and_its_libraries_for_the_python_of_the_gtk_runtime():
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert ". (Join-Path $ProjectRoot 'scripts\\python-bundle.ps1')" in build
    assert "[switch]$NoPython" in build and "[string]$PythonExe" in build and "[string]$PythonInstaller" in build
    assert (
        "[switch]$RequirePython" in build
        and 'throw "-RequirePython: $Message"' in build
        and build.count("Skip-PythonPart ") >= 4
    )
    # A reason is recorded for the Python installer and another for the libraries.
    assert build.count("$script:BundleProblem") == 2
    assert (
        build.index("Get-PortableGtk -Target")
        < build.index("Get-GtkPythonMinor")
        < build.index("Get-PythonInstaller -Target")
        < build.index("ISCC")
    )
    # The wheels are built for the Python of the GTK runtime.
    assert "-ExpectMinor $minor" in build
    # The Python installer that is bundled has the version of the Python that runs the build.
    assert "platform.python_version()" in build
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/packaging/python/" in ignore and "/packaging/wheelhouse/" in ignore


def test_the_inno_script_carries_python_and_the_wheelhouse_only_when_the_build_prepared_them():
    iss = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert 'Source: "..\\..\\scripts\\python-bundle.ps1"; DestDir: "{app}\\scripts"' in iss
    for folder, define in (("python", "PythonFolder"), ("wheelhouse", "WheelFolder")):
        assert f'#define {define} SourcePath + "\\..\\{folder}"' in iss
        assert (
            f'#if DirExists({define})\nSource: "{{#{define}}}\\*"; DestDir: "{{app}}\\{folder}"; '
            f'Flags: recursesubdirs ignoreversion\n#endif'
        ) in iss
        # The folder is replaced on an upgrade (InstallDelete) and removed on uninstall (UninstallDelete).
        assert f'Type: filesandordirs; Name: "{{app}}\\{folder}"' in iss
    assert iss.count('Name: "{app}\\python"') == 2 and iss.count('Name: "{app}\\wheelhouse"') == 2
    # The section header sits outside the conditionals, so the section exists whichever folders are present.
    assert "[InstallDelete]\n#if DirExists(GtkFolder)" in iss


def test_the_installation_check_knows_what_the_install_script_says_when_it_used_the_bundled_parts():
    check = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    for marker in ("Python libraries installed from the wheelhouse", "installer that came with KaramVT"):
        assert marker in install and marker in check, marker


def test_ci_checks_that_the_installer_carries_python_and_that_the_first_install_has_no_other_python():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installer_job = ci.split("  installer:")[1].split("  windows-installer-run:")[0]
    # The libraries are collected with the Python of the GTK runtime, so the job that builds the installer must
    # provide that Python.
    assert "actions/setup-python@v5" in installer_job and 'python-version: "3.14"' in installer_job
    heading = "- name: The installer carries the Python installer and the Python libraries"
    carries = installer_job.split(heading)[1].split("- uses:")[0]
    assert (
        "Find-PythonInstaller" in carries
        and "Test-Wheelhouse 'packaging\\wheelhouse'" in carries
        and "throw" in carries
    )

    run_job = ci.split("  windows-installer-run:")[1].split("  windows-full-install:")[0]
    first = run_job.split("- name: Run the installer (silently, as a user would, on a machine")[1].split("- name:")[0]
    # The step removes Python from the PATH (hostedtoolcache) and from the registry (PythonCore), so that neither
    # finds one.
    assert "hostedtoolcache" in first and "PythonCore" in first
    # The removal happens before the installer runs.
    assert first.index("PythonCore") < first.index("run-installer.ps1 -Label first")
    assert "check-installed.ps1 -ExpectBundledPython" in run_job
    upgrade = run_job.split("- name: Check the upgraded installation")[1]
    # An upgrade finds Python already installed, so it does not expect the bundled one.
    assert "-ExpectBundledPython" not in upgrade
    # It also keeps the Python environment of the first installation instead of making another one.
    assert "'recreating it'" in upgrade and upgrade.index("'recreating it'") < upgrade.index("check-installed.ps1")


def test_the_installers_built_in_ci_must_carry_python_or_the_build_says_why():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # The installer job and windows-installer-run both build with -RequirePython and -RequireVcRuntime.
    assert ci.count("run: ./packaging/build-windows.ps1 -RequirePython -RequireVcRuntime\n") == 2
    # No other build omits them: an installer that quietly lacks Python, its libraries or the Visual C++ runtime would
    # only fail much later.
    assert "run: ./packaging/build-windows.ps1\n" not in ci


def test_the_installation_check_asks_the_installed_program_what_ocr_settings_would_show():
    checker = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    section = checker.split("# 4b.")[1].split("# 5. The application starts")[0]
    assert (
        "karamvt.core import ocr" in section
        and "ocr.check_engine()" in section
        and "ocr.find_tesseract()" in section
    )
    # The check runs the program with a console (python.exe) and, as the Start menu does, without one (pythonw.exe).
    assert "'python.exe', 'pythonw.exe'" in section
    # The Tesseract under test is the one that came with the installer.
    assert "Join-Path $App 'tesseract\\tesseract.exe'" in section
    # The check fails if OCR does not find that Tesseract or cannot use it.
    assert "does not find the Tesseract that came with the installer" in section and "cannot use it" in section
    assert checker.index("# 4. GTK loads") < checker.index("# 4b.") < checker.index("# 5. The application starts")


def test_ci_checks_that_the_tesseract_row_and_ocr_agree_about_where_the_program_is():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.split("- name: The Tesseract row finds whatever OCR would run")[1].split("- name:")[0]
    assert "ocr.find_tesseract()" in step and "ocr.check_engine()" in step and "shutil.which" in step
    # This is what pytesseract does when it is not told where the program is.
    assert "subprocess.run([\"tesseract\", \"--version\"]" in step
    # The program is on the PATH only as a quoted folder.
    assert "$env:PATH = \"`\"$odd`\";\"" in step
    # The locations that KaramVT searches itself are emptied, so the program is found nowhere else.
    assert "$env:LOCALAPPDATA = $empty" in step and "$env:XDG_DATA_HOME = $empty" in step
    # The two must agree; a disagreement fails the step (throw) instead of only being reported.
    assert "if plain and not status.ok" in step and "throw" in step
    job = ci.split("windows-full-install:")[1]
    assert (
        job.index("- name: OCR with the real Tesseract")
        < job.index("- name: The Tesseract row finds whatever OCR would run")
        < job.index("- name: Test suite with the real libraries installed")
    )
