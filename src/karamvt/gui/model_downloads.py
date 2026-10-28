# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The "Download Models" section of Preferences: an expandable menu of models to fetch.

The menu is laid out as follows::

    Available models  ▾
      Download folder
      Speech recognition  ▾
        Large v3 Turbo · Large v3
      Translation model  ▾
        Gemma E2B  ▾     Q4_K_XL · Q6_K_XL · Q8_K_XL
        Gemma E4B  ▾     Q4_K_XL · Q6_K_XL · Q8_K_XL

Each model row shows what it can do right now: download, show progress with a stop button, be selected, or say
that it is the model in use. The downloads themselves are owned by ``DownloadManager``; this module only reflects
its state, so closing and reopening the window while a model downloads shows the same progress.

``ModelRows`` holds that behaviour, so the first-run welcome guide (``welcome.py``) lists the same rows with the
same states as Preferences does.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..config import AppConfig
from ..core.catalog import LLM_FAMILIES, WHISPERS, ModelKind, ModelOption, llms_of
from ..core.downloads import DownloadProgress
from ..services import DownloadBusyError, DownloadEvent, DownloadEventKind, DownloadManager, use_model
from .formatting import size_label
from .widgets import confirm

# Width in pixels of the download progress bar.
_PROGRESS_WIDTH = 110

# Short notes shown in the subtitle of a Whisper model (by catalog key) and of a quantization.
_WHISPER_NOTES = {
    "whisper.large-v3-turbo": "Nearly as accurate as Large v3, much faster",
    "whisper.large-v3": "Most accurate, slowest",
}
_QUANT_NOTES = {"Q4_K_XL": "Smallest", "Q6_K_XL": "Balanced", "Q8_K_XL": "Highest quality"}


class RowState(str, Enum):
    """The state of a model row, which decides the control the row shows."""

    DOWNLOAD = "download"
    DOWNLOADING = "downloading"
    READY = "ready"        # Downloaded, not selected.
    IN_USE = "in-use"      # Downloaded and selected.


def model_name(option: ModelOption) -> str:
    """Return a human-readable name such as "Whisper Large v3 Turbo" or "Gemma E2B Q4_K_XL"."""
    if option.kind is ModelKind.LLM:
        return f"Gemma {option.family} {option.quant}"
    return f"Whisper {option.label}"


def _in_use_box() -> Gtk.Box:
    """The tick and "In use", shown for the model that is selected."""
    box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
    box.append(Gtk.Image(icon_name="object-select-symbolic"))
    box.append(Gtk.Label(label="In use", css_classes=["dim-label"]))
    return box


