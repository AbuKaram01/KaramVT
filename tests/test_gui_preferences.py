# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Preferences window driven through the real interface (see gui_support.py).

Covers what it shows of the saved settings, that every change is saved at once without undoing changes made
elsewhere, and its pickers and confirmations. The model downloads have their own tests (test_gui_downloads.py).
"""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import pytest
from gui_support import Adw, AppConfig, Gio, GLib, Gtk, capture_toasts, downloader, env, open_preferences, pump, walk

from karamvt.core.history import MIN_LIMIT
from karamvt.core.llm_settings import LLM_PARAMS
from karamvt.core.models import ProcessingDevice
from karamvt.gui.preferences import PreferencesWindow


@pytest.fixture
def prefs(env):
    """The Preferences window, opened from the application."""
    app, window = env
    return open_preferences(app, window)


@pytest.fixture
def bare(env):
    """Preferences opened without the model downloads or the history (as a window built by itself)."""
    window = PreferencesWindow()
    yield window
    window.destroy()


def saved() -> AppConfig:
    """The settings as they are saved now."""
    return AppConfig.load()


def button_in(widget, *, tooltip=None, label=None):
    """The first button inside ``widget`` that has the given tooltip and label (a missing one matches any)."""
    return next(
        w for w in walk(widget)
        if isinstance(w, Gtk.Button) and (tooltip is None or w.get_tooltip_text() == tooltip)
        and (label is None or w.get_label() == label)
    )


def row_titled(widget, title):
    """The first preferences row inside ``widget`` with this title."""
    return next(w for w in walk(widget) if isinstance(w, Adw.PreferencesRow) and w.get_title() == title)


def groups_of(window) -> list[str]:
    """The titles of the window's preference groups, in the order shown."""
    return [w.get_title() for w in walk(window) if isinstance(w, Adw.PreferencesGroup) and w.get_title()]


class Dialogs:
    """Alert dialogs that are collected instead of shown, so that a test can answer them."""

    def __init__(self, monkeypatch):
        self.shown: list = []
        monkeypatch.setattr(Adw.AlertDialog, "present", lambda dialog, parent=None: self.shown.append((dialog, parent)))

    def answer(self, response):
        dialog, _parent = self.shown.pop()
        dialog.emit("response", response)
        return dialog


@pytest.fixture
def dialogs(monkeypatch):
    """Collect the alert dialogs that the window presents; the test answers them through ``answer``."""
    return Dialogs(monkeypatch)


class FilePickers:
    """File dialogs that record what was asked and answer with the path that the test sets in ``picked``."""

    def __init__(self, monkeypatch):
        self.asked: list = []
        self.picked = None  # A path, or None for a dialog that is dismissed.

        def ask(kind):
            def start(dialog, parent, cancellable, callback):
                self.asked.append((kind, dialog.get_title(), parent, dialog))
                callback(dialog, None)
            return start

        def finish(dialog, _result):
            if self.picked is None:
                raise GLib.Error("Dismissed by user")
            return Gio.File.new_for_path(self.picked)

        monkeypatch.setattr(Gtk.FileDialog, "select_folder", ask("folder"))
        monkeypatch.setattr(Gtk.FileDialog, "open", ask("file"))
        monkeypatch.setattr(Gtk.FileDialog, "select_folder_finish", finish)
        monkeypatch.setattr(Gtk.FileDialog, "open_finish", finish)


@pytest.fixture
def pickers(monkeypatch):
    """Replace the file dialogs with a FilePickers stand-in."""
    return FilePickers(monkeypatch)


# --- What it shows ---

def test_the_groups_come_in_the_order_the_settings_matter(prefs):
    assert groups_of(prefs) == [
        "Local Models", "Download Models", "Performance", "Notifications", "History",
        "Translation Model — Advanced", "Saved Progress", "OCR (Import from Image)",
    ]


def test_without_downloads_or_history_those_parts_are_left_out(bare):
    assert "Download Models" not in groups_of(bare)
    assert not hasattr(bare, "_folder_row") and not hasattr(bare, "_saved_row")
    assert not [w for w in walk(bare) if isinstance(w, Adw.ActionRow) and w.get_title() == "Saved translations"]


