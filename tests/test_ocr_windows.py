# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OCR on Windows: finding tesseract.exe, and reaching downloaded language data when paths hold spaces.

pytesseract splits its options string without understanding quotes on Windows, so the folder of the downloaded
language packs travels in TESSDATA_PREFIX there instead of in ``--tessdata-dir``. Windows is simulated, so these
run everywhere; a stand-in replaces pytesseract and records what it was asked.
"""

import os
import sys
import types
from pathlib import Path

import pytest

from karamvt.core import ocr, paths, tessdata_manager
from karamvt.core.languages import language_for_code


@pytest.fixture(autouse=True)
def no_windows_program_search(monkeypatch):
    """Windows' own search for a program called tesseract (the last resort) would find the one on a real Windows
    machine; the tests below are about the order of the places before it, and its own tests set what it finds."""
    monkeypatch.setattr(ocr, "_windows_program_search", lambda name: None)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(ocr, "is_windows", lambda: True)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(ocr, "is_windows", lambda: False)


# --- Finding the program ---

def test_windows_looks_where_the_installer_puts_tesseract(monkeypatch, windows, tmp_path):
    program = tmp_path / "Program Files" / "Tesseract-OCR" / "tesseract.exe"
    program.parent.mkdir(parents=True)
    program.write_text("")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert ocr.find_tesseract() == str(program)


def test_a_program_on_path_wins_over_the_installer_folder(monkeypatch, windows, tmp_path):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "C:/tools/tesseract.exe")
    assert ocr.find_tesseract() == "C:/tools/tesseract.exe"


def test_windows_own_search_is_the_last_resort_so_the_row_finds_whatever_ocr_would(monkeypatch, windows, tmp_path):
    """pytesseract runs a plain "tesseract" when it is not told where the program is, and Windows finds it by its
    own rules (the application's folder, system folders, PATH read its way). Whatever that finds, the "is Tesseract
    installed" row must find too, or it asks for an installation of a program that is working."""
    asked = []
    monkeypatch.setattr(
        ocr, "_windows_program_search", lambda name: asked.append(name) or "C:/Strange Place/tesseract.exe"
    )
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr, "_bundled_tesseract_candidates", lambda: [str(tmp_path / "nothing.exe")])
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert ocr.find_tesseract() == "C:/Strange Place/tesseract.exe" and asked == ["tesseract"]

    # Anything earlier wins, unasked.
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "C:/tools/tesseract.exe")
    asked.clear()
    assert ocr.find_tesseract() == "C:/tools/tesseract.exe" and asked == []


def test_windows_own_search_is_not_used_outside_windows(monkeypatch, linux):
    monkeypatch.setattr(ocr, "_windows_program_search", lambda name: pytest.fail("asked outside Windows"))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.find_tesseract() is None


@pytest.mark.skipif(sys.platform != "win32", reason="the search is Windows' own")
def test_windows_own_search_finds_a_program_the_way_windows_does(monkeypatch):
    # The real thing, not the stand-in of the autouse fixture.
    monkeypatch.undo()
    found = ocr._windows_program_search("cmd")
    assert found and found.lower().endswith("cmd.exe") and os.path.isfile(found)
    assert ocr._windows_program_search("no-such-program-karamvt-test") is None


def test_the_installer_folder_is_not_searched_outside_windows(monkeypatch, linux, tmp_path):
    program = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    program.parent.mkdir()
    program.write_text("")
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert ocr.find_tesseract() is None


