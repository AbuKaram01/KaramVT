# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Text tab: free-text translation, with optional OCR import from an image.

`TextTabView` only builds the UI and reacts to it. Translation and OCR run as jobs on a `TextJobWorker` background
thread; the tab shows their progress, streams partial translations into the output box and reports outcomes to the
main window through GObject signals. The loaded translation model is cached on the tab, so that translating again
does not reload it from disk.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from ...config import AppConfig
from ...core.hardware import resolve_llm_gpu_layers
from ...core.languages import Language, available_language_names, resolve_language
from ...core.llm_settings import sampling_from_config
from ...core.ocr import OcrEngine
from ...core.ocr_options import tess_vars_from_config
from ...core.text_translation import MAX_INPUT_CHARS, TextTranslationEngine, load_llm
from ...core.ui_state import TextState
from ..formatting import short_error
from ..language_picker import LanguagePickerButton
from ..ocr_settings_dialog import present_ocr_settings
from ..text_worker import TextJobWorker
from ..widgets import chosen_path, filter_list, set_css_class, start_in

# Label of the source-language picker entry that means "detect the language".
_AUTO_DETECT_LABEL = "Auto-detect"
# Margin, in pixels, around the text views and the controls that float over them.
_TEXT_MARGIN = 8


@dataclass(frozen=True)
class _TranslationRequest:
    """Snapshot of one press of Translate, taken on the main thread before the job starts on a worker thread."""

    config: AppConfig
    text: str
    source_label: str
    target_label: str
    source: Language
    target: Language
    started: float


def _circular_button(icon: str, tooltip: str, on_clicked) -> Gtk.Button:
    """Create a round icon button meant to float over a text box.

    The "osd" style class is deliberately not used: it paints the button near-black, which draws the eye. A plain
    circular button is a soft tint of the text colour and follows the theme.
    """
    button = Gtk.Button()
    button.set_icon_name(icon)
    button.set_tooltip_text(tooltip)
    button.add_css_class("circular")
    button.connect("clicked", on_clicked)
    return button


def _corner_button(icon: str, tooltip: str, on_clicked, *, left: bool) -> Gtk.Button:
    """Create a round icon button for the bottom-left (``left``) or bottom-right corner of the box it floats over."""
    button = _circular_button(icon, tooltip, on_clicked)
    button.set_halign(Gtk.Align.START if left else Gtk.Align.END)
    button.set_valign(Gtk.Align.END)
    if left:
        button.set_margin_start(_TEXT_MARGIN)
    else:
        button.set_margin_end(_TEXT_MARGIN)
    button.set_margin_bottom(_TEXT_MARGIN)
    return button


def _text_box(view: Gtk.TextView) -> tuple[Gtk.Box, Gtk.Overlay, Gtk.ScrolledWindow]:
    """Wrap a text view in a frame with a scrolled window and an overlay for the controls that float over it.

    Returns:
        The widget to place in the layout, the overlay to add the floating controls to, and the scrolled window.
    """
    scrolled = Gtk.ScrolledWindow(min_content_height=220, vexpand=True)
    scrolled.set_child(view)
    overlay = Gtk.Overlay()
    overlay.set_child(scrolled)
    frame = Gtk.Frame(vexpand=True)
    frame.set_child(overlay)
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    column.append(frame)
    return column, overlay, scrolled


def _text_view(buffer: Gtk.TextBuffer, **properties) -> Gtk.TextView:
    return Gtk.TextView(
        buffer=buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=_TEXT_MARGIN, bottom_margin=_TEXT_MARGIN,
        left_margin=_TEXT_MARGIN, right_margin=_TEXT_MARGIN, **properties,
    )


def _ocr_toast(result) -> str | None:
    """Return the toast message for a finished OCR import, or None when there is nothing worth saying."""
    if not result.text:
        return "No text found in the image."
    if result.notice:
        # The engine reported something the user should know (for example that script detection could not run, so
        # English was assumed). Show it instead of only a confidence number.
        return f"Imported text. {result.notice}"
    if result.confidence is not None:
        detected_note = f" \u2014 detected {result.detected_language.name}" if result.detected_language else ""
        return f"Imported text{detected_note} \u2014 OCR confidence: {result.confidence:.0f}%"
    return None