def test_the_saved_settings_are_shown(env, tmp_path):
    app, window = env
    whisper = tmp_path / "w"
    whisper.mkdir()
    AppConfig(
        whisper_model_path=str(whisper), processing_device="gpu", cpu_threads=7, notify_when_done=False,
        history_enabled=False, history_limit=300, resume_transcription=True, llm_temperature=0.9, llm_top_k=12,
    ).save()
    prefs = open_preferences(app, window)
    assert prefs._whisper_row.get_text() == str(whisper) and prefs._llm_row.get_text() == ""
    assert prefs._device_row.get_selected() == 2 and prefs._threads_row.get_value() == 7
    assert not prefs._notify_row.get_active() and not prefs._history_row.get_active()
    assert prefs._history_limit_row.get_value() == 300 and not prefs._history_limit_row.get_sensitive()
    assert prefs._resume_transcription_row.get_active()
    assert prefs._llm_rows["llm_temperature"].get_value() == pytest.approx(0.9)
    assert prefs._llm_rows["llm_top_k"].get_value() == 12


def test_a_device_the_program_does_not_know_is_shown_as_auto(env):
    app, window = env
    AppConfig(processing_device="warp-drive").save()
    assert open_preferences(app, window)._device_row.get_selected() == 0


def test_the_devices_are_offered_in_a_fixed_order(prefs):
    model = prefs._device_row.get_model()
    assert [model.get_string(i) for i in range(model.get_n_items())] == [
        device.label for device in (ProcessingDevice.AUTO, ProcessingDevice.CPU, ProcessingDevice.GPU)
    ]


def test_every_sampling_setting_has_a_row_with_its_range(prefs):
    assert list(prefs._llm_rows) == [param[0] for param in LLM_PARAMS]
    for field, _kwarg, title, subtitle, minimum, maximum, step, digits, _default in LLM_PARAMS:
        row = prefs._llm_rows[field]
        adjustment = row.get_adjustment()
        assert (row.get_title(), row.get_subtitle(), row.get_digits()) == (title, subtitle, digits)
        assert (adjustment.get_lower(), adjustment.get_upper(), adjustment.get_step_increment()) == (
            minimum, maximum, step,
        )


def test_the_history_count_is_shown_and_is_up_to_date_when_the_window_is_looked_at_again(prefs, env):
    app, _window = env
    assert prefs._saved_row.get_subtitle() == "Nothing saved yet" and not prefs._clear_history_button.get_sensitive()
    app.history.add("English", "Spanish", "a", "b", model="m", seconds=1.0, now=1_700_000_000.0)
    prefs.notify("is-active")
    assert prefs._saved_row.get_subtitle() == "1 saved on this computer"
    assert prefs._clear_history_button.get_sensitive()


# --- Changes are saved at once ---

def test_a_typed_model_path_is_saved_and_checked(prefs, tmp_path):
    folder = tmp_path / "whisper"
    model = tmp_path / "m.gguf"
    prefs._whisper_row.set_text(str(folder))
    prefs._llm_row.set_text(str(model))
    assert saved().whisper_model_path == str(folder) and saved().llm_model_path == str(model)
    assert prefs._whisper_row.has_css_class("error") and prefs._llm_row.has_css_class("error")
    folder.mkdir()
    model.write_bytes(b"x")
    prefs._whisper_row.set_text(str(folder) + "/")  # Any change of the text makes the row check the path again.
    prefs._whisper_row.set_text(str(folder))
    prefs._llm_row.set_text("")
    prefs._llm_row.set_text(str(model))
    assert not prefs._whisper_row.has_css_class("error") and not prefs._llm_row.has_css_class("error")


def test_a_path_of_the_wrong_kind_is_marked(prefs, tmp_path):
    a_file = tmp_path / "file.bin"
    a_file.write_bytes(b"x")
    prefs._whisper_row.set_text(str(a_file))
    prefs._llm_row.set_text(str(tmp_path))
    assert prefs._whisper_row.has_css_class("error") and prefs._llm_row.has_css_class("error")


def test_an_empty_path_is_not_an_error(prefs):
    prefs._whisper_row.set_text("nowhere")
    prefs._whisper_row.set_text("")
    assert not prefs._whisper_row.has_css_class("error")


