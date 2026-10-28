# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Starting tesseract.exe when the paths hold non-ASCII letters.

tesseract.exe cannot take paths with non-ASCII letters (a user name in Arabic puts them in TEMP, in the program's
folder and in the language-data folder). It is therefore started inside its working folder and given relative ASCII
names, short paths and a TESSDATA_PREFIX it can read.

Windows is simulated, so these run on every system that can start a POSIX script. The real thing is checked by the
windows-full-install job.
"""

import json
import os
import shutil
import stat
import sys
import tempfile

import pytest

from karamvt.core import ascii_paths, ocr
from karamvt.core.ascii_paths import ProgramNotFound, TesseractFailed, launchable, must_guard, run_tesseract
from karamvt.core.languages import language_for_code

ARABIC = "\u0623\u062d\u0645\u062f"                 # "Ahmad" in Arabic letters.
PICTURE_NAME = "\u0635\u0641\u062d\u0629.png"       # "page.png" with an Arabic file name.

needs_scripts = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in programs are POSIX scripts")


@pytest.fixture
def windows(monkeypatch):
    """Windows, without 8.3 names and without junctions unless a test says otherwise."""
    monkeypatch.setattr(ocr, "is_windows", lambda: True)
    monkeypatch.setattr(ascii_paths, "is_windows", lambda: True)
    monkeypatch.setattr(ascii_paths, "short_path", lambda path: None)
    monkeypatch.setattr(ascii_paths, "make_junction", lambda target, link: False)


@pytest.fixture
def arabic_temp(monkeypatch, tmp_path):
    """TEMP inside a folder whose name has Arabic letters, as with a user name in Arabic."""
    folder = tmp_path / ARABIC / "Temp"
    folder.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def link_to(target, link):
    """Stand in for make_junction with a symbolic link; a system that cannot make one skips the test."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links cannot be made here: {exc}")
    return True


# Stand-in for the tesseract program. It logs what it was given to $FAKE_LOG and writes the output file it was asked
# for. FAKE_MODE=fail makes it fail with an error message, and FAKE_MODE=silent makes it exit without writing anything.
FAKE_TESSERACT = """\
import json, os, sys

arguments = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({
        "arguments": arguments,
        "cwd": os.getcwd(),
        "tessdata": os.environ.get("TESSDATA_PREFIX"),
        "picture_is_png": open("input.png", "rb").read(8) == b"\\x89PNG\\r\\n\\x1a\\n",
        "tessdata_listing": (
            sorted(os.listdir(os.environ["TESSDATA_PREFIX"]))
            if os.environ.get("TESSDATA_PREFIX") and os.path.isdir(os.environ["TESSDATA_PREFIX"])
            else None
        ),
    }) + "\\n")

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "fail":
    sys.stderr.write("Error opening data file x.traineddata\\nFailed loading language 'xyz'\\n")
    sys.exit(1)
if mode == "silent":
    sys.exit(0)

output = arguments[1]
if "tessedit_create_tsv=1" in arguments:
    open(output + ".tsv", "w", encoding="utf-8").write(
        "level\\tconf\\ttext\\n5\\t90\\tHello\\n5\\t80\\tWorld\\n4\\t-1\\t\\n"
    )
elif "--psm" in arguments and arguments[arguments.index("--psm") + 1] == "0":
    open(output + ".osd", "w", encoding="utf-8").write(
        "Page number: 0\\nOrientation in degrees: 0\\nScript: Latin\\nScript confidence: 4.5\\n"
    )
else:
    open(output + ".txt", "w", encoding="utf-8").write("Hello World \\u0645\\u0631\\u062d\\u0628\\u0627\\n")
"""


@pytest.fixture
def fake_log(tmp_path, monkeypatch):
    """Point the stand-in program's log at a temporary file; return a function that reads the calls logged so far."""
    log = tmp_path / "fake-tesseract.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("FAKE_MODE", raising=False)

    def calls():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    return calls


