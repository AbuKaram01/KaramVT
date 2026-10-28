# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Finding Tesseract and the command that installs it, with stand-in programs instead of the real thing."""

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from karamvt.core import ocr
from karamvt.core.ocr import EngineStatus, check_engine, engine_install_command

ROOT = Path(__file__).resolve().parent.parent


def program(directory: Path, name: str, script: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH holding only the stand-ins a test puts there, so a Tesseract installed on this machine is never found."""
    if sys.platform == "win32":
        pytest.skip("the stand-in programs are POSIX shell scripts")
    folder = tmp_path / "bin"
    folder.mkdir()
    (folder / "sleep").symlink_to(shutil.which("sleep"))  # The one tool a stand-in needs.
    monkeypatch.setenv("PATH", str(folder))
    return folder


# --- Is Tesseract there? ---

def test_a_missing_program_is_reported_as_not_installed(bin_dir):
    status = check_engine()
    assert status == EngineStatus(None, None) and not status.ok


def test_the_version_is_read_from_the_first_line_of_a_current_release(bin_dir):
    path = program(bin_dir, "tesseract", 'echo "tesseract 5.3.4"; echo " leptonica-1.82.0"; echo "  libgif 5.2.1"\n')
    assert check_engine() == EngineStatus(str(path), "5.3.4") and check_engine().ok


def test_older_releases_print_their_version_on_stderr(bin_dir):
    path = program(bin_dir, "tesseract", 'echo "tesseract 4.1.1" >&2\n')
    assert check_engine() == EngineStatus(str(path), "4.1.1")


@pytest.mark.parametrize("line, version", [
    ("tesseract v5.3.0.20221214", "5.3.0.20221214"),  # How the Windows builds write it.
    ("Tesseract 5.5.0", "5.5.0"),
])
def test_other_ways_of_writing_the_version_line_are_understood(bin_dir, line, version):
    program(bin_dir, "tesseract", f'echo "{line}"\n')
    assert check_engine().version == version


def test_a_program_that_fails_is_found_but_not_usable(bin_dir):
    path = program(
        bin_dir, "tesseract", 'echo "error while loading shared libraries: libleptonica.so.6" >&2; exit 127\n'
    )
    status = check_engine()
    assert status.path == str(path) and not status.ok
    assert status.problem == "error while loading shared libraries: libleptonica.so.6"


def test_a_program_that_is_not_tesseract_is_not_taken_for_it(bin_dir):
    program(bin_dir, "tesseract", 'echo "Usage: something else"\n')
    status = check_engine()
    assert not status.ok and status.problem == "Usage: something else"


def test_a_program_that_prints_nothing_still_gets_an_explanation(bin_dir):
    program(bin_dir, "tesseract", "exit 3\n")
    assert check_engine().problem == "it exited with code 3"


def test_a_program_that_hangs_does_not_hang_the_application(bin_dir):
    program(bin_dir, "tesseract", "sleep 5\n")
    status = check_engine(timeout=0.2)
    assert not status.ok and status.problem == "it did not answer in time"


def test_a_file_that_cannot_be_run_is_explained(bin_dir):
    path = bin_dir / "tesseract"
    path.write_text("not a program")
    path.chmod(0o755)  # Executable, but with no interpreter line at all.
    status = check_engine()
    assert status.path == str(path) and not status.ok and status.problem


def test_the_version_is_asked_the_way_the_ocr_itself_starts_the_program(monkeypatch):
    """No window of its own, no input to wait for, output read as UTF-8, and time enough for a slow first start."""
    calls = []

    def fake_run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, stdout="tesseract v5.4.0.20240606\n", stderr="")

    monkeypatch.setattr(ocr, "find_tesseract", lambda: "C:/Apps/tesseract.exe")
    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "platform", "linux")  # (Not whatever this test happens to run on.)
    assert check_engine().version == "5.4.0.20240606"
    command, options = calls[-1]
    assert command == ["C:/Apps/tesseract.exe", "--version"]
    assert options["stdin"] is subprocess.DEVNULL and options["encoding"] == "utf-8" and options["errors"] == "replace"
    # Antivirus scanning a program's first start takes a while.
    assert options["timeout"] == ocr.ENGINE_CHECK_TIMEOUT >= 20
    assert options["creationflags"] == 0  # Nothing to hide off Windows.

    monkeypatch.setattr(sys, "platform", "win32")
    check_engine()
    assert calls[-1][1]["creationflags"] == 0x08000000  # CREATE_NO_WINDOW: no console flashes up.


def test_when_it_is_not_found_on_windows_the_places_it_looked_in_are_listed(monkeypatch):
    monkeypatch.setattr(ocr, "is_windows", lambda: True)
    monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
    monkeypatch.setenv("ProgramFiles", "C:/Program Files")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/Ali/AppData/Local")
    status = check_engine()
    assert status == EngineStatus(None, None)  # (The list is not part of what a status is.)
    assert status.searched[0].endswith("tesseract.exe") and "PATH" in status.searched
    assert any(
        place.replace("\\", "/").endswith("Program Files/Tesseract-OCR/tesseract.exe") for place in status.searched
    )
    # Its own copies first, as find_tesseract does.
    assert status.searched.index("PATH") > 0


def test_off_windows_there_is_only_path_to_look_in(bin_dir, monkeypatch):
    monkeypatch.setattr(ocr, "is_windows", lambda: False)
    assert check_engine().searched == ()


# --- The install command ---

def which_finds(monkeypatch, *names):
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in names else None)


@pytest.mark.parametrize("manager, command", [
    ("apt", "sudo apt install tesseract-ocr tesseract-ocr-osd"),
    ("dnf", "sudo dnf install tesseract tesseract-osd"),
    ("pacman", "sudo pacman -S tesseract tesseract-data-eng tesseract-data-osd"),
    ("brew", "brew install tesseract"),
])
def test_each_package_manager_gets_its_own_command(monkeypatch, manager, command):
    which_finds(monkeypatch, manager)
    assert engine_install_command() == command


def test_the_first_known_package_manager_wins_as_in_the_other_install_hints(monkeypatch):
    which_finds(monkeypatch, "pacman", "apt", "brew")
    assert engine_install_command() == "sudo apt install tesseract-ocr tesseract-ocr-osd"


def test_an_unknown_system_has_no_command_rather_than_a_wrong_one(monkeypatch):
    which_finds(monkeypatch)
    assert engine_install_command() is None


def test_the_commands_install_what_the_projects_own_packages_depend_on():
    """If a package is added to the dependencies of the .deb, .rpm or Arch package, the copied command must follow."""
    build = (ROOT / "packaging" / "build.sh").read_text(encoding="utf-8")
    pkgbuild = (ROOT / "packaging" / "arch" / "PKGBUILD").read_text(encoding="utf-8")

    deb = re.search(r"^Depends: .*$", build, re.MULTILINE).group(0)
    rpm = re.search(r"^Requires: .*$", build, re.MULTILINE).group(0)
    arch = re.search(r"_depends=\((.*?)\n\)", pkgbuild, re.DOTALL).group(1)
    for package in ("tesseract-ocr", "tesseract-ocr-osd"):
        assert re.search(rf"\b{package}\b", deb) and package in ocr._ENGINE_SETUP["apt"].split()
    for package in ("tesseract", "tesseract-osd"):
        assert re.search(rf"\b{package}\b", rpm) and package in ocr._ENGINE_SETUP["dnf"].split()
    for package in ("tesseract", "tesseract-data-eng", "tesseract-data-osd"):
        assert f"'{package}'" in arch and package in ocr._ENGINE_SETUP["pacman"].split()


def test_the_older_hints_in_error_messages_are_unchanged(monkeypatch):
    which_finds(monkeypatch, "pacman")
    assert ocr._install_hint() == "Run: sudo pacman -S tesseract"
    assert ocr._install_hint("ara") == "Run: sudo pacman -S tesseract-data-ara"
    assert ocr.osd_install_hint() == "Run: sudo pacman -S tesseract-data-osd"
    which_finds(monkeypatch)
    assert ocr._install_hint() == "Install Tesseract using whatever package manager your system provides."
