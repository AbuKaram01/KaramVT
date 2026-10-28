# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dialog for downloading and deleting OCR language packs.

Opened from the "Manage downloaded languages" row of OCR Settings, scoped to the OCR quality tier (fast, standard or
best) that is selected there. It also holds the one download that is not a language: ``osd.traineddata``, the
script and orientation data that Auto-detect and the "+ OSD" modes need. Downloads happen inside the application
through ``core.tessdata_manager``: they need no administrator rights and are independent of any
``tesseract-ocr-<lang>`` system packages that may be installed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..config import AppConfig
from ..core import tessdata_manager
from ..core.languages import ocr_languages
from ..core.ocr_options import psm_needs_osd


def _with_margins(widget: Gtk.Widget, sides: int, top: int = 0, bottom: int = 0) -> Gtk.Widget:
    widget.set_margin_start(sides)
    widget.set_margin_end(sides)
    widget.set_margin_top(top)
    widget.set_margin_bottom(bottom)
    return widget


def _boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    return listbox


def _flat_icon_button(icon_name: str, tooltip: str, on_clicked: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button()
    button.set_icon_name(icon_name)
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text(tooltip)
    button.connect("clicked", lambda *_: on_clicked())
    return button


class _PackRow:
    """One row of the lists: a language (or the orientation data) with the control that fits its state.

    The control is a download button, a progress indicator, or a delete button once the pack is present.
    """

    def __init__(self, code: str, name: str, on_download: Callable[[str], None], on_delete: Callable[[str], None]):
        self.code = code
        self.title = name
        self._on_download = on_download
        self._on_delete = on_delete
        self._progress: Gtk.Label | None = None

        self._action_row = Adw.ActionRow()
        self._action_row.set_title(name)
        self._action_row.set_subtitle(code)
        # A single Gtk.Box is added once as the suffix and its children are swapped when the state changes. This is
        # simpler and more robust than adding and removing widgets on the ActionRow itself.
        self._suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._suffix.set_valign(Gtk.Align.CENTER)
        self._action_row.add_suffix(self._suffix)

        self.widget = Gtk.ListBoxRow()
        self.widget.set_child(self._action_row)
        self.widget.set_activatable(False)

    def _replace_controls(self, *controls: Gtk.Widget) -> None:
        self._progress = None
        child = self._suffix.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._suffix.remove(child)
            child = following
        for control in controls:
            self._suffix.append(control)

    def show_downloading(self) -> None:
        label = Gtk.Label(label="…")
        label.add_css_class("dim-label")
        self._replace_controls(Gtk.Spinner(spinning=True), label)
        self._progress = label

    def show_percent(self, percent: int | None) -> None:
        """Show how far the download is, if it is known (only while the row is downloading)."""
        if self._progress is not None:
            self._progress.set_text(f"{percent}%" if percent is not None else "…")

    def show_downloaded(self) -> None:
        check = Gtk.Image.new_from_icon_name("object-select-symbolic")
        check.add_css_class("success")
        delete = _flat_icon_button(
            "user-trash-symbolic", "Delete downloaded language", lambda: self._on_delete(self.code)
        )
        self._replace_controls(check, delete)

    def show_available(self, can_download: bool) -> None:
        download = _flat_icon_button("folder-download-symbolic", "Download", lambda: self._on_download(self.code))
        download.set_sensitive(can_download)
        self._replace_controls(download)


class LanguageManagerDialog(Adw.Dialog):
    """Dialog to download and delete individual OCR language packs for one quality tier.

    Args:
        quality: The OCR quality tier whose packs are managed ("fast", "standard" or "best").
        on_changed: Called without arguments after a language pack was downloaded or deleted. The orientation data
            is not a language and does not trigger it.
    """

    def __init__(self, quality: str, on_changed=None):
        super().__init__(title=f"Manage Languages — {quality.capitalize()}")
        self.set_content_width(560)
        self.set_content_height(680)
        self._quality = quality
        self._on_changed = on_changed
        self._rows: dict[str, Gtk.ListBoxRow] = {}      # The language rows (not the orientation data's).
        self._pack_rows: dict[str, _PackRow] = {}       # All of them.
        self._active_download: str | None = None

        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)
        toolbar_view.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar_view.set_content(content)
        self._add_orientation_data(content)
        self._add_search_and_status(content)
        self._add_language_list(content)

    # --- Building ---

    def _add_orientation_data(self, content: Gtk.Box) -> None:
        """Add the orientation data in its own small list above the searchable language list.

        ``osd.traineddata`` is not a language, so it is kept out of the language list but has the same
        download and delete controls.
        """
        hint = Gtk.Label(
            label="Script and orientation detection — needed by Auto-detect and the “+ OSD” modes",
            xalign=0, wrap=True,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        content.append(_with_margins(hint, 14, top=12))

        listbox = _boxed_list()
        osd_row = self._add_pack_row(tessdata_manager.OSD_CODE, "Orientation and script detection")
        listbox.append(osd_row)
        content.append(_with_margins(listbox, 12, top=6))

    def _add_search_and_status(self, content: Gtk.Box) -> None:
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search languages…")
        self._search_entry.connect("search-changed", lambda *_: self._listbox.invalidate_filter())
        content.append(_with_margins(self._search_entry, 12, top=12))

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("error")
        self._status_label.set_wrap(True)
        self._status_label.set_visible(False)
        content.append(_with_margins(self._status_label, 12))

    def _add_language_list(self, content: Gtk.Box) -> None:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        _with_margins(scroll, 12, top=8, bottom=12)

        self._listbox = _boxed_list()
        self._listbox.set_filter_func(self._filter_row)
        scroll.set_child(self._listbox)
        content.append(scroll)

        for language in ocr_languages():
            row = self._add_pack_row(language.tesseract_code, language.name)
            self._rows[language.tesseract_code] = row
            self._listbox.append(row)

    def _add_pack_row(self, code: str, name: str) -> Gtk.ListBoxRow:
        pack_row = _PackRow(code, name, self._on_download, self._on_delete)
        self._pack_rows[code] = pack_row
        self._refresh_row(code)
        return pack_row.widget

    # --- Showing the state ---

    def _filter_row(self, row: Gtk.ListBoxRow) -> bool:
        query = self._search_entry.get_text().strip().lower()
        if not query:
            return True
        action_row = row.get_child()
        haystack = f"{action_row.get_title()} {action_row.get_subtitle()}".lower()
        return query in haystack

    def _refresh_row(self, code: str) -> None:
        row = self._pack_rows[code]
        if self._active_download == code:
            row.show_downloading()
        elif tessdata_manager.is_downloaded(self._quality, code):
            row.show_downloaded()
        else:
            row.show_available(can_download=self._active_download is None)

    def _refresh_all_rows(self) -> None:
        """Refresh every row. Only one download runs at a time, so while one does, no other row offers to start one."""
        for code in self._pack_rows:
            self._refresh_row(code)

    def _show_status(self, message: str) -> None:
        self._status_label.set_text(message)
        self._status_label.set_visible(True)

    def _tell_caller(self, code: str) -> None:
        """Notify the caller that a language pack was added or removed (the orientation data is not a language)."""
        if self._on_changed and code not in tessdata_manager.NON_LANGUAGE_CODES:
            self._on_changed()

    # --- Download and delete ---

    def _on_download(self, code: str) -> None:
        if self._active_download is not None:
            return
        self._status_label.set_visible(False)
        self._active_download = code
        self._refresh_all_rows()
        threading.Thread(target=self._download, args=(code,), daemon=True).start()

    def _download(self, code: str) -> None:
        """Download one pack. Runs on a worker thread and reports back on the main thread."""
        # The idle callbacks return False so that each one runs once.
        def on_progress(percent):
            GLib.idle_add(self._show_percent, code, percent)
        try:
            tessdata_manager.download(self._quality, code, progress_cb=on_progress)
        except Exception as exc:
            GLib.idle_add(self._on_download_done, code, str(exc))
        else:
            GLib.idle_add(self._on_download_done, code, None)

    def _show_percent(self, code: str, percent) -> bool:
        self._pack_rows[code].show_percent(percent)
        return False

    def _on_download_done(self, code: str, error: str | None) -> bool:
        self._active_download = None
        self._refresh_all_rows()
        if error:
            self._show_status(f"Couldn't download {self._pack_rows[code].title}: {error}")
        else:
            self._tell_caller(code)
        return False

    def _on_delete(self, code: str) -> None:
        tessdata_manager.delete(self._quality, code)
        self._refresh_row(code)
        if code == tessdata_manager.OSD_CODE and psm_needs_osd(AppConfig.load().ocr_psm):
            # The selected page segmentation mode cannot run without the orientation data. Say so now instead of
            # failing at the next import.
            self._show_status(
                "Orientation data removed — the selected page segmentation mode needs it. "
                "Re-download it or pick another mode in OCR Settings."
            )
        self._tell_caller(code)
