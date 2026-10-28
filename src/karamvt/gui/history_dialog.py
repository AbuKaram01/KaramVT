# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The translation history: everything translated in the Text tab, newest first.

Each entry is a row that opens to show the original and the translation in full, with the buttons to copy
either one, to put the pair back into the Text tab ("Use again") and to delete it. A deletion can be
undone from the message that follows it. The search finds every word typed, in either text or in the
language names, ignoring case, accents and Arabic vowel marks. Long histories are shown a page at a time.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ..config import AppConfig
from ..core.history import HistoryEntry, HistoryStore
from .formatting import duration_label, full_time, preview, relative_time
from .widgets import confirm

# Number of entries added to the list each time "Show More" is pressed.
PAGE_SIZE = 40
# Names of the stack pages: the list, the "nothing saved yet" state and the "search found nothing" state.
_PAGE_LIST, _PAGE_EMPTY, _PAGE_NO_MATCH = "list", "empty", "no-match"


def _text_box(text: str) -> Gtk.Widget:
    """Build the view of one side of a translation: wrapped, selectable and aligned the way its language is read.

    A text view lays each paragraph out by its own direction, so Arabic sits on the right as it does in the Text tab.
    """
    view = Gtk.TextView(
        editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
        top_margin=8, bottom_margin=8, left_margin=10, right_margin=10,
    )
    view.get_buffer().set_text(text)
    return Gtk.Frame(child=view)


