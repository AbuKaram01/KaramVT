# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which GTK release is used, and what the installer and the CI jobs leave behind when an installation fails."""

from packaging_support import ROOT, WINDOWS


def test_the_gtk_release_is_a_tested_one_and_not_the_newest():
    portable = (ROOT / "scripts" / "gtk-portable.ps1").read_text(encoding="utf-8")
    # gvsbuild 2026.10.0 builds its PyGObject wheels for Python 3.15; 2026.8.0 has them for 3.14, which is what CI uses.
    assert "$script:GtkDefaultTag = '2026.8.0'" in portable
    assert "python-version: \"3.14\"" in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "Get-GtkZipUrls" not in install and "releases/latest" not in install  # No "newest" lookup of its own.
    assert "[string]$GtkTag" in install and "-Tag $GtkTag" in install
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert "[string]$GtkTag = ''" in build


def test_a_wheel_mismatch_says_which_python_the_wheels_are_for():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "are for Python $($available -join ', ')" in install and "-GtkTag" in install


def test_the_installer_keeps_a_log_of_the_install_script_and_points_to_it():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    assert "[string]$LogFile" in install and "Start-Transcript" in install
    iss = (WINDOWS / "karamvt.iss").read_text(encoding="utf-8")
    assert "-LogFile" in iss and "karamvt\\install.log" in iss
    # Passed to the script, named in the error.
    assert iss.count("install.log") >= 2


def test_the_manual_installer_job_cannot_hang_and_shows_the_logs():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("windows-installer-run:")[1].split("windows-full-install:")[0]
    assert job.count("run-installer.ps1") == 2 and job.count("timeout-minutes: 35") == 2
    script = (WINDOWS / "run-installer.ps1").read_text(encoding="utf-8")
    assert "WaitForExit($TimeoutMinutes * 60 * 1000)" in script and "taskkill" in script
    assert script.index("finally") < script.index("Get-Content $scriptLog")  # The logs show on failure too.


def test_arabic_letters_in_the_paths_are_checked_in_the_full_install_job_without_a_red_report():
    """Tesseract's own limit is only reported (green); what KaramVT does about it is counted."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci.split("windows-full-install:")[1]
    report = job.split("- name: Tesseract on its own, with Arabic letters in each path")[1].split("- name:")[0]
    counted = job.split("- name: What KaramVT does about Arabic letters in the paths")[1].split("- name:")[0]
    # A report, so never red.
    assert "throw" not in report and "continue-on-error" not in report
    # An empty output is not an error.
    assert "if ($raw)" in report
    # What the program relies on is counted.
    assert "throw" in counted and "ocr_arabic_smoke.py" in counted
    # TEMP and the language folder are Arabic.
    assert "$env:TEMP" in counted and "$env:XDG_DATA_HOME" in counted
    # The checks that follow still run when this one fails.
    suite = job.split("- name: Test suite with the real libraries installed")[1].split("- name:")[0]
    assert "steps.install.outcome == 'success'" in suite


def test_ocr_reaches_pytesseract_only_through_the_helpers_that_know_about_ascii_paths():
    ocr_text = (ROOT / "src" / "karamvt" / "core" / "ocr.py").read_text(encoding="utf-8")
    assert ocr_text.count("pytesseract_module.image_to_") == 3  # One in each of _image_to_string/_data/_osd.
    assert "pytesseract.image_to_" not in ocr_text.replace("pytesseract_module.image_to_", "")


def test_a_failing_native_command_is_never_the_exit_code_of_a_step_that_otherwise_passed():
    """Actions ends a pwsh step with the exit code of the last native command: taskkill fails when part of the process
    tree is already gone, and Tesseract fails on a path it cannot use. Both are results here, not failures."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    launch = ci.split("- name: Launch the application")[1].split("- uses: actions/upload-artifact")[0]
    assert "$global:LASTEXITCODE = 0" in launch.split('cmd /c "taskkill')[1].split("\n")[0]
    checker = (WINDOWS / "check-installed.ps1").read_text(encoding="utf-8")
    assert "$global:LASTEXITCODE = 0" in checker.split('cmd /c "taskkill')[1].split("\n")[0]
    job = ci.split("windows-full-install:")[1]
    for name in (
        "Tesseract on its own, with Arabic letters in each path",
        "What KaramVT does about Arabic letters in the paths",
    ):
        step = job.split(f"- name: {name}")[1].split("- name:")[0]
        assert "$global:LASTEXITCODE = 0" in step.split("function Run-Case")[1].split("return $text")[0]
    # The console of a runner is cp1252: the script that prints Arabic paths must not stop on them.
    smoke = job.split("What KaramVT does about Arabic letters in the paths")[1].split("- name:")[0]
    assert 'sys.stdout.reconfigure(errors="backslashreplace")' in smoke
