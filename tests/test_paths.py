# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user locations on Linux and on Windows (the Windows branch is exercised from any system)."""

import pytest

from karamvt.core import paths


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Remove the variables that locate per-user folders and point the home folder at a temporary one; return it."""
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows; setting both keeps the tests independent of the system
    # they run on.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return tmp_path / "home"


def as_windows(monkeypatch):
    """Make the paths module behave as on Windows, whatever system the tests run on."""
    monkeypatch.setattr(paths, "is_windows", lambda: True)


def test_linux_uses_the_xdg_defaults_under_home(monkeypatch, clean_env):
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    assert paths.config_dir() == clean_env / ".config" / "karamvt"
    assert paths.data_dir() == clean_env / ".local" / "share" / "karamvt"
    assert paths.state_dir() == clean_env / ".local" / "state" / "karamvt"


def test_windows_keeps_settings_in_roaming_and_data_and_state_in_local_appdata(monkeypatch, clean_env, tmp_path):
    as_windows(monkeypatch)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert paths.config_dir() == tmp_path / "Roaming" / "karamvt"
    assert paths.data_dir() == tmp_path / "Local" / "karamvt"
    assert paths.state_dir() == tmp_path / "Local" / "karamvt" / "state"
    assert paths.default_models_dir() == tmp_path / "Local" / "karamvt" / "models"
    assert paths.log_file() == tmp_path / "Local" / "karamvt" / "karamvt.log"


def test_windows_falls_back_to_the_profile_when_the_variables_are_missing(monkeypatch, clean_env):
    as_windows(monkeypatch)
    assert paths.config_dir() == clean_env / "AppData" / "Roaming" / "karamvt"
    assert paths.data_dir() == clean_env / "AppData" / "Local" / "karamvt"


@pytest.mark.parametrize("variable, function", [
    ("XDG_CONFIG_HOME", paths.config_dir), ("XDG_DATA_HOME", paths.data_dir), ("XDG_STATE_HOME", paths.state_dir),
])
def test_an_explicit_xdg_variable_wins_on_windows_too(monkeypatch, clean_env, tmp_path, variable, function):
    as_windows(monkeypatch)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv(variable, str(tmp_path / "portable"))
    assert function() == tmp_path / "portable" / "karamvt"


def test_every_module_that_needs_a_folder_asks_paths_for_it(monkeypatch, clean_env, tmp_path):
    """Guard against the OCR language folder and the resume folder computing the XDG location themselves again."""
    from karamvt.core import checkpoint, tessdata_manager

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert checkpoint.default_root() == tmp_path / "data" / "karamvt" / "resume"
    assert str(tessdata_manager.APP_DATA_DIR).endswith("karamvt")
