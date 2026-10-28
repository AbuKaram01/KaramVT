# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the tests of the installation files share.

That is where the project is, which tools a test needs, and how to make a stand-in program.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

ARCH = ROOT / "packaging" / "arch"

APP_ID = "io.github.AbuKaram01.KaramVT"

# The shell scripts and their stubs are POSIX-only (":"-separated PATH, /bin/sh), so they are skipped on Windows.
needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32", reason="needs bash on a POSIX system",
)

not_root = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="the installers refuse to run as root",
)

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) is not available")

WINDOWS = ROOT / "packaging" / "windows"


def make_executable(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, make the file executable and return the path."""
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def run_gtk_script(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in PowerShell with scripts/gtk-portable.ps1 loaded, and return the finished process."""
    script = tmp_path / "case.ps1"
    script.write_text(f". '{ROOT / 'scripts' / 'gtk-portable.ps1'}'\n{body}\n", encoding="utf-8")
    return subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, check=False)


# --- Python's installer and the wheelhouse inside the installer ---

BUNDLE = ROOT / "scripts" / "python-bundle.ps1"
