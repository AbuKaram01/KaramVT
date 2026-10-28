# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The window opening the way it was left: its size, the tab, and the choices on each tab, driven through
the real interface (see gui_support.py)."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import json
import os

import pytest
from gui_support import AppConfig, Gio, Gtk, MainWindow, downloader, env, pump

from karamvt.core.ui_state import StateStore, TextState, UiState, VideoState, WindowState
from karamvt.gui.widgets import start_in


def reopen(app, window) -> MainWindow:
    """Close the window the way the user does (which saves the state) and open the application again."""
    window.close()
    pump(0.1)
    window.destroy()
    pump(0.1)
    fresh = MainWindow(application=app)
    fresh.present()
    pump(0.1)
    return fresh


def write_state(app, state: UiState) -> None:
    assert StateStore(app.state_store.path).save(state)


def let_the_first_save_pass(app, window) -> None:
    """A window saves its size and place a moment after it first appears, with a timer the test did not start.
    A slow computer can reach the test only after that, so a test that looks at when the file is written waits
    for it, forgets what it wrote, and begins from a window with nothing pending and nothing saved."""
    assert pump(3.0, until=lambda: window._state_save_source == 0)
    app.state_store.path.unlink(missing_ok=True)


def listen(tab) -> list:
    heard = []
    tab.connect("state-changed", lambda _tab: heard.append(True))
    return heard


# --- The first start ---

def test_a_first_start_opens_on_the_video_tab_at_the_usual_width(env):
    _app, window = env
    width, height = window.get_default_size()
    assert width == 760 and height >= 720  # (A window may grow to fit its content.)
    assert window._view_stack.get_visible_child_name() == "video"


def test_a_broken_state_file_never_stops_the_window_opening(env):
    app, window = env
    app.state_store.path.parent.mkdir(parents=True, exist_ok=True)
    app.state_store.path.write_text("{ this is not json", encoding="utf-8")
    fresh = MainWindow(application=app)
    fresh.present()
    pump(0.1)
    assert fresh.get_default_size()[0] == 760 and fresh._view_stack.get_visible_child_name() == "video"
    assert app.state_store.path.with_name("state.json.broken").exists()


# --- The window ---

def test_the_tab_that_was_open_is_open_again(env):
    app, window = env
    window._view_stack.set_visible_child_name("text")
    fresh = reopen(app, window)
    assert fresh._view_stack.get_visible_child_name() == "text"


def test_the_window_size_comes_back(env):
    app, window = env
    window.set_default_size(940, 960)                           # Taller than the content needs, so it stays as asked.
    pump(0.3)                                                   # ... unless the screen is smaller (the runners on
    asked = window.get_default_size()                           # Windows are), and then the system shrinks it.
    assert asked[0] == 940
    fresh = reopen(app, window)
    assert fresh.get_default_size() == asked


def test_a_window_that_was_maximised_is_maximised_again(env, monkeypatch):
    app, window = env
    write_state(app, UiState(window=WindowState(width=900, height=700, maximized=True)))
    maximised = []
    monkeypatch.setattr(MainWindow, "maximize", lambda self: maximised.append(True))
    fresh = MainWindow(application=app)
    assert maximised == [True] and fresh.get_default_size() == (900, 700)


def test_a_maximised_window_remembers_the_size_it_goes_back_to(env, monkeypatch):
    app, window = env
    window.set_default_size(880, 940)
    monkeypatch.setattr(window, "is_maximized", lambda: True, raising=False)
    window.save_state()
    saved = StateStore(app.state_store.path).load().window
    assert (saved.width, saved.height, saved.maximized) == (880, 940, True)


def test_an_unusable_saved_size_is_replaced_by_a_sensible_one(env):
    app, window = env
    app.state_store.path.parent.mkdir(parents=True, exist_ok=True)
    app.state_store.path.write_text(json.dumps({"window": {"width": 3, "height": 3}}), encoding="utf-8")
    fresh = MainWindow(application=app)
    width, height = fresh.get_default_size()
    assert width >= 360 and height >= 300


# --- The Video tab ---

