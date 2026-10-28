# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OcrEngine.extract_text with a stand-in pytesseract: what it asks Tesseract for and what the user is told.

The stand-in answers from a script each test sets up, so every branch of the engine (a language chosen or guessed, each
way Tesseract can fail, each source of language data) is run without Tesseract or its language data being installed.
"""

import shlex
import sys
import types

import pytest

from karamvt.core import ocr, tessdata_manager
from karamvt.core.languages import Language
from karamvt.core.ocr import OcrEngine, OcrUnsupportedError

Image = pytest.importorskip("PIL.Image")


class TesseractError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(status, message)
        self.status, self.message = status, message


class TesseractNotFoundError(RuntimeError):
    pass


class FakeTesseract(types.ModuleType):
    """What pytesseract offers the engine; ``text``, ``confidences``, ``osd`` and ``failure`` say what it answers."""

    TesseractError = TesseractError
    TesseractNotFoundError = TesseractNotFoundError
    Output = types.SimpleNamespace(DICT="dict")

    def __init__(self):
        super().__init__("pytesseract")
        self.pytesseract = types.SimpleNamespace(tesseract_cmd="tesseract")
        self.text = "  Hello world \n"
        self.confidences = [96, "-1", 88, "x"]
        self.osd = {"script": "Latin"}
        self.failure = None          # Raised by image_to_string.
        self.osd_failure = None      # Raised by image_to_osd.
        self.calls = []

    def image_to_string(self, image, lang=None, config=""):
        self.calls.append(("string", lang, config))
        if self.failure:
            raise self.failure
        return self.text

    def image_to_data(self, image, lang=None, config="", output_type=None):
        self.calls.append(("data", lang, config))
        return {"conf": self.confidences}

    def image_to_osd(self, image, config="", output_type=None):
        self.calls.append(("osd", None, config))
        if self.osd_failure:
            raise self.osd_failure
        return self.osd


@pytest.fixture
def tesseract(monkeypatch):
    fake = FakeTesseract()
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(ocr, "is_windows", lambda: False)
    monkeypatch.setattr(ocr, "detect_language_from_text", lambda text: None)
    return fake


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "picture.png"
    Image.new("RGB", (8, 8), "white").save(path)
    return str(path)


@pytest.fixture
def engine():
    messages = []
    result = OcrEngine(on_progress=messages.append)
    result.messages = messages
    return result


ENGLISH = Language("English", "en", tesseract_code="eng")


def failing(message):
    return TesseractError(1, message)


# --- A language is chosen ---

def test_the_text_is_stripped_and_the_confidence_is_the_mean_of_the_real_scores(tesseract, engine, picture):
    result = engine.extract_text(picture, ENGLISH)
    assert result.text == "Hello world"
    assert result.confidence == 92.0  # 96 and 88: the -1 row and the non-number are left out.
    assert result.notice is None and result.detected_language is None
    assert engine.messages == ["Reading text from image (English)..."]


def test_tesseract_is_given_the_language_the_modes_and_the_variables(tesseract, engine, picture):
    engine.extract_text(picture, ENGLISH, psm=4, oem=1, tess_vars=[("preserve_interword_spaces", "1")])
    assert tesseract.calls[0] == ("string", "eng", "--psm 4 --oem 1 -c preserve_interword_spaces=1")


def test_a_psm_that_is_not_offered_falls_back_to_the_default(tesseract, engine, picture):
    engine.extract_text(picture, ENGLISH, psm=2)
    assert tesseract.calls[0][2].startswith(f"--psm {ocr.DEFAULT_PSM} ")


def test_a_picture_without_text_has_no_confidence(tesseract, engine, picture):
    tesseract.text = " \n"
    result = engine.extract_text(picture, ENGLISH)
    assert result.text == "" and result.confidence is None


def test_a_confidence_that_cannot_be_read_is_left_out_and_the_text_still_comes_back(tesseract, engine, picture):
    def no_data(*args, **kwargs):
        raise OSError("no tsv")

    tesseract.image_to_data = no_data
    result = engine.extract_text(picture, ENGLISH)
    assert result.text == "Hello world" and result.confidence is None


# --- What is wrong with the request ---

def test_a_missing_file_is_named(tesseract, engine, tmp_path):
    with pytest.raises(FileNotFoundError, match="Image not found"):
        engine.extract_text(str(tmp_path / "gone.png"), ENGLISH)


def test_a_file_that_is_not_a_picture_is_refused_by_name(tesseract, engine, tmp_path):
    path = tmp_path / "notes.png"
    path.write_text("not an image")
    with pytest.raises(ValueError, match="'notes.png' doesn't look like a valid image file"):
        engine.extract_text(str(path), ENGLISH)


def test_a_language_without_tesseract_data_is_refused(tesseract, engine, picture):
    with pytest.raises(OcrUnsupportedError, match="OCR isn't supported for Klingon"):
        engine.extract_text(picture, Language("Klingon", "tlh", tesseract_code=""))


def test_without_pytesseract_the_user_is_told_what_to_install(monkeypatch, engine, picture):
    monkeypatch.setitem(sys.modules, "pytesseract", None)  # `import pytesseract` now fails.
    with pytest.raises(ImportError, match="'pytesseract' and 'Pillow'"):
        engine.extract_text(picture, ENGLISH)


# --- Tesseract fails ---

def test_a_missing_program_says_so_and_how_to_install_it(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: "Run: install it")
    tesseract.failure = TesseractNotFoundError()
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH)
    assert str(raised.value) == "The 'tesseract' program isn't installed on this system.\nRun: install it"
    assert isinstance(raised.value.__cause__, TesseractNotFoundError)


def test_any_other_failure_is_passed_on_with_what_tesseract_said(tesseract, engine, picture):
    tesseract.failure = failing("Segmentation fault")
    with pytest.raises(RuntimeError, match="Tesseract failed: .*Segmentation fault") as raised:
        engine.extract_text(picture, ENGLISH)
    assert raised.value.__cause__ is tesseract.failure


def test_a_missing_system_language_pack_names_the_package_to_install(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: f"Run: install {lang}")
    tesseract.failure = failing("Failed loading language 'eng'")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH)
    message = str(raised.value)
    assert message.startswith("Tesseract language pack 'eng' is not installed.\nRun: install eng")
    assert message.endswith("\nOr switch to a downloaded language source in OCR Settings — no admin rights needed.")
    assert "legacy-capable" not in message


@pytest.mark.parametrize("oem, mode", [(0, "Legacy only"), (2, "Legacy + LSTM")])
def test_a_legacy_engine_mode_adds_what_its_language_data_needs(tesseract, engine, picture, monkeypatch, oem, mode):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: "Run: install")
    tesseract.failure = failing("Could not initialize tesseract")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH, oem=oem)
    assert f"Engine mode “{mode}” needs legacy-capable language data" in str(raised.value)
    assert "Try the Standard source, or set the engine mode to Default." in str(raised.value)


def test_the_neural_engine_modes_add_nothing(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: "Run: install")
    tesseract.failure = failing("Could not initialize tesseract")
    for oem in (1, 3):
        with pytest.raises(RuntimeError) as raised:
            engine.extract_text(picture, ENGLISH, oem=oem)
        assert "legacy" not in str(raised.value)


@pytest.fixture
def downloaded(monkeypatch, tmp_path):
    """Downloaded language data: where it is, which languages are there and whether osd is."""
    state = types.SimpleNamespace(
        folder=str(tmp_path / "tessdata"), languages=set(), osd=False, fetched=[], fetch_error=None,
    )
    monkeypatch.setattr(tessdata_manager, "local_tessdata_dir", lambda quality: state.folder)
    monkeypatch.setattr(tessdata_manager, "is_downloaded", lambda quality, code: code in state.languages)
    monkeypatch.setattr(tessdata_manager, "has_osd", lambda quality: state.osd)

    def ensure_osd(quality, progress_cb=None):
        state.fetched.append(quality)
        if state.fetch_error:
            raise state.fetch_error
        for percent in (None, 3, 7, 12, 12, 100):
            progress_cb(percent)
        state.osd = True

    monkeypatch.setattr(tessdata_manager, "ensure_osd", ensure_osd)
    return state


def test_tesseract_reads_the_downloaded_data_from_its_folder(tesseract, engine, picture, downloaded):
    engine.extract_text(picture, ENGLISH, language_source="fast")
    folder = shlex.join(["--tessdata-dir", downloaded.folder])  # (A Windows path needs quotes.)
    assert tesseract.calls[0][2] == f"--psm {ocr.DEFAULT_PSM} --oem {ocr.DEFAULT_OEM} {folder}"


def test_downloaded_data_that_will_not_load_may_be_corrupt(tesseract, engine, picture, downloaded):
    downloaded.languages = {"eng"}
    tesseract.failure = failing("Failed loading language 'eng'")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH, language_source="fast")
    assert str(raised.value) == (
        "Could not load the downloaded 'eng' language data."
        "\nIt may be corrupt — try deleting and re-downloading it from OCR Settings → Manage downloaded languages."
    )


def test_downloaded_data_that_will_not_load_with_a_legacy_mode_blames_the_mode(tesseract, engine, picture, downloaded):
    downloaded.languages = {"eng"}
    tesseract.failure = failing("Failed loading language 'eng'")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH, language_source="fast", oem=2)
    message = str(raised.value)
    assert message.startswith("Could not load the downloaded 'eng' language data.\nEngine mode ")
    assert "corrupt" not in message


def test_a_language_not_downloaded_yet_says_where_to_download_it(tesseract, engine, picture, downloaded):
    tesseract.failure = failing("Failed loading language 'eng'")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH, language_source="best")
    assert str(raised.value) == (
        "'eng' isn't downloaded for the 'best' quality tier yet.\n"
        "Download it from OCR Settings → Manage downloaded languages."
    )


def test_a_page_mode_that_needs_osd_is_refused_before_anything_runs_when_it_is_not_downloaded(
    tesseract, engine, picture, downloaded,
):
    with pytest.raises(RuntimeError, match="Script/orientation data \\(osd\\) isn't downloaded for this language"):
        engine.extract_text(picture, ENGLISH, psm=1, language_source="fast")
    assert tesseract.calls == []
    downloaded.osd = True
    engine.extract_text(picture, ENGLISH, psm=1, language_source="fast")
    assert tesseract.calls


def test_a_page_mode_that_needs_osd_names_the_system_package_when_tesseract_cannot_load_it(
    tesseract, engine, picture, monkeypatch,
):
    monkeypatch.setattr(ocr, "osd_install_hint", lambda: "Run: install osd")
    tesseract.failure = failing("Error opening data file osd.traineddata: 'osd'")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, ENGLISH, psm=12)
    assert str(raised.value) == (
        "Script/orientation data (osd) isn't installed on this system.\nRun: install osd"
        "\nOr switch to a downloaded language source in OCR Settings — no admin rights needed."
    )


def test_osd_is_not_blamed_when_the_page_mode_does_not_use_it(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: "Run: install")
    tesseract.failure = failing("Failed loading language 'osd'")
    with pytest.raises(RuntimeError, match="Tesseract failed|language pack"):
        engine.extract_text(picture, ENGLISH, psm=6)


# --- No language is chosen: the script is detected ---

@pytest.mark.parametrize("script, code", [("Arabic", "ara"), ("Cyrillic", "rus"), ("Han", "chi_sim"), ("Latin", "eng")])
def test_the_language_pack_follows_the_script_of_the_image(tesseract, engine, picture, script, code):
    tesseract.osd = {"script": script}
    result = engine.extract_text(picture)
    assert [call[:2] for call in tesseract.calls if call[0] == "string"] == [("string", code)]
    assert result.notice is None
    assert engine.messages[0] == "Detecting the image's script…"
    assert engine.messages[1] == (
        f"No source language chosen — detected “{script}” script, using {code} for OCR. "
        "Pick a language above for better accuracy."
    )


def test_a_script_without_a_mapping_assumes_english_and_says_so(tesseract, engine, picture):
    tesseract.osd = {"script": "Ethiopic"}
    result = engine.extract_text(picture)
    assert tesseract.calls[1][:2] == ("string", "eng")
    assert result.notice == (
        "Detected “Ethiopic” script, which has no built-in mapping — assumed English. "
        "Pick a source language for better accuracy."
    )
    assert result.notice in engine.messages


@pytest.mark.parametrize("failure, reason", [
    ("Too few characters. Skipping this page", "there isn't enough text in the image to detect its script"),
    ("Something else went wrong", "script detection failed"),
])
def test_a_script_that_cannot_be_detected_assumes_english_and_says_why(tesseract, engine, picture, failure, reason):
    tesseract.osd_failure = failing(failure)
    result = engine.extract_text(picture)
    assert result.text == "Hello world" and tesseract.calls[1][:2] == ("string", "eng")
    assert result.notice == f"Assumed English — {reason}. Pick a source language for better accuracy."
    assert engine.messages[-1] == result.notice


def test_missing_osd_data_stops_the_guess_instead_of_misleading(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "osd_install_hint", lambda: "Run: install osd")
    tesseract.osd_failure = failing("Error opening data file /usr/share/osd.traineddata")
    with pytest.raises(RuntimeError, match="isn't installed on this system.\nRun: install osd"):
        engine.extract_text(picture)
    assert not [call for call in tesseract.calls if call[0] == "string"]


def test_the_program_missing_while_detecting_the_script_is_reported_the_same(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "_install_hint", lambda lang=None: "Run: install it")
    tesseract.osd_failure = TesseractNotFoundError()
    with pytest.raises(RuntimeError, match="The 'tesseract' program isn't installed on this system.\nRun: install it"):
        engine.extract_text(picture)


def test_the_osd_data_is_downloaded_first_when_a_downloaded_source_lacks_it(tesseract, engine, picture, downloaded):
    result = engine.extract_text(picture, language_source="standard")
    assert downloaded.fetched == ["standard"] and result.text == "Hello world"
    steps = [m for m in engine.messages if m.startswith("Downloading script-detection data")]
    # One message when it starts, then one for each tenth of the progress that is reached; a report of None is skipped,
    # and 7% (the same tenth as 3%) and the second 12% are not repeated.
    assert steps == [
        "Downloading script-detection data (osd)…",
        "Downloading script-detection data (osd)… 3%",
        "Downloading script-detection data (osd)… 12%",
        "Downloading script-detection data (osd)… 100%",
    ]
    # The script is detected from that folder too.
    assert tesseract.calls[0][2] == shlex.join(["--tessdata-dir", downloaded.folder])


def test_the_osd_data_is_not_downloaded_again_when_it_is_there(tesseract, engine, picture, downloaded):
    downloaded.osd = True
    engine.extract_text(picture, language_source="standard")
    assert downloaded.fetched == []


def test_osd_data_that_cannot_be_downloaded_stops_the_guess_with_the_reason(tesseract, engine, picture, downloaded):
    downloaded.fetch_error = RuntimeError("no network")
    with pytest.raises(RuntimeError) as raised:
        engine.extract_text(picture, language_source="standard")
    assert str(raised.value) == (
        "Auto-detect needs script-detection data (osd), which couldn't be downloaded: no network\n"
        "Pick a source language instead, or download it later from OCR Settings → Manage downloaded languages."
    )
    assert isinstance(raised.value.__cause__, RuntimeError)


# --- The language of the text that came out ---

def test_the_language_of_the_text_is_detected_when_none_was_chosen(tesseract, engine, picture, monkeypatch):
    seen = []
    monkeypatch.setattr(ocr, "detect_language_from_text", lambda text: seen.append(text) or ENGLISH)
    result = engine.extract_text(picture)
    assert result.detected_language is ENGLISH and seen == ["Hello world"]
    assert engine.messages[-1] == "Extracted text looks like English."


def test_only_the_first_500_characters_are_looked_at(tesseract, engine, picture, monkeypatch):
    seen = []
    tesseract.text = "x" * 900
    monkeypatch.setattr(ocr, "detect_language_from_text", lambda text: seen.append(len(text)))
    engine.extract_text(picture)
    assert seen == [500]


def test_the_language_is_not_detected_when_one_was_chosen_or_there_is_no_text(tesseract, engine, picture, monkeypatch):
    monkeypatch.setattr(ocr, "detect_language_from_text", lambda text: pytest.fail("not asked"))
    engine.extract_text(picture, ENGLISH)
    tesseract.text = "  "
    engine.extract_text(picture)


def test_a_missing_language_detector_is_not_an_error(tesseract, engine, picture, monkeypatch):
    def missing(text):
        raise ImportError("langdetect")

    monkeypatch.setattr(ocr, "detect_language_from_text", missing)
    result = engine.extract_text(picture)
    assert result.text == "Hello world" and result.detected_language is None
