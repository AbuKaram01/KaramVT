# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Windows-only start-up work: locating the GTK runtime, console-less launches, notifications, icons.

Every public function is a harmless no-op on other systems, and nothing here imports GTK, so the
module can be imported (and tested) anywhere. ``prepare_runtime`` must run *before* ``import gi``;
``karamvt.gui`` calls it from its package initializer, which is the first thing any entry point
imports.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import APP_ID
from .core.paths import data_dir, is_windows, log_file

GTK_DIR_ENV = "KARAMVT_GTK_DIR"
_ICON_NAME = "karamvt.ico"
_CREATE_NO_WINDOW = 0x08000000


# --- The GTK runtime ---

def gtk_candidates() -> list[Path]:
    """Folders that may hold a Windows GTK 4 runtime (as produced by gvsbuild), best guess first."""
    candidates: list[Path] = []
    explicit = os.environ.get(GTK_DIR_ENV)
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        Path(sys.prefix).parent / "gtk",                 # <install dir>\venv next to <install dir>\gtk
        data_dir() / "gtk",                              # Where scripts/install-windows.ps1 puts it.
        Path("C:/gtk"),                                  # gvsbuild's documented location for the prebuilt zip.
        Path("C:/gtk-build/gtk/x64/release"),            # gvsbuild's own build output.
    ]
    return candidates


def is_gtk_runtime(folder: Path) -> bool:
    """Whether ``folder`` looks like a GTK 4 runtime: it has a ``bin`` folder and the GObject typelibs."""
    return (folder / "bin").is_dir() and (folder / "lib" / "girepository-1.0").is_dir()


def find_gtk_runtime() -> Path | None:
    """The first of ``gtk_candidates()`` that is a GTK runtime, or None when there is none."""
    return next((folder for folder in gtk_candidates() if is_gtk_runtime(folder)), None)


def _prepend(variable: str, value: str) -> None:
    """Put ``value`` at the front of the path-list environment variable ``variable``, unless already in it."""
    current = os.environ.get(variable)
    parts = current.split(os.pathsep) if current else []
    if value not in parts:
        os.environ[variable] = os.pathsep.join([value, *parts])


def configure_gtk(gtk: Path) -> None:
    """Make the DLLs, typelibs, schemas and icons of ``gtk`` visible to this process.

    Since Python 3.8 Windows no longer searches ``PATH`` for the DLLs of extension modules, so the
    ``bin`` folder is registered with ``os.add_dll_directory`` as well as put on ``PATH`` (child
    processes and GLib's own module loading still use ``PATH``).
    """
    binaries = gtk / "bin"
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        add_dll_directory(str(binaries))
    _prepend("PATH", str(binaries))
    _prepend("GI_TYPELIB_PATH", str(gtk / "lib" / "girepository-1.0"))
    if (gtk / "share").is_dir():
        _prepend("XDG_DATA_DIRS", str(gtk / "share"))
    schemas = gtk / "share" / "glib-2.0" / "schemas"
    if (schemas / "gschemas.compiled").is_file():
        os.environ.setdefault("GSETTINGS_SCHEMA_DIR", str(schemas))


# --- The Visual C++ runtime ---

# Handles of the runtime DLLs loaded by configure_vcruntime(), kept for as long as the process lives.
_loaded_runtime: list[object] = []


def find_vcruntime() -> Path | None:
    """The folder of Visual C++ runtime DLLs that the installer carries (``vcruntime`` beside the program), if any."""
    # sys.prefix is <install dir>\.venv, and the DLLs are in <install dir>\vcruntime.
    folder = Path(sys.prefix).parent / "vcruntime"
    return folder if folder.is_dir() and any(folder.glob("*.dll")) else None


def _load_library(path: Path) -> None:
    import ctypes

    # Pass the full path: Windows then also looks beside the DLL for the libraries it depends on.
    _loaded_runtime.append(ctypes.WinDLL(str(path)))


def configure_vcruntime(folder: Path) -> list[str]:
    """Make this process use the Visual C++ runtime DLLs of ``folder`` (``msvcp140.dll``, ``vcomp140.dll`` ...).

    ctranslate2, onnxruntime and llama.cpp import ``msvcp140.dll``, which only a computer that has the Visual C++
    Redistributable has; the installer carries copies. They are loaded here, before any of those libraries is
    imported: a library loaded later finds a DLL of that name already in the process and uses it, so the copy that
    came with KaramVT is the one that runs, on a computer with or without the Redistributable (and one with an older
    Redistributable cannot give a library a runtime that is too old for it).

    The folder is also made known for DLLs that are looked up later and for programs started from here. Python's
    own ``vcruntime140.dll`` is not part of it: python.exe already has it loaded.

    Returns:
        The names of the DLLs that were loaded. A DLL that cannot be loaded is skipped, leaving the system to
        supply it (or not).
    """
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        try:
            add_dll_directory(str(folder))
        except OSError:
            pass
    _prepend("PATH", str(folder))
    loaded: list[str] = []
    # Sorted by name so that msvcp140.dll is loaded before msvcp140_1.dll.
    for dll in sorted(folder.glob("*.dll"), key=lambda path: path.name.lower()):
        if dll.name.lower().startswith("vcruntime"):
            # python.exe already has its own copy loaded; a second one would be a second C runtime.
            continue
        try:
            _load_library(dll)
        except OSError:
            continue
        loaded.append(dll.name)
    return loaded


# --- Process set-up ---

