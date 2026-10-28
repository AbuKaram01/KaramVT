# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Video tab: turn a video, a subtitle file or a whole folder of them into subtitle files.

`VideoTabView` collects the user's choices in a form, turns them into a `_JobPlan` and runs a `BatchPipeline` on a
`PipelineWorker` background thread, with a progress bar, a log and a Stop button. Before starting, it offers to
resume what an earlier run of the same job left behind and asks before overwriting existing output files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk

from ...config import AppConfig
from ...core.checkpoint import JobStore, ResumeReport
from ...core.languages import Language, available_language_names, resolve_language, speech_language_names
from ...core.models import SUBTITLE_EXTENSIONS, OutputFormat
from ...core.pipelines import BatchPipeline, OutputNames, batch_output_path
from ...core.ui_state import VideoState
from ..formatting import resume_summary, short_error, video_done_notice
from ..language_picker import LanguagePickerButton
from ..multi_language_picker import MultiLanguagePickerButton
from ..pipeline_worker import PipelineWorker
from ..widgets import button_row, chosen_path, combo_row, filter_list, set_css_class, start_in

# Label of the source-language picker entry that means "detect the language".
_AUTO_DETECT_LABEL = "Auto-detect"

# Characters that can end a folder path: only "/" on Linux, "\\" and "/" on Windows (which accepts both).
_SEPARATORS = os.sep + (os.altsep or "")

# Extensions of the video files a folder scan picks up (subtitle files come from SUBTITLE_EXTENSIONS).
_VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts",
}

_FILE_HINT = "Video, or a .srt / .vtt subtitle file to translate"
_SAME_FOLDER = "Same folder as each input file"
# The row of the Mode list that translates; the first one only transcribes.
_TRANSLATE_MODE = 1


def file_filters() -> Gio.ListStore:
    """Build the filters of the "choose a file" dialog; the first one is shown when the dialog opens.

    The first filter lets through both kinds of file the tab accepts. With only the video filter first, subtitle
    files were hidden until the user thought to switch to another filter. Files are matched by the ending of their
    name (in any capitalisation) as well as by type, because the type is often not known: a ".ts" video is taken for
    a Qt translation file on many systems, so the extension is all there is to go on.
    """
    both = Gtk.FileFilter(name="Videos and subtitles")
    video = Gtk.FileFilter(name="Video files")
    subtitles = Gtk.FileFilter(name="Subtitle files")
    everything = Gtk.FileFilter(name="All files")
    for kind in (both, video):
        kind.add_mime_type("video/*")
        for extension in sorted(_VIDEO_EXTENSIONS):
            kind.add_suffix(extension.lstrip("."))
    for kind in (both, subtitles):
        for mime_type in ("application/x-subrip", "text/vtt"):
            kind.add_mime_type(mime_type)
        for extension in sorted(SUBTITLE_EXTENSIONS):
            kind.add_suffix(extension.lstrip("."))
    everything.add_pattern("*")
    return filter_list(both, video, subtitles, everything)


def _is_subtitle_file(path: str) -> bool:
    """Return whether the path has a subtitle file extension, whatever its capitalisation."""
    return os.path.splitext(path)[1].lower() in SUBTITLE_EXTENSIONS


def _scan_folder(folder_path: str) -> list[str]:
    """Return every video or subtitle file under a folder, recursively, in a stable order.

    This is what makes "point it at a course folder" work.
    """
    matches: list[str] = []
    for root, _dirs, files in os.walk(folder_path):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext in _VIDEO_EXTENSIONS or ext in SUBTITLE_EXTENSIONS:
                matches.append(os.path.join(root, fname))
    return sorted(matches)


class _CannotStart(Exception):
    """The form is not ready to start a job. The message is what the user is told."""