def make_program(folder, name="tesseract"):
    """Write the stand-in program into ``folder`` as an executable script and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(f"#!{sys.executable}\n" + FAKE_TESSERACT)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def picture():
    """A small white picture to hand to Tesseract."""
    from PIL import Image

    return Image.new("RGB", (40, 20), "white")


# --- When it applies ---

def test_nothing_is_guarded_outside_windows(monkeypatch):
    monkeypatch.setattr(ascii_paths, "is_windows", lambda: False)
    monkeypatch.setattr(ascii_paths.tempfile, "gettempdir", lambda: f"/{ARABIC}/tmp")
    assert not must_guard(f"/{ARABIC}/tesseract", f"/{ARABIC}/tessdata")


def test_ascii_paths_are_not_guarded(windows, monkeypatch):
    monkeypatch.setattr(ascii_paths.tempfile, "gettempdir", lambda: "C:/Temp")
    assert not must_guard("C:/tools/tesseract.exe", "C:/data/tessdata")
    assert not must_guard("tesseract", None)


@pytest.mark.parametrize("program, tessdata, temp", [
    (f"C:/Users/{ARABIC}/tesseract.exe", None, "C:/Temp"),
    ("tesseract", f"C:/Users/{ARABIC}/tessdata", "C:/Temp"),
    ("tesseract", None, f"C:/Users/{ARABIC}/AppData/Local/Temp"),
])
def test_a_non_ascii_path_anywhere_is_guarded(windows, monkeypatch, program, tessdata, temp):
    monkeypatch.setattr(ascii_paths.tempfile, "gettempdir", lambda: temp)
    assert must_guard(program, tessdata)


# --- The program and the language data ---

def test_an_ascii_program_is_started_as_it_is(windows):
    assert launchable("C:/tools/tesseract.exe") == "C:/tools/tesseract.exe"


def test_a_program_with_arabic_letters_is_started_by_its_short_name(windows, monkeypatch):
    monkeypatch.setattr(ascii_paths, "short_path", lambda path: "C:/PROGRA~1/TESSER~1/tesseract.exe")
    assert launchable(f"C:/{ARABIC}/tesseract.exe") == "C:/PROGRA~1/TESSER~1/tesseract.exe"


def test_without_a_short_name_the_program_path_is_all_there_is(windows):
    assert launchable(f"C:/{ARABIC}/tesseract.exe") == f"C:/{ARABIC}/tesseract.exe"


def test_ascii_language_data_is_given_as_it_is(windows, tmp_path):
    assert (
        ascii_paths._safe_tessdata("tesseract", str(tmp_path / "tessdata"), str(tmp_path), [])
        == str(tmp_path / "tessdata")
    )


def test_no_language_data_asked_for_and_an_ascii_program_leave_the_environment_alone(windows, tmp_path):
    assert ascii_paths._safe_tessdata("C:/tools/tesseract.exe", None, str(tmp_path), []) is None


def test_arabic_language_data_is_given_by_its_short_name(windows, monkeypatch, tmp_path):
    monkeypatch.setattr(ascii_paths, "short_path", lambda path: "C:/USERS/AHMAD~1/tessdata")
    assert (
        ascii_paths._safe_tessdata("tesseract", f"C:/{ARABIC}/tessdata", str(tmp_path), [])
        == "C:/USERS/AHMAD~1/tessdata"
    )


def test_arabic_language_data_without_a_short_name_is_reached_through_a_junction(windows, monkeypatch, tmp_path):
    data = tmp_path / ARABIC / "tessdata"
    data.mkdir(parents=True)
    (data / "eng.traineddata").write_bytes(b"x")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(ascii_paths, "make_junction", link_to)
    junctions = []
    assert ascii_paths._safe_tessdata("tesseract", str(data), str(work), junctions) == "tessdata"
    assert junctions == [str(work / "tessdata")]
    assert (work / "tessdata" / "eng.traineddata").exists()
    ascii_paths._remove_link(junctions[0])
    assert not (work / "tessdata").exists() and (data / "eng.traineddata").exists()  # Only the link is removed.


def test_arabic_language_data_with_neither_is_given_as_it_is(windows, tmp_path):
    assert (
        ascii_paths._safe_tessdata("tesseract", f"C:/{ARABIC}/tessdata", str(tmp_path), [])
        == f"C:/{ARABIC}/tessdata"
    )


def test_a_program_in_an_arabic_folder_brings_its_own_language_data(windows, monkeypatch, tmp_path):
    folder = tmp_path / ARABIC / "tesseract"
    (folder / "tessdata").mkdir(parents=True)
    monkeypatch.setattr(
        ascii_paths, "short_path", lambda path: "C:/SHORT/tessdata" if path.endswith("tessdata") else None
    )
    assert ascii_paths._safe_tessdata(str(folder / "tesseract.exe"), None, str(tmp_path), []) == "C:/SHORT/tessdata"


def test_a_program_in_an_arabic_folder_without_language_data_beside_it_asks_for_none(windows, tmp_path):
    folder = tmp_path / ARABIC / "tesseract"
    folder.mkdir(parents=True)
    assert ascii_paths._safe_tessdata(str(folder / "tesseract.exe"), None, str(tmp_path), []) is None


# --- Running it ---

@needs_scripts
def test_tesseract_is_started_in_its_working_folder_with_ascii_names_only(
    windows, arabic_temp, fake_log, picture, monkeypatch
):
    monkeypatch.setenv("TESSDATA_PREFIX", "/elsewhere")
    program = make_program(arabic_temp.parent / "bin")
    text = run_tesseract(program, picture, "txt", "eng", "--psm 3 --oem 1")
    assert text.startswith("Hello World")
    (call,) = fake_log()
    assert call["arguments"] == ["input.png", "result", "-l", "eng", "--psm", "3", "--oem", "1", "txt"]
    assert all(argument.isascii() for argument in call["arguments"])
    assert os.path.dirname(call["cwd"]) == str(arabic_temp)  # In the Arabic TEMP, only as its folder.
    assert not os.path.exists(call["cwd"])  # Cleaned up afterwards.
    assert call["picture_is_png"]
    assert call["tessdata"] == "/elsewhere"  # What was there is left alone.


@needs_scripts
def test_the_language_data_travels_in_the_environment_as_an_ascii_path(
    windows, arabic_temp, fake_log, picture, monkeypatch, tmp_path
):
    data = tmp_path / ARABIC / "tessdata"
    data.mkdir(parents=True)
    (data / "eng.traineddata").write_bytes(b"x")
    monkeypatch.setattr(ascii_paths, "make_junction", link_to)
    program = make_program(tmp_path / "bin")
    run_tesseract(program, picture, "txt", "eng", "", str(data))
    (call,) = fake_log()
    assert call["tessdata"] == "tessdata" and call["tessdata"].isascii()
    assert call["tessdata_listing"] == ["eng.traineddata"]  # The junction led to the real folder...
    assert (data / "eng.traineddata").exists()  # ...and what it pointed to is intact.


@needs_scripts
def test_confidence_and_script_output_come_back_for_the_pytesseract_parsers(
    windows, arabic_temp, fake_log, picture, tmp_path
):
    program = make_program(tmp_path / "bin")
    tsv = run_tesseract(program, picture, "tsv", "eng", "-c tessedit_create_tsv=1 --psm 3")
    assert tsv.startswith("level\tconf\ttext")
    osd = run_tesseract(program, picture, "osd", "osd", "--psm 0 ")
    assert "Script: Latin" in osd
    for call in fake_log():
        assert all(argument.isascii() for argument in call["arguments"])
    # Each output is requested by option (tessedit_create_tsv, --psm 0), not by a config name such as tsv or osd.
    assert "tsv" not in fake_log()[0]["arguments"] and "osd" not in fake_log()[1]["arguments"][2:3]


@needs_scripts
def test_a_transparent_picture_is_flattened_onto_white(windows, arabic_temp, fake_log, tmp_path):
    from PIL import Image

    program = make_program(tmp_path / "bin")
    run_tesseract(program, Image.new("RGBA", (10, 10), (0, 0, 0, 0)), "txt", "eng")
    assert fake_log()[0]["picture_is_png"]


@needs_scripts
def test_a_program_that_fails_is_reported_with_what_it_said(
    windows, arabic_temp, fake_log, picture, monkeypatch, tmp_path
):
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(TesseractFailed) as raised:
        run_tesseract(make_program(tmp_path / "bin"), picture, "txt", "xyz")
    assert raised.value.returncode == 1
    assert "Failed loading language 'xyz'" in raised.value.message


@needs_scripts
def test_a_program_that_writes_nothing_is_reported(windows, arabic_temp, fake_log, picture, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "silent")
    with pytest.raises(TesseractFailed, match="did not produce any output file"):
        run_tesseract(make_program(tmp_path / "bin"), picture, "txt", "eng")


@needs_scripts
def test_a_missing_program_is_reported_as_missing(windows, arabic_temp, picture, tmp_path):
    with pytest.raises(ProgramNotFound):
        run_tesseract(str(tmp_path / "nowhere" / "tesseract"), picture, "txt", "eng")


# --- Through OcrEngine, with pytesseract's own parsers ---

@pytest.fixture
def engine_setup(windows, arabic_temp, fake_log, monkeypatch, tmp_path):
    """OcrEngine wired to the stand-in program; return the path of a picture in an Arabic folder and the call log."""
    pytesseract = pytest.importorskip("pytesseract")
    from PIL import Image

    program = make_program(tmp_path / "bin")
    monkeypatch.setattr(ocr, "find_tesseract", lambda: program)
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")  # Restored by monkeypatch.
    image = tmp_path / ARABIC / PICTURE_NAME
    image.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 30), "white").save(image)
    return str(image), fake_log


@needs_scripts
def test_ocr_with_arabic_letters_in_temp_gives_tesseract_ascii_names_only(engine_setup):
    image, calls = engine_setup
    result = ocr.OcrEngine(on_progress=lambda message: None).extract_text(image, language=language_for_code("en"))
    assert result.text.startswith("Hello World")
    assert result.confidence == 85  # (90 + 80) / 2; the -1 row is left out.
    assert len(calls()) == 2  # The text, then the confidence.
    for call in calls():
        assert all(argument.isascii() for argument in call["arguments"])
        assert call["arguments"][:2] == ["input.png", "result"]


@needs_scripts
def test_auto_detect_reads_the_script_the_same_way(engine_setup):
    image, calls = engine_setup
    messages = []
    result = ocr.OcrEngine(on_progress=messages.append).extract_text(image)  # No source language chosen.
    assert result.text.startswith("Hello World")
    assert any("Latin" in message for message in messages)
    assert len(calls()) == 3  # The script, the text, the confidence.
    for call in calls():
        assert all(argument.isascii() for argument in call["arguments"])


@needs_scripts
def test_a_failure_reaches_the_user_in_the_usual_words(engine_setup, monkeypatch):
    image, _calls = engine_setup
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(RuntimeError, match="Tesseract language pack 'eng' is not installed"):
        ocr.OcrEngine(on_progress=lambda message: None).extract_text(image, language=language_for_code("en"))


@needs_scripts
def test_a_missing_program_reaches_the_user_in_the_usual_words(engine_setup, monkeypatch, tmp_path):
    image, _calls = engine_setup
    pytest.importorskip("pytesseract")
    monkeypatch.setattr(ocr, "find_tesseract", lambda: str(tmp_path / "gone" / "tesseract"))
    with pytest.raises(RuntimeError, match="isn't installed"):
        ocr.OcrEngine(on_progress=lambda message: None).extract_text(image, language=language_for_code("en"))


@needs_scripts
def test_with_ascii_paths_pytesseract_does_the_work_as_before(windows, monkeypatch, tmp_path):
    pytest.importorskip("pytesseract")
    monkeypatch.setattr(ascii_paths.tempfile, "gettempdir", lambda: "C:/Temp")
    called = []
    monkeypatch.setattr(ascii_paths, "run_tesseract", lambda *args, **kwargs: called.append(args))
    stand_in = type("Module", (), {"pytesseract": type("Inner", (), {"tesseract_cmd": "tesseract"})(),
                                   "image_to_string": staticmethod(lambda image, lang=None, config="": "plain")})
    assert ocr._image_to_string(stand_in, object(), "eng", "", None) == "plain"
    assert called == []


@pytest.mark.skipif(
    shutil.which("tesseract") is None or sys.platform == "win32", reason="needs a real tesseract on PATH"
)
def test_a_real_tesseract_reads_a_picture_with_arabic_letters_in_temp(windows, arabic_temp, monkeypatch, tmp_path):
    pytest.importorskip("pytesseract")
    from PIL import Image, ImageDraw, ImageFont

    monkeypatch.setattr(ocr, "find_tesseract", lambda: shutil.which("tesseract"))
    import pytesseract
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    image = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(image).text((20, 40), "Hello KaramVT 12345", fill="black", font=ImageFont.load_default(size=64))
    path = tmp_path / ARABIC / PICTURE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    result = ocr.OcrEngine(on_progress=lambda message: None).extract_text(str(path), language=language_for_code("en"))
    assert "Hello" in result.text
    assert result.confidence is not None and result.confidence > 50