def test_paths_that_do_not_exist_are_marked_when_the_window_opens(env, tmp_path):
    app, window = env
    AppConfig(whisper_model_path=str(tmp_path / "gone"), llm_model_path=str(tmp_path / "gone.gguf")).save()
    prefs = open_preferences(app, window)
    assert prefs._whisper_row.has_css_class("error") and prefs._llm_row.has_css_class("error")


def test_the_device_and_the_threads_are_saved(prefs):
    prefs._device_row.set_selected(1)
    assert saved().processing_device == ProcessingDevice.CPU.value
    prefs._threads_row.set_value(5)
    assert saved().cpu_threads == 5 and saved().processing_device == ProcessingDevice.CPU.value


def test_a_sampling_value_is_saved_as_the_kind_of_number_it_is(prefs):
    prefs._llm_rows["llm_temperature"].set_value(0.35)
    prefs._llm_rows["llm_top_k"].set_value(64)
    prefs._llm_rows["llm_seed"].set_value(12345)
    config = saved()
    assert config.llm_temperature == pytest.approx(0.35) and isinstance(config.llm_temperature, float)
    assert config.llm_top_k == 64 and isinstance(config.llm_top_k, int)
    assert config.llm_seed == 12345 and isinstance(config.llm_seed, int)


def test_reset_puts_every_sampling_setting_back_to_its_default(prefs):
    for row in prefs._llm_rows.values():
        row.set_value(row.get_adjustment().get_upper())
    reset = button_in(prefs, label="Reset")
    reset.emit("clicked")
    for field, _kwarg, _title, _subtitle, _minimum, _maximum, _step, _digits, default in LLM_PARAMS:
        assert prefs._llm_rows[field].get_value() == pytest.approx(default)
        assert getattr(saved(), field) == pytest.approx(default)


def test_a_change_here_does_not_undo_a_change_made_elsewhere_since(prefs):
    config = saved()
    config.ocr_psm = 11
    config.save()
    prefs._threads_row.set_value(3)
    assert saved().ocr_psm == 11 and saved().cpu_threads == 3
    config = saved()
    config.cpu_threads = 9
    config.save()
    prefs._notify_row.set_active(False)
    prefs._history_row.set_active(False)
    assert saved().cpu_threads == 9


def test_showing_the_saved_paths_does_not_write_them_back(prefs, tmp_path):
    config = saved()
    config.whisper_model_path, config.llm_model_path = str(tmp_path / "new-whisper"), str(tmp_path / "new.gguf")
    config.save()
    written = []
    original = AppConfig.save
    AppConfig.save = lambda self: (written.append(self), original(self))[1]
    try:
        prefs.refresh_model_paths()
    finally:
        AppConfig.save = original
    assert written == []
    assert prefs._whisper_row.get_text() == str(tmp_path / "new-whisper")
    assert prefs._llm_row.get_text() == str(tmp_path / "new.gguf")
    assert saved().llm_model_path == str(tmp_path / "new.gguf")
    assert prefs._whisper_row.has_css_class("error") and prefs._llm_row.has_css_class("error")
    prefs._threads_row.set_value(2)  # Typing is saved again afterwards.
    assert saved().cpu_threads == 2


def test_the_notification_switch_is_saved(prefs):
    prefs._notify_row.set_active(False)
    assert saved().notify_when_done is False
    prefs._notify_row.set_active(True)
    assert saved().notify_when_done is True


def test_the_history_switch_is_saved_and_gives_the_limit_its_use(prefs):
    prefs._history_row.set_active(False)
    assert saved().history_enabled is False and not prefs._history_limit_row.get_sensitive()
    prefs._history_row.set_active(True)
    assert saved().history_enabled is True and prefs._history_limit_row.get_sensitive()


def test_the_history_limit_is_saved(prefs):
    prefs._history_limit_row.set_value(250)
    assert saved().history_limit == 250
    assert MIN_LIMIT <= prefs._history_limit_row.get_adjustment().get_lower()


# --- Confirmations ---