@dataclass(frozen=True)
class _JobPlan:
    """What a job covers, as the form describes it when Start is pressed.

    Attributes:
        input_paths: Every file to process.
        input_root: Folder the inputs are relative to; with an output folder, its structure is mirrored there.
        source_lang: The spoken language, or None to auto-detect it.
        target_langs: The languages to translate into; empty when the job only transcribes.
        output_format: Subtitle format to write.
        output_dir: Folder for the output files, or None to write each next to its input.
    """

    input_paths: list[str]
    input_root: str
    source_lang: Language | None
    target_langs: list[Language]
    output_format: OutputFormat
    output_dir: str | None

    def expected_outputs(self) -> list[str]:
        """Return the paths this job would write, wherever they can be predicted ahead of time.

        The paths are used only for the overwrite warning, so it is fine to leave out cases where the language is not
        known yet (auto-detect). Nothing is created on disk: the user may still cancel, and the output folder is made
        by the pipeline when the job writes its first file.
        """
        expected: list[str] = []
        # Same rule and order as the pipeline, so that inputs sharing a name get the same names as in the real run.
        names = OutputNames()
        codes = self._output_language_codes()
        for input_path in self.input_paths:
            for code in codes:
                path = batch_output_path(input_path, code, self.output_format, self.output_dir, self.input_root)
                expected.append(names.claim(input_path, path))
        return expected

    def _output_language_codes(self) -> list[str]:
        """Return the language code of each file written for one input.

        That is every target except the source language itself (nothing is translated into it), or the source
        language when the job only transcribes.
        """
        source_code = self.source_lang.whisper_code if self.source_lang is not None else None
        if self.target_langs:
            return [t.whisper_code for t in self.target_langs if t.whisper_code != source_code]
        return [source_code] if source_code is not None else []

    def scan(self, store: JobStore, config: AppConfig) -> ResumeReport:
        """Report what an earlier run of this same job left in ``store``."""
        return store.scan(
            self.input_paths,
            self.source_lang.whisper_code if self.source_lang else None,
            [t.whisper_code for t in self.target_langs],
            config.llm_model_path, config.whisper_model_path,
            self.output_format.extension, self.output_dir, self.input_root,
            use_partial_transcripts=config.resume_transcription,
        )

    def pipeline_factory(self, config: AppConfig):
        """Return the factory that builds this job's pipeline, for `PipelineWorker`.

        The worker calls it on its own thread with its callbacks and cancellation token.
        """
        def factory(on_progress, on_percent, cancel):
            return BatchPipeline(
                self.input_paths, self.source_lang, self.target_langs, self.output_format, config,
                output_dir=self.output_dir, input_root=self.input_root,
                on_progress=on_progress, on_percent=on_percent, cancel=cancel,
            )
        return factory


