# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Preferences window: model locations and downloads, performance, history, saved progress and OCR.

Every change is saved to the config file immediately, so the window has no "Apply" step.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..config import AppConfig, update_saved_config
from ..core.checkpoint import JobStore
from ..core.history import MIN_LIMIT, HistoryStore
from ..core.llm_settings import LLM_PARAMS
from ..core.models import ProcessingDevice
from ..services import DownloadManager
from .model_downloads import ModelDownloadGroup
from .ocr_settings_dialog import present_ocr_settings
from .widgets import browse_button, button_row, confirm, flat_button, pick_file, pick_folder, set_css_class

# The order of the entries in the processing device row.
_DEVICE_ORDER = [ProcessingDevice.AUTO, ProcessingDevice.CPU, ProcessingDevice.GPU]
_HISTORY_LIMIT_CHOICES_MAX = 10_000     # The most the spin button offers; a hand-edited file may hold more.
# Scrolling to the download group: the space in pixels kept above it, then the retry interval and the number of
# attempts (about two seconds in all) while the window is not laid out yet.
_SCROLL_MARGIN = 12
_SCROLL_RETRY_MS = 50
_SCROLL_MAX_ATTEMPTS = 40

_RESUME_WARNING = (
    "If a video is stopped (or the app is interrupted) while it is being transcribed, "
    "the next run will continue from the last saved point instead of starting the "
    "video over.\n\n"
    "The place where the two parts are joined may be slightly off: a word there can be "
    "cut, repeated or missing, so the result isn’t guaranteed to match a "
    "transcription done in one go. Translation resuming is not affected and is exact."
)


def _find_scrolled_window(widget: Gtk.Widget) -> Gtk.ScrolledWindow | None:
    """Return the first scrolled window below ``widget`` (the one that holds the preferences page), if any."""
    if isinstance(widget, Gtk.ScrolledWindow):
        return widget
    child = widget.get_first_child()
    while child is not None:
        if (found := _find_scrolled_window(child)) is not None:
            return found
        child = child.get_next_sibling()
    return None


def _switch_row(title: str, subtitle: str, active: bool, on_toggled) -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=title, subtitle=subtitle)
    row.set_active(active)
    row.connect("notify::active", on_toggled)
    return row


def _confirm_resume_dialog(on_enable) -> Adw.AlertDialog:
    """Build the warning shown before resuming is enabled; ``on_enable`` is called only if the user confirms."""
    dialog = Adw.AlertDialog(heading="Enable resuming transcription?", body=_RESUME_WARNING)
    dialog.add_response("cancel", "Keep Off")
    dialog.add_response("enable", "Enable")
    dialog.set_response_appearance("enable", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _dialog, response: on_enable() if response == "enable" else None)
    return dialog


