# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""scripts/vcruntime-bundle.ps1: collecting the Visual C++ runtime DLLs that the installer carries.

The script runs under PowerShell 7 here with a Visual Studio folder that the tests make, so the choice of files and
of the newest version is checked everywhere; that Visual Studio's real Redist folder looks like this on the build
machine is what the CI job that builds the installer checks (it passes -RequireVcRuntime).
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "vcruntime-bundle.ps1"

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) is not available")

CRT_FILES = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
             "concrt140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll", "vccorlib140.dll")


def run(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    case = tmp_path / "case.ps1"
    # Strict mode, like the build script that dot-sources it: an unset variable is an error there, not an empty value.
    case.write_text(
        f"Set-StrictMode -Version 2.0\n$ErrorActionPreference = 'Stop'\n. '{SCRIPT}'\n{body}\n", encoding="utf-8"
    )
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(case)], capture_output=True, text=True, check=False, env=dict(os.environ)
    )


def get_vcruntime(
    tmp_path: Path, target: Path, source: str, failure: str = "$script:VcRuntimeProblem"
) -> subprocess.CompletedProcess:
    """Get-VcRuntime into *target* from *source* (its -VisualStudioRoot or -From argument); the run ends with an
    error, saying *failure*, when the function answers no."""
    return run(tmp_path, f"if (-not (Get-VcRuntime -Target '{target}' {source})) {{ throw {failure} }}")


def make_studio(
    root: Path, version: str = "14.44.35112", mark: str | None = None, openmp: bool = True, x86: bool = True
) -> Path:
    """A Visual Studio folder with a Redist\\MSVC\\<version> tree; every DLL says *mark* (default: the version)."""
    mark = mark or version
    redist = root / "VC" / "Redist" / "MSVC" / version
    crt = redist / "x64" / "Microsoft.VC143.CRT"
    crt.mkdir(parents=True)
    for name in CRT_FILES:
        (crt / name).write_text(f"{name} {mark}", encoding="utf-8")
    if openmp:
        (redist / "x64" / "Microsoft.VC143.OpenMP").mkdir()
        (redist / "x64" / "Microsoft.VC143.OpenMP" / "vcomp140.dll").write_text(
            f"vcomp140.dll {mark}", encoding="utf-8"
        )
    if x86:
        (redist / "x86" / "Microsoft.VC143.CRT").mkdir(parents=True)
        (redist / "x86" / "Microsoft.VC143.CRT" / "msvcp140.dll").write_text("32-bit", encoding="utf-8")
    (root / "VC" / "Redist" / "MSVC" / "v143").mkdir(exist_ok=True)  # A folder without a version number.
    return root


