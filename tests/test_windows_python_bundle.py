# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Python's installer and the wheelhouse that the Windows installer carries (scripts/python-bundle.ps1, run for
real), and how a Python is asked which version it is."""

import os
import re
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest
from packaging_support import BUNDLE, ROOT, make_executable, needs_pwsh


def run_bundle_script(tmp_path: Path, body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    script = tmp_path / "bundle-case.ps1"
    # Strict mode like the scripts that use python-bundle.ps1 (an unset variable is an error there, not an empty value).
    script.write_text(
        f"Set-StrictMode -Version 2.0\n$ErrorActionPreference = 'Stop'\n. '{BUNDLE}'\n{body}\n", encoding="utf-8"
    )
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, check=False,
        env={**os.environ, **(env or {})},
    )


# The files these tests make are not signed, and on Windows the signature is checked for real (its own tests below).

NO_SIGNATURE_CHECK = "function Assert-PythonInstallerSignature([string]$File) { }\n"


def make_wheel(folder: Path, name: str, version: str = "1.0", tag: str = "py3-none-any") -> Path:
    """A real (empty) wheel that pip accepts: just its metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    dist = f"{name.replace('-', '_')}-{version}"
    path = folder / f"{dist}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr(f"{dist}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        wheel.writestr(
            f"{dist}.dist-info/WHEEL",
            f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n",
        )
        wheel.writestr(f"{dist}.dist-info/RECORD", "")
    return path


def this_pythons_tag() -> tuple[str, str]:
    """("3.12", "cp312-cp312-linux_x86_64"): a wheel tag that pip accepts for the Python running the tests."""
    major, minor = sys.version_info[:2]
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    return f"{major}.{minor}", f"cp{major}{minor}-cp{major}{minor}-{platform}"


def make_wheelhouse_folder(
    folder: Path, minor: str = "3.14", skip: tuple[str, ...] = (), marker: str | None = None
) -> Path:
    """A folder that looks like a built wheelhouse; the marker says for which Python (the default: *minor*)."""
    folder.mkdir(parents=True, exist_ok=True)
    tag = f"cp{minor.replace('.', '')}"
    for name, wheel_tag in (("llama_cpp_python", f"{tag}-{tag}-win_amd64"), ("faster_whisper", "py3-none-any"),
                            ("setuptools", "py3-none-any"), ("wheel", "py3-none-any"), ("langdetect", "py3-none-any")):
        if name not in skip:
            (folder / f"{name}-1.0-{wheel_tag}.whl").write_bytes(b"x")
    (folder / "for-python.txt").write_text((marker or minor) + "\n", encoding="ascii")
    return folder


def test_no_python_snippet_handed_to_a_program_has_a_double_quote():
    """Windows PowerShell 5.1 (it runs the install script) drops the double quotes of an argument that it hands to a
    program: `python -c 'print("%d.%d" % ...)'` was a SyntaxError there, so the environment of the same Python was
    thought to be another one and was recreated on every run, and a Python on PATH was never found."""
    scripts = sorted((ROOT / "scripts").glob("*.ps1")) + sorted((ROOT / "packaging").rglob("*.ps1"))
    assert scripts
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        for snippet in re.findall(r"'-c',\s*'([^']*)'", text):
            assert '"' not in snippet, f"{script.name}: {snippet}"
    assert '"' not in re.search(r"PythonMinorSnippet = '([^']*)'", BUNDLE.read_text(encoding="utf-8")).group(1)


def test_the_version_checks_use_the_one_snippet_and_it_prints_what_they_compare_with():
    install = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")
    # The Python on PATH, and the existing environment.
    assert install.count("@('-c', $script:PythonMinorSnippet)") == 2
    # The Python that builds the wheelhouse.
    assert BUNDLE.read_text(encoding="utf-8").count("@('-c', $script:PythonMinorSnippet)") == 2
    snippet = re.search(r"PythonMinorSnippet = '([^']*)'", BUNDLE.read_text(encoding="utf-8")).group(1)
    run = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, check=True)
    # "3.14", the form the install script compares.
    assert run.stdout.strip() == f"{sys.version_info[0]}.{sys.version_info[1]}"


