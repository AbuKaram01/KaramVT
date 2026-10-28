# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Video tab: what Start checks, asks and launches, and how the form and the run button behave
(see gui_support.py). The pipeline is a stand-in, since nothing here loads a model."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import threading
from types import SimpleNamespace

import pytest
from gui_support import Adw, Gio, GLib, Gtk, downloader, env, pump, ready_models, walk

from karamvt.config import AppConfig
from karamvt.core.cancel import OperationCancelled
from karamvt.core.checkpoint import JobStore, ResumeReport, UnitStatus
from karamvt.core.languages import language_for_code
from karamvt.core.models import OutputFormat
from karamvt.gui.tabs import video_tab

HINT = "Video, or a .srt / .vtt subtitle file to translate"
SAME_FOLDER = "Same folder as each input file"


@pytest.fixture
def tab(env):
    return env[1]._video_tab


@pytest.fixture
def pipelines(monkeypatch):
    """Stands in for BatchPipeline: remembers how each one was made, and runs whatever the test puts in ``run``."""
    made: list = []
    stand_in = SimpleNamespace(made=made, run=lambda pipeline: None)

    class FakePipeline:
        def __init__(self, input_paths, source_lang, target_langs, output_format, config, **kwargs):
            self.input_paths, self.source_lang, self.target_langs = input_paths, source_lang, target_langs
            self.output_format, self.config, self.kwargs = output_format, config, kwargs
            made.append(self)

        def run(self):
            stand_in.run(self)

    monkeypatch.setattr(video_tab, "BatchPipeline", FakePipeline)
    return stand_in


@pytest.fixture
def shown(monkeypatch):
    """The dialogs the tab shows, instead of showing them."""
    dialogs: list = []
    monkeypatch.setattr(Adw.AlertDialog, "present", lambda dialog, parent=None: dialogs.append(dialog))
    monkeypatch.setattr(Adw.MessageDialog, "present", lambda dialog, *_args: dialogs.append(dialog))
    return dialogs


@pytest.fixture
def toasts(tab):
    heard: list[str] = []
    tab.connect("toast-requested", lambda _tab, message: heard.append(message))
    return heard


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "lecture.mp4"
    path.write_bytes(b"x")
    return path


def press_start(tab):
    tab._on_start_clicked(None)
    pump(0.05)


def wait_for_the_job(tab):
    assert pump(3.0, until=lambda: tab._worker is not None and not tab.is_busy)
    pump(0.1)


def run_job(tab):
    press_start(tab)
    wait_for_the_job(tab)


def answer(dialog, response):
    dialog.emit("response", response)
    pump(0.05)


def make_files(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"x")


# --- The form as it opens ---

def test_the_form_opens_with_these_rows_and_buttons(tab):
    groups = [w for w in walk(tab) if isinstance(w, Adw.PreferencesGroup)]
    assert [g.get_title() for g in groups] == ["Input", "Options"]
    assert (tab._file_row.get_title(), tab._file_row.get_subtitle()) == ("No file selected", HINT)
    assert tab._choose_button.get_label() == "Choose File…"
    assert (tab._batch_row.get_title(), tab._batch_row.get_active()) == ("Batch mode (folder)", False)
    assert tab._batch_row.get_subtitle() == "Process every video/subtitle file in a folder — e.g. a whole course"
    assert tab._mode_row.get_title() == "Mode" and tab._mode_row.get_selected() == 0
    assert [tab._mode_row.get_model().get_string(i) for i in range(2)] == [
        "Transcribe only (same language)", "Transcribe & Translate",
    ]
    assert tab._source_row.get_title() == "Source language" and tab._source_picker.get_selected() == "Auto-detect"
    assert tab._target_row.get_title() == "Target language(s)" and not tab._target_row.get_visible()
    assert tab._format_row.get_title() == "Output format" and tab._format_row.get_selected() == 0
    assert [tab._format_row.get_model().get_string(i) for i in range(2)] == ["SRT (.srt)", "VTT (.vtt)"]
    assert (tab._output_dir_row.get_title(), tab._output_dir_row.get_subtitle()) == ("Output folder", SAME_FOLDER)
    assert tab._output_dir_button.get_label() == "Choose…"
    clear = tab._output_dir_clear_button
    assert (clear.get_icon_name(), clear.get_tooltip_text(), clear.get_visible()) == (
        "edit-clear-symbolic", "Reset to default", False,
    )