def test_the_video_tab_comes_back_as_it_was_left(env, tmp_path):
    app, window = env
    video = tmp_path / "courses" / "lecture 1.mp4"
    video.parent.mkdir()
    video.write_bytes(b"x")
    subtitles = tmp_path / "subtitles"
    subtitles.mkdir()
    tab = window._video_tab
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["Arabic", "French"])
    tab._format_row.set_selected(1)
    tab._show_file(str(video))
    tab._show_output_dir(str(subtitles))

    tab = reopen(app, window)._video_tab
    assert tab._mode_row.get_selected() == 1 and tab._target_row.get_visible()
    assert tab._source_picker.get_selected() == "English"
    assert tab._target_picker.get_selected() == ["Arabic", "French"]
    assert tab._format_row.get_selected() == 1
    assert tab._selected_path == str(video)
    assert tab._file_row.get_title() == "lecture 1.mp4" and tab._file_row.get_subtitle() == str(video)
    assert tab._output_dir_path == str(subtitles) and tab._output_dir_clear_button.get_visible()


def test_a_chosen_folder_in_batch_mode_comes_back_with_its_file_count(env, tmp_path):
    app, window = env
    course = tmp_path / "python course"
    course.mkdir()
    for name in ("one.mp4", "two.mkv", "notes.txt"):
        (course / name).write_bytes(b"x")
    tab = window._video_tab
    tab._batch_row.set_active(True)
    tab._show_folder(str(course))

    tab = reopen(app, window)._video_tab
    assert tab._batch_row.get_active() and tab._selected_path == str(course)
    assert tab._file_row.get_title() == "python course" and "2 file(s) found" in tab._file_row.get_subtitle()
    assert tab._choose_button.get_label() == "Choose Folder…"


def test_a_file_that_has_gone_is_not_restored_but_its_folder_is_still_where_dialogs_open(env, tmp_path):
    app, window = env
    folder = tmp_path / "videos"
    folder.mkdir()
    write_state(app, UiState(video=VideoState(input_path=str(folder / "moved.mp4"))))
    tab = MainWindow(application=app)._video_tab
    assert tab._selected_path is None and tab._file_row.get_title() == "No file selected"
    assert tab._input_dir == str(folder)


def test_a_folder_that_has_gone_is_not_restored(env, tmp_path):
    app, window = env
    write_state(app, UiState(video=VideoState(
        batch=True, input_path=str(tmp_path / "deleted course"), output_dir=str(tmp_path / "deleted output"),
    )))
    tab = MainWindow(application=app)._video_tab
    assert tab._batch_row.get_active() and tab._selected_path is None
    assert tab._output_dir_path is None and not tab._output_dir_clear_button.get_visible()


def test_a_language_the_picker_does_not_offer_is_ignored_and_the_rest_still_restored(env):
    app, window = env
    write_state(app, UiState(video=VideoState(
        mode="translate", source_language="Klingon", target_languages=["Arabic", "Klingon"], output_format="vtt",
    )))
    tab = MainWindow(application=app)._video_tab
    assert tab._source_picker.get_selected() == "Auto-detect"  # The default.
    assert tab._target_picker.get_selected() == ["Arabic"]
    assert tab._mode_row.get_selected() == 1 and tab._format_row.get_selected() == 1


def test_a_source_language_whisper_cannot_hear_is_not_restored_on_the_video_tab(env):
    """The Video tab offers only the languages speech recognition handles; a name from elsewhere is ignored."""
    app, window = env
    tab = window._video_tab
    assert "Auto-detect" in tab._source_picker.language_names()
    offered = set(tab._source_picker.language_names())
    elsewhere = next(name for name in window._text_tab._source_picker.language_names() if name not in offered)
    write_state(app, UiState(video=VideoState(source_language=elsewhere)))
    assert MainWindow(application=app)._video_tab._source_picker.get_selected() == "Auto-detect"


def test_the_file_dialogs_open_in_the_folder_the_user_last_worked_in(env, tmp_path):
    app, window = env
    tab = window._video_tab
    folder = tmp_path / "videos"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x")
    tab._show_file(str(folder / "a.mp4"))
    assert tab._input_dir == str(folder)

    dialog = Gtk.FileDialog()
    start_in(dialog, tab._input_dir)
    assert dialog.get_initial_folder().get_path() == str(folder)