@needs_pwsh
def test_the_python_installer_of_the_right_version_is_found(tmp_path):
    for name in ("python-3.14.9-amd64.exe", "python-3.15.0-amd64.exe", "python-3.1.5-amd64.exe",
                 "python-3.14.9-arm64.exe", "notes.txt"):
        (tmp_path / "python" / name).parent.mkdir(exist_ok=True)
        (tmp_path / "python" / name).write_bytes(b"x")
    found = run_bundle_script(
        tmp_path, f"Write-Output (\"FOUND=\" + (Find-PythonInstaller '{tmp_path / 'python'}' '3.14'))"
    )
    assert f"FOUND={tmp_path / 'python' / 'python-3.14.9-amd64.exe'}" in found.stdout, found.stdout + found.stderr
    none = run_bundle_script(
        tmp_path, f"Write-Output (\"FOUND=[\" + (Find-PythonInstaller '{tmp_path / 'python'}' '3.13') + \"]\")"
    )
    assert "FOUND=[]" in none.stdout, none.stdout + none.stderr
    nothing = run_bundle_script(
        tmp_path, f"Write-Output (\"FOUND=[\" + (Find-PythonInstaller '{tmp_path / 'nowhere'}' '3.14') + \"]\")"
    )
    assert "FOUND=[]" in nothing.stdout, nothing.stdout + nothing.stderr


@needs_pwsh
def test_a_python_installer_is_copied_under_its_own_name_and_replaces_an_older_one(tmp_path):
    import hashlib

    installer = tmp_path / "somewhere" / "installer.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"MZ python installer")
    target = tmp_path / "out" / "python"
    target.mkdir(parents=True)
    (target / "python-3.14.1-amd64.exe").write_bytes(b"old")
    result = run_bundle_script(
        tmp_path,
        NO_SIGNATURE_CHECK
        + (
            f"$ok = Get-PythonInstaller -Target '{target}' -Version 3.14.9 -InstallerPath '{installer}'\n"
            f"Write-Output \"RESULT=$ok\""
        ),
    )
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    assert sorted(path.name for path in target.iterdir()) == ["python-3.14.9-amd64.exe"]
    assert (target / "python-3.14.9-amd64.exe").read_bytes() == b"MZ python installer"
    # The hash that was seen is printed.
    assert hashlib.sha256(b"MZ python installer").hexdigest().upper() in result.stdout


@needs_pwsh
def test_a_python_installer_must_match_the_hash_it_is_pinned_to(tmp_path):
    import hashlib

    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"MZ python installer")
    right = hashlib.sha256(installer.read_bytes()).hexdigest()
    wrong = ("0" if right[0] != "0" else "1") + right[1:]
    bad = run_bundle_script(
        tmp_path,
        NO_SIGNATURE_CHECK
        + (
            f"$ok = Get-PythonInstaller -Target '{tmp_path / 'bad'}' -Version 3.14.9 "
            f"-InstallerPath '{installer}' -Sha256 '{wrong}'\n"
            f"Write-Output \"RESULT=$ok\""
        ),
    )
    assert "RESULT=False" in bad.stdout and "does not match the expected SHA256" in bad.stdout, bad.stdout + bad.stderr
    assert not (tmp_path / "bad").exists()
    good = run_bundle_script(
        tmp_path,
        NO_SIGNATURE_CHECK
        + (
            f"$ok = Get-PythonInstaller -Target '{tmp_path / 'good'}' -Version 3.14.9 "
            f"-InstallerPath '{installer}' -Sha256 '{right.upper()}'\n"
            f"Write-Output \"RESULT=$ok\""
        ),
    )
    assert "RESULT=True" in good.stdout, good.stdout + good.stderr
    missing = run_bundle_script(
        tmp_path,
        NO_SIGNATURE_CHECK
        + (
            f"$ok = Get-PythonInstaller -Target '{tmp_path / 'm'}' -Version 3.14.9 "
            f"-InstallerPath '{tmp_path / 'none.exe'}'\n"
            f"Write-Output \"RESULT=$ok\""
        ),
    )
    assert "RESULT=False" in missing.stdout and "does not exist" in missing.stdout
    reason = run_bundle_script(
        tmp_path,
        NO_SIGNATURE_CHECK
        + (
            f"$null = Get-PythonInstaller -Target '{tmp_path / 'r'}' -Version 3.14.9 "
            f"-InstallerPath '{tmp_path / 'none.exe'}'\n"
            f"Write-Output \"WHY=$script:BundleProblem\""
        ),
    )
    # The build can say why.
    assert "WHY=The Python installer could not be prepared: " in reason.stdout and "does not exist" in reason.stdout