class VideoTabView(Adw.Bin):
    """The Video tab: turn a video, a subtitle file or a course folder into subtitle file(s).

    The job transcribes the speech, or transcribes and translates it into one or several languages at once. While
    it runs, the form is locked and the Start button turns into Stop.

    Attributes:
        is_busy: True while a job runs. The window reads it to decide whether it may close.

    Signals:
        toast-requested (str): A short message for the window to show as a toast.
        job-finished (str, str): A job ended, for a notification from outside the app: (title, body).
        state-changed: Something the window remembers between sessions changed (see `capture_state`).
    """

    __gsignals__ = {
        "toast-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "job-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # A single file or a folder, depending on the batch switch.
        self._selected_path: str | None = None
        self._output_dir_path: str | None = None
        # Where the file dialogs open: the folder the last input came from.
        self._input_dir = ""
        self._worker: PipelineWorker | None = None
        self.is_busy = False
        self._stopping = False
        # How many files the job being started covers, for the notification when it ends.
        self._job_files = 0

        self.set_child(self._build_content())

    # --- UI construction ---

    def _build_content(self) -> Gtk.Widget:
        # The form is a centred column of a comfortable width, but the log below it is not held to that width: it
        # grows with the window, so a maximised window uses all of it.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)
        clamp = Adw.Clamp(maximum_size=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(box)
        page.append(clamp)
        box.append(self._build_input_group())
        box.append(self._build_options_group())
        box.append(self._build_run_button())
        box.append(self._build_progress())
        page.append(self._build_log())
        return page

    def _build_input_group(self) -> Adw.PreferencesGroup:
        self._choose_button = Gtk.Button(label="Choose File…", valign=Gtk.Align.CENTER)
        self._choose_button.connect("clicked", self._on_choose_clicked)
        self._file_row = button_row("No file selected", self._choose_button, subtitle=_FILE_HINT)

        self._batch_row = Adw.SwitchRow(
            title="Batch mode (folder)",
            subtitle="Process every video/subtitle file in a folder — e.g. a whole course",
        )
        self._batch_row.connect("notify::active", self._on_batch_toggled)

        self._file_group = Adw.PreferencesGroup(title="Input")
        self._file_group.add(self._file_row)
        self._file_group.add(self._batch_row)
        return self._file_group

    def _build_options_group(self) -> Adw.PreferencesGroup:
        self._options_group = Adw.PreferencesGroup(title="Options")
        self._mode_row = combo_row(
            "Mode", ["Transcribe only (same language)", "Transcribe & Translate"], 0, self._on_mode_changed
        )
        self._options_group.add(self._mode_row)
        self._add_language_rows(self._options_group)
        self._format_row = combo_row("Output format", ["SRT (.srt)", "VTT (.vtt)"], 0, self._on_state_changed)
        self._options_group.add(self._format_row)
        self._options_group.add(self._build_output_dir_row())
        return self._options_group

    def _add_language_rows(self, group: Adw.PreferencesGroup) -> None:
        # The source is the language spoken in the video: only those Whisper recognises reliably are offered, and
        # anything else works with Auto-detect. The targets are every language the translation model handles well.
        self._source_picker = LanguagePickerButton(
            speech_language_names(), include_auto=True, valign=Gtk.Align.CENTER,
        )
        self._source_picker.connect("language-selected", self._on_state_changed)
        self._source_row = Adw.ActionRow(title="Source language")
        self._source_row.add_suffix(self._source_picker)
        group.add(self._source_row)

        # Target languages are multi-select: translating one file or folder into several languages in one pass is
        # the whole point of reusing a single transcription.
        self._target_picker = MultiLanguagePickerButton(available_language_names(), valign=Gtk.Align.CENTER)
        self._target_picker.connect("selection-changed", self._on_state_changed)
        self._target_row = Adw.ActionRow(title="Target language(s)")
        self._target_row.add_suffix(self._target_picker)
        self._target_row.set_visible(False)
        group.add(self._target_row)

    def _build_output_dir_row(self) -> Adw.ActionRow:
        self._output_dir_button = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        self._output_dir_button.connect("clicked", self._on_choose_output_dir)
        self._output_dir_clear_button = Gtk.Button(
            icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, visible=False,
            tooltip_text="Reset to default",
        )
        self._output_dir_clear_button.connect("clicked", self._on_clear_output_dir)
        self._output_dir_row = Adw.ActionRow(title="Output folder", subtitle=_SAME_FOLDER)
        self._output_dir_row.add_suffix(self._output_dir_clear_button)
        self._output_dir_row.add_suffix(self._output_dir_button)
        return self._output_dir_row

    def _build_run_button(self) -> Gtk.Button:
        self._run_button = Gtk.Button(label="Start")
        self._run_button.add_css_class("suggested-action")
        self._run_button.add_css_class("pill")
        self._run_button.set_halign(Gtk.Align.CENTER)
        self._run_button.connect("clicked", self._on_run_button_clicked)
        return self._run_button

    def _build_progress(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        self._spinner = Gtk.Spinner(visible=False)
        self._progress_bar = Gtk.ProgressBar(visible=False, hexpand=True)
        box.append(self._spinner)
        box.append(self._progress_bar)
        return box

    def _build_log(self) -> Gtk.Frame:
        self._log_buffer = Gtk.TextBuffer()
        log_view = Gtk.TextView(
            buffer=self._log_buffer, editable=False, cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8,
            left_margin=8, right_margin=8,
        )
        log_view.add_css_class("monospace")
        scrolled = Gtk.ScrolledWindow(min_content_height=180, vexpand=True)
        scrolled.set_child(log_view)
        frame = Gtk.Frame(vexpand=True)
        frame.set_child(scrolled)
        return frame

    # --- Handlers: mode/batch toggles ---

    def _on_state_changed(self, *_args) -> None:
        self.emit("state-changed")

    def _on_mode_changed(self, row: Adw.ComboRow, _pspec) -> None:
        self._target_row.set_visible(row.get_selected() == _TRANSLATE_MODE)
        self.emit("state-changed")

    def _on_batch_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self._selected_path = None
        if row.get_active():
            self._choose_button.set_label("Choose Folder…")
            self._file_row.set_title("No folder selected")
            self._file_row.set_subtitle("Every video/subtitle file inside will be processed")
        else:
            self._choose_button.set_label("Choose File…")
            self._file_row.set_title("No file selected")
            self._file_row.set_subtitle(_FILE_HINT)
        self.emit("state-changed")

    # --- Handlers: file/folder pickers ---

    def _on_choose_clicked(self, _button: Gtk.Button) -> None:
        if self._batch_row.get_active():
            dialog = Gtk.FileDialog(title="Select a course folder")
            start_in(dialog, self._input_dir)
            dialog.select_folder(self.get_root(), None, self._on_folder_dialog_response)
        else:
            dialog = Gtk.FileDialog(title="Select a video or subtitle file")
            start_in(dialog, self._input_dir)
            filters = file_filters()
            dialog.set_filters(filters)
            dialog.set_default_filter(filters.get_item(0))
            dialog.open(self.get_root(), None, self._on_file_dialog_response)

    def _on_file_dialog_response(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        if path := chosen_path(dialog.open_finish, result):
            self._show_file(path)
            self._translate_if_subtitles(path)

    def _translate_if_subtitles(self, path: str) -> None:
        """Switch to Translate mode when a subtitle file was chosen, and say so.

        A subtitle file can only be translated, because there is no speech to recognise in it. The tab starts in
        Transcribe mode, so this tells the user at once instead of leaving them to find out when they press Start.
        """
        if _is_subtitle_file(path) and not self._translating():
            self._mode_row.set_selected(_TRANSLATE_MODE)
            self.emit(
                "toast-requested",
                "Subtitle files are translated, not transcribed: switched to Translate. Choose the target language(s).",
            )

    def _on_folder_dialog_response(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        if path := chosen_path(dialog.select_folder_finish, result):
            self._show_folder(path)

    def _show_file(self, path: str) -> None:
        self._selected_path = path
        self._input_dir = os.path.dirname(path)
        self._file_row.set_title(os.path.basename(path))
        self._file_row.set_subtitle(path)
        self.emit("state-changed")

    def _show_folder(self, path: str) -> None:
        self._selected_path = path
        # Open the dialogs beside the folder, so that it stays in view.
        self._input_dir = os.path.dirname(path.rstrip(_SEPARATORS))
        file_count = len(_scan_folder(path))
        self._file_row.set_title(os.path.basename(path) or path)
        self._file_row.set_subtitle(f"{path}  ·  {file_count} file(s) found")
        self.emit("state-changed")

    def _on_choose_output_dir(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Select an output folder")
        start_in(dialog, self._output_dir_path or self._input_dir)
        dialog.select_folder(self.get_root(), None, self._on_output_dir_response)

    def _on_output_dir_response(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        if path := chosen_path(dialog.select_folder_finish, result):
            self._show_output_dir(path)

    def _show_output_dir(self, path: str) -> None:
        self._output_dir_path = path
        self._output_dir_row.set_subtitle(path)
        self._output_dir_clear_button.set_visible(True)
        self.emit("state-changed")

    def _on_clear_output_dir(self, _button: Gtk.Button) -> None:
        self._output_dir_path = None
        self._output_dir_row.set_subtitle(_SAME_FOLDER)
        self._output_dir_clear_button.set_visible(False)
        self.emit("state-changed")

    # --- What is remembered between sessions ---

    def capture_state(self) -> VideoState:
        """Collect the choices to remember between sessions."""
        return VideoState(
            batch=self._batch_row.get_active(),
            input_path=self._selected_path or "",
            mode="translate" if self._translating() else "transcribe",
            source_language=self._source_picker.get_selected(),
            target_languages=self._target_picker.get_selected(),
            output_format="srt" if self._format_row.get_selected() == 0 else "vtt",
            output_dir=self._output_dir_path or "",
        )

    def apply_state(self, state: VideoState) -> None:
        """Restore a saved state.

        Whatever no longer exists (a moved file, a deleted folder, a language the list does not offer any more) is
        left at its default instead of being restored.
        """
        # First, because switching batch mode forgets the chosen path.
        self._batch_row.set_active(state.batch)
        self._mode_row.set_selected(_TRANSLATE_MODE if state.mode == "translate" else 0)
        if state.source_language in self._source_picker.language_names():
            self._source_picker.set_selected(state.source_language)
        # Language names the picker does not know are ignored.
        self._target_picker.set_selected(state.target_languages)
        self._format_row.set_selected(1 if state.output_format == "vtt" else 0)
        if state.output_dir and os.path.isdir(state.output_dir):
            self._show_output_dir(state.output_dir)
        path = state.input_path
        if path:
            # Remembered even when the path is gone, so that the file dialogs can still open in its folder.
            self._input_dir = os.path.dirname(path.rstrip(_SEPARATORS))
            if os.path.isdir(path) if state.batch else os.path.isfile(path):
                (self._show_folder if state.batch else self._show_file)(path)

    # --- Start / run ---

    def _on_start_clicked(self, _button: Gtk.Button) -> None:
        # Load the configuration as it is now: Preferences may have changed since the tab was built.
        config = AppConfig.load()
        try:
            plan = self._plan_job(config)
        except _CannotStart as problem:
            self.emit("toast-requested", str(problem))
            return
        self._job_files = len(plan.input_paths)
        factory = plan.pipeline_factory(config)
        expected_outputs = plan.expected_outputs()

        # If an earlier run of this same job (stopped, crashed or interrupted) saved anything, offer to continue
        # instead of starting over.
        store = JobStore()
        report = plan.scan(store, config)
        if report.has_progress:
            self._ask_resume(report, store, plan.input_paths, expected_outputs, factory)
        else:
            self._maybe_confirm_overwrite(expected_outputs, factory)

    def _plan_job(self, config: AppConfig) -> _JobPlan:
        """Read the form into a job plan.

        Raises:
            _CannotStart: The form is not ready; the message is what to tell the user.
        """
        batch = self._batch_row.get_active()
        path = self._selected_path
        if not path:
            raise _CannotStart(f"Please choose a {'folder' if batch else 'file'} first.")
        if not os.path.exists(path):
            raise _CannotStart("The selected path no longer exists.")

        errors = config.validation_errors()
        if errors:
            raise _CannotStart(errors[0])

        input_paths = _scan_folder(path) if batch else [path]
        if not input_paths:
            raise _CannotStart("That folder has no video or subtitle files in it.")

        source_label = self._source_picker.get_selected()
        source_lang = None if source_label == _AUTO_DETECT_LABEL else resolve_language(source_label)
        target_langs = self._chosen_target_languages()
        self._check_languages(source_lang, target_langs)
        return _JobPlan(
            input_paths=input_paths,
            input_root=path if batch else os.path.dirname(path),
            source_lang=source_lang,
            target_langs=target_langs,
            output_format=OutputFormat.SRT if self._format_row.get_selected() == 0 else OutputFormat.VTT,
            output_dir=self._output_dir_path,
        )

    def _chosen_target_languages(self) -> list[Language]:
        """Return the languages to translate into: none when the job only transcribes."""
        if not self._translating():
            return []
        return [resolve_language(name) for name in self._target_picker.get_selected()]

    def _check_languages(self, source_lang: Language | None, target_langs: list[Language]) -> None:
        """Check that the chosen mode and languages make a job.

        Raises:
            _CannotStart: The mode and languages cannot produce any output.
        """
        if self._translating():
            if not target_langs:
                raise _CannotStart("Please choose at least one target language.")
            if source_lang is not None and all(t.whisper_code == source_lang.whisper_code for t in target_langs):
                raise _CannotStart("Target language(s) can't all be the same as the source.")
        elif not self._batch_row.get_active() and _is_subtitle_file(self._selected_path):
            raise _CannotStart(
                "A subtitle file can only be translated: switch to Translate and choose a target language."
            )

    # --- Resume ---

    def _ask_resume(self, report, store, input_paths, expected_outputs, factory) -> None:
        """Ask whether to continue an earlier run of this job, start over, or cancel, and act on the answer."""
        dialog = Adw.AlertDialog(
            heading="Continue where you left off?",
            body=(
                "An earlier run of this job didn\u2019t finish:\n\n"
                + resume_summary(report)
                + "\n\nContinue to pick up from there, or start over from the beginning."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("restart", "Start Over")
        dialog.add_response("resume", "Continue")
        dialog.set_response_appearance("resume", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("resume")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "resume":
                # Finished outputs are skipped, not replaced, so they must not appear in the overwrite warning.
                remaining = [p for p in expected_outputs if p not in report.completed_outputs]
                self._maybe_confirm_overwrite(remaining, factory)
            elif response == "restart":
                store.clear_inputs(input_paths)
                self._maybe_confirm_overwrite(expected_outputs, factory)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _maybe_confirm_overwrite(self, expected_outputs: list[str], factory) -> None:
        """Start the job, after asking first when some of its output files already exist."""
        existing = [p for p in expected_outputs if os.path.exists(p)]
        if not existing:
            self._launch_job(factory)
            return

        if len(existing) == 1:
            body = f"“{os.path.basename(existing[0])}” already exists and will be replaced."
        else:
            body = f"{len(existing)} output files already exist and will be replaced."

        dialog = Adw.MessageDialog(
            transient_for=self.get_root(), heading="Overwrite existing file(s)?", body=body,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("overwrite", "Overwrite")
        dialog.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: self._on_overwrite_response(response, factory))
        dialog.present()

    def _on_overwrite_response(self, response: str, factory) -> None:
        if response == "overwrite":
            self._launch_job(factory)

    def _launch_job(self, factory) -> None:
        """Start the pipeline built by ``factory`` on a worker thread and route its signals back to this tab."""
        self._log_buffer.set_text("")
        self._set_busy(True)

        self._worker = PipelineWorker(factory)
        self._worker.connect("log", lambda _w, msg: self._append_log(msg))
        self._worker.connect("progress", lambda _w, frac: self._progress_bar.set_fraction(frac))
        self._worker.connect("finished", self._on_finished)
        self._worker.connect("cancelled", self._on_cancelled)
        self._worker.start()

    def _on_run_button_clicked(self, button: Gtk.Button) -> None:
        """Handle the main button, which is Start when idle and Stop while a job runs."""
        if self.is_busy:
            self._request_stop()
        else:
            self._on_start_clicked(button)

    def stop_for_quit(self) -> None:
        """Ask the running job, if any, to stop because the app is closing."""
        if self.is_busy:
            self._request_stop()

    def _request_stop(self) -> None:
        if self._worker is None or self._stopping:
            return
        self._stopping = True
        self._worker.cancel()
        self._run_button.set_sensitive(False)
        self._run_button.set_label("Stopping\u2026")
        self._append_log("\nStopping \u2014 finishing the current step\u2026")

    def _on_cancelled(self, _worker: PipelineWorker) -> None:
        self._set_busy(False)
        self._append_log(
            "Stopped. Your progress is saved \u2014 press Start again to continue where you left off. "
            "Finished files are kept; the file in progress is only written once it\u2019s complete."
        )
        self.emit("toast-requested", "Stopped \u2014 progress saved.")

    def _on_finished(self, _worker: PipelineWorker, success: bool, message: str) -> None:
        self._set_busy(False)
        if success:
            self.emit("toast-requested", "Done! Check the log below for details on each file.")
            self.emit("job-finished", *video_done_notice(
                self._selected_path, self._batch_row.get_active(), self._job_files,
            ))
        else:
            self._append_log(f"Error: {message}")
            self.emit("toast-requested", "Something went wrong — check the log for details.")
            self.emit("job-finished", "Job failed", short_error(message))

    # --- Small helpers ---

    def _translating(self) -> bool:
        """Whether the Mode row is on "Transcribe & Translate"."""
        return self._mode_row.get_selected() == _TRANSLATE_MODE

    def _append_log(self, message: str) -> None:
        end_iter = self._log_buffer.get_end_iter()
        self._log_buffer.insert(end_iter, message + "\n")

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        if not busy:
            self._stopping = False
        # The Start button turns into Stop while a job runs. It is enabled again here because a pending stop
        # disables it.
        self._run_button.set_sensitive(True)
        self._run_button.set_label("Stop" if busy else "Start")
        set_css_class(self._run_button, "suggested-action", not busy)
        set_css_class(self._run_button, "destructive-action", busy)
        self._choose_button.set_sensitive(not busy)
        self._batch_row.set_sensitive(not busy)
        self._options_group.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._progress_bar.set_visible(busy)
        self._progress_bar.set_fraction(0.0)
