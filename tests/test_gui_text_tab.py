# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Text tab driven through the real interface (see gui_support.py): what is checked before a translation starts,
what the translation does while it runs and when it ends, the buttons that follow it, and importing text from an
image. The translation model and Tesseract are replaced by stand-ins that record what they were asked."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import os
import threading
import time
from types import SimpleNamespace

import pytest
from gui_support import Gio, GLib, Gtk, downloader, env, pump, ready_models

from karamvt.config import AppConfig
from karamvt.core.cancel import OperationCancelled
from karamvt.core.languages import LANGUAGES
from karamvt.core.llm_settings import sampling_from_config
from karamvt.core.ocr import OcrResult, tess_vars_from_config
from karamvt.core.text_translation import MAX_INPUT_CHARS
from karamvt.gui.formatting import short_error
from karamvt.gui.tabs import text_tab

SPANISH = LANGUAGES["es"]


class ModelStandIn:
    """The translation model and what is done with it: records every use, and does what the test says."""

    def __init__(self):
        self.loads: list[SimpleNamespace] = []
        self.engines: list[SimpleNamespace] = []
        self.gpu_layers_for: list[str] = []
        self.result, self.error = "Hola", None
        self.messages: list[str] = []          # What the engine reports while it works.
        self.partial: str | None = None        # Shown first; the engine then waits until `release` is set.
        self.release = threading.Event()
        self.started = threading.Event()
        self.wait_for_stop = False


@pytest.fixture
def model(monkeypatch):
    stand_in = ModelStandIn()

    def load_llm(path, **kwargs):
        loaded = SimpleNamespace(path=path, kwargs=kwargs)
        stand_in.loads.append(loaded)
        return loaded

    class Engine:
        def __init__(self, source, target, model_path, **kwargs):
            self.use = SimpleNamespace(source=source, target=target, model=model_path, kwargs=kwargs, texts=[])
            stand_in.engines.append(self.use)

        def translate(self, text, on_partial=None, cancel=None):
            self.use.texts.append(text)
            stand_in.started.set()
            for message in stand_in.messages:
                self.use.kwargs["on_progress"](message)
            if stand_in.partial is not None:
                on_partial(stand_in.partial)
                while not stand_in.release.wait(0.005):
                    cancel.raise_if_cancelled()
            if stand_in.wait_for_stop:
                while not cancel.is_cancelled:
                    time.sleep(0.005)
                raise OperationCancelled()
            if stand_in.error is not None:
                raise stand_in.error
            return stand_in.result

    def gpu_layers(device):
        stand_in.gpu_layers_for.append(device)
        return 33

    monkeypatch.setattr(text_tab, "load_llm", load_llm)
    monkeypatch.setattr(text_tab, "TextTranslationEngine", Engine)
    monkeypatch.setattr(text_tab, "resolve_llm_gpu_layers", gpu_layers)
    yield stand_in
    stand_in.release.set()


@pytest.fixture
def tab(env, tmp_path):
    ready_models(tmp_path)
    return env[1]._text_tab


def save_config(**changes) -> AppConfig:
    config = AppConfig.load()
    for field, value in changes.items():
        setattr(config, field, value)
    config.save()
    return config


def toasts_of(tab) -> list[str]:
    heard: list[str] = []
    tab.connect("toast-requested", lambda _tab, message: heard.append(message))
    return heard


def events_of(tab, name) -> list[tuple]:
    heard: list[tuple] = []
    tab.connect(name, lambda _tab, *args: heard.append(args))
    return heard


def translate(tab, text="Hello", source="English", target="Spanish"):
    tab._input_buffer.set_text(text)
    tab._source_picker.set_selected(source)
    tab._target_picker.set_selected(target)
    tab._translate_button.emit("clicked")


def finish(tab):
    assert pump(3.0, until=lambda: tab._worker is not None and not tab.is_busy)
    pump(0.1)


def output(tab) -> str:
    return tab._get_buffer_text(tab._output_buffer)


# --- What is checked before a translation starts ---

def test_without_models_set_nothing_starts_and_the_first_problem_is_told(env, model):
    tab = env[1]._text_tab
    heard = toasts_of(tab)
    translate(tab)
    assert heard == ["No Faster-Whisper model selected."] and not tab.is_busy and model.engines == []


def test_a_model_file_that_is_gone_is_told_before_anything_loads(tab, model, tmp_path):
    config = AppConfig.load()
    os.remove(config.llm_model_path)
    heard = toasts_of(tab)
    translate(tab)
    assert heard == [f"LLM model file not found: {config.llm_model_path}"] and model.loads == []