@needs_pwsh
@pytest.mark.skipif(sys.platform != "win32", reason="only Windows can check an Authenticode signature")
def test_a_python_installer_must_be_validly_signed_by_the_python_software_foundation(tmp_path):
    unsigned = tmp_path / "python-3.14.9-amd64.exe"
    unsigned.write_bytes(b"MZ not really an installer")
    refused = run_bundle_script(
        tmp_path,
        f"$ok = Get-PythonInstaller -Target '{tmp_path / 'out'}' -Version 3.14.9 -InstallerPath '{unsigned}'\n"
        f"Write-Output \"RESULT=$ok\"",
    )
    assert "RESULT=False" in refused.stdout and "signature is not valid" in refused.stdout, (
        refused.stdout + refused.stderr
    )
    # Nothing is copied from a file that is not trusted.
    assert not (tmp_path / "out").exists()
    # PowerShell's own program is validly signed, by Microsoft: a good signature from the wrong signer is refused too.
    other = run_bundle_script(
        tmp_path,
        "try { Assert-PythonInstallerSignature (Get-Process -Id $PID).Path; Write-Output 'RESULT=accepted' } "
        "catch { Write-Output \"RESULT=$($_.Exception.Message)\" }",
    )
    assert "RESULT=accepted" not in other.stdout and "RESULT=" in other.stdout, other.stdout + other.stderr
    assert (
        "not signed by the Python Software Foundation" in other.stdout or "signature is not valid" in other.stdout
    ), other.stdout


@needs_pwsh
@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in installer is a POSIX script")
# (3010, "restart advisable", is 194 on a POSIX system.)
@pytest.mark.parametrize("exit_code, expected", [(0, True), (1, False), (5, False)])
def test_python_is_installed_silently_for_the_current_user(tmp_path, exit_code, expected):
    record = tmp_path / "arguments.txt"
    installer = make_executable(
        tmp_path / "python-3.14.9-amd64.exe", f'#!/bin/sh\nprintf "%s\\n" "$@" > "{record}"\nexit {exit_code}\n'
    )
    result = run_bundle_script(
        tmp_path,
        f"$ok = Install-PythonFromInstaller '{installer}' '{tmp_path / 'python.log'}'\n"
        f"Write-Output \"RESULT=$ok\"",
    )
    assert f"RESULT={expected}" in result.stdout, result.stdout + result.stderr
    arguments = record.read_text(encoding="utf-8").split()
    for needed in ("/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "Include_test=0",
                   "Shortcuts=0", "AssociateFiles=0", "/log"):
        assert needed in arguments, arguments
    assert "InstallAllUsers=1" not in arguments  # Never needs administrator rights.
    # "Installed, a restart is advisable" is a success.
    assert "@(0, 3010) -contains $process.ExitCode" in BUNDLE.read_text(encoding="utf-8")


