# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Small widget and dialog helpers shared by several windows."""

from __future__ import annotations

import os
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk


def confirm(
    parent: Gtk.Widget,
    *,
    heading: str,
    body: str,
    confirm_label: str,
    cancel_label: str = "Cancel",
    on_confirm: Callable[[], None],
    destructive: bool = True,
) -> None:
    """Ask the user to confirm an action; ``on_confirm`` runs only if they accept."""
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", cancel_label)
    dialog.add_response("confirm", confirm_label)
    appearance = Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED
    dialog.set_response_appearance("confirm", appearance)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _dialog, response: on_confirm() if response == "confirm" else None)
    dialog.present(parent)


def start_in(dialog: Gtk.FileDialog, folder: str) -> None:
    """Make a file dialog open in ``folder``, the one the user last worked in, if it still exists."""
    if folder and os.path.isdir(folder):
        dialog.set_initial_folder(Gio.File.new_for_path(folder))


def chosen_path(finish: Callable[[Gio.AsyncResult], Gio.File | None], result: Gio.AsyncResult) -> str | None:
    """Return the path the user picked in a file dialog, or None if it was dismissed or has no local path.

    Args:
        finish: The dialog's ``*_finish`` method that matches the call that opened it.
        result: The async result that the dialog passed to its callback.
    """
    try:
        gfile = finish(result)
    except GLib.Error:
        return None
    return gfile.get_path() if gfile is not None else None


def pick_folder(parent: Gtk.Widget, title: str, on_chosen: Callable[[str], None]) -> None:
    """Ask for a folder; ``on_chosen`` gets its path unless the dialog is dismissed."""
    dialog = Gtk.FileDialog(title=title)

    def finished(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        if path := chosen_path(source.select_folder_finish, result):
            on_chosen(path)

    dialog.select_folder(parent, None, finished)


def filter_list(*file_filters: Gtk.FileFilter) -> Gio.ListStore:
    """Return the filters a file dialog offers (the first one is the one it opens with, unless told otherwise)."""
    filters = Gio.ListStore.new(Gtk.FileFilter)
    for file_filter in file_filters:
        filters.append(file_filter)
    return filters


def set_css_class(widget: Gtk.Widget, name: str, on: bool) -> None:
    """Give ``widget`` the style class ``name`` when ``on``, take it away otherwise."""
    if on:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)


def pick_file(parent: Gtk.Widget, title: str, on_chosen: Callable[[str], None], *, file_filter: Gtk.FileFilter) -> None:
    """Ask for a file of the kind ``file_filter`` lets through; ``on_chosen`` gets its path unless dismissed."""
    dialog = Gtk.FileDialog(title=title)
    dialog.set_filters(filter_list(file_filter))

    def finished(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        if path := chosen_path(source.open_finish, result):
            on_chosen(path)

    dialog.open(parent, None, finished)


def browse_button(on_clicked: Callable[[Gtk.Button], None], *, flat: bool = False) -> Gtk.Button:
    """Return the folder icon button that sits at the end of a row and opens a picker."""
    button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
    if flat:
        button.add_css_class("flat")
    button.set_tooltip_text("Browse\u2026")
    button.connect("clicked", on_clicked)
    return button


def button_row(title: str, button: Gtk.Button, *, subtitle: str = "", activatable: bool = False) -> Adw.ActionRow:
    """Return a row with ``button`` at its end (pressing the row presses the button when ``activatable``)."""
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.add_suffix(button)
    if activatable:
        row.set_activatable_widget(button)
    return row


def flat_button(label: str, on_clicked: Callable[[Gtk.Button], None]) -> Gtk.Button:
    """Return a button without a frame, vertically centred, as it sits at the end of a row."""
    button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    button.add_css_class("flat")
    button.connect("clicked", on_clicked)
    return button


def combo_row(title: str, labels: list[str], selected: int, on_selected: Callable[..., None]) -> Adw.ComboRow:
    """Return a row that picks one of ``labels``.

    ``on_selected`` is connected after the initial choice is shown, so showing it does not call it.
    """
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new(labels))
    row.set_selected(selected)
    row.connect("notify::selected", on_selected)
    return row


def language_popover() -> tuple[Gtk.Popover, Gtk.SearchEntry, Gtk.ListBox]:
    """Build the drop-down of a language picker: a search box above a scrolling list.

    The caller fills the list with rows that have a ``language_name`` attribute. The search narrows the list to the
    rows whose name contains what was typed, and the search box has the focus when the popover opens.

    Returns:
        The popover, the search box and the list.
    """
    search = Gtk.SearchEntry(placeholder_text="Search languages…")
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")

    def matches(row: Gtk.ListBoxRow) -> bool:
        query = search.get_text().strip().lower()
        return not query or query in row.language_name.lower()

    listbox.set_filter_func(matches)
    search.connect("search-changed", lambda _entry: listbox.invalidate_filter())

    scrolled = Gtk.ScrolledWindow(min_content_height=320, max_content_height=320, vexpand=True)
    scrolled.set_child(listbox)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                  margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
    box.set_size_request(240, -1)
    box.append(search)
    box.append(scrolled)

    popover = Gtk.Popover()
    popover.set_child(box)
    popover.connect("show", lambda _popover: search.grab_focus())
    return popover, search, listbox