def test_the_saved_settings_are_read_again_each_time(tab, model):
    heard = toasts_of(tab)
    save_config(whisper_model_path="", llm_model_path="")
    translate(tab)
    assert heard == ["No Faster-Whisper model selected."]


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_a_text_without_words_is_not_translated(tab, model, text):
    heard = toasts_of(tab)
    translate(tab, text)
    assert heard == ["Type or paste some text first."] and model.loads == []


def test_a_text_over_the_limit_is_refused_and_one_at_the_limit_is_not(tab, model):
    heard = toasts_of(tab)
    translate(tab, "x" * (MAX_INPUT_CHARS + 1))
    assert heard == [f"Text is too long — keep it under {MAX_INPUT_CHARS} characters."] and model.loads == []
    translate(tab, "x" * MAX_INPUT_CHARS)
    finish(tab)
    assert len(heard) == 1 and model.engines[0].texts == ["x" * MAX_INPUT_CHARS]


def test_the_same_language_on_both_sides_is_refused(tab, model):
    heard = toasts_of(tab)
    translate(tab, source="English", target="English")
    assert heard == ["Source and target language can't be the same."] and model.loads == []


def test_when_the_source_is_auto_detect_the_model_starts_from_english(tab, model):
    translate(tab, source="Auto-detect", target="Spanish")
    finish(tab)
    assert (model.engines[0].source.name, model.engines[0].target.name) == ("English", "Spanish")


def test_auto_detect_with_english_as_the_target_is_allowed(tab, model):
    heard = toasts_of(tab)
    translate(tab, source="Auto-detect", target="English")
    finish(tab)
    assert heard == [] and model.engines[0].source.name == "English" == model.engines[0].target.name


# --- A translation ---

def test_a_translation_asks_the_model_with_the_saved_settings_and_shows_the_result(tab, model):
    config = save_config(cpu_threads=3, processing_device="cpu", llm_temperature=0.7)
    translate(tab, "Hello world")
    finish(tab)
    [loaded] = model.loads
    assert loaded.path == config.llm_model_path
    assert loaded.kwargs["cpu_threads"] == 3 and loaded.kwargs["n_gpu_layers"] == 33
    assert callable(loaded.kwargs["on_progress"]) and model.gpu_layers_for == ["cpu"]
    [engine] = model.engines
    assert (engine.source.name, engine.target.name, engine.model) == ("English", "Spanish", config.llm_model_path)
    assert engine.kwargs["llm"] is loaded and engine.kwargs["sampling"] == sampling_from_config(config)
    assert engine.texts == ["Hello world"] and output(tab) == "Hola"
    assert tab._status_label.get_label() == "" and not tab.is_busy


def test_the_window_is_told_when_a_translation_is_ready_and_what_was_made(tab, model):
    done, finished = events_of(tab, "translation-done"), events_of(tab, "job-finished")
    translate(tab, "Hello world")
    finish(tab)
    assert finished == [("Translation is ready", "English → Spanish")]
    [(source, target, text, result, model_name, seconds)] = done
    assert (source, target, text, result) == ("English", "Spanish", "Hello world", "Hola")
    assert model_name == "model.gguf" and 0 <= seconds < 5


def test_the_text_that_was_translated_is_the_one_in_the_box_when_the_button_was_pressed(tab, model):
    model.partial = "Ho"
    done = events_of(tab, "translation-done")
    translate(tab, "first")
    assert pump(2.0, until=lambda: output(tab) == "Ho")
    tab._input_buffer.set_text("typed later")
    model.release.set()
    finish(tab)
    assert done[0][2] == "first" and model.engines[0].texts == ["first"]


def test_the_old_translation_is_wiped_when_a_new_one_starts(tab, model):
    tab._output_buffer.set_text("old")
    model.error = RuntimeError("out of memory")
    translate(tab)
    finish(tab)
    assert output(tab) == ""


def test_the_translation_is_shown_as_it_arrives_and_the_checked_result_replaces_it(tab, model):
    model.partial, model.result = "Hola mu", "Hola mundo"
    translate(tab)
    assert pump(2.0, until=lambda: output(tab) == "Hola mu")
    model.release.set()
    finish(tab)
    assert output(tab) == "Hola mundo"