class HistoryDialog(Adw.Dialog):
    """Dialog that lists the saved translations, with search, copy, delete (with undo) and "Use Again".

    Args:
        history: The store the entries are read from and deleted from.
        on_use: Called with the entry when "Use Again" is pressed. It returns whether the entry was used, and the
            dialog closes if so.
    """

    def __init__(self, history: HistoryStore, on_use: Callable[[HistoryEntry], bool]) -> None:
        super().__init__(title="Translation History", content_width=600, content_height=720)
        self._history = history
        self._on_use = on_use
        self._query = ""
        self._total = 0                 # Entries matching the search.
        self._saved = 0                 # Entries in all.
        # Id of the oldest entry shown so far; the next page continues before it.
        self._last_id: int | None = None
        self._shown = 0                 # Entries currently in the list.

        self._toasts = Adw.ToastOverlay(child=self._build_pages())
        toolbar = Adw.ToolbarView(content=self._toasts)
        toolbar.add_top_bar(self._build_header())
        toolbar.add_top_bar(self._build_banner())
        toolbar.add_top_bar(self._build_search_bar())
        self.set_child(toolbar)
        self.set_focus(self._search)

        self.refresh()

    # --- Building ---

    def _build_header(self) -> Adw.HeaderBar:
        self._title = Adw.WindowTitle(title="Translation History", subtitle="")
        self._clear_button = Gtk.Button(
            icon_name="user-trash-symbolic", tooltip_text="Clear History…", css_classes=["flat"],
        )
        self._clear_button.connect("clicked", self._on_clear_clicked)
        header = Adw.HeaderBar(title_widget=self._title)
        header.pack_start(self._clear_button)
        return header

    def _build_search_bar(self) -> Gtk.Box:
        self._search = Gtk.SearchEntry(placeholder_text="Search translations…", hexpand=True)
        self._search.connect("search-changed", self._on_search_changed)
        self._search.connect("stop-search", lambda entry: entry.set_text(""))
        search_bar = Gtk.Box(margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        search_bar.append(self._search)
        return search_bar

    def _build_banner(self) -> Adw.Banner:
        """Build the banner shown on top of the list while new translations are not being kept."""
        self._banner = Adw.Banner(
            title="History is turned off: new translations are not being saved.", button_label="Preferences",
        )
        self._banner.set_action_name("app.preferences")
        return self._banner

    def _build_pages(self) -> Gtk.Stack:
        self._empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic", title="No Translations Yet",
            description="Translations you make in the Text tab are kept here.",
        )
        self._no_match = Adw.StatusPage(icon_name="system-search-symbolic", title="No Results")
        self._pages = Gtk.Stack(vexpand=True)
        self._pages.add_named(self._build_list_page(), _PAGE_LIST)
        self._pages.add_named(self._empty, _PAGE_EMPTY)
        self._pages.add_named(self._no_match, _PAGE_NO_MATCH)
        return self._pages

    def _build_list_page(self) -> Gtk.ScrolledWindow:
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self._more = Gtk.Button(label="Show More", halign=Gtk.Align.CENTER, css_classes=["pill"])
        self._more.connect("clicked", lambda _button: self._load_page())
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=6, margin_bottom=18,
                         margin_start=12, margin_end=12)
        column.append(self._list)
        column.append(self._more)
        return Gtk.ScrolledWindow(child=column, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)

    # --- Showing the entries ---

    def refresh(self) -> None:
        """Read the history again, starting from the newest entry.

        Also used for a change made elsewhere, such as a translation finishing while this dialog is open.
        """
        self._banner.set_revealed(not AppConfig.load().history_enabled)
        self._saved = self._history.count()
        self._total = self._history.count(self._query) if self._query else self._saved
        self._last_id, self._shown = None, 0
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)
        self._load_page()
        self._update_chrome()

    def _load_page(self) -> None:
        entries = self._history.find(self._query, limit=PAGE_SIZE, before_id=self._last_id)
        for entry in entries:
            self._list.append(self._row_for(entry))
        if entries:
            self._last_id = entries[-1].id
            self._shown += len(entries)
        self._more.set_visible(self._shown < self._total)

    def _update_chrome(self) -> None:
        if self._saved == 0:
            self._pages.set_visible_child_name(_PAGE_EMPTY)
        elif self._total == 0:
            self._no_match.set_description(f"Nothing matches “{self._query.strip()}”.")
            self._pages.set_visible_child_name(_PAGE_NO_MATCH)
        else:
            self._pages.set_visible_child_name(_PAGE_LIST)
        noun = "translation" if self._saved == 1 else "translations"
        if self._query and self._saved:
            self._title.set_subtitle(f"{self._total} of {self._saved} {noun}")
        else:
            self._title.set_subtitle(f"{self._saved} {noun}" if self._saved else "")
        self._clear_button.set_sensitive(self._saved > 0)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text()
        self.refresh()

    # --- One entry ---

    def _row_for(self, entry: HistoryEntry) -> Gtk.Widget:
        row = Adw.ExpanderRow(
            title=preview(entry.source_text, 70), subtitle=preview(entry.translated_text, 90),
            title_lines=1, subtitle_lines=1, use_markup=False,
        )
        when = Gtk.Label(
            label=f"{entry.source_language} → {entry.target_language}\n{relative_time(entry.created_at, time.time())}",
            justify=Gtk.Justification.RIGHT, xalign=1, valign=Gtk.Align.CENTER, css_classes=["caption", "dim-label"],
        )
        row.add_suffix(when)
        # The full texts are built when the row is first opened: a long history stays quick to show.
        built = []

        def on_expanded(expander: Adw.ExpanderRow, _pspec) -> None:
            if expander.get_expanded() and not built:
                built.append(True)
                expander.add_row(self._details_for(entry, expander))

        row.connect("notify::expanded", on_expanded)
        return row

    def _details_for(self, entry: HistoryEntry, row: Adw.ExpanderRow) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=10, margin_bottom=10,
                      margin_start=12, margin_end=12)
        sides = ((entry.source_language, entry.source_text), (entry.target_language, entry.translated_text))
        for language, text in sides:
            box.append(Gtk.Label(label=language, xalign=0, css_classes=["caption-heading", "dim-label"]))
            box.append(_text_box(text))

        facts = [full_time(entry.created_at)]
        if entry.model:
            facts.append(entry.model)
        if entry.seconds > 0:
            facts.append(duration_label(entry.seconds))
        box.append(Gtk.Label(label=" · ".join(facts), xalign=0, wrap=True, selectable=True,
                             css_classes=["caption", "dim-label"], margin_top=4))

        actions = Gtk.Box(spacing=6, margin_top=6)
        copy_translation = Gtk.Button(label="Copy Translation")
        copy_translation.connect("clicked", lambda _b: self._copy(entry.translated_text))
        copy_original = Gtk.Button(label="Copy Original")
        copy_original.connect("clicked", lambda _b: self._copy(entry.source_text))
        use_again = Gtk.Button(label="Use Again", css_classes=["suggested-action"])
        use_again.connect("clicked", lambda _b: self._use(entry))
        delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete", halign=Gtk.Align.END,
                            hexpand=True, css_classes=["flat", "error"])
        delete.connect("clicked", lambda _b: self._delete(entry, row))
        for button in (copy_translation, copy_original, use_again, delete):
            actions.append(button)
        box.append(actions)
        return Gtk.ListBoxRow(child=box, activatable=False, selectable=False)

    # --- Actions ---

    def _copy(self, text: str) -> None:
        self.get_clipboard().set(text)
        self._toasts.add_toast(Adw.Toast(title="Copied to clipboard."))

    def _use(self, entry: HistoryEntry) -> None:
        if self._on_use(entry):
            self.close()

    def _delete(self, entry: HistoryEntry, row: Adw.ExpanderRow) -> None:
        if not self._history.delete(entry.id):
            return
        toast = Adw.Toast(title="Translation deleted", button_label="Undo", timeout=6)
        toast.connect("button-clicked", lambda _toast: self._undelete(entry))
        self._toasts.add_toast(toast)
        self._list.remove(row)
        self._saved -= 1
        self._total -= 1
        self._shown -= 1
        if self._shown == 0 and self._total > 0:
            self.refresh()                      # The page was emptied but matches remain further down.
        else:
            self._more.set_visible(self._shown < self._total)
            self._update_chrome()

    def _undelete(self, entry: HistoryEntry) -> None:
        if self._history.restore(entry):
            self.refresh()

    def _on_clear_clicked(self, _button: Gtk.Button) -> None:
        noun = "translation" if self._saved == 1 else "translations"
        confirm(
            self, heading="Clear the whole history?",
            body=f"All {self._saved} saved {noun} will be deleted. This can’t be undone.",
            confirm_label="Clear History", on_confirm=self._clear,
        )

    def _clear(self) -> None:
        self._history.clear()
        self._query = ""
        self._search.set_text("")
        self.refresh()
        self._toasts.add_toast(Adw.Toast(title="History cleared"))
