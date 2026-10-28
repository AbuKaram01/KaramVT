# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Giving the Video tab a subtitle file: the file dialog shows it, the tab accepts it and says what it will do
with it (see gui_support.py)."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import sys

import pytest
from gui_support import Gio, GLib, Gtk, downloader, env, pump, ready_models

from karamvt.gui.tabs import video_tab
from karamvt.gui.tabs.video_tab import file_filters

HINT = "Video, or a .srt / .vtt subtitle file to translate"
SWITCHED = "Subtitle files are translated, not transcribed: switched to Translate. Choose the target language(s)."


def entry(name, content_type="application/octet-stream"):
    """What the file dialog hands a filter for a file: its name and the type the system gave it."""
    info = Gio.FileInfo()
    info.set_display_name(name)
    info.set_name(name)
    info.set_content_type(content_type)
    return info


def filter_named(name):
    store = file_filters()
    return next(store.get_item(i) for i in range(store.get_n_items()) if store.get_item(i).get_name() == name)


def toasts_of(tab) -> list[str]:
    heard: list[str] = []
    tab.connect("toast-requested", lambda _tab, message: heard.append(message))
    return heard


class FakeDialog:
    """Stands in for the dialog in the answer to "which file?"."""

    def __init__(self, path=None, error=None):
        self.path, self.error = path, error

    def open_finish(self, _result):
        if self.error is not None:
            raise self.error
        return Gio.File.new_for_path(self.path) if self.path else None


def choose(tab, path):
    tab._on_file_dialog_response(FakeDialog(path), None)


# --- The dialog's filters ---

def test_the_dialog_offers_these_filters_and_opens_on_the_one_with_everything_the_tab_accepts():
    store = file_filters()
    assert [store.get_item(i).get_name() for i in range(store.get_n_items())] == [
        "Videos and subtitles", "Video files", "Subtitle files", "All files",
    ]


@pytest.mark.parametrize("name", [
    "lecture.srt", "lecture.vtt", "LECTURE.SRT", "Lecture.Vtt", "lecture.en.srt", "محاضرة.srt",
    "clip.mp4", "clip.MKV", "clip.avi", "clip.mov", "clip.webm", "clip.flv", "clip.wmv", "clip.m4v", "clip.mpg",
    "clip.mpeg",
])
def test_the_first_filter_lets_through_videos_and_subtitles_by_their_names_alone(name):
    """The type the system gives a file is often a guess (or no use), so the name's ending decides."""
    assert filter_named("Videos and subtitles").match(entry(name, "application/octet-stream"))


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-subrip", "text/vtt", "application/octet-stream"])
def test_a_subtitle_file_is_let_through_whatever_type_the_system_gives_it(content_type):
    assert filter_named("Videos and subtitles").match(entry("lecture.srt", content_type))
    assert filter_named("Subtitle files").match(entry("lecture.vtt", content_type))


def test_a_ts_video_is_let_through_though_the_system_may_call_it_a_qt_translation_file():
    assert filter_named("Videos and subtitles").match(entry("broadcast.ts", "text/vnd.qt.linguist"))
    assert filter_named("Video files").match(entry("broadcast.ts", "text/vnd.qt.linguist"))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="GLib on Windows knows a file's type from its extension only, so a made-up MIME string means nothing there",
)
def test_a_video_is_let_through_by_its_type_when_its_name_is_unusual():
    assert filter_named("Videos and subtitles").match(entry("clip.3gp", "video/3gpp"))


@pytest.mark.parametrize("name, content_type", [
    ("notes.txt", "text/plain"), ("photo.png", "image/png"), ("song.mp3", "audio/mpeg"), ("srt", "text/plain"),
    ("lecture.srt.bak", "application/octet-stream"),
])
def test_the_first_filter_does_not_let_other_files_through(name, content_type):
    assert not filter_named("Videos and subtitles").match(entry(name, content_type))


def test_the_other_filters_do_their_one_job():
    assert filter_named("Video files").match(entry("clip.mp4", "video/mp4"))
    assert not filter_named("Video files").match(entry("lecture.srt", "text/plain"))
    assert filter_named("Subtitle files").match(entry("lecture.srt", "text/plain"))
    assert not filter_named("Subtitle files").match(entry("clip.mp4", "video/mp4"))
    assert filter_named("All files").match(entry("notes.txt", "text/plain"))


def test_every_filter_list_is_a_fresh_one():
    assert file_filters() is not file_filters()