def test_resuming_transcription_is_only_switched_on_when_the_warning_is_accepted(prefs, dialogs):
    prefs._resume_transcription_row.set_active(True)
    assert not prefs._resume_transcription_row.get_active() and saved().resume_transcription is False
    [(dialog, parent)] = dialogs.shown
    assert dialog.get_heading() == "Enable resuming transcription?" and parent is prefs
    assert [dialog.get_response_label(r) for r in ("cancel", "enable")] == ["Keep Off", "Enable"]
    assert dialog.get_response_appearance("enable") == Adw.ResponseAppearance.DESTRUCTIVE
    assert dialog.get_default_response() == "cancel" and dialog.get_close_response() == "cancel"
    dialogs.answer("enable")
    assert prefs._resume_transcription_row.get_active() and saved().resume_transcription is True


def test_resuming_transcription_stays_off_when_the_warning_is_declined(prefs, dialogs):
    prefs._resume_transcription_row.set_active(True)
    dialogs.answer("cancel")
    assert not prefs._resume_transcription_row.get_active() and saved().resume_transcription is False


def test_resuming_transcription_is_switched_off_without_asking(env, dialogs):
    app, window = env
    AppConfig(resume_transcription=True).save()
    prefs = open_preferences(app, window)
    # The row shows as on, and no question is asked.
    assert prefs._resume_transcription_row.get_active() and dialogs.shown == []
    prefs._resume_transcription_row.set_active(False)
    assert dialogs.shown == [] and saved().resume_transcription is False


def test_the_history_is_cleared_after_the_user_agrees(prefs, env, dialogs):
    app, _window = env
    for number in range(2):
        app.history.add("English", "Spanish", f"a{number}", "b", model="m", seconds=1.0, now=1_700_000_000.0 + number)
    prefs.notify("is-active")
    toasts = capture_toasts(prefs)
    prefs._clear_history_button.emit("clicked")
    [(dialog, _parent)] = dialogs.shown
    assert dialog.get_heading() == "Clear the whole history?"
    assert dialog.get_body() == "All 2 saved translations will be deleted. This can’t be undone."
    dialogs.answer("cancel")
    assert app.history.count() == 2 and toasts == []
    prefs._clear_history_button.emit("clicked")
    dialogs.answer("confirm")
    assert app.history.count() == 0 and [t.get_title() for t in toasts] == ["History cleared"]
    assert prefs._saved_row.get_subtitle() == "Nothing saved yet" and not prefs._clear_history_button.get_sensitive()


def test_a_single_saved_translation_is_not_called_plural(prefs, env, dialogs):
    app, _window = env
    app.history.add("English", "Spanish", "a", "b", model="m", seconds=1.0, now=1_700_000_000.0)
    prefs._clear_history_button.emit("clicked")
    assert dialogs.shown[0][0].get_body().startswith("All 1 saved translation will")


# --- Saved progress ---

def test_the_unfinished_jobs_row_says_whether_there_is_anything_to_clear(prefs, env, tmp_path):
    assert prefs._resume_row.get_subtitle() == "Nothing saved — stopped jobs would appear here."
    assert not prefs._clear_progress_button.get_sensitive()
    from karamvt.core.checkpoint import JobStore
    store = JobStore()
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"video")
    journal, _note = store.open_journal(str(video), "ar", {"tgt": "ar"}, total=1, digest="d", sampling={})
    journal.close()
    prefs._update_resume_row()
    assert prefs._resume_row.get_subtitle().startswith("Stopped or interrupted jobs can be continued.")
    assert prefs._clear_progress_button.get_sensitive()
    toasts = capture_toasts(prefs)
    prefs._clear_progress_button.emit("clicked")
    assert JobStore().count_files() == 0 and [t.get_title() for t in toasts] == ["Saved progress cleared."]
    assert not prefs._clear_progress_button.get_sensitive()


def test_saved_progress_is_kept_while_a_video_job_is_running(prefs, env, monkeypatch, tmp_path):
    _app, window = env
    from karamvt.core.checkpoint import JobStore
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"video")
    journal, _note = JobStore().open_journal(str(video), "ar", {"tgt": "ar"}, total=1, digest="d", sampling={})
    journal.close()
    monkeypatch.setattr(type(window), "video_job_running", property(lambda _self: True))
    toasts = capture_toasts(prefs)
    prefs._clear_progress_button.emit("clicked")
    assert [t.get_title() for t in toasts] == ["Can’t clear saved progress while a video job is running."]
    assert JobStore().count_files() > 0