@needs_pwsh
def test_a_wheelhouse_is_complete_only_with_what_the_installation_needs(tmp_path):
    def check(folder, minor=""):
        result = run_bundle_script(tmp_path, f"Write-Output \"RESULT=$(Test-Wheelhouse '{folder}' '{minor}')\"")
        return "RESULT=True" in result.stdout

    assert check(make_wheelhouse_folder(tmp_path / "complete"))
    assert check(tmp_path / "complete", "3.14")
    # It was verified for 3.14.
    assert not check(tmp_path / "complete", "3.13")
    # What pip accepts for a Python is not guessed from the wheels' names: the wheel's tag does not matter, the
    # marker does.
    assert check(make_wheelhouse_folder(tmp_path / "abi3", marker="3.14"), "3.14")
    assert not check(make_wheelhouse_folder(tmp_path / "unverified", marker="3.13"), "3.14")
    (tmp_path / "unmarked").mkdir()
    for wheel in (tmp_path / "complete").glob("*.whl"):
        (tmp_path / "unmarked" / wheel.name).write_bytes(b"x")
    # No marker: not verified for any Python.
    assert check(tmp_path / "unmarked") and not check(tmp_path / "unmarked", "3.14")
    for missing in ("llama_cpp_python", "faster_whisper", "setuptools", "wheel"):
        assert not check(make_wheelhouse_folder(tmp_path / f"no-{missing}", skip=(missing,))), missing


@needs_pwsh
def test_a_wheelhouse_that_cannot_be_used_says_why(tmp_path):
    def why(folder, minor=""):
        return run_bundle_script(
            tmp_path, f"Write-Output \"WHY=[$(Get-WheelhouseProblem '{folder}' '{minor}')]\""
        ).stdout

    assert "WHY=[]" in why(make_wheelhouse_folder(tmp_path / "ok"), "3.14")
    assert "no llama_cpp_python wheel" in why(make_wheelhouse_folder(tmp_path / "a", skip=("llama_cpp_python",)))
    assert "verified for Python 3.13, not for Python 3.14" in why(
        make_wheelhouse_folder(tmp_path / "b", marker="3.13"), "3.14"
    )
    assert "there is no folder" in why(tmp_path / "nowhere")


@needs_pwsh
def test_the_python_of_the_gtk_runtime_is_the_newest_its_wheels_are_for(tmp_path):
    wheels = tmp_path / "gtk" / "wheels"
    wheels.mkdir(parents=True)
    for name in ("PyGObject-3.54.0-cp313-cp313-win_amd64.whl", "PyGObject-3.54.0-cp314-cp314-win_amd64.whl",
                 "pycairo-1.28.0-cp315-cp315-win_amd64.whl"):
        (wheels / name).write_bytes(b"x")
    found = run_bundle_script(tmp_path, f"Write-Output \"MINOR=$(Get-GtkPythonMinor '{tmp_path / 'gtk'}')\"")
    # pycairo does not count.
    assert "MINOR=3.14" in found.stdout, found.stdout + found.stderr
    nothing = run_bundle_script(tmp_path, f"Write-Output \"MINOR=[$(Get-GtkPythonMinor '{tmp_path / 'nowhere'}')]\"")
    assert "MINOR=[]" in nothing.stdout


def make_project(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=77", "wheel"]\n'
        '[project]\nname = "demo"\nversion = "1.0"\n'
        'dependencies = ["faster-whisper", "llama-cpp-python", '
        '"PyGObject; sys_platform != \'win32\' and python_version < \'3\'"]\n',
        encoding="utf-8",
    )
    return folder


def make_pip_index(folder: Path, llama_tag: str) -> Path:
    """A folder of empty wheels that pip can build the project's wheelhouse from (--no-index); the wheel of
    llama-cpp-python has the tag *llama_tag*."""
    for name in ("faster-whisper", "setuptools", "wheel"):
        make_wheel(folder, name, version="80.0" if name == "setuptools" else "1.0")
    make_wheel(folder, "llama-cpp-python", tag=llama_tag)
    return folder