def test_the_runtime_dlls_are_copied_and_the_python_runtime_is_left_to_pythons_own_installer(tmp_path):
    studio = make_studio(tmp_path / "vs")
    done = run(
        tmp_path,
        f"$ok = Get-VcRuntime -Target '{tmp_path / 'out'}' -VisualStudioRoot '{studio}'\n"
        f"if ($ok -ne $true) {{ throw $script:VcRuntimeProblem }}",
    )
    assert done.returncode == 0, done.stderr
    names = sorted(path.name for path in (tmp_path / "out").iterdir())
    assert names == sorted(["NOTICE.txt", "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
                            "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "versions.txt", "vcomp140.dll"])
    assert not any(name.startswith("vcruntime140") for name in names)  # Beside python.exe already.


def test_the_function_answers_with_a_plain_true_and_leaves_nothing_else_in_the_pipeline(tmp_path):
    studio = make_studio(tmp_path / "vs")
    done = run(
        tmp_path,
        f"$r = @(Get-VcRuntime -Target '{tmp_path / 'out'}' -VisualStudioRoot '{studio}')\n"
        "if ($r.Count -ne 1 -or $r[0] -isnot [bool] -or -not $r[0]) { throw \"answer was: $($r -join '|')\" }",
    )
    assert done.returncode == 0, done.stderr


def test_the_newest_redistributable_of_all_the_visual_studios_is_used(tmp_path):
    older = make_studio(tmp_path / "vs2019", "14.29.30133", mark="old")
    newer = make_studio(tmp_path / "vs2022", "14.44.35112", mark="new")
    for roots in (f"'{older}', '{newer}'", f"'{newer}', '{older}'"):  # In either order
        target = tmp_path / ("out" + str(abs(hash(roots))))
        assert get_vcruntime(tmp_path, target, f"-VisualStudioRoot {roots}").returncode == 0
        assert (target / "msvcp140.dll").read_text(encoding="utf-8") == "msvcp140.dll new"
        assert (target / "vcomp140.dll").read_text(encoding="utf-8") == "vcomp140.dll new"


def test_versions_are_compared_as_numbers_not_as_text(tmp_path):
    ninth = make_studio(tmp_path / "a", "14.9.30000", mark="ninth")
    tenth = make_studio(tmp_path / "b", "14.10.30000", mark="tenth")
    done = get_vcruntime(tmp_path, tmp_path / "out", f"-VisualStudioRoot '{ninth}', '{tenth}'")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "out" / "msvcp140.dll").read_text(encoding="utf-8") == "msvcp140.dll tenth"


def test_only_the_64_bit_files_are_taken(tmp_path):
    studio = make_studio(tmp_path / "vs")
    assert get_vcruntime(tmp_path, tmp_path / "out", f"-VisualStudioRoot '{studio}'", failure="'no'").returncode == 0
    assert (tmp_path / "out" / "msvcp140.dll").read_text(encoding="utf-8") != "32-bit"


def test_the_note_says_where_the_files_came_from_and_which_files_they_are(tmp_path):
    studio = make_studio(tmp_path / "vs")
    assert get_vcruntime(tmp_path, tmp_path / "out", f"-VisualStudioRoot '{studio}'", failure="'no'").returncode == 0
    notes = (tmp_path / "out" / "versions.txt").read_text(encoding="utf-8-sig")
    assert "Taken from: Visual Studio's Redistributable 14.44.35112" in notes
    digest = hashlib.sha256(b"msvcp140.dll 14.44.35112").hexdigest().upper()
    assert "msvcp140.dll  version " in notes and f"sha256 {digest}" in notes
    assert "vcruntime140" not in notes
    assert "Microsoft" in (tmp_path / "out" / "NOTICE.txt").read_text(encoding="utf-8-sig")


def test_a_folder_given_by_the_user_is_used_as_it_is(tmp_path):
    mine = tmp_path / "mine"
    mine.mkdir()
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll", "vcruntime140.dll", "unrelated.dll"):
        (mine / name).write_text(name, encoding="utf-8")
    assert get_vcruntime(tmp_path, tmp_path / "out", f"-From '{mine}'").returncode == 0
    assert sorted(path.name for path in (tmp_path / "out").iterdir()) == sorted(
        ["msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll", "NOTICE.txt", "versions.txt"])


@pytest.mark.parametrize("missing", ["msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll"])
def test_a_folder_that_lacks_a_dll_the_libraries_need_is_refused_with_its_name(tmp_path, missing):
    mine = tmp_path / "mine"
    mine.mkdir()
    for name in ("msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll"):
        if name != missing:
            (mine / name).write_text(name, encoding="utf-8")
    done = run(
        tmp_path,
        f"$ok = Get-VcRuntime -Target '{tmp_path / 'out'}' -From '{mine}'\n"
        "if ($ok) { throw 'should have been refused' }\nWrite-Output \"PROBLEM: $script:VcRuntimeProblem\"",
    )
    assert done.returncode == 0, done.stderr
    assert f"lacks {missing}" in done.stdout and not (tmp_path / "out").exists()


def test_a_redistributable_without_openmp_is_refused(tmp_path):
    studio = make_studio(tmp_path / "vs", openmp=False)
    done = run(
        tmp_path,
        f"$ok = Get-VcRuntime -Target '{tmp_path / 'out'}' -VisualStudioRoot '{studio}'\n"
        "if ($ok) { throw 'should have been refused' }\nWrite-Output \"PROBLEM: $script:VcRuntimeProblem\"",
    )
    assert done.returncode == 0, done.stderr
    assert "lacks vcomp140.dll" in done.stdout


def test_a_folder_that_does_not_exist_and_a_machine_without_visual_studio_say_what_to_do(tmp_path):
    done = run(
        tmp_path,
        f"$ok = Get-VcRuntime -Target '{tmp_path / 'out'}' -From '{tmp_path / 'nothing'}'\n"
        "Write-Output \"1: $script:VcRuntimeProblem\"\n"
        f"$ok = Get-VcRuntime -Target '{tmp_path / 'out'}' -VisualStudioRoot '{tmp_path / 'empty-vs'}'\n"
        "Write-Output \"2: $script:VcRuntimeProblem\"",
    )
    assert done.returncode == 0, done.stderr
    assert "1: The folder" in done.stdout and "does not exist" in done.stdout
    assert (
        "2: No Visual Studio with a Visual C++ Redistributable folder" in done.stdout
        and "-VcRuntimeDir" in done.stdout
    )


def test_an_older_copy_is_replaced_whole_and_a_failed_try_leaves_it_alone(tmp_path):
    target = tmp_path / "out"
    target.mkdir()
    (target / "old-file.dll").write_text("old", encoding="utf-8")
    studio = make_studio(tmp_path / "vs")
    assert get_vcruntime(tmp_path, target, f"-VisualStudioRoot '{studio}'", failure="'no'").returncode == 0
    assert not (target / "old-file.dll").exists() and (target / "msvcp140.dll").exists()
    assert not (tmp_path / "out.partial").exists()
    before = sorted(path.name for path in target.iterdir())
    run(tmp_path, f"Get-VcRuntime -Target '{target}' -VisualStudioRoot '{tmp_path / 'empty-vs'}' | Out-Null")
    assert sorted(path.name for path in target.iterdir()) == before


def test_the_build_script_asks_for_the_runtime_and_can_be_told_to_insist_on_it():
    build = (ROOT / "packaging" / "build-windows.ps1").read_text(encoding="utf-8")
    assert (
        "scripts\\vcruntime-bundle.ps1" in build
        and "[switch]$NoVcRuntime" in build
        and "[switch]$RequireVcRuntime" in build
    )
    assert (
        "[string]$VcRuntimeDir" in build
        and "Get-VcRuntime -Target (Join-Path $PSScriptRoot 'vcruntime') -From $VcRuntimeDir" in build
    )
    assert "-RequireVcRuntime: " in build and "throw" in build.split("-RequireVcRuntime: ")[0].splitlines()[-1]
    # Made afresh every time: an old copy is removed before anything is collected, so that none can be shipped by
    # accident.
    assert (
        build.index("Remove-Item (Join-Path $PSScriptRoot 'vcruntime') -Recurse -Force")
        < build.index("Get-VcRuntime -Target")
    )
    # And before the installer is compiled.
    assert build.index("Get-VcRuntime -Target") < build.index("ISCC.exe")


def test_the_installer_carries_the_runtime_and_replaces_it_whole_on_an_upgrade_and_removes_it():
    iss = (ROOT / "packaging" / "windows" / "karamvt.iss").read_text(encoding="utf-8")
    assert '#define VcRuntimeFolder SourcePath + "\\..\\vcruntime"' in iss
    assert 'Source: "{#VcRuntimeFolder}\\*"; DestDir: "{app}\\vcruntime"; Flags: recursesubdirs ignoreversion' in iss
    assert iss.split("[InstallDelete]")[1].split("[Icons]")[0].count('Name: "{app}\\vcruntime"') == 1
    assert iss.split("[UninstallDelete]")[1].split("[Code]")[0].count('Name: "{app}\\vcruntime"') == 1
    assert "/packaging/vcruntime/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_the_script_is_not_copied_to_the_users_machine():
    # Build time only.
    assert "vcruntime-bundle.ps1" not in (ROOT / "packaging" / "windows" / "karamvt.iss").read_text(encoding="utf-8")
