# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dialog that downloads and deletes OCR language data, one quality tier at a time (see gui_support.py).

Nothing is downloaded: the data on disk and the downloads are stand-ins.
"""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import threading

import pytest
from gui_support import Adw, AppConfig, Gtk, downloader, env, pump, walk

from karamvt.core import tessdata_manager
from karamvt.core.languages import _ENTRIES
from karamvt.core.ocr import PSM_OPTIONS
from karamvt.gui.language_manager_dialog import LanguageManagerDialog

OSD = tessdata_manager.OSD_CODE
PSM_WITH_OSD = next(psm for psm, _label, _description in PSM_OPTIONS if psm == 1)
PSM_WITHOUT_OSD = next(psm for psm, _label, _description in PSM_OPTIONS if psm == 6)
OCR_LANGUAGES = sorted([language for language in _ENTRIES if language.tesseract_code], key=lambda each: each.name)


class Disk:
    """What is downloaded (instead of looking at the disk), the downloads asked for and the deletions made."""

    def __init__(self):
        self.have: set[tuple[str, str]] = set()
        self.asked: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.percents: list = []
        self.failure: Exception | None = None
        self.hold = threading.Event()
        self.hold.set()


@pytest.fixture
def disk(monkeypatch):
    """Replace the disk and network access of tessdata_manager with a Disk that records what was asked."""
    fake = Disk()
    monkeypatch.setattr(tessdata_manager, "is_downloaded", lambda quality, code: (quality, code) in fake.have)

    def download(quality, code, progress_cb=None, cancel_event=None):
        fake.asked.append((quality, code))
        for percent in fake.percents:
            progress_cb(percent)
        fake.hold.wait(3)
        if fake.failure is not None:
            raise fake.failure
        fake.have.add((quality, code))

    def delete(quality, code):
        fake.deleted.append((quality, code))
        fake.have.discard((quality, code))

    monkeypatch.setattr(tessdata_manager, "download", download)
    monkeypatch.setattr(tessdata_manager, "delete", delete)
    yield fake
    fake.hold.set()


@pytest.fixture
def open_manager(env, disk):
    """A function that opens the dialog for a quality tier; every dialog it opened is closed after the test."""
    opened: list[LanguageManagerDialog] = []

    def open_one(quality="fast", **kwargs) -> LanguageManagerDialog:
        dialog = LanguageManagerDialog(quality, **kwargs)
        dialog.present(env[1])
        pump(0.3)
        opened.append(dialog)
        return dialog

    yield open_one
    for dialog in opened:
        dialog.force_close()
    pump(0.1)


@pytest.fixture
def manager(open_manager):
    """The dialog for the default (fast) quality tier."""
    return open_manager()


def action_row(dialog, code):
    """The row of a language, or of the orientation data."""
    if code == OSD:
        return next(w for w in walk(dialog) if isinstance(w, Adw.ActionRow) and w.get_subtitle() == OSD)
    return dialog._rows[code].get_child()


def controls(row):
    """What is at the end of a row, in the order it is shown."""
    found = []
    for widget in walk(row):
        if isinstance(widget, Gtk.Button):
            found.append(("button", widget.get_icon_name(), widget.get_tooltip_text(), widget.get_sensitive()))
        elif isinstance(widget, Gtk.Spinner):
            found.append(("spinner", widget.get_spinning()))
        elif isinstance(widget, Gtk.Image) and widget.get_icon_name() == "object-select-symbolic":
            found.append(("check",))
        elif isinstance(widget, Gtk.Label) and (widget.get_text() == "…" or widget.get_text().endswith("%")):
            found.append(("progress", widget.get_text()))
    return found


def button_of(row) -> Gtk.Button:
    return next(w for w in walk(row) if isinstance(w, Gtk.Button))


# What the control of a language row looks like when the language can be downloaded, and when it can be deleted.
DOWNLOAD = ("button", "folder-download-symbolic", "Download", True)
DELETE = ("button", "user-trash-symbolic", "Delete downloaded language", True)


def wait_for_the_download(dialog):
    """Wait until the dialog has no download in progress."""
    assert pump(3.0, until=lambda: dialog._active_download is None)
    pump(0.05)


def status_of(dialog):
    """The text of the dialog's status line, or None while the line is hidden."""
    return dialog._status_label.get_text() if dialog._status_label.get_visible() else None


# --- What it shows ---

def test_the_dialog_is_named_after_the_tier_and_sized_for_a_long_list(open_manager):
    dialog = open_manager("best")
    assert dialog.get_title() == "Manage Languages — Best"
    assert (dialog.get_content_width(), dialog.get_content_height()) == (560, 680)


