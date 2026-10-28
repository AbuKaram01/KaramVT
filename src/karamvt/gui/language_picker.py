# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A button with a searchable popover list for choosing one language."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk

from .widgets import language_popover


class LanguagePickerButton(Gtk.MenuButton):
    """A button that opens a searchable popover list of languages and shows the chosen one as its label.

    Used instead of the plain dropdown of ``Adw.ComboRow``: typing a few letters to filter the list beats scrolling
    through it, and the list can grow if more languages are offered (see ``core/languages.py``).

    The ``language-selected`` signal is emitted with the language name when the user picks a row. It is not emitted
    by ``set_selected``.

    Args:
        language_names: The names offered, in display order.
        include_auto: Whether to offer "Auto-detect" as the first entry and select it by default.
        **kwargs: Properties passed on to ``Gtk.MenuButton``.
    """

    __gsignals__ = {
        "language-selected": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, language_names: list[str], include_auto: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._selected_name: str = ""

        popover, self._search_entry, self._listbox = language_popover()
        self._listbox.connect("row-activated", self._on_row_activated)

        names = (["Auto-detect"] if include_auto else []) + language_names
        self._rows: list[Gtk.ListBoxRow] = []
        for name in names:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(label=name, xalign=0, margin_top=8, margin_bottom=8,
                              margin_start=10, margin_end=10)
            row.set_child(label)
            row.language_name = name  # type: ignore[attr-defined]
            self._listbox.append(row)
            self._rows.append(row)
        self.set_popover(popover)

        default_name = "Auto-detect" if include_auto else (language_names[0] if language_names else "")
        self.set_selected(default_name)

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self.set_selected(row.language_name)
        self.get_popover().popdown()
        self.emit("language-selected", row.language_name)

    def set_selected(self, name: str) -> None:
        """Select a language by name without emitting a signal."""
        self._selected_name = name
        self.set_label(name)

    def get_selected(self) -> str:
        """Return the name of the selected language."""
        return self._selected_name

    def language_names(self) -> list[str]:
        """Return every name the picker offers (including "Auto-detect" when it has it), whatever the search shows."""
        return [row.language_name for row in self._rows]

    def visible_language_names(self) -> list[str]:
        """Return the names of the rows that currently pass the search filter.

        Exposed mainly so that tests can verify the filter without iterating the underlying GTK container.
        """
        query = self._search_entry.get_text().strip().lower()
        if not query:
            return [row.language_name for row in self._rows]
        return [row.language_name for row in self._rows if query in row.language_name.lower()]