def test_what_the_model_says_while_it_works_is_shown_as_the_status(tab, model):
    model.partial = "Ho"
    model.messages = ["Loading the model", "Almost there"]
    translate(tab)
    assert pump(2.0, until=lambda: tab._status_label.get_label() == "Almost there")
    model.release.set()
    finish(tab)
    assert tab._status_label.get_label() == ""


def test_the_first_warning_of_the_model_is_told_in_a_toast(tab, model):
    model.messages = ["Loading", "Warning: first", "Warning: second"]
    heard = toasts_of(tab)
    translate(tab)
    finish(tab)
    assert heard == ["Warning: first"]


def test_the_model_is_loaded_once_and_used_again_while_the_settings_that_matter_stay(tab, model):
    translate(tab)
    finish(tab)
    translate(tab, "Again", target="French")
    finish(tab)
    assert len(model.loads) == 1 and len(model.engines) == 2
    assert model.engines[0].kwargs["llm"] is model.engines[1].kwargs["llm"]


@pytest.mark.parametrize("change", [{"cpu_threads": 1}, {"processing_device": "gpu"}, {"llm_model_path": "other"}])
def test_a_different_model_thread_count_or_device_loads_the_model_again(tab, model, tmp_path, change):
    translate(tab)
    finish(tab)
    config = AppConfig.load()
    if "llm_model_path" in change:
        other = tmp_path / "other.gguf"
        other.write_bytes(b"x")
        change = {"llm_model_path": str(other)}
    if "cpu_threads" in change and config.cpu_threads == 1:
        change = {"cpu_threads": 2}
    for field, value in change.items():
        setattr(config, field, value)
    config.save()
    translate(tab)
    finish(tab)
    assert len(model.loads) == 2 and model.engines[1].kwargs["llm"] is model.loads[1]


def test_changing_only_a_sampling_setting_does_not_load_the_model_again(tab, model):
    translate(tab)
    finish(tab)
    config = AppConfig.load()
    config.llm_temperature = 0.2
    config.save()
    translate(tab)
    finish(tab)
    assert len(model.loads) == 1
    assert model.engines[1].kwargs["sampling"]["temperature"] == 0.2


def test_a_failure_is_told_shown_and_announced_but_nothing_is_reported_as_done(tab, model):
    model.error = RuntimeError("out of memory\nsecond line")
    heard = toasts_of(tab)
    done, finished = events_of(tab, "translation-done"), events_of(tab, "job-finished")
    translate(tab)
    finish(tab)
    assert heard == ["Something went wrong — see below."]
    assert tab._status_label.get_label() == "Error: out of memory\nsecond line"
    assert finished == [("Translation failed", short_error("out of memory\nsecond line"))] and done == []
    assert not tab.is_busy and tab._translate_button.get_label() == "Translate"


def test_every_job_that_ends_tells_the_window_to_remember_the_state(tab, model):
    changed = events_of(tab, "state-changed")
    translate(tab)
    finish(tab)
    assert changed  # (The boxes changed too; what counts is the end.)
    changed.clear()
    tab._on_job_finished(True, ("x", []), lambda _outcome: None)
    assert changed == [()]


# --- The button, and what is locked while a job runs ---

def test_while_a_translation_runs_the_button_stops_it_and_the_rest_is_locked(tab, model):
    model.wait_for_stop = True
    translate(tab)
    assert pump(2.0, until=lambda: tab._status_label.get_label() == "Translating…")
    button = tab._translate_button
    assert button.get_label() == "Stop" and button.get_sensitive()
    assert button.has_css_class("destructive-action") and not button.has_css_class("suggested-action")
    for locked in (tab._ocr_button, tab._swap_button, tab._clear_button, tab._source_picker, tab._target_picker,
                   tab._ocr_settings_button):
        assert not locked.get_sensitive()
    assert tab._ocr_settings_button.get_tooltip_text() == "OCR Settings — unavailable while a job is running"
    assert tab._spinner.get_visible() and tab.is_busy
    tab._request_stop()
    assert button.get_label() == "Stopping…" and not button.get_sensitive()
    assert tab._status_label.get_label() == "Stopping…"
    finish(tab)


