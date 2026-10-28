# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Windows start-up support. Windows behaviour is simulated so these run on every system."""

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from karamvt import winsupport

ARABIC = "اكتملت الإجابة: «ما هو الحاسوب؟»"


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(winsupport, "is_windows", lambda: True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(winsupport.GTK_DIR_ENV, raising=False)
    return tmp_path


def make_runtime(root: Path, schemas: bool = False) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "lib" / "girepository-1.0").mkdir(parents=True)
    (root / "share").mkdir()
    if schemas:
        (root / "share" / "glib-2.0" / "schemas").mkdir(parents=True)
        (root / "share" / "glib-2.0" / "schemas" / "gschemas.compiled").write_bytes(b"x")
    return root


# --- Finding the runtime ---

def test_an_explicit_folder_is_tried_first(monkeypatch, tmp_path):
    monkeypatch.setenv(winsupport.GTK_DIR_ENV, str(tmp_path / "mine"))
    assert winsupport.gtk_candidates()[0] == tmp_path / "mine"


def test_a_folder_is_a_runtime_only_with_binaries_and_typelibs(tmp_path):
    (tmp_path / "bin").mkdir()
    assert not winsupport.is_gtk_runtime(tmp_path)
    (tmp_path / "lib" / "girepository-1.0").mkdir(parents=True)
    assert winsupport.is_gtk_runtime(tmp_path)


def test_the_first_valid_candidate_is_used(monkeypatch, windows):
    runtime = make_runtime(windows / "gtk")
    monkeypatch.setenv(winsupport.GTK_DIR_ENV, str(windows / "missing"))
    monkeypatch.setattr(winsupport, "gtk_candidates", lambda: [windows / "missing", runtime])
    assert winsupport.find_gtk_runtime() == runtime


def test_the_installer_location_is_among_the_candidates(windows):
    assert windows / "data" / "karamvt" / "gtk" in winsupport.gtk_candidates()


def test_the_runtime_inside_the_installer_is_found_before_a_downloaded_one(monkeypatch, windows):
    # The installer puts it in <application folder>\gtk, beside the .venv, and nothing has to tell the application.
    monkeypatch.setattr(sys, "prefix", str(windows / "app" / ".venv"))
    candidates = winsupport.gtk_candidates()
    assert candidates.index(windows / "app" / "gtk") < candidates.index(windows / "data" / "karamvt" / "gtk")


def test_an_explicit_folder_still_wins_over_the_runtime_inside_the_installer(monkeypatch, windows):
    monkeypatch.setattr(sys, "prefix", str(windows / "app" / ".venv"))
    monkeypatch.setenv(winsupport.GTK_DIR_ENV, str(windows / "mine"))
    assert winsupport.gtk_candidates()[:2] == [windows / "mine", windows / "app" / "gtk"]


# --- Configuring the process ---

def test_configuring_makes_dlls_typelibs_and_data_visible(monkeypatch, tmp_path):
    runtime = make_runtime(tmp_path / "gtk", schemas=True)
    added = []
    monkeypatch.setattr(os, "add_dll_directory", added.append, raising=False)
    for name in ("PATH", "GI_TYPELIB_PATH", "XDG_DATA_DIRS", "GSETTINGS_SCHEMA_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "other")

    winsupport.configure_gtk(runtime)

    assert added == [str(runtime / "bin")]
    assert os.environ["PATH"].split(os.pathsep) == [str(runtime / "bin"), "other"]
    assert os.environ["GI_TYPELIB_PATH"] == str(runtime / "lib" / "girepository-1.0")
    assert os.environ["XDG_DATA_DIRS"] == str(runtime / "share")
    assert os.environ["GSETTINGS_SCHEMA_DIR"] == str(runtime / "share" / "glib-2.0" / "schemas")


def test_configuring_twice_does_not_repeat_entries(monkeypatch, tmp_path):
    runtime = make_runtime(tmp_path / "gtk")
    monkeypatch.setattr(os, "add_dll_directory", lambda path: None, raising=False)
    monkeypatch.setenv("PATH", "other")
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)
    monkeypatch.delenv("GSETTINGS_SCHEMA_DIR", raising=False)
    winsupport.configure_gtk(runtime)
    winsupport.configure_gtk(runtime)
    assert os.environ["PATH"].split(os.pathsep).count(str(runtime / "bin")) == 1
    assert "GSETTINGS_SCHEMA_DIR" not in os.environ  # No compiled schemas in this runtime.


# --- The Visual C++ runtime that the installer carries ---

RUNTIME_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll", "concrt140.dll")


def make_vcruntime(folder: Path, names=RUNTIME_DLLS) -> Path:
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"x")
    (folder / "NOTICE.txt").write_text("not a library", encoding="utf-8")
    return folder


def test_the_runtime_dlls_that_came_with_the_installer_are_found_beside_the_program(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))  # <install dir>\.venv
    assert winsupport.find_vcruntime() is None
    (tmp_path / "vcruntime").mkdir()
    # A folder without libraries is no runtime.
    assert winsupport.find_vcruntime() is None
    folder = make_vcruntime(tmp_path / "other" / "vcruntime")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "other" / ".venv"))
    assert winsupport.find_vcruntime() == folder