def test_the_run_button_the_progress_and_the_log_start_idle(tab):
    run = tab._run_button
    assert run.get_label() == "Start" and run.get_halign() == Gtk.Align.CENTER
    assert run.has_css_class("suggested-action") and run.has_css_class("pill")
    assert not tab._spinner.get_visible() and not tab._progress_bar.get_visible()
    assert tab._log_buffer.get_char_count() == 0 and not tab.is_busy
    log_views = [w for w in walk(tab) if isinstance(w, Gtk.TextView)]
    assert len(log_views) == 1 and not log_views[0].get_editable() and log_views[0].has_css_class("monospace")


def test_the_form_sits_in_a_clamp_but_the_log_does_not(tab):
    clamp = next(w for w in walk(tab) if isinstance(w, Adw.Clamp))
    assert clamp.get_maximum_size() == 560
    assert tab._run_button in list(walk(clamp)) and tab._options_group in list(walk(clamp))
    log_view = next(w for w in walk(tab) if isinstance(w, Gtk.TextView))
    assert log_view not in list(walk(clamp))


# --- The toggles ---

def test_the_mode_decides_whether_target_languages_are_asked_for(tab):
    heard = []
    tab.connect("state-changed", lambda _tab: heard.append("changed"))
    tab._mode_row.set_selected(1)
    assert tab._target_row.get_visible() and heard == ["changed"]
    tab._mode_row.set_selected(0)
    assert not tab._target_row.get_visible() and heard == ["changed", "changed"]


def test_batch_mode_asks_for_a_folder_and_forgets_the_chosen_path(tab, video):
    tab._show_file(str(video))
    heard = []
    tab.connect("state-changed", lambda _tab: heard.append("changed"))
    tab._batch_row.set_active(True)
    assert tab._selected_path is None and heard == ["changed"]
    assert tab._choose_button.get_label() == "Choose Folder…"
    assert tab._file_row.get_title() == "No folder selected"
    assert tab._file_row.get_subtitle() == "Every video/subtitle file inside will be processed"
    tab._batch_row.set_active(False)
    assert tab._choose_button.get_label() == "Choose File…"
    assert (tab._file_row.get_title(), tab._file_row.get_subtitle()) == ("No file selected", HINT)
    assert heard == ["changed", "changed"]


# --- The pickers ---

class FolderDialog:
    """Stands in for Gtk.FileDialog: remembers how it was asked, and answers "which folder?" when told to."""

    opened: dict = {}

    def __init__(self, title=""):
        FolderDialog.opened = {"title": title}

    def set_initial_folder(self, folder):
        FolderDialog.opened["folder"] = folder

    def select_folder(self, parent, cancellable, callback):
        FolderDialog.opened.update(parent=parent, callback=callback)


class FolderAnswer:
    def __init__(self, path=None, error=None):
        self.path, self.error = path, error

    def select_folder_finish(self, _result):
        if self.error is not None:
            raise self.error
        return Gio.File.new_for_path(self.path) if self.path else None


def test_batch_mode_asks_for_a_course_folder(tab, monkeypatch):
    monkeypatch.setattr(video_tab.Gtk, "FileDialog", FolderDialog)
    tab._batch_row.set_active(True)
    tab._on_choose_clicked(None)
    assert FolderDialog.opened["title"] == "Select a course folder"
    assert FolderDialog.opened["callback"] == tab._on_folder_dialog_response
    assert FolderDialog.opened["parent"] == tab.get_root()


def test_a_chosen_folder_is_shown_with_how_many_files_it_holds(tab, tmp_path):
    course = tmp_path / "course"
    make_files(course, "one.mp4", "two.mkv", "notes.txt", "one.srt")
    make_files(course / "week 2", "three.webm")
    tab._on_folder_dialog_response(FolderAnswer(str(course)), None)
    assert tab._selected_path == str(course)
    assert tab._file_row.get_title() == "course"
    assert tab._file_row.get_subtitle() == f"{course}  ·  4 file(s) found"


@pytest.mark.parametrize("answer_", [FolderAnswer(error=GLib.Error("dismissed")), FolderAnswer(None)])
def test_dismissing_the_folder_dialog_changes_nothing(tab, answer_):
    tab._on_folder_dialog_response(answer_, None)
    assert tab._selected_path is None and tab._file_row.get_title() == "No file selected"