def test_pressing_stop_ends_the_translation_and_everything_is_as_before(tab, model):
    model.wait_for_stop = True
    heard = toasts_of(tab)
    done, finished = events_of(tab, "translation-done"), events_of(tab, "job-finished")
    translate(tab)
    assert pump(2.0, until=lambda: model.started.is_set() and tab._translate_button.get_label() == "Stop")
    tab._translate_button.emit("clicked")
    finish(tab)
    assert heard == ["Translation stopped."] and tab._status_label.get_label() == ""
    assert done == [] and finished == []
    button = tab._translate_button
    assert button.get_label() == "Translate" and button.get_sensitive()
    assert button.has_css_class("suggested-action") and not button.has_css_class("destructive-action")
    for unlocked in (tab._ocr_button, tab._swap_button, tab._clear_button, tab._source_picker, tab._target_picker,
                     tab._ocr_settings_button):
        assert unlocked.get_sensitive()
    assert tab._ocr_settings_button.get_tooltip_text() == "OCR Settings" and not tab._spinner.get_visible()


def test_what_had_been_translated_stays_when_the_translation_is_stopped(tab, model):
    model.partial = "Hola mu"
    translate(tab)
    assert pump(2.0, until=lambda: output(tab) == "Hola mu")
    tab._request_stop()
    finish(tab)
    assert output(tab) == "Hola mu"


def test_stop_is_asked_for_once_however_often_it_is_pressed(tab, model):
    model.wait_for_stop = True
    translate(tab)
    assert pump(2.0, until=lambda: model.started.is_set() and tab._worker is not None)
    cancels = []
    original = tab._worker.cancel
    tab._worker.cancel = lambda: (cancels.append(1), original())[1]
    tab._request_stop()
    tab._request_stop()
    finish(tab)
    assert cancels == [1]


def test_closing_the_app_stops_a_translation_but_not_an_ocr_job(tab, model):
    tab.stop_for_quit()  # Nothing runs: nothing to stop.
    assert tab._worker is None
    release = threading.Event()
    tab._launch_job(lambda _ctx: release.wait(2), on_success=lambda _result: None)
    tab.stop_for_quit()
    assert not tab._stopping and tab._translate_button.get_label() == "Translate"
    assert not tab._translate_button.get_sensitive() and tab._ocr_settings_button.get_tooltip_text().endswith("running")
    release.set()
    finish(tab)
    model.wait_for_stop = True
    translate(tab)
    assert pump(2.0, until=lambda: tab._translating and model.started.is_set())
    tab.stop_for_quit()
    assert tab._stopping
    finish(tab)


# --- The boxes ---

def test_swapping_exchanges_the_languages_and_the_texts(tab):
    changed = events_of(tab, "state-changed")
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected("Spanish")
    tab._input_buffer.set_text("Hello")
    tab._output_buffer.set_text("Hola")
    changed.clear()
    tab._swap_button.emit("clicked")
    assert (tab._source_picker.get_selected(), tab._target_picker.get_selected()) == ("Spanish", "English")
    assert (tab._get_buffer_text(tab._input_buffer), output(tab)) == ("Hola", "Hello")
    assert changed


def test_swapping_is_refused_while_the_source_is_auto_detect(tab):
    heard = toasts_of(tab)
    tab._source_picker.set_selected("Auto-detect")
    tab._target_picker.set_selected("Spanish")
    tab._input_buffer.set_text("Hello")
    tab._swap_button.emit("clicked")
    assert heard == ["Can't swap while source is Auto-detect."]
    assert (tab._source_picker.get_selected(), tab._target_picker.get_selected()) == ("Auto-detect", "Spanish")
    assert tab._get_buffer_text(tab._input_buffer) == "Hello"


def test_the_translated_text_is_copied_and_there_is_nothing_to_copy_when_it_is_empty(tab):
    heard = toasts_of(tab)
    tab._copy_button.emit("clicked")
    assert heard == []
    tab._output_buffer.set_text("Hola")
    tab._copy_button.emit("clicked")
    assert heard == ["Copied to clipboard."]


def test_clearing_empties_the_translation_and_leaves_the_original(tab):
    changed = events_of(tab, "state-changed")
    tab._input_buffer.set_text("Hello")
    tab._output_buffer.set_text("Hola")
    changed.clear()
    tab._clear_button.emit("clicked")
    assert output(tab) == "" and tab._get_buffer_text(tab._input_buffer) == "Hello" and changed


def test_the_count_of_letters_turns_red_over_the_limit(tab):
    label = tab._char_count_label
    assert label.get_label() == f"0 / {MAX_INPUT_CHARS}"
    tab._input_buffer.set_text("abc")
    assert label.get_label() == f"3 / {MAX_INPUT_CHARS}" and not label.has_css_class("error")
    tab._input_buffer.set_text("x" * (MAX_INPUT_CHARS + 1))
    assert label.has_css_class("error")
    tab._input_buffer.set_text("x" * MAX_INPUT_CHARS)
    assert not label.has_css_class("error")