def test_configuring_loads_every_runtime_dll_in_order_and_makes_the_folder_known(monkeypatch, tmp_path):
    folder = make_vcruntime(tmp_path / "vcruntime")
    loaded, added = [], []
    monkeypatch.setattr(winsupport, "_load_library", lambda path: loaded.append(path))
    monkeypatch.setattr(os, "add_dll_directory", added.append, raising=False)
    monkeypatch.setenv("PATH", "other")
    names = winsupport.configure_vcruntime(folder)
    # msvcp140.dll has to be there before msvcp140_1.dll, which imports it.
    assert names == ["concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll"]
    assert [path.name for path in loaded] == names and all(path.parent == folder for path in loaded)  # By full path.
    assert added == [str(folder)]
    assert os.environ["PATH"].split(os.pathsep) == [str(folder), "other"]


def test_a_runtime_dll_that_cannot_be_loaded_is_skipped_and_the_others_still_are(monkeypatch, tmp_path):
    folder = make_vcruntime(tmp_path / "vcruntime")
    tried = []

    def load(path):
        tried.append(path.name)
        if path.name == "msvcp140_1.dll":
            raise OSError("not a valid Win32 application")

    monkeypatch.setattr(winsupport, "_load_library", load)
    monkeypatch.setattr(os, "add_dll_directory", lambda path: None, raising=False)
    assert winsupport.configure_vcruntime(folder) == ["concrt140.dll", "msvcp140.dll", "vcomp140.dll"]
    assert tried == ["concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll"]


def test_a_folder_that_windows_will_not_register_does_not_stop_the_libraries_from_being_loaded(monkeypatch, tmp_path):
    folder = make_vcruntime(tmp_path / "vcruntime")
    loaded = []

    def refuse(path):
        raise OSError("refused")

    monkeypatch.setattr(os, "add_dll_directory", refuse, raising=False)
    monkeypatch.setattr(winsupport, "_load_library", lambda path: loaded.append(path.name))
    assert winsupport.configure_vcruntime(folder) == loaded == [
        "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "vcomp140.dll"
    ]


def test_python_own_runtime_is_never_loaded_a_second_time(monkeypatch, tmp_path):
    # vcruntime140.dll is already in the process (python.exe loaded it from the Python folder); a second copy would be a
    # second C runtime. The build leaves it out (scripts/vcruntime-bundle.ps1); this makes sure nothing here loads one.
    folder = make_vcruntime(tmp_path / "vcruntime", (*RUNTIME_DLLS, "vcruntime140.dll", "vcruntime140_1.dll"))
    loaded = []
    monkeypatch.setattr(winsupport, "_load_library", lambda path: loaded.append(path.name))
    monkeypatch.setattr(os, "add_dll_directory", lambda path: None, raising=False)
    winsupport.configure_vcruntime(folder)
    assert not [name for name in loaded if name.startswith("vcruntime")]


def test_prepare_loads_the_installers_runtime_before_gtk_is_set_up(monkeypatch, windows):
    folder = make_vcruntime(windows / "vcruntime")
    runtime = make_runtime(windows / "gtk")
    steps = []
    monkeypatch.setattr(winsupport, "ensure_standard_streams", lambda: steps.append("streams"))
    monkeypatch.setattr(winsupport, "find_vcruntime", lambda: folder)
    monkeypatch.setattr(winsupport, "configure_vcruntime", lambda path: steps.append(("vcruntime", path)))
    monkeypatch.setattr(winsupport, "find_gtk_runtime", lambda: runtime)
    monkeypatch.setattr(winsupport, "configure_gtk", lambda gtk: steps.append(("gtk", gtk)))
    monkeypatch.setattr(winsupport, "set_app_user_model_id", lambda: steps.append("identity"))
    winsupport.prepare_runtime()
    assert steps == ["streams", ("vcruntime", folder), ("gtk", runtime), "identity"]


def test_prepare_without_the_installers_runtime_loads_nothing(monkeypatch, windows):
    monkeypatch.setattr(winsupport, "ensure_standard_streams", lambda: None)
    monkeypatch.setattr(winsupport, "find_vcruntime", lambda: None)
    monkeypatch.setattr(winsupport, "configure_vcruntime", lambda path: pytest.fail("there is nothing to load"))
    monkeypatch.setattr(winsupport, "find_gtk_runtime", lambda: None)
    monkeypatch.setattr(winsupport, "set_app_user_model_id", lambda: None)
    winsupport.prepare_runtime()


@pytest.mark.skipif(sys.platform != "win32", reason="loads a real Windows library")
def test_on_windows_a_library_is_loaded_by_its_full_path_and_kept(tmp_path):
    version_dll = Path(os.environ["SystemRoot"]) / "System32" / "version.dll"
    before = len(winsupport._loaded_runtime)
    winsupport._load_library(version_dll)
    assert len(winsupport._loaded_runtime) == before + 1
    with pytest.raises(OSError):
        winsupport._load_library(tmp_path / "missing.dll")