def test_the_output_folder_is_asked_for_and_can_be_reset(tab, tmp_path, monkeypatch):
    monkeypatch.setattr(video_tab.Gtk, "FileDialog", FolderDialog)
    tab._on_choose_output_dir(None)
    assert FolderDialog.opened["title"] == "Select an output folder"
    assert FolderDialog.opened["callback"] == tab._on_output_dir_response

    heard = []
    tab.connect("state-changed", lambda _tab: heard.append("changed"))
    out = tmp_path / "subtitles"
    tab._on_output_dir_response(FolderAnswer(str(out)), None)
    assert tab._output_dir_path == str(out) and tab._output_dir_row.get_subtitle() == str(out)
    assert tab._output_dir_clear_button.get_visible() and heard == ["changed"]

    tab._on_clear_output_dir(None)
    assert tab._output_dir_path is None and tab._output_dir_row.get_subtitle() == SAME_FOLDER
    assert not tab._output_dir_clear_button.get_visible() and heard == ["changed", "changed"]


@pytest.mark.parametrize("answer_", [FolderAnswer(error=GLib.Error("dismissed")), FolderAnswer(None)])
def test_dismissing_the_output_folder_dialog_changes_nothing(tab, answer_):
    tab._on_output_dir_response(answer_, None)
    assert tab._output_dir_path is None and tab._output_dir_row.get_subtitle() == SAME_FOLDER


# --- What Start refuses ---

def test_start_without_a_file_or_a_folder_asks_for_one(tab, toasts, pipelines):
    press_start(tab)
    tab._batch_row.set_active(True)
    press_start(tab)
    assert toasts == ["Please choose a file first.", "Please choose a folder first."]
    assert pipelines.made == [] and not tab.is_busy


def test_start_on_a_path_that_has_gone_says_so(tab, toasts, video, tmp_path):
    ready_models(tmp_path)
    tab._show_file(str(video))
    video.unlink()
    press_start(tab)
    assert toasts == ["The selected path no longer exists."]


def test_start_without_the_models_says_which_one_is_missing(tab, toasts, video, pipelines):
    tab._show_file(str(video))
    press_start(tab)
    assert toasts == [AppConfig().validation_errors()[0]] and "model" in toasts[0].lower()
    assert pipelines.made == []


def test_start_reads_the_settings_as_they_are_now_not_as_they_were_when_the_tab_opened(
    tab, toasts, video, tmp_path, pipelines
):
    tab._show_file(str(video))
    ready_models(tmp_path)
    run_job(tab)
    assert not any("model" in message.lower() for message in toasts) and len(pipelines.made) == 1


def test_a_folder_with_nothing_to_process_says_so(tab, toasts, tmp_path, pipelines):
    ready_models(tmp_path)
    course = tmp_path / "course"
    make_files(course, "notes.txt", "cover.png")
    tab._batch_row.set_active(True)
    tab._show_folder(str(course))
    press_start(tab)
    assert toasts == ["That folder has no video or subtitle files in it."] and pipelines.made == []


def test_translating_into_the_language_it_is_spoken_in_is_refused(tab, toasts, video, tmp_path, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["English"])
    press_start(tab)
    assert toasts == ["Target language(s) can't all be the same as the source."] and pipelines.made == []


def test_one_target_that_differs_from_the_source_is_enough(tab, toasts, video, tmp_path, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["English", "Spanish"])
    run_job(tab)
    assert toasts == ["Done! Check the log below for details on each file."]
    assert [t.whisper_code for t in pipelines.made[0].target_langs] == ["en", "es"]