def test_a_folder_that_is_gone_is_not_offered_to_a_dialog(tmp_path):
    dialog = Gtk.FileDialog()
    start_in(dialog, str(tmp_path / "gone"))
    start_in(dialog, "")
    assert dialog.get_initial_folder() is None


def test_a_batch_folder_is_opened_beside_so_it_is_in_view(env, tmp_path):
    app, window = env
    course = tmp_path / "courses" / "python"
    course.mkdir(parents=True)
    tab = window._video_tab
    tab._show_folder(str(course) + "/")
    assert tab._input_dir == str(tmp_path / "courses")


# --- The Text tab ---

def test_the_text_tab_comes_back_as_it_was_left(env, tmp_path):
    app, window = env
    tab = window._text_tab
    tab._source_picker.set_selected("Arabic")
    tab._target_picker.set_selected("French")
    tab._input_buffer.set_text("مرحبا بالعالم")
    tab._output_buffer.set_text("Bonjour le monde")
    tab._image_dir = str(tmp_path)

    tab = reopen(app, window)._text_tab
    assert (tab._source_picker.get_selected(), tab._target_picker.get_selected()) == ("Arabic", "French")
    assert tab._get_buffer_text(tab._input_buffer) == "مرحبا بالعالم"
    assert tab._get_buffer_text(tab._output_buffer) == "Bonjour le monde"
    assert tab._char_count_label.get_label().startswith("13 /")
    assert tab._image_dir == str(tmp_path)


def test_the_text_is_not_kept_when_the_history_is_turned_off(env):
    app, window = env
    AppConfig(history_enabled=False).save()
    tab = window._text_tab
    tab._source_picker.set_selected("Arabic")
    tab._input_buffer.set_text("private words")
    tab._output_buffer.set_text("palabras privadas")
    window.save_state()

    raw = app.state_store.path.read_text(encoding="utf-8")
    assert "private words" not in raw and "palabras privadas" not in raw
    tab = reopen(app, window)._text_tab
    assert tab._source_picker.get_selected() == "Arabic"  # The choice stays.
    assert tab._get_buffer_text(tab._input_buffer) == ""


def test_text_that_is_too_long_for_the_box_is_cut_when_restored(env):
    from karamvt.core.text_translation import MAX_INPUT_CHARS

    app, window = env
    write_state(app, UiState(text=TextState(input_text="x" * (MAX_INPUT_CHARS + 300))))
    tab = MainWindow(application=app)._text_tab
    assert len(tab._get_buffer_text(tab._input_buffer)) == MAX_INPUT_CHARS


def test_a_language_the_text_tab_does_not_offer_is_ignored(env):
    app, window = env
    default_source = window._text_tab._source_picker.get_selected()
    default_target = window._text_tab._target_picker.get_selected()
    write_state(app, UiState(text=TextState(source_language="Klingon", target_language="Auto-detect")))
    tab = MainWindow(application=app)._text_tab
    assert tab._source_picker.get_selected() == default_source
    assert tab._target_picker.get_selected() == default_target  # "Auto-detect" is no target.


# --- When it is saved ---

def test_a_change_is_saved_a_moment_later_not_on_every_step(env):
    app, window = env
    let_the_first_save_pass(app, window)
    path = app.state_store.path
    tab = window._text_tab
    tab._source_picker.set_selected("French")
    tab.emit("state-changed")
    assert not path.exists()
    assert pump(3.0, until=path.exists)
    assert StateStore(path).load().text.source_language == "French"


def test_many_quick_changes_make_one_write(env, monkeypatch):
    app, window = env
    writes = []
    real_save = app.state_store.save
    monkeypatch.setattr(app.state_store, "save", lambda state: writes.append(state) or real_save(state))
    tab = window._text_tab
    for number in range(30):
        tab._input_buffer.set_text(f"typing, character {number}")
    assert pump(3.0, until=lambda: bool(writes))
    pump(1.5)
    assert len(writes) == 1 and writes[0].text.input_text == "typing, character 29"


def test_closing_the_window_saves_straight_away(env):
    app, window = env
    window._text_tab._target_picker.set_selected("German")
    window._view_stack.set_visible_child_name("text")
    window.close()
    pump(0.1)
    saved = StateStore(app.state_store.path).load()
    assert saved.text.target_language == "German" and saved.tab == "text"