def ensure_standard_streams() -> None:
    """Give a console-less process (``pythonw.exe``) working ``stdout`` and ``stderr``.

    Without them any ``print`` or warning raises, and libraries that write to ``sys.stderr`` crash.
    Errors go to a log file so a user can still report them.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")                      # noqa: SIM115
    if sys.stderr is None:
        try:
            log_file().parent.mkdir(parents=True, exist_ok=True)
            sys.stderr = open(log_file(), "a", encoding="utf-8", buffering=1)     # noqa: SIM115
        except OSError:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")                  # noqa: SIM115


def set_app_user_model_id() -> None:
    """Give the process its own taskbar identity instead of sharing python.exe's. Errors are ignored."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)     # type: ignore[attr-defined]
    except Exception:
        pass


def prepare_runtime() -> None:
    """Do everything that must happen before GTK is imported; a no-op off Windows.

    Safe to call more than once.
    """
    if not is_windows():
        return
    ensure_standard_streams()
    vcruntime = find_vcruntime()
    if vcruntime is not None:
        configure_vcruntime(vcruntime)
    gtk = find_gtk_runtime()
    if gtk is not None:
        configure_gtk(gtk)
    set_app_user_model_id()


def report_missing_toolkit(error: BaseException) -> None:
    """Tell the user that GTK could not be loaded; a window-less launch has nowhere else to say it."""
    text = (
        "KaramVT could not load GTK 4 and libadwaita.\n\n"
        f"{error}\n\n"
        "Run scripts\\install-windows.ps1 to install them, or set the KARAMVT_GTK_DIR environment "
        "variable to a GTK 4 runtime folder (see the Windows section of the README)."
    )
    print(text, file=sys.stderr)
    if is_windows():
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "KaramVT", 0x10)       # type: ignore[attr-defined]
        except Exception:
            pass


# --- Icon ---

def icon_file() -> Path | None:
    """The multi-size ``.ico`` of the application, when it is present (a checkout or an installation)."""
    for base in (Path(__file__).resolve().parents[2], Path(sys.prefix).parent):
        candidate = base / "data" / "windows" / _ICON_NAME
        if candidate.is_file():
            return candidate
    return None


def apply_icon_to_own_windows() -> None:
    """Best effort: set the title-bar and taskbar icon of this process's visible top-level windows.

    GTK 4 has no icon API on Windows, so the window would show python's icon. Failure is silent:
    the application works the same without it.
    """
    icon = icon_file()
    if not is_windows() or icon is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32                                             # type: ignore[attr-defined]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        # Win32 constants: IMAGE_ICON, LR_LOADFROMFILE and WM_SETICON.
        image_icon, load_from_file, wm_seticon = 1, 0x10, 0x80
        # GetSystemMetrics indices 11/12 and 49/50 are SM_CXICON/SM_CYICON and SM_CXSMICON/SM_CYSMICON: the
        # sizes the shell uses for the large (taskbar, Alt+Tab) and the small (title bar) icon.
        big = user32.LoadImageW(None, str(icon), image_icon, user32.GetSystemMetrics(11), user32.GetSystemMetrics(12),
                                load_from_file)
        small = user32.LoadImageW(None, str(icon), image_icon, user32.GetSystemMetrics(49),
                                  user32.GetSystemMetrics(50), load_from_file)
        own_pid = os.getpid()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            # EnumWindows callback; returning True continues the enumeration.
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid and user32.IsWindowVisible(hwnd):
                # WM_SETICON's wParam selects the slot: 1 = ICON_BIG, 0 = ICON_SMALL.
                if big:
                    user32.SendMessageW(hwnd, wm_seticon, 1, big)
                if small:
                    user32.SendMessageW(hwnd, wm_seticon, 0, small)
            return True

        user32.EnumWindows(visit, 0)
    except Exception:
        pass


# --- Notifications ---
#
# GLib's Gio.Notification has no Windows backend, so "your answer is ready" would never appear.
# A toast is raised through Windows PowerShell 5.1 (it ships with Windows 10 and 11 and projects the
# WinRT toast API), which needs no extra Python package.

def toast_script(title: str, body: str, app_id: str = APP_ID) -> str:
    """The PowerShell program that shows one toast. Text is XML-escaped, then quoted for PowerShell."""
    def quote(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"

    xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(title)}</text><text>{escape(body)}</text>"
        "</binding></visual></toast>"
    )
    manager = "Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications"
    xml_document = "Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument"
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"[{manager}, ContentType = WindowsRuntime] | Out-Null",
        f"[{xml_document}, ContentType = WindowsRuntime] | Out-Null",
        "$document = New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$document.LoadXml({quote(xml)})",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($document)",
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({quote(app_id)}).Show($toast)",
    ])


def powershell_command(script: str) -> list[str]:
    """The ``powershell.exe`` arguments that run ``script`` without quoting problems.

    The script is passed as base64 of its UTF-16LE bytes, the encoding that ``-EncodedCommand`` expects.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
    ]


def register_toast_app(display_name: str = "KaramVT") -> None:
    """Register the application's identity so toasts carry its name and icon (per user, no admin)."""
    import winreg  # Windows only.

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        icon = icon_file()
        if icon is not None:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))


def send_toast(title: str, body: str) -> bool:
    """Show a Windows toast.

    Returns:
        False when it could not even be attempted (not Windows, the registration failed, or PowerShell could not
        be started). True only means that the PowerShell process was started; the toast is raised asynchronously
        and its outcome is not checked.
    """
    if not is_windows():
        return False
    try:
        register_toast_app()
        subprocess.Popen(
            powershell_command(toast_script(title, body)),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return False
    return True