def test_an_unknown_source_language_can_be_translated_into_anything(tab, toasts, video, tmp_path, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._target_picker.set_selected(["English"])
    run_job(tab)
    assert pipelines.made[0].source_lang is None and len(pipelines.made) == 1


def test_a_subtitle_file_in_a_folder_is_fine_in_transcribe_mode(tab, toasts, tmp_path, pipelines):
    """Only a single subtitle file is refused there; in a folder, it is simply one of the files."""
    ready_models(tmp_path)
    course = tmp_path / "course"
    make_files(course, "one.mp4", "two.srt")
    tab._batch_row.set_active(True)
    tab._show_folder(str(course))
    run_job(tab)
    assert pipelines.made[0].input_paths == [str(course / "one.mp4"), str(course / "two.srt")]
    assert pipelines.made[0].target_langs == []


# --- What Start hands the pipeline ---

def test_start_hands_the_form_to_the_pipeline(tab, video, tmp_path, pipelines):
    ready_models(tmp_path)
    out = tmp_path / "out"
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["Spanish", "French"])
    tab._format_row.set_selected(1)
    tab._show_output_dir(str(out))
    run_job(tab)

    pipeline, = pipelines.made
    assert pipeline.input_paths == [str(video)]
    assert pipeline.source_lang.whisper_code == "en"
    assert [t.whisper_code for t in pipeline.target_langs] == ["fr", "es"]  # the order the picker lists them
    assert pipeline.output_format is OutputFormat.VTT
    assert pipeline.config.llm_model_path == AppConfig.load().llm_model_path
    assert pipeline.kwargs["output_dir"] == str(out) and pipeline.kwargs["input_root"] == str(tmp_path)
    assert {"on_progress", "on_percent", "cancel"} <= set(pipeline.kwargs)


def test_transcribing_with_the_defaults_has_no_known_language_and_no_targets(tab, video, tmp_path, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    run_job(tab)
    pipeline, = pipelines.made
    assert (pipeline.source_lang, pipeline.target_langs, pipeline.output_format) == (None, [], OutputFormat.SRT)
    assert pipeline.kwargs["output_dir"] is None


def test_a_folder_hands_over_every_video_and_subtitle_file_in_it_in_order(tab, tmp_path, pipelines):
    ready_models(tmp_path)
    course = tmp_path / "course"
    make_files(course, "b.mkv", "a.MP4", "notes.txt", "c.vtt")
    make_files(course / "week 2", "d.webm", "readme.md")
    tab._batch_row.set_active(True)
    tab._show_folder(str(course))
    run_job(tab)
    pipeline, = pipelines.made
    assert pipeline.input_paths == sorted(str(course / name) for name in ("a.MP4", "b.mkv", "c.vtt", "week 2/d.webm"))
    assert pipeline.kwargs["input_root"] == str(course) and tab._job_files == 4


def test_the_number_of_files_is_remembered_for_the_notification(tab, video, tmp_path, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    run_job(tab)
    assert tab._job_files == 1


# --- The question about what was saved by an earlier run ---

@pytest.fixture
def store(monkeypatch):
    """What the job store finds on disk (a report chosen by the test), and what is asked of it."""
    asked = SimpleNamespace(scans=[], cleared=[], report=ResumeReport())

    def scan(self, *args, **kwargs):
        asked.scans.append((args, kwargs))
        return asked.report

    monkeypatch.setattr(JobStore, "scan", scan)
    monkeypatch.setattr(JobStore, "clear_inputs", lambda self, paths: asked.cleared.append(list(paths)))
    return asked


def test_the_store_is_asked_about_exactly_this_job(tab, video, tmp_path, pipelines, store):
    ready_models(tmp_path)
    saved = AppConfig.load()
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["Spanish"])
    tab._format_row.set_selected(1)
    run_job(tab)
    (args, kwargs), = store.scans
    assert args == ([str(video)], "en", ["es"], saved.llm_model_path, saved.whisper_model_path, ".vtt",
                    None, str(tmp_path))
    assert kwargs == {"use_partial_transcripts": saved.resume_transcription}


def test_the_store_is_told_whether_an_interrupted_transcription_may_be_continued(
    tab, video, tmp_path, store, pipelines
):
    ready_models(tmp_path)
    config = AppConfig.load()
    config.resume_transcription = True
    config.save()
    tab._show_file(str(video))
    run_job(tab)
    assert store.scans[0][1] == {"use_partial_transcripts": True}
    assert store.scans[0][0][1] is None and store.scans[0][0][2] == []


def partial_translation(video):
    return ResumeReport(units=[UnitStatus(str(video), "es", "partial", 5, 9)])


def start_a_resumable_job(tab, video, tmp_path, store, report=None):
    ready_models(tmp_path)
    store.report = report or partial_translation(video)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["Spanish"])
    press_start(tab)


