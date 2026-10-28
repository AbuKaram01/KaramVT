# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A button with a searchable popover list for choosing several languages."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk

from .widgets import language_popover


class MultiLanguagePickerButton(Gtk.MenuButton):
    """A button that opens a searchable popover list of languages with check boxes.

    It selects several target languages at once, for translating a file or folder into every checked language.
    The ``selection-changed`` signal is emitted each time a check box changes, whether by the user or by
    ``set_selected``.

    Args:
        language_names: The names offered, in display order.
        **kwargs: Properties passed on to ``Gtk.MenuButton``.
    """

    __gsignals__ = {
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, language_names: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._selected: set[str] = set()

        popover, self._search_entry, self._listbox = language_popover()

        self._checks: dict[str, Gtk.CheckButton] = {}
        self._rows: list[Gtk.ListBoxRow] = []
        for name in language_names:
            row = Gtk.ListBoxRow(activatable=False)
            check = Gtk.CheckButton(label=name, margin_top=6, margin_bottom=6,
                                    margin_start=10, margin_end=10)
            check.connect("toggled", self._on_check_toggled, name)
            row.set_child(check)
            row.language_name = name  # type: ignore[attr-defined]
            self._listbox.append(row)
            self._rows.append(row)
            self._checks[name] = check
        self.set_popover(popover)
        self._update_label()

    def _on_check_toggled(self, check: Gtk.CheckButton, name: str) -> None:
        if check.get_active():
            self._selected.add(name)
        else:
            self._selected.discard(name)
        self._update_label()
        self.emit("selection-changed")

    def _update_label(self) -> None:
        if not self._selected:
            self.set_label("Choose languages…")
        elif len(self._selected) == 1:
            self.set_label(next(iter(self._selected)))
        else:
            self.set_label(f"{len(self._selected)} languages")

    def get_selected(self) -> list[str]:
        """Return the selected language names, in the same order they are listed in."""
        return [row.language_name for row in self._rows if row.language_name in self._selected]

    def set_selected(self, names: list[str]) -> None:
        """Check exactly the given languages and uncheck all others."""
        wanted = set(names)
        for name, check in self._checks.items():
            check.set_active(name in wanted)
        # No explicit update is needed here: each check box that changes state runs _on_check_toggled, which updates
        # the selection and the label.

    def clear(self) -> None:
        """Uncheck every language."""
        self.set_selected([])