def build_wheelhouse(
    tmp_path: Path, target: Path, project: Path, minor: str, index: Path
) -> subprocess.CompletedProcess:
    """Build-Wheelhouse for *project* (it must be for Python *minor*), with pip reading only the wheels in *index*."""
    return run_bundle_script(
        tmp_path,
        f"$ok = Build-Wheelhouse -Target '{target}' -Python '{sys.executable}' -ProjectRoot '{project}' "
        f"-ExpectMinor '{minor}' -PipArguments '--no-index','--find-links','{index}'\n"
        f"Write-Output \"RESULT=$ok\"",
    )


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_the_wheelhouse_is_built_from_what_the_project_asks_for(tmp_path):
    minor, llama_tag = this_pythons_tag()
    index = make_pip_index(tmp_path / "index", llama_tag)
    target = tmp_path / "out" / "wheelhouse"
    result = build_wheelhouse(tmp_path, target, make_project(tmp_path / "project"), minor, index)
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr
    names = sorted(path.name for path in target.iterdir())
    assert any(name.startswith("llama_cpp_python-") for name in names), names
    assert any(name.startswith("setuptools-") for name in names), names
    assert not (tmp_path / "out" / "wheelhouse.partial").exists()
    # Verified by pip for this Python.
    assert (target / "for-python.txt").read_text(encoding="ascii").strip() == minor
    assert "Checking that pip can install everything from the wheelhouse alone" in result.stdout


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_the_wheelhouse_does_not_depend_on_how_the_llama_cpp_wheel_is_tagged(tmp_path):
    """Whether a wheel suits this Python is for pip to say, not for the wheel's file name (an abi3 wheel has no cp
    tag of its own)."""
    minor, _ = this_pythons_tag()
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    index = make_pip_index(tmp_path / "index", f"cp38-abi3-{platform}")
    result = build_wheelhouse(
        tmp_path, tmp_path / "out" / "wheelhouse", make_project(tmp_path / "project"), minor, index
    )
    assert "RESULT=True" in result.stdout, result.stdout + result.stderr


@needs_pwsh
@pytest.mark.skipif(sys.version_info < (3, 11), reason="reading pyproject.toml needs tomllib")
def test_a_wheelhouse_for_another_python_or_with_a_missing_library_is_refused_and_leaves_nothing(tmp_path):
    minor, llama_tag = this_pythons_tag()
    project = make_project(tmp_path / "project")
    index = make_pip_index(tmp_path / "index", llama_tag)

    wrong = build_wheelhouse(tmp_path, tmp_path / "wrong", project, "3.0", index)
    assert "RESULT=False" in wrong.stdout and "have to be for Python 3.0" in wrong.stdout, wrong.stdout + wrong.stderr

    # pip cannot find it any more.
    (index / next(path.name for path in index.iterdir() if path.name.startswith("faster_whisper"))).unlink()
    missing = build_wheelhouse(tmp_path, tmp_path / "missing", project, minor, index)
    assert "RESULT=False" in missing.stdout and "pip wheel failed" in missing.stdout, missing.stdout + missing.stderr
    # The reason carries what pip complained about.
    assert "faster-whisper" in missing.stdout.split("pip wheel failed")[1]
    assert not (tmp_path / "wrong").exists() and not (tmp_path / "missing").exists()
    assert not (tmp_path / "missing.partial").exists()

    # A llama-cpp-python wheel built for some other Python is no use: pip does not accept it, so there is no wheelhouse.
    other = make_pip_index(tmp_path / "other-index", "cp27-cp27m-win_amd64")
    foreign = build_wheelhouse(tmp_path, tmp_path / "foreign", project, minor, other)
    assert "RESULT=False" in foreign.stdout and "llama-cpp-python" in foreign.stdout, foreign.stdout
    assert not (tmp_path / "foreign").exists(), foreign.stdout