def test_prepare_does_nothing_outside_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(winsupport, "is_windows", lambda: False)
    monkeypatch.setattr(winsupport, "find_gtk_runtime", lambda: pytest.fail("must not search"))
    before = dict(os.environ)
    winsupport.prepare_runtime()
    assert dict(os.environ) == before


def test_prepare_on_windows_sets_up_streams_runtime_and_identity(monkeypatch, windows):
    runtime = make_runtime(windows / "gtk")
    steps = []
    monkeypatch.setattr(winsupport, "ensure_standard_streams", lambda: steps.append("streams"))
    monkeypatch.setattr(winsupport, "find_gtk_runtime", lambda: runtime)
    monkeypatch.setattr(winsupport, "configure_gtk", lambda gtk: steps.append(("gtk", gtk)))
    monkeypatch.setattr(winsupport, "set_app_user_model_id", lambda: steps.append("identity"))
    winsupport.prepare_runtime()
    assert steps == ["streams", ("gtk", runtime), "identity"]


def test_prepare_still_works_when_no_runtime_is_found(monkeypatch, windows):
    monkeypatch.setattr(winsupport, "ensure_standard_streams", lambda: None)
    monkeypatch.setattr(winsupport, "find_gtk_runtime", lambda: None)
    monkeypatch.setattr(winsupport, "configure_gtk", lambda gtk: pytest.fail("no runtime to configure"))
    monkeypatch.setattr(winsupport, "set_app_user_model_id", lambda: None)
    winsupport.prepare_runtime()


def test_a_process_without_a_console_gets_streams_and_a_log_file(monkeypatch, windows):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    winsupport.ensure_standard_streams()
    try:
        print("hello")  # Would raise without a stdout.
        print(ARABIC, file=sys.stderr)
        sys.stderr.flush()
        log = windows / "data" / "karamvt" / "karamvt.log"
        assert ARABIC in log.read_text(encoding="utf-8")
    finally:
        sys.stdout.close()
        sys.stderr.close()


def test_existing_streams_are_left_alone(monkeypatch, windows, capsys):
    before = (sys.stdout, sys.stderr)
    winsupport.ensure_standard_streams()
    assert (sys.stdout, sys.stderr) == before


# --- Missing toolkit ---

def test_a_missing_toolkit_is_reported_with_the_way_out(monkeypatch, capsys):
    monkeypatch.setattr(winsupport, "is_windows", lambda: False)
    winsupport.report_missing_toolkit(ImportError("DLL load failed"))
    error = capsys.readouterr().err
    assert "DLL load failed" in error and "install-windows.ps1" in error and winsupport.GTK_DIR_ENV in error


# --- Notifications ---

def decode(command):
    return base64.b64decode(command[command.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_the_toast_script_escapes_xml_and_powershell_quotes():
    script = winsupport.toast_script("It's <ready> & done", "a 'quoted' <b>", app_id="x.y")
    assert "It''s &lt;ready&gt; &amp; done" in script
    assert "a ''quoted'' &lt;b&gt;" in script
    assert "CreateToastNotifier('x.y')" in script


def test_arabic_text_survives_the_encoded_command():
    script = winsupport.toast_script("كرم", ARABIC)
    command = winsupport.powershell_command(script)
    assert command[0] == "powershell.exe" and "-NonInteractive" in command
    assert decode(command) == script and ARABIC in decode(command)


def test_no_toast_is_attempted_outside_windows(monkeypatch):
    monkeypatch.setattr(winsupport, "is_windows", lambda: False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not start a process"))
    assert winsupport.send_toast("t", "b") is False


def test_a_toast_registers_the_identity_and_starts_powershell(monkeypatch, windows):
    started, registered = [], []
    monkeypatch.setattr(winsupport, "register_toast_app", lambda: registered.append(True))
    monkeypatch.setattr(subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)))
    assert winsupport.send_toast("كرم", ARABIC) is True
    assert registered == [True]
    command, kwargs = started[0]
    assert ARABIC in decode(command) and kwargs["creationflags"] == winsupport._CREATE_NO_WINDOW


def test_a_toast_that_cannot_be_shown_reports_failure_instead_of_raising(monkeypatch, windows):
    monkeypatch.setattr(winsupport, "register_toast_app", lambda: None)

    def fail(*args, **kwargs):
        raise OSError("no powershell")

    monkeypatch.setattr(subprocess, "Popen", fail)
    assert winsupport.send_toast("t", "b") is False


# --- Icon ---

def test_the_windows_icon_ships_with_the_sources():
    icon = winsupport.icon_file()
    assert icon is not None and icon.name == "karamvt.ico"
    assert icon.read_bytes()[:4] == b"\x00\x00\x01\x00"  # An ICO header with at least one image.


def test_setting_the_icon_is_a_no_op_outside_windows(monkeypatch):
    monkeypatch.setattr(winsupport, "is_windows", lambda: False)
    winsupport.apply_icon_to_own_windows()
