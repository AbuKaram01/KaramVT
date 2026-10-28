# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The desktop notification for a job that ends while the window is not in front.

Driven through the real interface (see gui_support.py). The speech and translation models are replaced by stand-ins.
"""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import pytest
from gui_support import (
    AppConfig, Gio, Gtk, downloader, env, fake_translation, open_preferences, pump, ready_models, run_text_job, walk,
)

from karamvt import winsupport
from karamvt.gui.formatting import short_error, video_done_notice


# --- The wording ---

def test_a_single_video_is_named_by_its_file():
    assert video_done_notice("/home/me/courses/lecture 1.mp4", False, 1) == ("Subtitles are ready", "lecture 1.mp4")


def test_a_batch_is_named_by_its_folder_and_how_many_files_it_held():
    assert video_done_notice("/home/me/courses/python/", True, 12) == ("Batch finished", "python — 12 files")
    assert video_done_notice("/home/me/courses/python", True, 1) == ("Batch finished", "python — 1 file")
    assert video_done_notice(None, True, 3) == ("Batch finished", "3 files")


def test_an_error_is_cut_down_to_its_first_line_and_a_sensible_length():
    assert short_error("Disk full\nTraceback (most recent call last): ...") == "Disk full"
    assert short_error("\n  \n  second line  \n") == "second line"
    long = short_error("x" * 500)
    assert len(long) == 120 and long.endswith("…")
    assert short_error("") == "Check the log for details."


def test_the_setting_is_on_unless_it_was_turned_off(config_file):
    assert AppConfig().notify_when_done is True
    AppConfig(notify_when_done=False).save()
    assert AppConfig.load().notify_when_done is False
    config_file.write_text('{"cpu_threads": 2}')  # A file from before the setting existed.
    assert AppConfig.load().notify_when_done is True


# --- Helpers ---

@pytest.fixture
def away(env):
    """The application and window, with the window not in front and every notification collected."""
    app, window = env
    sent = []
    app.send_notification = lambda notification_id, notification: sent.append(notification_id)
    window.is_active = lambda: False
    return app, window, sent


def listen(tab) -> list:
    """What the tab announces when a job ends: (title, body) pairs."""
    heard = []
    tab.connect("job-finished", lambda _tab, title, body: heard.append((title, body)))
    return heard


def fake_pipeline(monkeypatch, error=None):
    """Replace the Video tab's BatchPipeline with a stand-in whose run() raises ``error`` when one is given."""
    class FakePipeline:
        def __init__(self, input_paths, *_args, **_kwargs):
            self.input_paths = input_paths

        def run(self):
            if error is not None:
                raise error

    monkeypatch.setattr("karamvt.gui.tabs.video_tab.BatchPipeline", FakePipeline)


def run_video_job(tab):
    """Press Start in the Video tab and wait until the job has ended."""
    tab._run_button.emit("clicked")
    assert pump(3.0, until=lambda: tab._worker is not None and not tab.is_busy)
    pump(0.1)


# --- The window decides ---

def test_a_notification_is_sent_when_the_window_is_not_in_front(away):
    _app, window, sent = away
    window.notify_finished("text-finished", "Translation is ready", "English → Spanish")
    assert sent == ["text-finished"]


def test_on_windows_a_toast_takes_the_place_of_the_desktop_notification(away, monkeypatch):
    _app, window, sent = away
    toasts = []
    monkeypatch.setattr(winsupport, "send_toast", lambda title, body: toasts.append((title, body)) or True)
    window.notify_finished("text-finished", "Translation is ready", "English → Spanish")
    assert toasts == [("Translation is ready", "English → Spanish")]
    assert sent == []  # One notice, not two.


def test_no_toast_is_raised_while_the_window_is_in_front(away, monkeypatch):
    _app, window, sent = away
    window.is_active = lambda: True
    monkeypatch.setattr(winsupport, "send_toast", lambda title, body: pytest.fail("the window is in front"))
    window.notify_finished("text-finished", "Translation is ready", "")
    assert sent == []


def test_when_the_toast_cannot_be_shown_the_desktop_notification_is_used(away, monkeypatch):
    _app, window, sent = away
    monkeypatch.setattr(winsupport, "send_toast", lambda title, body: False)
    window.notify_finished("text-finished", "Translation is ready", "")
    assert sent == ["text-finished"]


def test_nothing_is_sent_while_the_window_is_in_front(away):
    _app, window, sent = away
    window.is_active = lambda: True
    window.notify_finished("text-finished", "Translation is ready", "")
    assert sent == []


def test_nothing_is_sent_while_preferences_is_in_front(away):
    app, window, sent = away
    prefs = open_preferences(app, window)
    prefs.is_active = lambda: True
    window.notify_finished("video-finished", "Subtitles are ready", "")
    assert sent == []

    prefs.is_active = lambda: False
    window.notify_finished("video-finished", "Subtitles are ready", "")
    assert sent == ["video-finished"]


def test_nothing_is_sent_when_the_notifications_are_turned_off(away):
    _app, window, sent = away
    AppConfig(notify_when_done=False).save()
    window.notify_finished("video-finished", "Subtitles are ready", "")
    assert sent == []


def test_a_notification_is_a_real_gio_notification_that_the_application_accepts(env):
    """The real send, not the stand-in: no session bus is reachable here, so this only has to not fail."""
    _app, window = env
    window.is_active = lambda: False
    window.notify_finished("video-finished", "Subtitles are ready", "lecture.mp4")