def test_the_orientation_data_comes_first_with_its_own_explanation(manager):
    hints = [w for w in walk(manager) if isinstance(w, Gtk.Label) and w.get_text().startswith("Script and orientation")]
    [hint] = hints
    assert hint.get_text() == "Script and orientation detection — needed by Auto-detect and the “+ OSD” modes"
    assert hint.has_css_class("dim-label") and hint.has_css_class("caption")
    row = action_row(manager, OSD)
    assert (row.get_title(), row.get_subtitle()) == ("Orientation and script detection", OSD)
    lists = [w for w in walk(manager) if isinstance(w, Gtk.ListBox)]
    assert len(lists) == 2 and lists[0].has_css_class("boxed-list") and row in list(walk(lists[0]))


def test_there_is_a_row_for_every_language_tesseract_can_read_in_order_of_name(manager):
    rows = [manager._listbox.get_row_at_index(i) for i in range(len(OCR_LANGUAGES))]
    assert [r.get_child().get_title() for r in rows] == [language.name for language in OCR_LANGUAGES]
    assert [r.get_child().get_subtitle() for r in rows] == [language.tesseract_code for language in OCR_LANGUAGES]
    assert manager._listbox.get_row_at_index(len(OCR_LANGUAGES)) is None and len(OCR_LANGUAGES) == 34
    assert not any(r.get_activatable() for r in rows)


def test_the_search_box_and_the_error_line_start_as_they_should(manager):
    assert manager._search_entry.get_placeholder_text() == "Search languages…"
    assert not manager._status_label.get_visible() and manager._status_label.has_css_class("error")


def test_a_language_that_is_not_downloaded_offers_to_download_it(manager):
    assert controls(action_row(manager, "ara")) == [DOWNLOAD]
    assert controls(action_row(manager, OSD)) == [DOWNLOAD]


def test_a_language_that_is_downloaded_shows_it_and_offers_to_delete_it(open_manager, disk):
    disk.have.add(("fast", "ara"))
    disk.have.add(("standard", "eng"))  # Another quality tier, so not this dialog's.
    manager = open_manager("fast")
    assert controls(action_row(manager, "ara")) == [("check",), DELETE]
    assert controls(action_row(manager, "eng")) == [DOWNLOAD]
    assert button_of(action_row(manager, "ara")).has_css_class("flat")


# --- Searching ---

def visible_codes(dialog):
    return [code for code, row in dialog._rows.items() if row.get_child_visible()]


@pytest.mark.parametrize("query, expected", [
    ("arab", {"ara"}),
    ("  ARAB ", {"ara"}),
    ("chi_", {"chi_sim", "chi_tra"}),
    ("zzzz", set()),
])
def test_searching_narrows_the_list_by_name_or_code(manager, query, expected):
    manager._search_entry.set_text(query)
    manager._search_entry.emit("search-changed")
    pump(0.1)
    assert set(visible_codes(manager)) == expected


def test_searching_for_a_code_finds_the_language_and_clearing_shows_everything_again(manager):
    manager._search_entry.set_text("eng")
    manager._search_entry.emit("search-changed")
    pump(0.1)
    assert "eng" in visible_codes(manager) and "ara" not in visible_codes(manager)
    manager._search_entry.set_text("")
    manager._search_entry.emit("search-changed")
    pump(0.1)
    assert len(visible_codes(manager)) == 34


# --- Downloading ---

def test_downloading_shows_a_spinner_and_ends_with_the_language_ready_and_the_caller_told(open_manager, disk):
    heard = []
    disk.hold.clear()
    manager = open_manager("standard", on_changed=lambda: heard.append("changed"))
    button_of(action_row(manager, "ara")).emit("clicked")
    assert pump(1.0, until=lambda: disk.asked)
    pump(0.05)
    assert disk.asked == [("standard", "ara")] and manager._active_download == "ara"
    assert controls(action_row(manager, "ara")) == [("spinner", True), ("progress", "…")]
    assert heard == [] and status_of(manager) is None

    disk.hold.set()
    wait_for_the_download(manager)
    assert controls(action_row(manager, "ara")) == [("check",), DELETE]
    assert heard == ["changed"] and status_of(manager) is None


def test_the_percentage_is_shown_while_it_is_known(open_manager, disk):
    disk.hold.clear()
    disk.percents = [10, 55, None]
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    assert pump(1.0, until=lambda: len(disk.asked) == 1)
    pump(0.1)
    assert controls(action_row(manager, "ara"))[1] == ("progress", "…")  # The last thing it said.
    manager._show_percent("ara", 55)
    assert controls(action_row(manager, "ara"))[1] == ("progress", "55%")
    manager._show_percent("ara", None)
    assert controls(action_row(manager, "ara"))[1] == ("progress", "…")


def test_a_second_download_is_ignored_while_one_runs(open_manager, disk):
    disk.hold.clear()
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    assert pump(1.0, until=lambda: disk.asked)
    manager._on_download("eng")
    pump(0.1)
    assert disk.asked == [("fast", "ara")] and manager._active_download == "ara"