def test_pytesseract_is_pointed_at_the_program_only_on_windows(monkeypatch, windows):
    monkeypatch.setattr(ocr, "find_tesseract", lambda: "C:/Program Files/Tesseract-OCR/tesseract.exe")
    module = types.SimpleNamespace(pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"))
    ocr._use_found_tesseract(module)
    assert module.pytesseract.tesseract_cmd == "C:/Program Files/Tesseract-OCR/tesseract.exe"

    monkeypatch.setattr(ocr, "is_windows", lambda: False)
    module = types.SimpleNamespace(pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"))
    ocr._use_found_tesseract(module)
    assert module.pytesseract.tesseract_cmd == "tesseract"


def test_winget_is_the_install_command_on_windows(monkeypatch):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "winget.exe" if name == "winget" else None)
    assert ocr.engine_install_command() == "winget install UB-Mannheim.TesseractOCR"
    assert "winget install UB-Mannheim.TesseractOCR" in ocr._install_hint()


# --- KaramVT's own copy of tesseract.exe ---

@pytest.fixture
def own_copy(monkeypatch, tmp_path):
    """A tesseract.exe where the installer (or the install script) puts KaramVT's own copy."""
    exe = tmp_path / "app" / "tesseract" / "tesseract.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(ocr, "_bundled_tesseract_candidates", lambda: [str(tmp_path / "missing.exe"), str(exe)])
    return str(exe)


def test_the_copy_that_ships_with_karamvt_wins_over_everything_else(monkeypatch, windows, own_copy):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "C:/tools/tesseract.exe")  # Another one is on PATH.
    assert ocr.find_tesseract() == own_copy


def test_without_an_own_copy_the_old_order_still_applies(monkeypatch, windows, tmp_path):
    monkeypatch.setattr(ocr, "_bundled_tesseract_candidates", lambda: [str(tmp_path / "nothing.exe")])
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "C:/tools/tesseract.exe")
    assert ocr.find_tesseract() == "C:/tools/tesseract.exe"


def test_an_own_copy_is_not_looked_for_outside_windows(monkeypatch, linux, own_copy):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.find_tesseract() is None


def test_the_own_copy_sits_beside_the_program_and_in_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "data_dir", lambda: tmp_path / "data")
    # These two come first on every system; on Windows the install script's folder follows them (see below).
    program_side, data_side = ocr._bundled_tesseract_candidates()[:2]
    assert program_side.endswith(os.path.join("tesseract", "tesseract.exe"))
    assert Path(program_side).parent.parent == Path(ocr.__file__).resolve().parents[3]  # The folder holding src/.
    assert data_side == str(tmp_path / "data" / "tesseract" / "tesseract.exe")


# scripts/install-windows.ps1 unpacks the copy into %LOCALAPPDATA%\karamvt whatever XDG_DATA_HOME says, so that
# folder is looked in too (the CI job that reads a picture through a language folder with a space in its path
# sets XDG_DATA_HOME, and the program must still be found).

def test_the_copy_the_install_script_unpacked_is_found_when_xdg_data_home_points_elsewhere(
    monkeypatch, windows, tmp_path
):
    exe = tmp_path / "Local" / "karamvt" / "tesseract" / "tesseract.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "Ali Hassan"))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.find_tesseract() == str(exe)


def test_a_copy_in_the_xdg_data_folder_wins_over_the_install_script_folder(monkeypatch, windows, tmp_path):
    for base in ("Local", "Ali Hassan"):
        exe = tmp_path / base / "karamvt" / "tesseract" / "tesseract.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "Ali Hassan"))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    assert ocr.find_tesseract() == str(tmp_path / "Ali Hassan" / "karamvt" / "tesseract" / "tesseract.exe")


def test_the_install_script_folder_is_the_last_of_the_own_copies(monkeypatch, windows, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "Ali Hassan"))
    program_side, data_side, installed_side = ocr._bundled_tesseract_candidates()
    assert data_side == str(tmp_path / "Ali Hassan" / "karamvt" / "tesseract" / "tesseract.exe")
    assert installed_side == str(tmp_path / "Local" / "karamvt" / "tesseract" / "tesseract.exe")


def test_one_data_folder_is_listed_once(monkeypatch, windows, tmp_path):
    monkeypatch.setattr(paths, "is_windows", lambda: True)  # So the data folder is %LOCALAPPDATA%\karamvt too.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    candidates = ocr._bundled_tesseract_candidates()
    assert candidates[1:] == [str(tmp_path / "Local" / "karamvt" / "tesseract" / "tesseract.exe")]


def test_there_is_no_install_script_folder_without_localappdata(monkeypatch, windows, tmp_path):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert len(ocr._bundled_tesseract_candidates()) == 2