def test_an_earlier_run_is_offered_to_be_continued(tab, video, tmp_path, store, shown, pipelines):
    start_a_resumable_job(tab, video, tmp_path, store)
    dialog, = shown
    assert isinstance(dialog, Adw.AlertDialog)
    assert dialog.get_heading() == "Continue where you left off?"
    assert dialog.get_body() == (
        "An earlier run of this job didn’t finish:\n\n"
        "• lecture.mp4 → Spanish: 5 of 9 subtitles translated\n\n"
        "Continue to pick up from there, or start over from the beginning."
    )
    assert [dialog.get_response_label(r) for r in ("cancel", "restart", "resume")] == [
        "Cancel", "Start Over", "Continue",
    ]
    assert dialog.get_response_appearance("resume") == Adw.ResponseAppearance.SUGGESTED
    assert dialog.get_default_response() == "resume" and dialog.get_close_response() == "cancel"
    assert pipelines.made == [] and not tab.is_busy


def test_cancelling_the_offer_starts_nothing_and_clears_nothing(tab, video, tmp_path, store, shown, pipelines):
    start_a_resumable_job(tab, video, tmp_path, store)
    answer(shown[0], "cancel")
    assert pipelines.made == [] and store.cleared == [] and not tab.is_busy


def test_continuing_starts_the_job_without_clearing_what_was_saved(tab, video, tmp_path, store, shown, pipelines):
    start_a_resumable_job(tab, video, tmp_path, store)
    answer(shown[0], "resume")
    wait_for_the_job(tab)
    assert len(pipelines.made) == 1 and store.cleared == []


def test_starting_over_clears_what_was_saved_for_these_files_and_starts(tab, video, tmp_path, store, shown, pipelines):
    start_a_resumable_job(tab, video, tmp_path, store)
    answer(shown[0], "restart")
    wait_for_the_job(tab)
    assert store.cleared == [[str(video)]] and len(pipelines.made) == 1


def test_continuing_does_not_warn_about_outputs_that_were_already_finished(
    tab, video, tmp_path, store, shown, pipelines
):
    finished = tmp_path / "lecture_es.srt"
    finished.write_bytes(b"x")
    report = ResumeReport(units=[UnitStatus(str(video), "es", "done", 9, 9)], completed_outputs={str(finished)})
    start_a_resumable_job(tab, video, tmp_path, store, report)
    answer(shown[0], "resume")
    wait_for_the_job(tab)
    assert len(shown) == 1 and len(pipelines.made) == 1


def test_continuing_still_warns_about_an_output_that_is_not_one_of_the_finished(
    tab, video, tmp_path, store, shown, pipelines
):
    (tmp_path / "lecture_es.srt").write_bytes(b"x")
    start_a_resumable_job(tab, video, tmp_path, store)
    answer(shown[0], "resume")
    overwrite = shown[1]
    assert isinstance(overwrite, Adw.MessageDialog) and pipelines.made == []


def test_starting_over_warns_about_every_output_including_the_finished_ones(
    tab, video, tmp_path, store, shown, pipelines
):
    finished = tmp_path / "lecture_es.srt"
    finished.write_bytes(b"x")
    report = ResumeReport(units=[UnitStatus(str(video), "es", "done", 9, 9)], completed_outputs={str(finished)})
    start_a_resumable_job(tab, video, tmp_path, store, report)
    answer(shown[0], "restart")
    assert store.cleared == [[str(video)]]
    assert shown[1].get_body() == "“lecture_es.srt” already exists and will be replaced."


def test_nothing_saved_goes_straight_on(tab, video, tmp_path, store, shown, pipelines):
    ready_models(tmp_path)
    tab._show_file(str(video))
    run_job(tab)
    assert shown == [] and len(pipelines.made) == 1


# --- The warning about files that would be replaced ---

def start_transcribing(tab, video, tmp_path, language="English"):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._source_picker.set_selected(language)
    press_start(tab)


def test_a_file_that_would_be_replaced_is_named(tab, video, tmp_path, shown, pipelines):
    (tmp_path / "lecture_en.srt").write_bytes(b"old")
    start_transcribing(tab, video, tmp_path)
    dialog, = shown
    assert isinstance(dialog, Adw.MessageDialog)
    assert dialog.get_heading() == "Overwrite existing file(s)?"
    assert dialog.get_body() == "“lecture_en.srt” already exists and will be replaced."
    assert [dialog.get_response_label(r) for r in ("cancel", "overwrite")] == ["Cancel", "Overwrite"]
    assert dialog.get_response_appearance("overwrite") == Adw.ResponseAppearance.DESTRUCTIVE
    assert dialog.get_default_response() == "cancel" and dialog.get_close_response() == "cancel"
    assert dialog.get_transient_for() == tab.get_root()
    assert pipelines.made == [] and not tab.is_busy