class TextTabView(Adw.Bin):
    """The Text tab: translate typed or pasted text, or text imported from an image with OCR.

    While a job (translation or OCR import) runs, the controls that would change its input are locked and the main
    button turns into Stop for a translation. The window is told about results through signals and decides what to
    do with them.

    Attributes:
        is_busy: True while a job (translation or OCR import) runs. The window reads it to decide whether it may
            close and whether the history can be applied.

    Signals:
        toast-requested (str): A short message for the window to show as a toast.
        job-finished (str, str): A translation ended, for a notification from outside the app: (title, body).
        translation-done (str, str, str, str, str, float): A translation finished: (source language, target
            language, original text, translation, model file name, seconds). The window decides whether to keep it
            in the history.
        state-changed: Something the window remembers between sessions changed (see `capture_state`).
    """

    __gsignals__ = {
        "toast-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "job-finished": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "translation-done": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str, str, str, float)),
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._config = AppConfig.load()
        self._worker: TextJobWorker | None = None
        self.is_busy = False
        # True only while a translation runs: the one job the Stop button applies to.
        self._translating = False
        self._stopping = False
        # Where the image dialog opens: the folder the last image came from.
        self._image_dir = ""
        # The loaded model is kept across translations, even for different language pairs, instead of being reloaded
        # from disk on every click: reloading a GGUF model each time was the app's single biggest avoidable slowdown.
        self._cached_llm = None
        self._cached_llm_key: tuple | None = None

        self.set_child(self._build_content())

    # --- UI construction ---

    def _build_content(self) -> Gtk.Widget:
        # No Adw.Clamp here: the two text boxes grow with the window, so a maximised window uses all of it.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)
        box.append(self._build_language_row())
        box.append(self._build_hint())
        box.append(self._build_text_areas())
        box.append(self._build_actions())

        self._spinner = Gtk.Spinner(visible=False, halign=Gtk.Align.CENTER)
        box.append(self._spinner)
        self._status_label = Gtk.Label(label="", halign=Gtk.Align.CENTER, wrap=True)
        self._status_label.add_css_class("dim-label")
        box.append(self._status_label)
        return box

    def _build_language_row(self) -> Gtk.Box:
        names = available_language_names()
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER)
        self._source_picker = LanguagePickerButton(names, include_auto=True)
        self._swap_button = Gtk.Button(label="⇄", tooltip_text="Swap languages", valign=Gtk.Align.CENTER)
        self._swap_button.connect("clicked", self._on_swap_clicked)
        self._target_picker = LanguagePickerButton(names, include_auto=False)
        self._source_picker.connect("language-selected", self._on_state_changed)
        self._target_picker.connect("language-selected", self._on_state_changed)
        for widget in (self._source_picker, self._swap_button, self._target_picker):
            row.append(widget)
        return row

    @staticmethod
    def _build_hint() -> Gtk.Label:
        hint = Gtk.Label(
            label="Translation quality depends on how well your chosen model supports the selected language.",
            halign=Gtk.Align.CENTER, wrap=True, justify=Gtk.Justification.CENTER,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        return hint

    def _build_text_areas(self) -> Gtk.Box:
        areas = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True, vexpand=True)
        areas.append(self._build_input_box())
        areas.append(self._build_output_box())
        return areas

    def _build_input_box(self) -> Gtk.Box:
        self._input_buffer = Gtk.TextBuffer()
        self._input_buffer.connect("changed", self._on_input_changed)
        column, overlay, _scrolled = _text_box(_text_view(self._input_buffer))
        overlay.add_overlay(self._build_ocr_controls())
        overlay.add_overlay(self._build_char_count_badge())
        return column

    def _build_ocr_controls(self) -> Gtk.Box:
        """Build the import-from-image button and its OCR-settings gear.

        Both float inside the input box (bottom-left corner) instead of sitting below it as a separate labelled
        button. The gear sits next to the import button because it opens that button's OCR settings.
        """
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_halign(Gtk.Align.START)
        controls.set_valign(Gtk.Align.END)
        controls.set_margin_start(_TEXT_MARGIN)
        controls.set_margin_bottom(_TEXT_MARGIN)
        self._ocr_button = _circular_button(
            "image-x-generic-symbolic", "Import from image\u2026", self._on_import_image_clicked,
        )
        self._ocr_settings_button = _circular_button(
            "emblem-system-symbolic", "OCR Settings", self._on_ocr_settings_clicked,
        )
        controls.append(self._ocr_button)
        controls.append(self._ocr_settings_button)
        return controls

    def _build_char_count_badge(self) -> Gtk.Box:
        """Build the "n / limit" character counter as a small pill badge floating in the input box's corner.

        Like the icon buttons, it floats over the box instead of being a plain label below it.
        """
        self._char_count_label = Gtk.Label(label=f"0 / {MAX_INPUT_CHARS}")
        self._char_count_label.add_css_class("caption")
        self._char_count_label.add_css_class("dim-label")
        self._char_count_label.set_margin_start(10)
        self._char_count_label.set_margin_end(10)
        self._char_count_label.set_margin_top(4)
        self._char_count_label.set_margin_bottom(4)
        badge = Gtk.Box()
        badge.append(self._char_count_label)
        badge.add_css_class("card")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_margin_end(_TEXT_MARGIN)
        badge.set_margin_bottom(_TEXT_MARGIN)
        return badge

    def _build_output_box(self) -> Gtk.Box:
        self._output_buffer = Gtk.TextBuffer()
        view = _text_view(self._output_buffer, editable=False, cursor_visible=False)
        column, overlay, self._output_scrolled = _text_box(view)
        # One icon per corner: Clear mirrors the OCR button's spot (bottom-left) on the input box, and Copy mirrors
        # the count badge (bottom-right). Copy uses the standard OS "copy" glyph.
        self._clear_button = _corner_button(
            "edit-clear-symbolic", "Clear translated text", self._on_clear_clicked, left=True,
        )
        self._copy_button = _corner_button(
            "edit-copy-symbolic", "Copy translated text", self._on_copy_clicked, left=False,
        )
        overlay.add_overlay(self._clear_button)
        overlay.add_overlay(self._copy_button)
        return column

    def _build_actions(self) -> Gtk.Box:
        """Build the row with the Translate button (Import from image lives inside the input box)."""
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        # The expanding spacer keeps Translate pinned to the end of the row.
        actions.append(Gtk.Box(hexpand=True))
        self._translate_button = Gtk.Button(label="Translate")
        self._translate_button.add_css_class("suggested-action")
        self._translate_button.add_css_class("pill")
        self._translate_button.connect("clicked", self._on_translate_button_clicked)
        actions.append(self._translate_button)
        return actions

    # --- Handlers ---

    def _on_swap_clicked(self, _button: Gtk.Button) -> None:
        source = self._source_picker.get_selected()
        target = self._target_picker.get_selected()
        if source == _AUTO_DETECT_LABEL:
            self.emit("toast-requested", "Can't swap while source is Auto-detect.")
            return
        self._source_picker.set_selected(target)
        self._target_picker.set_selected(source)

        input_text = self._get_buffer_text(self._input_buffer)
        output_text = self._get_buffer_text(self._output_buffer)
        self._input_buffer.set_text(output_text)
        self._output_buffer.set_text(input_text)
        self.emit("state-changed")

    def _on_state_changed(self, *_args) -> None:
        self.emit("state-changed")

    def _on_input_changed(self, buffer: Gtk.TextBuffer) -> None:
        self.emit("state-changed")
        length = buffer.get_char_count()
        self._char_count_label.set_label(f"{length} / {MAX_INPUT_CHARS}")
        if length > MAX_INPUT_CHARS:
            self._char_count_label.add_css_class("error")
        else:
            self._char_count_label.remove_css_class("error")

    def _on_copy_clicked(self, _button: Gtk.Button) -> None:
        text = self._get_buffer_text(self._output_buffer)
        if not text:
            return
        self.get_clipboard().set(text)
        self.emit("toast-requested", "Copied to clipboard.")

    def _on_clear_clicked(self, _button: Gtk.Button) -> None:
        self._output_buffer.set_text("")
        self.emit("state-changed")

    def _on_import_image_clicked(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Select an image")
        image_filter = Gtk.FileFilter(name="Image files")
        image_filter.add_mime_type("image/*")
        dialog.set_filters(filter_list(image_filter))
        start_in(dialog, self._image_dir)
        dialog.open(self.get_root(), None, self._on_image_dialog_response)

    def _on_image_dialog_response(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        image_path = chosen_path(dialog.open_finish, result)
        if image_path is None:
            return
        self._image_dir = os.path.dirname(image_path)
        self.emit("state-changed")

        source_label = self._source_picker.get_selected()
        language = None if source_label == _AUTO_DETECT_LABEL else resolve_language(source_label)
        # Reload the configuration: Preferences may have changed since this tab was opened.
        ocr_config = AppConfig.load()
        self._launch_job(
            lambda ctx: self._read_image(ctx, image_path, language, ocr_config), on_success=self._on_ocr_success,
        )

    @staticmethod
    def _read_image(ctx, image_path: str, language: Language | None, config: AppConfig):
        engine = OcrEngine(on_progress=ctx.log)
        return engine.extract_text(
            image_path, language,
            psm=config.ocr_psm,
            oem=config.ocr_oem,
            language_source=config.ocr_language_source,
            tess_vars=tess_vars_from_config(config),
        )

    def _on_ocr_success(self, result) -> None:
        self._input_buffer.set_text(result.text)
        if result.detected_language is not None:
            # Auto-detect found a specific language. Show it in the picker instead of silently leaving "Auto-detect"
            # selected with no indication of what OCR actually assumed.
            self._source_picker.set_selected(result.detected_language.name)
        if (message := _ocr_toast(result)) is not None:
            self.emit("toast-requested", message)

    def _on_ocr_settings_clicked(self, _button: Gtk.Button) -> None:
        present_ocr_settings(self.get_root())

    def _on_translate_clicked(self, _button: Gtk.Button) -> None:
        request = self._prepare_translation()
        if request is None:
            return
        self._output_buffer.set_text("")
        self._launch_job(
            lambda ctx: self._translate(ctx, request), on_success=lambda outcome: self._on_translated(outcome, request),
            translating=True, on_partial=self._show_partial,
        )

    def _prepare_translation(self) -> _TranslationRequest | None:
        """Read the form into a request, or return None (after telling the user why) when it cannot be translated."""
        self._config = AppConfig.load()
        text = self._get_buffer_text(self._input_buffer)
        source_label = self._source_picker.get_selected()
        target_label = self._target_picker.get_selected()
        source = None if source_label == _AUTO_DETECT_LABEL else resolve_language(source_label)
        target = resolve_language(target_label)

        if problem := self._problem_with(text, source, target):
            self.emit("toast-requested", problem)
            return None
        if source is None:
            # Unlike the video pipeline, there is no separate detection step here. English is a neutral nominal
            # source that the model can work from whatever the text really is.
            source = resolve_language("english")
        return _TranslationRequest(
            self._config, text, source_label, target_label, source, target, started=time.monotonic(),
        )

    def _problem_with(self, text: str, source: Language | None, target: Language) -> str | None:
        """Return the message explaining why this translation cannot start, or None when it can."""
        errors = self._config.validation_errors()
        if errors:
            return errors[0]
        if not text.strip():
            return "Type or paste some text first."
        if len(text) > MAX_INPUT_CHARS:
            return f"Text is too long — keep it under {MAX_INPUT_CHARS} characters."
        if source is not None and source.whisper_code == target.whisper_code:
            return "Source and target language can't be the same."
        return None

    def _translate(self, ctx, request: _TranslationRequest):
        """Translate the request's text; this is the job body and runs on the worker thread.

        Returns:
            The translation and the warnings the model reported while producing it.
        """
        warnings: list[str] = []

        def on_progress(message: str) -> None:
            if message.startswith("Warning"):
                warnings.append(message)
            ctx.log(message)

        config = request.config
        llm = self._model(config, on_progress)
        # Loading cannot be interrupted, so Stop pressed meanwhile is honoured here, before translating starts.
        ctx.cancel.raise_if_cancelled()
        ctx.log("Translating\u2026")
        engine = TextTranslationEngine(
            request.source, request.target, config.llm_model_path,
            on_progress=on_progress, llm=llm, sampling=sampling_from_config(config),
        )
        return engine.translate(request.text, on_partial=ctx.partial, cancel=ctx.cancel), warnings

    def _model(self, config: AppConfig, on_progress):
        """Return the translation model, loading it only when the model file, device or thread count changed."""
        key = (config.llm_model_path, config.processing_device, config.cpu_threads)
        if self._cached_llm is None or self._cached_llm_key != key:
            self._cached_llm = load_llm(
                config.llm_model_path, cpu_threads=config.cpu_threads,
                n_gpu_layers=resolve_llm_gpu_layers(config.processing_device), on_progress=on_progress,
            )
            self._cached_llm_key = key
        return self._cached_llm

    def _on_translated(self, outcome, request: _TranslationRequest) -> None:
        result, warnings = outcome
        # The finished text replaces the streamed one: it is the checked result, which can differ (for example the
        # original text is kept when the output looked wrong).
        self._output_buffer.set_text(result)
        if warnings:
            self.emit("toast-requested", warnings[0])
        self.emit("job-finished", "Translation is ready", f"{request.source_label} → {request.target_label}")
        self.emit(
            "translation-done", request.source_label, request.target_label, request.text, result,
            os.path.basename(request.config.llm_model_path), time.monotonic() - request.started,
        )

    def _on_translate_button_clicked(self, button: Gtk.Button) -> None:
        """Handle the main button, which is Translate when idle and Stop while a translation runs."""
        if self._translating:
            self._request_stop()
        else:
            self._on_translate_clicked(button)

    def stop_for_quit(self) -> None:
        """Ask the running translation, if any, to stop because the app is closing.

        An OCR import cannot be interrupted. It is short, so the window simply waits for it.
        """
        if self.is_busy and self._translating:
            self._request_stop()

    def _request_stop(self) -> None:
        if self._worker is None or self._stopping:
            return
        self._stopping = True
        self._worker.cancel()
        self._translate_button.set_sensitive(False)
        self._translate_button.set_label("Stopping\u2026")
        self._set_status("Stopping\u2026")

    def _show_partial(self, text: str) -> None:
        """Show the translation received so far (live translation).

        The view follows the newest text only while the reader is already at the bottom. If they scrolled up to
        re-read something, it is left where they put it.
        """
        adjustment = self._output_scrolled.get_vadjustment()
        at_bottom = adjustment.get_value() >= adjustment.get_upper() - adjustment.get_page_size() - 40
        self._output_buffer.set_text(text)
        if at_bottom:
            GLib.idle_add(
                lambda: (adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size()), False)[1]
            )

    # --- What is remembered between sessions ---

    def capture_state(self, keep_text: bool = True) -> TextState:
        """Collect the choices to remember between sessions.

        Args:
            keep_text: False leaves the two text boxes out of the state. It is used when the user turned the history
                off, which means their text is not kept on disk.
        """
        return TextState(
            source_language=self._source_picker.get_selected(),
            target_language=self._target_picker.get_selected(),
            input_text=self._get_buffer_text(self._input_buffer) if keep_text else "",
            output_text=self._get_buffer_text(self._output_buffer) if keep_text else "",
            image_dir=self._image_dir,
        )

    def apply_state(self, state: TextState) -> None:
        """Restore a saved state; a language the list no longer offers is left at its default."""
        if state.source_language in self._source_picker.language_names():
            self._source_picker.set_selected(state.source_language)
        if state.target_language in self._target_picker.language_names():
            self._target_picker.set_selected(state.target_language)
        self._image_dir = state.image_dir
        self._input_buffer.set_text(state.input_text[:MAX_INPUT_CHARS])
        self._output_buffer.set_text(state.output_text)

    def show_translation(self, source_language: str, target_language: str, source_text: str, translated: str) -> None:
        """Put a translation from the history back into the two boxes, with the languages it was made with."""
        for picker, name in ((self._source_picker, source_language), (self._target_picker, target_language)):
            if name in picker.language_names():
                picker.set_selected(name)
        self._input_buffer.set_text(source_text[:MAX_INPUT_CHARS])
        self._output_buffer.set_text(translated)
        self.emit("state-changed")

    # --- Job plumbing ---

    def _launch_job(self, job, on_success, translating: bool = False, on_partial=None) -> None:
        """Run ``job`` on a worker thread and route its signals back to this tab.

        Args:
            job: Callable taking the worker's job context; runs on the worker thread.
            on_success: Called on the main thread with the job's result.
            translating: True for a translation, the one job the Stop button applies to.
            on_partial: Called on the main thread with the result so far, for jobs that stream one.
        """
        self._translating = translating
        self._stopping = False
        self._set_busy(True)
        self._worker = TextJobWorker(job)
        self._worker.connect("log", lambda _w, msg: self._set_status(msg))
        if on_partial is not None:
            self._worker.connect("partial", lambda _w, text: on_partial(text))
        self._worker.connect(
            "finished",
            lambda _w, success, result: self._on_job_finished(success, result, on_success),
        )
        self._worker.connect("cancelled", lambda _w: self._on_job_cancelled())
        self._worker.start()

    def _on_job_finished(self, success: bool, result, on_success) -> None:
        translating = self._translating
        self._end_job()
        if success:
            on_success(result)
            self._set_status("")
        else:
            self.emit("toast-requested", "Something went wrong — see below.")
            self._set_status(f"Error: {result}")
            # Only a translation is worth a notification: importing from an image takes seconds.
            if translating:
                self.emit("job-finished", "Translation failed", short_error(str(result)))
        self.emit("state-changed")

    def _on_job_cancelled(self) -> None:
        # The partial translation received before Stop stays in the output box.
        self._end_job()
        self._set_status("")
        self.emit("toast-requested", "Translation stopped.")
        self.emit("state-changed")

    def _end_job(self) -> None:
        self._translating = False
        self._stopping = False
        self._set_busy(False)

    # --- Small helpers ---

    def _get_buffer_text(self, buffer: Gtk.TextBuffer) -> str:
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, False)

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        # While a translation runs the main button turns into Stop. During any other job (OCR) it is simply
        # disabled, since OCR cannot be interrupted.
        stop_mode = busy and self._translating
        self._translate_button.set_sensitive(not busy or stop_mode)
        self._translate_button.set_label("Stop" if stop_mode else "Translate")
        set_css_class(self._translate_button, "suggested-action", not stop_mode)
        set_css_class(self._translate_button, "destructive-action", stop_mode)
        # While a job runs these would corrupt it: swapping or clearing moves or erases the very text the job is
        # reading or filling in (the translation streams into the output box), and changing the languages midway
        # would make the result not match what the pickers show. Copy stays available, because copying a partial
        # result is harmless. The OCR settings are locked too: changing engine settings under a running OCR job
        # would make the result not match what the dialog shows.
        for locked_while_busy in (
            self._ocr_button, self._swap_button, self._clear_button, self._source_picker, self._target_picker,
            self._ocr_settings_button,
        ):
            locked_while_busy.set_sensitive(not busy)
        self._ocr_settings_button.set_tooltip_text(
            "OCR Settings" if not busy else "OCR Settings \u2014 unavailable while a job is running"
        )
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _set_status(self, message: str) -> None:
        self._status_label.set_label(message)