# --- OCR settings ---

def test_the_ocr_settings_open_over_the_preferences(prefs, monkeypatch):
    opened = []
    monkeypatch.setattr("karamvt.gui.preferences.present_ocr_settings", opened.append)
    button_in(prefs, label="Open").emit("clicked")
    assert opened == [prefs]


def test_the_ocr_settings_do_not_open_while_a_text_job_is_running(prefs, env, monkeypatch):
    _app, window = env
    opened = []
    monkeypatch.setattr("karamvt.gui.preferences.present_ocr_settings", opened.append)
    monkeypatch.setattr(type(window), "text_job_running", property(lambda _self: True))
    toasts = capture_toasts(prefs)
    button_in(prefs, label="Open").emit("clicked")
    assert opened == [] and [t.get_title() for t in toasts] == ["OCR Settings are unavailable while a job is running."]


def test_without_a_window_behind_it_the_ocr_settings_open_and_saved_progress_clears(bare, monkeypatch):
    opened = []
    monkeypatch.setattr("karamvt.gui.preferences.present_ocr_settings", opened.append)
    button_in(bare, label="Open").emit("clicked")
    button_in(bare, label="Clear").emit("clicked")
    assert opened == [bare]


# --- Pickers ---

def test_the_whisper_folder_is_chosen_with_a_folder_picker(prefs, pickers, tmp_path):
    pickers.picked = str(tmp_path)
    button_in(prefs._whisper_row, tooltip="Browse…").emit("clicked")
    [(kind, title, parent, _dialog)] = pickers.asked
    assert (kind, title, parent) == ("folder", "Select the Faster-Whisper model directory", prefs)
    assert prefs._whisper_row.get_text() == str(tmp_path) and saved().whisper_model_path == str(tmp_path)


def test_the_llm_file_is_chosen_with_a_picker_that_offers_gguf_files(prefs, pickers, tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    pickers.picked = str(model)
    button_in(prefs._llm_row, tooltip="Browse…").emit("clicked")
    [(kind, title, parent, dialog)] = pickers.asked
    assert (kind, title, parent) == ("file", "Select the LLM .gguf model file", prefs)
    [only] = list(dialog.get_filters())
    assert only.get_name() == "GGUF models"
    assert prefs._llm_row.get_text() == str(model) and saved().llm_model_path == str(model)
    assert not prefs._llm_row.has_css_class("error")


def test_a_picker_that_is_dismissed_changes_nothing(prefs, pickers):
    prefs._whisper_row.set_text("before")
    pickers.picked = None
    button_in(prefs._whisper_row, tooltip="Browse…").emit("clicked")
    button_in(prefs._llm_row, tooltip="Browse…").emit("clicked")
    button_in(prefs._folder_row, tooltip="Browse…").emit("clicked")
    assert len(pickers.asked) == 3
    assert prefs._whisper_row.get_text() == "before" and prefs._llm_row.get_text() == ""
    assert saved().whisper_model_path == "before"


def test_the_download_folder_is_chosen_with_a_picker_and_applied_at_once(prefs, pickers, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    pickers.picked = str(elsewhere)
    button_in(prefs._folder_row, tooltip="Browse…").emit("clicked")
    [(kind, title, parent, _dialog)] = pickers.asked
    assert (kind, title, parent) == ("folder", "Select the download folder", prefs)
    assert prefs._folder_row.get_text() == str(elsewhere) and saved().models_dir == str(elsewhere)


def test_the_browse_buttons_look_the_same_except_the_one_inside_the_menu(prefs):
    whisper = button_in(prefs._whisper_row, tooltip="Browse…")
    llm = button_in(prefs._llm_row, tooltip="Browse…")
    folder = button_in(prefs._folder_row, tooltip="Browse…")
    for each in (whisper, llm, folder):
        assert each.get_icon_name() == "folder-open-symbolic" and each.get_valign() == Gtk.Align.CENTER
    assert [b.has_css_class("flat") for b in (whisper, llm, folder)] == [False, False, True]