def test_several_files_that_would_be_replaced_are_counted(tab, video, tmp_path, shown):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected(["Spanish", "French"])
    make_files(tmp_path, "lecture_es.srt", "lecture_fr.srt")
    press_start(tab)
    assert shown[0].get_body() == "2 output files already exist and will be replaced."


def test_overwriting_starts_the_job_and_cancelling_does_not(tab, video, tmp_path, shown, pipelines):
    (tmp_path / "lecture_en.srt").write_bytes(b"old")
    start_transcribing(tab, video, tmp_path)
    answer(shown[0], "cancel")
    assert pipelines.made == [] and not tab.is_busy
    answer(shown[0], "overwrite")
    wait_for_the_job(tab)
    assert len(pipelines.made) == 1


def test_no_file_is_named_when_the_language_is_not_known_before_the_run(tab, video, tmp_path, shown, pipelines):
    (tmp_path / "lecture_en.srt").write_bytes(b"old")
    start_transcribing(tab, video, tmp_path, language="Auto-detect")
    wait_for_the_job(tab)
    assert shown == [] and len(pipelines.made) == 1


def test_a_target_that_is_the_source_language_makes_no_output_so_none_is_warned_about(
    tab, video, tmp_path, shown, pipelines
):
    make_files(tmp_path, "lecture_es.srt")
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._mode_row.set_selected(1)
    tab._source_picker.set_selected("Spanish")
    tab._target_picker.set_selected(["Spanish", "French"])
    press_start(tab)
    wait_for_the_job(tab)
    assert shown == []
    make_files(tmp_path, "lecture_fr.srt")
    press_start(tab)
    assert shown[0].get_body() == "“lecture_fr.srt” already exists and will be replaced."


def test_the_output_folder_is_where_the_warning_looks(tab, video, tmp_path, shown, pipelines):
    out = tmp_path / "out"
    make_files(tmp_path, "lecture_en.srt")
    make_files(out)
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._source_picker.set_selected("English")
    tab._show_output_dir(str(out))
    press_start(tab)
    wait_for_the_job(tab)
    assert shown == []
    make_files(out, "lecture_en.srt")
    press_start(tab)
    assert shown[0].get_body() == "“lecture_en.srt” already exists and will be replaced."


def test_predicting_the_outputs_for_the_warning_creates_no_output_folder(tmp_path):
    # The user can still answer Cancel to the warning; a folder made by then would be left behind for nothing.
    plan = video_tab._JobPlan(
        input_paths=[str(tmp_path / "lecture.mp4")],
        input_root=str(tmp_path),
        source_lang=language_for_code("en"),
        target_langs=[language_for_code("es")],
        output_format=OutputFormat.SRT,
        output_dir=str(tmp_path / "out"),
    )
    assert plan.expected_outputs() == [str(tmp_path / "out" / "lecture_es.srt")]
    assert not (tmp_path / "out").exists()


def test_two_inputs_that_would_get_the_same_name_are_warned_about_under_their_own_names(
    tab, tmp_path, shown, pipelines
):
    course = tmp_path / "course"
    make_files(course, "a.mp4", "a.srt", "a_en.srt", "a_en (2).srt")
    ready_models(tmp_path)
    tab._batch_row.set_active(True)
    tab._show_folder(str(course))
    tab._source_picker.set_selected("English")
    press_start(tab)
    assert shown[0].get_body() == "2 output files already exist and will be replaced."


# --- The run button, stopping and the end of a job ---

class Gate:
    """A pipeline that works until the test lets it end (or until it is asked to stop)."""

    def __init__(self, pipelines):
        self.release = threading.Event()
        self.started = threading.Event()
        pipelines.run = self.work

    def work(self, pipeline):
        pipeline.kwargs["on_progress"]("Reading the audio")
        pipeline.kwargs["on_percent"](0.5)
        self.started.set()
        cancel = pipeline.kwargs["cancel"]
        while not self.release.is_set():
            if cancel.is_cancelled:
                raise OperationCancelled
            threading.Event().wait(0.005)


@pytest.fixture
def gate(pipelines):
    gate = Gate(pipelines)
    yield gate
    gate.release.set()


