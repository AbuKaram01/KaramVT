# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fixtures shared by the whole test suite.

They keep every test away from the real user's settings, data and state folders and from the operating system's
notifications. The stand-ins for the speech and translation models live in ``model_stand_ins`` and are registered
below as a plugin.
"""

import pytest

from karamvt import config, winsupport

# Provides the fixtures `pipelines`, `fakes` and `text_translation` to the tests of the code that drives the models.
pytest_plugins = ["model_stand_ins"]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the saved settings at a temporary file, so a test never touches the real one."""
    folder = tmp_path / "config"
    monkeypatch.setattr(config, "APP_CONFIG_DIR", folder)
    monkeypatch.setattr(config, "APP_CONFIG_FILE", folder / "config.json")
    return folder / "config.json"


@pytest.fixture(autouse=True)
def private_user_folders(tmp_path, monkeypatch):
    """Redirect the user's data and state folders (translation history, saved window state) to a temporary one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    # The Windows install script unpacks a real tesseract.exe into %LOCALAPPDATA%\karamvt, and the program looks there
    # regardless of XDG_DATA_HOME (paths.installed_data_dir). Redirect it too, so that a machine where the installer
    # ran never lets a test find that copy.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))


@pytest.fixture(autouse=True)
def no_real_toasts(request, monkeypatch):
    """Replace the Windows toast with a no-op, so that no test runs PowerShell or writes to the registry.

    On Windows a finished job raises its toast through PowerShell and the registry. The tests of the toast itself
    (test_winsupport.py) keep the real function and replace the system calls instead.
    """
    if not request.module.__name__.endswith("test_winsupport"):
        monkeypatch.setattr(winsupport, "send_toast", lambda title, body: False)