def test_clicking_a_notification_brings_the_window_back(env):
    app, window = env
    presented = []
    window.present = lambda: presented.append(True)
    app.activate_action("show-window", None)
    assert presented == [True]


# --- Video ---

def test_a_finished_video_job_notifies_with_the_file_name(away, tmp_path, monkeypatch):
    _app, window, sent = away
    ready_models(tmp_path)
    fake_pipeline(monkeypatch)
    video = tmp_path / "lecture 1.mp4"
    video.write_bytes(b"x")
    tab = window._video_tab
    tab._selected_path = str(video)
    heard = listen(tab)

    run_video_job(tab)
    assert heard == [("Subtitles are ready", "lecture 1.mp4")]
    assert sent == ["video-finished"]


def test_a_finished_batch_notifies_with_the_folder_and_the_number_of_files(away, tmp_path, monkeypatch):
    _app, window, sent = away
    ready_models(tmp_path)
    fake_pipeline(monkeypatch)
    folder = tmp_path / "python course"
    folder.mkdir()
    for name in ("one.mp4", "two.mkv", "notes.txt"):
        (folder / name).write_bytes(b"x")
    tab = window._video_tab
    tab._batch_row.set_active(True)
    tab._selected_path = str(folder)
    heard = listen(tab)

    run_video_job(tab)
    assert heard == [("Batch finished", "python course — 2 files")]
    assert sent == ["video-finished"]


def test_a_failed_video_job_notifies_with_the_reason(away, tmp_path, monkeypatch):
    _app, window, sent = away
    ready_models(tmp_path)
    fake_pipeline(monkeypatch, error=ValueError("no speech detected in the audio"))
    video = tmp_path / "silent.mp4"
    video.write_bytes(b"x")
    tab = window._video_tab
    tab._selected_path = str(video)
    heard = listen(tab)

    run_video_job(tab)
    assert heard == [("Job failed", "no speech detected in the audio")]
    assert sent == ["video-finished"]


def test_a_video_job_that_ends_in_front_of_the_user_only_shows_the_toast(away, tmp_path, monkeypatch):
    _app, window, sent = away
    window.is_active = lambda: True
    ready_models(tmp_path)
    fake_pipeline(monkeypatch)
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    tab = window._video_tab
    tab._selected_path = str(video)
    toasts = []
    tab.connect("toast-requested", lambda _tab, message: toasts.append(message))

    run_video_job(tab)
    assert toasts == ["Done! Check the log below for details on each file."]
    assert sent == []


def test_stopping_a_video_job_does_not_notify(away):
    _app, window, sent = away
    tab = window._video_tab
    heard = listen(tab)
    tab._on_cancelled(None)
    assert heard == [] and sent == []


# --- Text ---

def test_a_finished_translation_notifies_with_the_language_pair(away, tmp_path, monkeypatch):
    _app, window, sent = away
    ready_models(tmp_path)
    fake_translation(monkeypatch)
    tab = window._text_tab
    heard = listen(tab)

    run_text_job(tab)
    assert tab._get_buffer_text(tab._output_buffer) == "Hola"
    assert heard == [("Translation is ready", "English → Spanish")]
    assert sent == ["text-finished"]


def test_a_failed_translation_notifies_with_the_reason(away, tmp_path, monkeypatch):
    _app, window, sent = away
    ready_models(tmp_path)
    fake_translation(monkeypatch, error=RuntimeError("out of memory\nsecond line"))
    tab = window._text_tab
    heard = listen(tab)

    run_text_job(tab)
    assert heard == [("Translation failed", "out of memory")]
    assert sent == ["text-finished"]


def test_a_translation_that_ends_in_front_of_the_user_sends_nothing(away, tmp_path, monkeypatch):
    _app, window, sent = away
    window.is_active = lambda: True
    ready_models(tmp_path)
    fake_translation(monkeypatch)

    run_text_job(window._text_tab)
    assert sent == []


def test_importing_text_from_an_image_never_notifies(away):
    """OCR takes seconds, and the person is looking at the window when they start it."""
    _app, window, sent = away
    tab = window._text_tab
    heard = listen(tab)

    def unreadable_image(_ctx):
        raise RuntimeError("bad image")

    tab._launch_job(unreadable_image, on_success=lambda _result: None)
    assert pump(2.0, until=lambda: not tab.is_busy)
    pump(0.1)
    assert heard == [] and sent == []


# --- Preferences ---

def test_preferences_has_a_switch_that_is_on_by_default_and_saves_when_changed(env):
    app, window = env
    prefs = open_preferences(app, window)
    row = prefs._notify_row
    assert row.get_title() == "Notify when a job finishes" and row.get_active()

    row.set_active(False)
    assert AppConfig.load().notify_when_done is False
    row.set_active(True)
    assert AppConfig.load().notify_when_done is True


def test_changing_the_switch_keeps_the_rest_of_the_saved_settings(env):
    app, window = env
    AppConfig(cpu_threads=7, welcome_done=True).save()
    prefs = open_preferences(app, window)
    AppConfig(cpu_threads=9, welcome_done=True).save()  # Changed meanwhile (e.g. from OCR Settings).
    prefs._notify_row.set_active(False)
    saved = AppConfig.load()
    assert saved.notify_when_done is False and saved.cpu_threads == 9 and saved.welcome_done