def busy_tab(tab, video, tmp_path, gate):
    ready_models(tmp_path)
    tab._show_file(str(video))
    tab._run_button.emit("clicked")
    assert pump(3.0, until=lambda: gate.started.is_set() and tab._log_buffer.get_char_count() > 0)
    pump(0.1)
    return tab


def test_a_running_job_shows_what_it_says_and_how_far_it_is(tab, video, tmp_path, gate):
    busy_tab(tab, video, tmp_path, gate)
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False) == "Reading the audio\n"
    assert tab.is_busy and tab._progress_bar.get_visible() and tab._progress_bar.get_fraction() == 0.5


def test_a_new_job_starts_with_an_empty_log(tab, video, tmp_path, gate):
    tab._log_buffer.set_text("left over from before")
    busy_tab(tab, video, tmp_path, gate)
    start, end = tab._log_buffer.get_bounds()
    assert "left over" not in tab._log_buffer.get_text(start, end, False)


def test_while_a_job_runs_the_form_is_locked_and_start_becomes_stop(tab, video, tmp_path, gate):
    busy_tab(tab, video, tmp_path, gate)
    run = tab._run_button
    assert run.get_label() == "Stop" and run.get_sensitive()
    assert run.has_css_class("destructive-action") and not run.has_css_class("suggested-action")
    assert run.has_css_class("pill")
    assert not tab._choose_button.get_sensitive() and not tab._batch_row.get_sensitive()
    assert not tab._options_group.get_sensitive()
    assert tab._spinner.get_visible() and tab._spinner.get_spinning()


def test_stopping_asks_the_job_to_stop_and_says_so(tab, video, tmp_path, gate, toasts):
    busy_tab(tab, video, tmp_path, gate)
    tab._run_button.emit("clicked")
    assert tab._stopping and tab._run_button.get_label() == "Stopping…" and not tab._run_button.get_sensitive()
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False).endswith("\nStopping — finishing the current step…\n")
    assert pump(3.0, until=lambda: not tab.is_busy)
    assert toasts == ["Stopped — progress saved."]
    assert tab._run_button.get_label() == "Start" and tab._run_button.get_sensitive() and not tab._stopping
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False).endswith(
        "Stopped. Your progress is saved — press Start again to continue where you left off. "
        "Finished files are kept; the file in progress is only written once it’s complete.\n"
    )


def test_stopping_twice_says_it_once(tab, video, tmp_path, gate):
    busy_tab(tab, video, tmp_path, gate)
    tab._request_stop()
    tab._request_stop()
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False).count("Stopping") == 1


def test_stopping_with_nothing_running_does_nothing(tab):
    tab._request_stop()
    assert not tab._stopping and tab._log_buffer.get_char_count() == 0


def test_quitting_stops_a_running_job_and_only_then(tab, video, tmp_path, gate):
    tab.stop_for_quit()
    assert not tab._stopping
    busy_tab(tab, video, tmp_path, gate)
    tab.stop_for_quit()
    assert tab._stopping


def test_after_a_job_the_form_is_open_again_and_the_bar_is_empty(tab, video, tmp_path, gate):
    busy_tab(tab, video, tmp_path, gate)
    gate.release.set()
    assert pump(3.0, until=lambda: not tab.is_busy)
    run = tab._run_button
    assert run.get_label() == "Start" and run.has_css_class("suggested-action")
    assert not run.has_css_class("destructive-action")
    assert tab._choose_button.get_sensitive() and tab._batch_row.get_sensitive() and tab._options_group.get_sensitive()
    assert not tab._spinner.get_visible() and not tab._spinner.get_spinning()
    assert not tab._progress_bar.get_visible() and tab._progress_bar.get_fraction() == 0.0


def test_a_job_that_fails_says_why_in_the_log_and_in_a_toast(tab, video, tmp_path, pipelines, toasts):
    def fail(_pipeline):
        raise ValueError("no speech detected\nsecond line")

    pipelines.run = fail
    ready_models(tmp_path)
    tab._show_file(str(video))
    run_job(tab)
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False) == "Error: no speech detected\nsecond line\n"
    assert toasts == ["Something went wrong — check the log for details."]
    assert not tab.is_busy


def test_the_log_gets_a_line_for_every_message(tab):
    tab._append_log("one")
    tab._append_log("two")
    start, end = tab._log_buffer.get_bounds()
    assert tab._log_buffer.get_text(start, end, False) == "one\ntwo\n"