def test_closing_cancels_the_pending_save_so_nothing_is_written_twice(env):
    app, window = env
    window._text_tab.emit("state-changed")
    assert window._state_save_source != 0
    window.save_state()
    assert window._state_save_source == 0


def test_restoring_the_saved_state_does_not_itself_schedule_a_save(env, tmp_path):
    app, window = env
    write_state(app, UiState(tab="text", video=VideoState(mode="translate", target_languages=["Arabic"])))
    fresh = MainWindow(application=app)
    assert fresh._state_save_source == 0
    assert fresh._video_tab._mode_row.get_selected() == 1  # (It was restored; that was not announced.)


def test_a_window_that_is_taken_apart_with_a_save_pending_writes_nothing(env):
    """The save is a moment away when the window goes (its widgets are gone by the time it would run):
    what is left of them must not be written down as the state."""
    app, window = env
    let_the_first_save_pass(app, window)
    window._text_tab._target_picker.set_selected("Italian")
    window._text_tab.emit("state-changed")
    assert window._state_save_source != 0
    window.run_dispose()  # What happens when the last reference goes.
    assert window._state_save_source == 0
    pump(1.5)
    assert not app.state_store.path.exists()
    assert window.save_state() is False


def test_a_window_taken_apart_after_closing_leaves_the_state_it_saved_on_closing(env):
    app, window = env
    window._text_tab._target_picker.set_selected("Italian")
    window.close()
    pump(0.1)
    window.run_dispose()
    pump(1.5)
    assert StateStore(app.state_store.path).load().text.target_language == "Italian"


def test_the_state_is_saved_even_if_the_window_is_closed_while_a_job_is_stopping(env, tmp_path, monkeypatch):
    """Close Anyway, under a running job, ends in destroy(): that path saves too."""
    app, window = env
    window._text_tab._target_picker.set_selected("Italian")
    window._video_tab.is_busy = True
    monkeypatch.setattr(window._video_tab, "stop_for_quit", lambda: setattr(window._video_tab, "is_busy", False))
    window._on_close_dialog_response(None, "close")
    pump(0.5)
    assert StateStore(app.state_store.path).load().text.target_language == "Italian"


# --- Every choice is announced ---

@pytest.mark.parametrize("act", [
    lambda tab, tmp: tab._mode_row.set_selected(1),
    lambda tab, tmp: tab._format_row.set_selected(1),
    lambda tab, tmp: tab._batch_row.set_active(True),
    lambda tab, tmp: tab._source_picker.emit("language-selected", "English"),
    lambda tab, tmp: tab._target_picker._checks["Arabic"].set_active(True),
    lambda tab, tmp: tab._show_file(str(tmp / "a.mp4")),
    lambda tab, tmp: tab._show_folder(str(tmp)),
    lambda tab, tmp: tab._show_output_dir(str(tmp)),
    lambda tab, tmp: tab._on_clear_output_dir(None),
], ids=["mode", "format", "batch", "source", "target", "file", "folder", "output folder", "clear output folder"])
def test_every_choice_on_the_video_tab_is_announced(env, tmp_path, act):
    _app, window = env
    tab = window._video_tab
    heard = listen(tab)
    act(tab, tmp_path)
    assert heard


@pytest.mark.parametrize("act", [
    lambda tab: tab._input_buffer.set_text("typing"),
    lambda tab: tab._source_picker.emit("language-selected", "English"),
    lambda tab: tab._target_picker.emit("language-selected", "French"),
    lambda tab: tab._clear_button.emit("clicked"),
    lambda tab: tab._swap_button.emit("clicked"),
    lambda tab: tab.show_translation("English", "French", "Hello", "Bonjour"),
], ids=["typing", "source", "target", "clear", "swap", "from history"])
def test_every_choice_on_the_text_tab_is_announced(env, act):
    _app, window = env
    tab = window._text_tab
    tab._source_picker.set_selected("English")
    heard = listen(tab)
    act(tab)
    assert heard


def test_the_end_of_a_translation_is_announced_so_the_new_text_is_saved(env, tmp_path, monkeypatch):
    from gui_support import fake_translation, ready_models, run_text_job

    _app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch)
    tab = window._text_tab
    heard = listen(tab)
    run_text_job(tab)
    assert heard and tab._get_buffer_text(tab._output_buffer) == "Hola"