class PreferencesWindow(Adw.PreferencesWindow):
    """Window where the user points the app at the local Whisper and LLM models and adjusts the other settings.

    Every change is saved to the config file immediately. There is no separate "Apply" step, matching how GNOME
    Settings-style dialogs behave.

    Args:
        downloads: The manager behind the model download group. The group is left out when it is None.
        history: The translation history. The "Saved translations" row is left out when it is None.
        **kwargs: Properties passed on to ``Adw.PreferencesWindow``.
    """

    def __init__(
        self, downloads: DownloadManager | None = None, history: HistoryStore | None = None, **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._history = history
        self._downloads_group: ModelDownloadGroup | None = None
        self._scroll_attempts = 0
        self._showing_saved_paths = False   # True while the path rows are being refreshed from the saved file.
        self._resume_switch_guard = False   # True while code, not the user, moves the resume switch.
        self.set_title("Preferences")
        self.set_default_size(480, -1)
        self.set_search_enabled(False)

        self._config = AppConfig.load()

        page = Adw.PreferencesPage()
        self.add(page)
        self._add_models_group(page)
        if downloads is not None:
            self._add_downloads_group(page, downloads)
        self._add_performance_group(page)
        self._add_notifications_group(page)
        self._add_history_group(page)
        self._add_sampling_group(page)
        self._add_saved_progress_group(page)
        self._add_ocr_group(page)
        self._save_when_changed()

        # Reflect the state of whatever was already saved, for example a hand-edited config file or a model folder
        # that was moved since the last run.
        self._update_validation_styles()

    # --- Building the page ---

    def _add_models_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Local Models",
            description=(
                "Everything runs on this machine — no cloud service is used "
                "for transcription or translation."
            ),
        )
        page.add(group)
        self._whisper_row = self._path_row(
            group, "Faster-Whisper model directory", self._config.whisper_model_path, self._on_browse_whisper,
        )
        self._llm_row = self._path_row(
            group, "LLM model file (.gguf)", self._config.llm_model_path, self._on_browse_llm,
        )

    @staticmethod
    def _path_row(group: Adw.PreferencesGroup, title: str, path: str, on_browse) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_text(path)
        row.add_suffix(browse_button(on_browse))
        group.add(row)
        return row

    def _add_downloads_group(self, page: Adw.PreferencesPage, downloads: DownloadManager) -> None:
        # The folder row sits inside the model download menu. Its apply button keeps a half-typed path from being
        # saved (and the rows from flickering) on every key press.
        self._folder_row = Adw.EntryRow(title="Download folder", show_apply_button=True)
        self._folder_row.set_text(str(self._config.models_folder()))
        self._folder_row.add_suffix(browse_button(self._on_browse_models_folder, flat=True))
        self._folder_row.connect("apply", self._on_models_folder_applied)
        self._downloads_group = ModelDownloadGroup(self, downloads, self._folder_row, self.refresh_model_paths)
        page.add(self._downloads_group)
        self.connect("close-request", self._on_close_request)

    def _add_performance_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Performance")
        page.add(group)

        self._device_row = Adw.ComboRow(
            title="Processing device",
            subtitle="Auto uses a GPU when a CUDA-capable one is detected, otherwise CPU",
        )
        self._device_row.set_model(Gtk.StringList.new([d.label for d in _DEVICE_ORDER]))
        self._device_row.set_selected(_DEVICE_ORDER.index(self._saved_device()))
        group.add(self._device_row)

        self._threads_row = Adw.SpinRow.new_with_range(1, 32, 1)
        self._threads_row.set_title("CPU threads")
        self._threads_row.set_subtitle("Used for both transcription and translation")
        self._threads_row.set_value(self._config.cpu_threads)
        group.add(self._threads_row)

    def _saved_device(self) -> ProcessingDevice:
        try:
            return ProcessingDevice(self._config.processing_device)
        except ValueError:
            return ProcessingDevice.AUTO

    def _add_notifications_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Notifications")
        page.add(group)
        self._notify_row = _switch_row(
            "Notify when a job finishes",
            "A desktop notification when a video job or a text translation ends while this window isn’t in front",
            self._config.notify_when_done,
            lambda row, _param: update_saved_config(notify_when_done=row.get_active()),
        )
        group.add(self._notify_row)

    def _add_history_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="History")
        page.add(group)
        self._history_row = _switch_row(
            "Save translation history",
            "Keep every translation from the Text tab, and the text in its two boxes, on this computer "
            "so it is still there next time. Nothing is ever sent anywhere.",
            self._config.history_enabled,
            self._on_history_toggled,
        )
        group.add(self._history_row)

        self._history_limit_row = Adw.SpinRow.new_with_range(MIN_LIMIT, _HISTORY_LIMIT_CHOICES_MAX, 10)
        self._history_limit_row.set_title("Keep up to")
        self._history_limit_row.set_subtitle("Translations. Older ones are removed first.")
        self._history_limit_row.set_value(self._config.history_keep)
        self._history_limit_row.set_sensitive(self._config.history_enabled)
        self._history_limit_row.connect(
            "notify::value", lambda row, _param: update_saved_config(history_limit=int(row.get_value())),
        )
        group.add(self._history_limit_row)

        if self._history is not None:
            self._clear_history_button = flat_button("Clear…", self._on_clear_history_clicked)
            self._saved_row = button_row("Saved translations", self._clear_history_button)
            group.add(self._saved_row)
            self._update_saved_row()
            # Translations can finish while this window is open: show the count that is true when it is looked at.
            self.connect("notify::is-active", lambda *_args: self._update_saved_row())

    def _add_sampling_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Translation Model — Advanced",
            description=(
                "How the translation model picks words. The defaults are tuned for translation — "
                "change them only if you know what you want. This applies to the translation model only."
            ),
        )
        page.add(group)

        expander = Adw.ExpanderRow(
            title="Sampling settings",
            subtitle="Temperature, top-p, top-k, min-p, repeat penalty and seed",
        )
        group.add(expander)

        self._llm_rows: dict[str, Adw.SpinRow] = {}
        for field, _kwarg, title, subtitle, minimum, maximum, step, digits, _default in LLM_PARAMS:
            row = Adw.SpinRow.new_with_range(minimum, maximum, step)
            row.set_title(title)
            row.set_subtitle(subtitle)
            row.set_digits(digits)
            row.set_value(getattr(self._config, field))
            expander.add_row(row)
            self._llm_rows[field] = row

        expander.add_row(
            button_row("Reset to defaults", flat_button("Reset", self._on_reset_llm_clicked), activatable=True)
        )

    def _add_saved_progress_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Saved Progress")
        page.add(group)
        # Off by default, and enabling it asks for confirmation: the point where an interrupted transcription is
        # rejoined can occasionally cut or repeat a word.
        self._resume_transcription_row = _switch_row(
            "Resume interrupted transcription",
            "Continue speech recognition from where it stopped instead of starting the video over. "
            "Warning: the point where it rejoins can occasionally cut or repeat a word.",
            self._config.resume_transcription,
            self._on_resume_transcription_toggled,
        )
        group.add(self._resume_transcription_row)

        self._clear_progress_button = flat_button("Clear", self._on_clear_progress_clicked)
        self._resume_row = button_row("Unfinished video jobs", self._clear_progress_button)
        group.add(self._resume_row)
        self._update_resume_row()

    def _add_ocr_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="OCR (Import from Image)")
        page.add(group)
        # The OCR controls live in one place, the same dialog the gear button in the Text tab opens, instead of
        # being duplicated here.
        group.add(button_row(
            "OCR Settings", flat_button("Open", self._on_open_ocr_clicked), activatable=True,
            subtitle="Language source, engine mode and page layout for importing text from images",
        ))

    def _save_when_changed(self) -> None:
        for row, signal in (
            (self._whisper_row, "notify::text"), (self._llm_row, "notify::text"),
            (self._device_row, "notify::selected"), (self._threads_row, "notify::value"),
            *((row, "notify::value") for row in self._llm_rows.values()),
        ):
            row.connect(signal, self._on_any_change)

    # --- Model downloads ---

    def show_downloads(self) -> None:
        """Open the model download menu and bring it into view (used by the Download Models action)."""
        if self._downloads_group is None:
            return
        self._downloads_group.expand()
        self._scroll_attempts = 0
        GLib.timeout_add(_SCROLL_RETRY_MS, self._scroll_to_downloads)

    def _scroll_to_downloads(self) -> bool:
        """Scroll once the window has been laid out; try again shortly if it has not been yet."""
        self._scroll_attempts += 1
        scrolled = _find_scrolled_window(self)
        if scrolled is not None and scrolled.get_child() is not None and self._downloads_group is not None:
            adjustment = scrolled.get_vadjustment()
            found, bounds = self._downloads_group.compute_bounds(scrolled.get_child())
            if found and adjustment.get_upper() > adjustment.get_page_size():
                adjustment.set_value(max(0.0, bounds.get_y() - _SCROLL_MARGIN))
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE if self._scroll_attempts < _SCROLL_MAX_ATTEMPTS else GLib.SOURCE_REMOVE

    def refresh_model_paths(self) -> None:
        """Show the model paths the saved settings now hold, after a download or "Use"."""
        config = AppConfig.load()
        # The saved file already holds these values, so they must not be written straight back: the change of the
        # first row would otherwise save the second row's old text over the new path.
        self._showing_saved_paths = True
        try:
            self._whisper_row.set_text(config.whisper_model_path)
            self._llm_row.set_text(config.llm_model_path)
        finally:
            self._showing_saved_paths = False
        self._update_validation_styles()

    def _on_browse_models_folder(self, _button: Gtk.Button) -> None:
        def chosen(path: str) -> None:
            self._folder_row.set_text(path)
            self._on_models_folder_applied()

        pick_folder(self, "Select the download folder", chosen)

    def _on_models_folder_applied(self, *_args) -> None:
        text = self._folder_row.get_text().strip()
        # Typing the default folder back in means "use the default", which keeps following XDG_DATA_HOME.
        update_saved_config(models_dir="" if text in ("", str(AppConfig().models_folder())) else text)
        if self._downloads_group is not None:
            self._downloads_group.refresh()

    def _on_close_request(self, *_args) -> bool:
        if self._downloads_group is not None:
            self._downloads_group.detach()
        return False

    # --- File pickers ---

    def _on_browse_whisper(self, _button: Gtk.Button) -> None:
        pick_folder(self, "Select the Faster-Whisper model directory", self._whisper_row.set_text)

    def _on_browse_llm(self, _button: Gtk.Button) -> None:
        gguf_filter = Gtk.FileFilter(name="GGUF models")
        gguf_filter.add_pattern("*.gguf")
        pick_file(self, "Select the LLM .gguf model file", self._llm_row.set_text, file_filter=gguf_filter)

    # --- History ---

    def _on_history_toggled(self, row, _param) -> None:
        update_saved_config(history_enabled=row.get_active())
        self._history_limit_row.set_sensitive(row.get_active())

    def _update_saved_row(self) -> None:
        count = self._history.count()
        self._saved_row.set_subtitle(
            f"{count} saved on this computer" if count else "Nothing saved yet"
        )
        self._clear_history_button.set_sensitive(count > 0)

    def _on_clear_history_clicked(self, _button: Gtk.Button) -> None:
        count = self._history.count()
        noun = "translation" if count == 1 else "translations"
        confirm(
            self, heading="Clear the whole history?",
            body=f"All {count} saved {noun} will be deleted. This can’t be undone.",
            confirm_label="Clear History", on_confirm=self._clear_history,
        )

    def _clear_history(self) -> None:
        self._history.clear()
        self._update_saved_row()
        self.add_toast(Adw.Toast(title="History cleared"))

    # --- Saved progress ---

    def _on_resume_transcription_toggled(self, row, _param) -> None:
        if self._resume_switch_guard:
            return
        if not row.get_active():
            update_saved_config(resume_transcription=False)
            return

        # Turning it on: state the trade-off first and switch it on only if confirmed. The switch goes back to off
        # meanwhile, so it never shows on before the user agrees.
        self._set_resume_switch(row, False)

        def enable() -> None:
            self._set_resume_switch(row, True)
            update_saved_config(resume_transcription=True)

        _confirm_resume_dialog(enable).present(self)

    def _set_resume_switch(self, row: Adw.SwitchRow, active: bool) -> None:
        """Move the switch without that being taken for a change made by the user."""
        self._resume_switch_guard = True
        row.set_active(active)
        self._resume_switch_guard = False

    def _update_resume_row(self) -> None:
        count = JobStore().count_files()
        if count:
            self._resume_row.set_subtitle(
                "Stopped or interrupted jobs can be continued. Clearing this makes them start from scratch."
            )
        else:
            self._resume_row.set_subtitle("Nothing saved — stopped jobs would appear here.")
        self._clear_progress_button.set_sensitive(count > 0)

    def _a_job_is_running(self, kind: str) -> bool:
        """Return whether the main window behind this one has a ``kind`` ("video" or "text") job running."""
        main = self.get_transient_for()
        return main is not None and getattr(main, f"{kind}_job_running", False)

    def _on_clear_progress_clicked(self, _button: Gtk.Button) -> None:
        if self._a_job_is_running("video"):
            self.add_toast(Adw.Toast(title="Can’t clear saved progress while a video job is running."))
            return
        JobStore().clear_all()
        self._update_resume_row()
        self.add_toast(Adw.Toast(title="Saved progress cleared."))

    # --- OCR and sampling ---

    def _on_open_ocr_clicked(self, _button: Gtk.Button) -> None:
        # Open the dialog over this window, not the main window behind it, where it would be hidden. The same rule
        # applies as elsewhere: no changes while a job is running.
        if self._a_job_is_running("text"):
            self.add_toast(Adw.Toast(title="OCR Settings are unavailable while a job is running."))
            return
        present_ocr_settings(self)

    def _on_reset_llm_clicked(self, _button: Gtk.Button) -> None:
        for field, _kwarg, _title, _subtitle, _mn, _mx, _step, _digits, default in LLM_PARAMS:
            self._llm_rows[field].set_value(default)

    # --- Persistence ---

    def _on_any_change(self, *_args) -> None:
        if self._showing_saved_paths:
            return
        # Read-modify-write on the current file: the OCR Settings dialog saves to the same file, and writing back a
        # copy loaded when this window opened would silently undo whatever was changed there since.
        config = AppConfig.load()
        config.whisper_model_path = self._whisper_row.get_text()
        config.llm_model_path = self._llm_row.get_text()
        config.processing_device = _DEVICE_ORDER[self._device_row.get_selected()].value
        config.cpu_threads = int(self._threads_row.get_value())
        for field, row in self._llm_rows.items():
            current = getattr(config, field)
            setattr(config, field, type(current)(row.get_value()))
        config.save()
        self._update_validation_styles()

    def _update_validation_styles(self) -> None:
        """Mark a model path that does not exist with the standard GNOME red-underline "error" style.

        This gives immediate feedback on a typed or browsed path, instead of the user finding out only after Start is
        pressed.
        """
        whisper_path = self._whisper_row.get_text()
        set_css_class(self._whisper_row, "error", bool(whisper_path) and not os.path.isdir(whisper_path))
        llm_path = self._llm_row.get_text()
        set_css_class(self._llm_row, "error", bool(llm_path) and not os.path.isfile(llm_path))