def test_the_install_script_folder_is_not_looked_in_outside_windows(monkeypatch, linux, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert len(ocr._bundled_tesseract_candidates()) == 2


def test_pytesseract_is_pointed_at_the_own_copy(monkeypatch, windows, own_copy):
    module = types.SimpleNamespace(pytesseract=types.SimpleNamespace(tesseract_cmd="tesseract"))
    ocr._use_found_tesseract(module)
    assert module.pytesseract.tesseract_cmd == own_copy


# --- The language folder ---

@pytest.fixture
def packs(monkeypatch, tmp_path):
    """A downloaded-language folder whose path holds a space, as a Windows user name often does."""
    folder = tmp_path / "Ali Hassan" / "tessdata" / "fast"
    folder.mkdir(parents=True)
    monkeypatch.setattr(tessdata_manager, "local_tessdata_dir", lambda quality: str(folder))
    monkeypatch.setattr(tessdata_manager, "has_osd", lambda quality: True)
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    return folder


def test_windows_passes_the_language_folder_in_the_environment_and_restores_it(windows, packs, monkeypatch):
    with ocr._tessdata_environment("fast"):
        assert os.environ["TESSDATA_PREFIX"] == str(packs)
    assert "TESSDATA_PREFIX" not in os.environ

    monkeypatch.setenv("TESSDATA_PREFIX", "C:/previous")
    with ocr._tessdata_environment("fast"):
        assert os.environ["TESSDATA_PREFIX"] == str(packs)
    assert os.environ["TESSDATA_PREFIX"] == "C:/previous"


def test_the_environment_is_restored_even_when_ocr_fails(windows, packs):
    with pytest.raises(RuntimeError), ocr._tessdata_environment("fast"):
        raise RuntimeError("tesseract failed")
    assert "TESSDATA_PREFIX" not in os.environ


def test_the_system_language_source_leaves_the_environment_alone(windows, packs):
    with ocr._tessdata_environment("system"):
        assert "TESSDATA_PREFIX" not in os.environ


def test_linux_never_touches_the_environment(linux, packs):
    with ocr._tessdata_environment("fast"):
        assert "TESSDATA_PREFIX" not in os.environ


def test_the_folder_option_is_dropped_on_windows_only(monkeypatch):
    monkeypatch.setattr(ocr, "is_windows", lambda: True)
    assert ocr._tessdata_option("C:/a b/tessdata") is None
    monkeypatch.setattr(ocr, "is_windows", lambda: False)
    assert ocr._tessdata_option("/a b/tessdata") == "/a b/tessdata"


# --- End to end, with a stand-in for pytesseract ---

class StandInTesseract(types.ModuleType):
    """Records the options string and the environment each call saw."""

    class TesseractNotFoundError(Exception):
        pass

    class TesseractError(Exception):
        pass

    class Output:
        DICT = "dict"

    def __init__(self):
        super().__init__("pytesseract")
        self.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")
        self.calls = []

    def image_to_string(self, image, lang=None, config=""):
        self.calls.append((lang, config, os.environ.get("TESSDATA_PREFIX")))
        return "نص عربي"

    def image_to_data(self, image, lang=None, config="", output_type=None):
        self.calls.append((lang, config, os.environ.get("TESSDATA_PREFIX")))
        return {"conf": ["90", "80"]}


@pytest.fixture
def stand_in(monkeypatch, tmp_path):
    module = StandInTesseract()
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    from PIL import Image

    image = tmp_path / "scan.png"
    Image.new("RGB", (20, 20), "white").save(image)
    return module, str(image)


def test_on_windows_a_language_folder_with_a_space_reaches_tesseract_unquoted(windows, packs, stand_in):
    module, image = stand_in
    result = ocr.OcrEngine(on_progress=lambda message: None).extract_text(
        image, language=language_for_code("ar"), language_source="fast",
    )
    assert result.text == "نص عربي"
    assert module.calls, "tesseract was never asked"
    for lang, config, prefix in module.calls:
        assert lang == "ara"
        assert "--tessdata-dir" not in config and "'" not in config and '"' not in config
        assert prefix == str(packs)
    assert "TESSDATA_PREFIX" not in os.environ


def test_on_linux_the_folder_still_goes_through_the_option(linux, packs, stand_in):
    module, image = stand_in
    ocr.OcrEngine(on_progress=lambda message: None).extract_text(
        image, language=language_for_code("ar"), language_source="fast",
    )
    for _lang, config, prefix in module.calls:
        assert "--tessdata-dir" in config and "Ali Hassan" in config
        assert prefix is None