class ModelRow(Adw.ActionRow):
    """One downloadable model with a button that changes with its state.

    Args:
        title: The model name shown in the row.
        subtitle: The size and a short note; replaced by the downloaded amount while a download runs.
        on_download: Called when "Download" is pressed.
        on_stop: Called when the stop button of a running download is pressed.
        on_use: Called when "Use" is pressed on a downloaded model.
        recommended: Whether to label the row "Recommended".
    """

    def __init__(
        self,
        title: str,
        subtitle: str,
        *,
        on_download: Callable[[], None],
        on_stop: Callable[[], None],
        on_use: Callable[[], None],
        recommended: bool = False,
    ) -> None:
        super().__init__(title=title, subtitle=subtitle)
        self._base_subtitle = subtitle
        if recommended:
            self.add_suffix(
                Gtk.Label(label="Recommended", valign=Gtk.Align.CENTER, css_classes=["accent", "caption-heading"])
            )

        self._download = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        self._download.connect("clicked", lambda _b: on_download())
        use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER, css_classes=["flat"])
        use.connect("clicked", lambda _b: on_use())

        self._stack = Gtk.Stack(valign=Gtk.Align.CENTER, hhomogeneous=False)
        for state, widget in (
            (RowState.DOWNLOAD, self._download),
            (RowState.DOWNLOADING, self._progress_box(on_stop)),
            (RowState.READY, use),
            (RowState.IN_USE, _in_use_box()),
        ):
            self._stack.add_named(widget, state.value)
        self.add_suffix(self._stack)
        self.state = RowState.DOWNLOAD

    def _progress_box(self, on_stop: Callable[[], None]) -> Gtk.Box:
        """Build the bar and the button that stops the download, shown while it runs."""
        self._bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=_PROGRESS_WIDTH)
        stop = Gtk.Button(icon_name="process-stop-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        stop.set_tooltip_text("Stop download")
        stop.update_property([Gtk.AccessibleProperty.LABEL], ["Stop download"])
        stop.connect("clicked", lambda _b: on_stop())
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        box.append(self._bar)
        box.append(stop)
        return box

    def set_state(self, state: RowState, progress: DownloadProgress | None = None) -> None:
        """Show the control for ``state``; ``progress`` updates the bar and the subtitle while downloading."""
        self.state = state
        self._stack.set_visible_child_name(state.value)
        if state is RowState.DOWNLOADING and progress is not None:
            self._bar.set_fraction(progress.fraction)
            self.set_subtitle(f"{size_label(progress.done_bytes)} of {size_label(progress.total_bytes)}")
        else:
            self._bar.set_fraction(0.0)
            self.set_subtitle(self._base_subtitle)

    def set_download_enabled(self, enabled: bool) -> None:
        """Enable or disable the "Download" button (only one download runs at a time)."""
        self._download.set_sensitive(enabled)


class ModelRows:
    """The rows of downloadable models and what pressing their buttons does.

    A mixin for any widget that lists models: call ``_init_rows`` once, build the rows with
    ``_whisper_rows`` / ``_llm_expanders``, call ``refresh()`` and ``downloads.subscribe(self._on_event)``,
    and ``detach()`` when the widget goes away. ``parent`` must be a widget that can show a
    dialog and has ``add_toast``.
    """

    def _init_rows(self, parent, downloads: DownloadManager, on_model_selected: Callable[[], None]) -> None:
        self._parent = parent
        self._downloads = downloads
        self._on_model_selected = on_model_selected
        self._rows: dict[str, ModelRow] = {}
        self._options: dict[str, ModelOption] = {}
        self._nested: list[Adw.ExpanderRow] = []      # The expanders holding rows, opened by ``expand(True)``.

    # --- Construction ---

    def _row(self, option: ModelOption, title: str, subtitle: str) -> ModelRow:
        row = ModelRow(
            title, subtitle,
            on_download=lambda: self._ask_to_download(option),
            on_stop=self._downloads.cancel,
            on_use=lambda: self._use(option),
            recommended=option.recommended,
        )
        self._rows[option.key] = row
        self._options[option.key] = option
        return row

    def _whisper_rows(self) -> list[ModelRow]:
        return [
            self._row(option, option.label, f"{size_label(option.approx_bytes)} · {_WHISPER_NOTES[option.key]}")
            for option in WHISPERS
        ]

    def _llm_expanders(self) -> list[Adw.ExpanderRow]:
        """Build one expander per Gemma family, each holding its quantizations."""
        expanders = []
        for family in LLM_FAMILIES:
            options = llms_of(family)
            sizes = [option.approx_bytes for option in options]
            expander = Adw.ExpanderRow(
                title=f"Gemma {family}", subtitle=f"{size_label(min(sizes))} to {size_label(max(sizes))}",
            )
            for option in options:
                subtitle = f"{size_label(option.approx_bytes)} · {_QUANT_NOTES[option.quant]}"
                expander.add_row(self._row(option, option.quant, subtitle))
            self._nested.append(expander)
            expanders.append(expander)
        return expanders

    # --- State ---

    def refresh(self) -> None:
        """Show every row in the state that matches the disk, the settings and the running download."""
        config = AppConfig.load()
        models_dir = config.models_folder()
        for key, row in self._rows.items():
            option = self._options[key]
            if self._downloads.current is option:
                row.set_state(RowState.DOWNLOADING, self._downloads.progress)
            elif option.is_downloaded(models_dir):
                chosen = getattr(config, option.kind.config_field)
                in_use = bool(chosen) and Path(chosen) == option.local_path(models_dir)
                row.set_state(RowState.IN_USE if in_use else RowState.READY)
            else:
                row.set_state(RowState.DOWNLOAD)
                row.set_download_enabled(not self._downloads.active)

    def detach(self) -> None:
        """Stop listening to download events. Called when the widget's window closes."""
        self._downloads.unsubscribe(self._on_event)

    def _on_event(self, event: DownloadEvent) -> None:
        if event.kind is DownloadEventKind.PROGRESS:
            self._rows[event.option.key].set_state(RowState.DOWNLOADING, event.progress)
        else:
            self.refresh()
            if event.kind is DownloadEventKind.FINISHED:
                self._on_model_selected()      # The finished model was selected in the saved settings.

    # --- Actions ---

    def _ask_to_download(self, option: ModelOption) -> None:
        confirm(
            self._parent,
            heading=f"Download {model_name(option)}?",
            body=(
                f"About {size_label(option.approx_bytes)} will be downloaded from Hugging Face ({option.repo}). "
                "Make sure you have enough free disk space. If the connection drops, press Download again "
                "to continue where it stopped."
            ),
            confirm_label="Download", on_confirm=lambda: self._start(option), destructive=False,
        )

    def _start(self, option: ModelOption) -> None:
        try:
            self._downloads.start(option)
        except DownloadBusyError:
            self._parent.add_toast(Adw.Toast(title="Another download is already running."))

    def _use(self, option: ModelOption) -> None:
        use_model(option)
        self.refresh()
        self._on_model_selected()


class ModelDownloadGroup(ModelRows, Adw.PreferencesGroup):
    """The "Download Models" group in Preferences.

    Args:
        parent: The Preferences window, which hosts confirmation dialogs and toasts.
        downloads: The manager that runs the downloads.
        folder_row: The row for choosing the models folder, shown at the top of the menu.
        on_model_selected: Called after a model was selected, by the user or by a finished download.
    """

    def __init__(
        self,
        parent: Adw.PreferencesWindow,
        downloads: DownloadManager,
        folder_row: Adw.PreferencesRow,
        on_model_selected: Callable[[], None],
    ) -> None:
        super().__init__(
            title="Download Models",
            description=(
                "Fetched from Hugging Face. This is the only time KaramVT uses the internet "
                "for models, and only when you press Download."
            ),
        )
        self._init_rows(parent, downloads, on_model_selected)

        self.expander = Adw.ExpanderRow(
            title="Available models", subtitle="Speech recognition and translation models",
            icon_name="folder-download-symbolic",
        )
        self.add(self.expander)
        self.expander.add_row(folder_row)
        self.expander.add_row(self._whisper_models())
        self.expander.add_row(self._language_models())

        self.refresh()
        downloads.subscribe(self._on_event)

    def _whisper_models(self) -> Adw.ExpanderRow:
        models = Adw.ExpanderRow(
            title="Speech recognition", subtitle="Whisper · multilingual · faster-whisper format",
        )
        self._nested.append(models)
        for row in self._whisper_rows():
            models.add_row(row)
        return models

    def _language_models(self) -> Adw.ExpanderRow:
        models = Adw.ExpanderRow(title="Translation model", subtitle="Gemma 4 · GGUF · Unsloth")
        self._nested.append(models)
        for expander in self._llm_expanders():
            models.add_row(expander)
        return models

    def expand(self, everything: bool = False) -> None:
        """Expand the menu and, with ``everything``, the model lists inside it."""
        self.expander.set_expanded(True)
        if everything:
            for expander in self._nested:
                expander.set_expanded(True)