def test_typing_or_choosing_a_language_tells_the_window_to_remember(tab):
    changed = events_of(tab, "state-changed")
    tab._input_buffer.set_text("a")
    assert changed
    changed.clear()
    tab._target_picker.emit("language-selected", "French")  # (As when the user picks one from the list.)
    tab._source_picker.emit("language-selected", "Spanish")
    assert changed == [(), ()]


# --- Importing text from an image ---

class FakeDialog:
    """The answer of the file dialog to "which image?"."""

    def __init__(self, path=None, error=None):
        self.path, self.error = path, error

    def open_finish(self, _result):
        if self.error is not None:
            raise self.error
        return Gio.File.new_for_path(self.path) if self.path else None


@pytest.fixture
def tesseract(monkeypatch):
    """Tesseract's reading of a picture: `reads.result` is what it finds, and every call is kept."""
    reads = SimpleNamespace(calls=[], result=OcrResult("Hello", 91.6), error=None)

    class Engine:
        def __init__(self, on_progress=None):
            reads.progress = on_progress

        def extract_text(self, path, language, **kwargs):
            reads.calls.append((path, language, kwargs))
            if reads.error is not None:
                raise reads.error
            return reads.result

    monkeypatch.setattr(text_tab, "OcrEngine", Engine)
    return reads


def import_image(tab, path="/pictures/page.png"):  # (Where the picture is matters only to the tests that say so.)
    tab._on_image_dialog_response(FakeDialog(str(path)), None)
    finish(tab)


def test_the_image_dialog_offers_pictures_and_opens_where_the_last_one_came_from(tab, monkeypatch, tmp_path):
    opened = {}

    class Dialog:
        def __init__(self, title=""):
            opened["title"] = title

        def set_initial_folder(self, folder):
            opened["folder"] = folder.get_path()

        def set_filters(self, filters):
            opened["filters"] = filters

        def open(self, parent, cancellable, callback):
            opened["parent"], opened["callback"] = parent, callback

    monkeypatch.setattr(text_tab.Gtk, "FileDialog", Dialog)
    tab._on_import_image_clicked(None)
    assert opened["title"] == "Select an image" and "folder" not in opened
    [only] = list(opened["filters"])
    assert only.get_name() == "Image files" and opened["callback"] == tab._on_image_dialog_response
    assert opened["parent"] is tab.get_root()
    tab._image_dir = str(tmp_path)
    tab._on_import_image_clicked(None)
    assert opened["folder"] == str(tmp_path)
    tab._image_dir = str(tmp_path / "gone")
    opened.clear()
    tab._on_import_image_clicked(None)
    assert "folder" not in opened


def test_a_dismissed_image_dialog_starts_nothing(tab, tesseract):
    for dialog in (FakeDialog(error=GLib.Error("dismissed")), FakeDialog(None)):
        tab._on_image_dialog_response(dialog, None)
    assert not tab.is_busy and tesseract.calls == [] and tab._worker is None


def test_the_picture_is_read_with_the_saved_ocr_settings_and_the_language_chosen(tab, tesseract, tmp_path):
    save_config(ocr_psm=11, ocr_oem=1, ocr_language_source="fast", ocr_preserve_spaces=True)
    tab._source_picker.set_selected("Spanish")
    picture = tmp_path / "page.png"
    import_image(tab, picture)
    [(path, language, kwargs)] = tesseract.calls
    assert (path, language.name) == (str(picture), "Spanish")
    assert kwargs == {
        "psm": 11, "oem": 1, "language_source": "fast", "tess_vars": tess_vars_from_config(AppConfig.load()),
    }
    assert callable(tesseract.progress)


def test_with_auto_detect_no_language_is_given_to_tesseract(tab, tesseract, tmp_path):
    tab._source_picker.set_selected("Auto-detect")
    import_image(tab, tmp_path / "page.png")
    assert tesseract.calls[0][1] is None


def test_the_folder_of_the_picture_is_remembered(tab, tesseract, tmp_path):
    changed = events_of(tab, "state-changed")
    folder = tmp_path / "old"
    import_image(tab, folder / "page.png")
    assert tab._image_dir == str(folder) and tab.capture_state().image_dir == str(folder) and changed


