# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user filesystem locations.

Linux follows the XDG Base Directory specification. Windows uses ``%APPDATA%`` for settings and
``%LOCALAPPDATA%`` for the (large, machine-specific) data and state. On either system an explicit
``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` / ``XDG_STATE_HOME`` wins, which also makes a portable setup possible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "karamvt"


def is_windows() -> bool:
    """Return whether the program runs on Windows."""
    return sys.platform == "win32"


def _windows_base(variable: str, fallback: tuple[str, ...]) -> Path:
    """Return the folder named by the environment variable, or ``fallback`` below the home folder if it is unset."""
    value = os.environ.get(variable)
    return Path(value) if value else Path.home().joinpath(*fallback)


def config_dir() -> Path:
    """Return the per-user configuration directory (``config.json``)."""
    explicit = os.environ.get("XDG_CONFIG_HOME")
    if explicit:
        return Path(explicit) / APP_NAME
    if is_windows():
        return _windows_base("APPDATA", ("AppData", "Roaming")) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def data_dir() -> Path:
    """Return the per-user data directory (downloaded models, OCR language data, saved progress)."""
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit) / APP_NAME
    if is_windows():
        return _windows_base("LOCALAPPDATA", ("AppData", "Local")) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def installed_data_dir() -> Optional[Path]:
    r"""Return ``%LOCALAPPDATA%\karamvt``, where scripts/install-windows.ps1 unpacks what it installs for KaramVT.

    That is the portable Tesseract. The script uses this folder whatever ``XDG_DATA_HOME`` says, so it can differ
    from :func:`data_dir`.

    Returns:
        The folder, or None when ``LOCALAPPDATA`` is not set (never the case on Windows).
    """
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) / APP_NAME if value else None


def state_dir() -> Path:
    """Return the per-user state directory (what the window looked like last time), kept apart from the data."""
    explicit = os.environ.get("XDG_STATE_HOME")
    if explicit:
        return Path(explicit) / APP_NAME
    if is_windows():
        return data_dir() / "state"
    return Path.home() / ".local" / "state" / APP_NAME


def default_models_dir() -> Path:
    """Return where downloaded models are stored unless the user chooses another folder."""
    return data_dir() / "models"


def log_file() -> Path:
    """Return the log file used when the process has no console (a Windows GUI launch)."""
    return data_dir() / "karamvt.log"