def test_while_one_language_downloads_no_other_row_offers_to_download(open_manager, disk):
    disk.have.add(("fast", "jpn"))
    disk.hold.clear()
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    assert pump(1.0, until=lambda: disk.asked)
    pump(0.05)
    unavailable = ("button", "folder-download-symbolic", "Download", False)
    assert controls(action_row(manager, "eng")) == [unavailable]
    assert controls(action_row(manager, OSD)) == [unavailable]
    assert controls(action_row(manager, "jpn")) == [("check",), DELETE]  # Deleting one is still possible.

    disk.hold.set()
    wait_for_the_download(manager)
    assert controls(action_row(manager, "eng")) == [DOWNLOAD] and controls(action_row(manager, OSD)) == [DOWNLOAD]


def test_a_failed_download_gives_the_other_rows_their_buttons_back(open_manager, disk):
    disk.failure = RuntimeError("boom")
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    wait_for_the_download(manager)
    assert controls(action_row(manager, "eng")) == [DOWNLOAD] and controls(action_row(manager, "ara")) == [DOWNLOAD]


def test_a_failed_download_says_which_language_and_why_and_can_be_tried_again(open_manager, disk):
    heard = []
    disk.failure = RuntimeError("HTTP 503")
    manager = open_manager(on_changed=lambda: heard.append("changed"))
    button_of(action_row(manager, "ara")).emit("clicked")
    wait_for_the_download(manager)
    assert status_of(manager) == "Couldn't download Arabic: HTTP 503"
    assert controls(action_row(manager, "ara")) == [DOWNLOAD] and heard == []

    disk.failure = None
    button_of(action_row(manager, "ara")).emit("clicked")
    assert pump(0.05) and status_of(manager) is None  # Starting again clears the old complaint.
    wait_for_the_download(manager)
    assert controls(action_row(manager, "ara")) == [("check",), DELETE] and heard == ["changed"]


def test_the_orientation_data_is_downloaded_like_a_language_but_the_caller_is_not_told(open_manager, disk):
    heard = []
    manager = open_manager(on_changed=lambda: heard.append("changed"))
    button_of(action_row(manager, OSD)).emit("clicked")
    wait_for_the_download(manager)
    assert disk.asked == [("fast", OSD)] and controls(action_row(manager, OSD)) == [("check",), DELETE]
    assert heard == []


def test_nobody_has_to_be_told_about_the_downloads(open_manager, disk):
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    wait_for_the_download(manager)
    assert controls(action_row(manager, "ara")) == [("check",), DELETE]


# --- Deleting ---

def test_deleting_a_language_removes_it_and_tells_the_caller(open_manager, disk):
    heard = []
    disk.have.add(("fast", "ara"))
    manager = open_manager(on_changed=lambda: heard.append("changed"))
    button_of(action_row(manager, "ara")).emit("clicked")
    pump(0.05)
    assert disk.deleted == [("fast", "ara")] and heard == ["changed"]
    assert controls(action_row(manager, "ara")) == [DOWNLOAD] and status_of(manager) is None


def test_deleting_without_a_caller_to_tell_is_fine(open_manager, disk):
    disk.have.add(("fast", "ara"))
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    assert disk.deleted == [("fast", "ara")]


def test_deleting_the_orientation_data_warns_when_the_selected_mode_needs_it(open_manager, disk):
    heard = []
    config = AppConfig.load()
    config.ocr_psm = PSM_WITH_OSD
    config.save()
    disk.have.add(("fast", OSD))
    manager = open_manager(on_changed=lambda: heard.append("changed"))
    button_of(action_row(manager, OSD)).emit("clicked")
    assert disk.deleted == [("fast", OSD)]
    assert status_of(manager) == (
        "Orientation data removed — the selected page segmentation mode needs it. "
        "Re-download it or pick another mode in OCR Settings."
    )
    assert heard == []


def test_deleting_the_orientation_data_says_nothing_when_the_mode_does_not_need_it(open_manager, disk):
    config = AppConfig.load()
    config.ocr_psm = PSM_WITHOUT_OSD
    config.save()
    disk.have.add(("fast", OSD))
    manager = open_manager()
    button_of(action_row(manager, OSD)).emit("clicked")
    assert disk.deleted == [("fast", OSD)] and status_of(manager) is None
    assert controls(action_row(manager, OSD)) == [DOWNLOAD]


def test_the_warning_about_a_deleted_language_does_not_appear_for_a_language(open_manager, disk):
    config = AppConfig.load()
    config.ocr_psm = PSM_WITH_OSD
    config.save()
    disk.have.add(("fast", "ara"))
    manager = open_manager()
    button_of(action_row(manager, "ara")).emit("clicked")
    assert status_of(manager) is None