def test_while_a_picture_is_read_the_translate_button_waits_and_is_not_a_stop_button(tab):
    release = threading.Event()
    tab._launch_job(lambda _ctx: release.wait(2) or OcrResult("Hello"), on_success=lambda _result: None)
    button = tab._translate_button
    assert button.get_label() == "Translate" and not button.get_sensitive()
    assert button.has_css_class("suggested-action") and not tab._ocr_button.get_sensitive()
    release.set()
    finish(tab)
    assert button.get_sensitive()


def test_the_text_found_is_put_in_the_box_with_the_confidence_and_the_language_it_found(tab, tesseract):
    toasts = toasts_of(tab)
    tesseract.result = OcrResult("Hola mundo", 91.6, SPANISH)
    tab._source_picker.set_selected("Auto-detect")
    import_image(tab)
    assert tab._get_buffer_text(tab._input_buffer) == "Hola mundo"
    assert tab._source_picker.get_selected() == "Spanish"
    assert toasts == ["Imported text — detected Spanish — OCR confidence: 92%"]


def test_the_confidence_alone_is_reported_when_no_language_was_found(tab, tesseract):
    toasts = toasts_of(tab)
    tesseract.result = OcrResult("Hello", 80.2)
    tab._source_picker.set_selected("English")
    import_image(tab)
    assert toasts == ["Imported text — OCR confidence: 80%"] and tab._source_picker.get_selected() == "English"


def test_a_notice_of_the_reader_is_told_instead_of_the_confidence(tab, tesseract):
    toasts = toasts_of(tab)
    tesseract.result = OcrResult("Hello", 80.0, None, "Script detection failed, so English was assumed.")
    import_image(tab)
    assert toasts == ["Imported text. Script detection failed, so English was assumed."]


def test_a_picture_without_text_says_so_and_still_shows_the_language_it_recognised(tab, tesseract):
    toasts = toasts_of(tab)
    tab._input_buffer.set_text("typed before")
    tesseract.result = OcrResult("", None, SPANISH, "ignored")
    tab._source_picker.set_selected("Auto-detect")
    import_image(tab)
    assert toasts == ["No text found in the image."] and tab._get_buffer_text(tab._input_buffer) == ""
    assert tab._source_picker.get_selected() == "Spanish"


def test_nothing_is_said_when_there_is_text_and_no_confidence_to_report(tab, tesseract):
    toasts = toasts_of(tab)
    tesseract.result = OcrResult("Hello")
    import_image(tab)
    assert toasts == [] and tab._get_buffer_text(tab._input_buffer) == "Hello"


def test_a_picture_that_cannot_be_read_is_shown_as_an_error_without_a_notification(tab, tesseract):
    toasts, finished = toasts_of(tab), events_of(tab, "job-finished")
    tesseract.error = RuntimeError("bad image")
    import_image(tab)
    assert toasts == ["Something went wrong — see below."]
    assert tab._status_label.get_label() == "Error: bad image" and finished == []


def test_the_settings_button_opens_the_ocr_settings_over_the_window(tab, monkeypatch):
    opened = []
    monkeypatch.setattr(text_tab, "present_ocr_settings", opened.append)
    tab._ocr_settings_button.emit("clicked")
    assert opened == [tab.get_root()]


# --- How it looks ---

def test_the_floating_buttons_sit_in_the_corners_of_their_boxes(tab):
    corners = {
        button: (button.get_halign(), button.get_valign(), button.get_tooltip_text(), button.get_icon_name())
        for button in (tab._clear_button, tab._copy_button)
    }
    clear, copy = tab._clear_button, tab._copy_button
    assert corners[clear] == (Gtk.Align.START, Gtk.Align.END, "Clear translated text", "edit-clear-symbolic")
    assert corners[copy] == (Gtk.Align.END, Gtk.Align.END, "Copy translated text", "edit-copy-symbolic")
    assert (tab._clear_button.get_margin_start(), tab._clear_button.get_margin_bottom()) == (8, 8)
    assert (tab._copy_button.get_margin_end(), tab._copy_button.get_margin_bottom()) == (8, 8)
    for button in (tab._clear_button, tab._copy_button, tab._ocr_button, tab._ocr_settings_button):
        assert button.has_css_class("circular")
    assert tab._ocr_button.get_tooltip_text() == "Import from image…"
    assert tab._ocr_settings_button.get_tooltip_text() == "OCR Settings"
    assert tab._ocr_button.get_icon_name() == "image-x-generic-symbolic"
    assert tab._ocr_settings_button.get_icon_name() == "emblem-system-symbolic"