def test_choosing_a_file_opens_the_dialog_on_the_first_filter(env, monkeypatch, tmp_path):
    _app, window = env
    tab = window._video_tab
    opened = {}

    class Dialog:
        def __init__(self, title=""):
            opened["title"] = title

        def set_initial_folder(self, folder):
            opened["folder"] = folder

        def set_filters(self, filters):
            opened["filters"] = filters

        def set_default_filter(self, chosen):
            opened["default"] = chosen

        def open(self, parent, cancellable, callback):
            opened["callback"] = callback

    monkeypatch.setattr(video_tab.Gtk, "FileDialog", Dialog)
    tab._on_choose_clicked(None)
    assert opened["title"] == "Select a video or subtitle file"
    assert opened["default"] is opened["filters"].get_item(0)
    assert opened["default"].get_name() == "Videos and subtitles"
    assert opened["callback"] == tab._on_file_dialog_response


# --- Choosing a subtitle file ---

def test_the_tab_says_in_its_hint_that_a_subtitle_file_is_accepted(env):
    _app, window = env
    tab = window._video_tab
    assert tab._file_row.get_subtitle() == HINT
    tab._batch_row.set_active(True)
    tab._batch_row.set_active(False)
    assert tab._file_row.get_subtitle() == HINT


@pytest.mark.parametrize("name", ["lecture.srt", "lecture.vtt", "LECTURE.SRT", "Lecture.VTT"])
def test_choosing_a_subtitle_file_in_transcribe_mode_switches_to_translate_and_says_so(env, tmp_path, name):
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(0)
    assert not tab._target_row.get_visible()
    heard = toasts_of(tab)
    path = tmp_path / name
    path.write_text("x")
    choose(tab, str(path))
    assert tab._selected_path == str(path)
    assert tab._mode_row.get_selected() == 1
    assert heard == [SWITCHED]
    assert tab._target_row.get_visible()  # The row to choose the target language(s) is there.


def test_choosing_a_subtitle_file_while_already_on_translate_changes_nothing_and_says_nothing(env, tmp_path):
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(1)
    heard = toasts_of(tab)
    choose(tab, str(tmp_path / "lecture.srt"))
    assert tab._mode_row.get_selected() == 1 and heard == []


@pytest.mark.parametrize("name", ["lecture.mp4", "broadcast.ts", "clip.MKV"])
def test_choosing_a_video_leaves_the_mode_alone(env, tmp_path, name):
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(0)
    heard = toasts_of(tab)
    choose(tab, str(tmp_path / name))
    assert tab._selected_path == str(tmp_path / name)
    assert tab._mode_row.get_selected() == 0 and heard == []


def test_choosing_a_video_does_not_take_the_user_back_out_of_translate(env, tmp_path):
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(1)
    choose(tab, str(tmp_path / "lecture.mp4"))
    assert tab._mode_row.get_selected() == 1


def test_closing_the_dialog_without_a_file_changes_nothing(env):
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(0)
    heard = toasts_of(tab)
    tab._on_file_dialog_response(FakeDialog(error=GLib.Error("dismissed")), None)
    tab._on_file_dialog_response(FakeDialog(None), None)
    assert tab._selected_path is None and tab._mode_row.get_selected() == 0 and heard == []


def test_showing_a_file_again_when_the_window_is_restored_does_not_switch_the_mode_or_announce(env, tmp_path):
    """Restoring the last session is not the user choosing a file: the mode comes back as it was saved."""
    _app, window = env
    tab = window._video_tab
    tab._mode_row.set_selected(0)
    heard = toasts_of(tab)
    tab._show_file(str(tmp_path / "lecture.srt"))
    assert tab._mode_row.get_selected() == 0 and heard == []


def test_choosing_a_folder_never_switches_the_mode(env, tmp_path):
    _app, window = env
    tab = window._video_tab
    (tmp_path / "a.srt").write_text("x")
    tab._mode_row.set_selected(0)
    tab._show_folder(str(tmp_path))
    assert tab._mode_row.get_selected() == 0


# --- Pressing Start in the wrong mode ---

def test_a_subtitle_file_in_transcribe_mode_is_explained_if_start_is_pressed_anyway(env, tmp_path):
    """The user switched back to Transcribe after choosing the file."""
    _app, window = env
    tab = window._video_tab
    ready_models(tmp_path)
    path = tmp_path / "lecture.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    choose(tab, str(path))
    tab._mode_row.set_selected(0)
    heard = toasts_of(tab)
    tab._on_start_clicked(None)
    assert heard == ["A subtitle file can only be translated: switch to Translate and choose a target language."]
    assert not tab.is_busy


def test_translate_mode_without_a_target_language_asks_for_one(env, tmp_path):
    _app, window = env
    tab = window._video_tab
    ready_models(tmp_path)
    path = tmp_path / "lecture.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    choose(tab, str(path))
    heard = toasts_of(tab)
    tab._on_start_clicked(None)
    assert heard == ["Please choose at least one target language."]
