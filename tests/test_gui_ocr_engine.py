# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The "is Tesseract installed?" row, in OCR Settings and on the last page of the welcome guide."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import threading
import time

import pytest
from gui_support import Adw, Gdk, Gtk, capture_toasts, downloader, env, pump, walk

from karamvt.core.ocr import EngineStatus
from karamvt.gui.ocr_engine_row import OcrEngineRow
from karamvt.gui.ocr_settings_dialog import OcrSettingsDialog

OK = EngineStatus("/usr/bin/tesseract", "5.3.4")
MISSING = EngineStatus(None, None)
BROKEN = EngineStatus("/usr/bin/tesseract", None, "error while loading shared libraries: libleptonica.so.6")
COMMAND = "sudo pacman -S tesseract tesseract-data-eng tesseract-data-osd"


@pytest.fixture
def system(monkeypatch):
    """What the row will find: set ``system.status`` and ``system.command`` before building or updating it."""
    class System:
        status, command = MISSING, COMMAND

    state = System()
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.check_engine", lambda: state.status)
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.engine_install_command", lambda: state.command)
    return state


def make_row(env):
    """Build an OcrEngineRow that collects its toasts; return the row and the list of toasts."""
    toasts = []
    return OcrEngineRow(toasts.append), toasts


def css(widget):
    return set(widget.get_css_classes())


def test_an_installed_tesseract_shows_its_version_and_location_and_no_buttons(env, system):
    system.status = OK
    row, _ = make_row(env)
    assert row.get_title() == "Tesseract is installed"
    assert row.get_subtitle() == "Version 5.3.4 · /usr/bin/tesseract"
    assert "success" in css(row._icon) and row._icon.get_icon_name() == "object-select-symbolic"
    assert not row._copy.get_visible() and not row._recheck.get_visible()


def test_a_missing_tesseract_shows_the_command_for_this_system_and_offers_to_copy_it(env, system):
    row, _ = make_row(env)
    assert row.get_title() == "Tesseract is not installed"
    assert COMMAND in row.get_subtitle() and "Needed to read text from images." in row.get_subtitle()
    assert "warning" in css(row._icon) and row._icon.get_icon_name() == "dialog-warning-symbolic"
    assert row._copy.get_visible() and row._recheck.get_visible()


def test_copying_puts_the_command_on_the_clipboard_and_says_so(env, system):
    row, toasts = make_row(env)
    row._copy.emit("clicked")

    received = []
    Gdk.Display.get_default().get_clipboard().read_text_async(None, lambda c, r: received.append(c.read_text_finish(r)))
    assert pump(2.0, until=lambda: bool(received))
    assert received == [COMMAND]
    assert [t.get_title() for t in toasts] == ["Install command copied. Paste it in a terminal."]


def test_on_an_unknown_system_there_is_no_copy_button_and_the_advice_is_general(env, system):
    system.command = None
    row, toasts = make_row(env)
    assert not row._copy.get_visible() and row._recheck.get_visible()
    assert "your system's package manager" in row.get_subtitle() and "sudo" not in row.get_subtitle()
    row._copy.emit("clicked")  # Nothing to copy: nothing happens.
    assert toasts == []


def test_a_tesseract_that_will_not_run_says_why_and_still_offers_the_command(env, system):
    system.status = BROKEN
    row, _ = make_row(env)
    assert row.get_title() == "Tesseract can't be used"
    assert "/usr/bin/tesseract" in row.get_subtitle() and "libleptonica.so.6" in row.get_subtitle()
    assert COMMAND in row.get_subtitle() and row._copy.get_visible()


def test_checking_again_notices_an_install_made_in_the_meantime_and_a_removal_too(env, system):
    row, _ = make_row(env)
    assert row.get_title() == "Tesseract is not installed"

    system.status = OK  # The user ran the command in a terminal.
    row._recheck.emit("clicked")
    assert row.get_title() == "Tesseract is installed" and not row._copy.get_visible()
    assert "warning" not in css(row._icon)  # The old colour does not linger.

    system.status = MISSING
    row.update()
    assert row.get_title() == "Tesseract is not installed" and "success" not in css(row._icon)


def test_the_subtitle_can_be_selected_so_the_command_can_be_copied_by_hand(env, system):
    row, _ = make_row(env)
    assert row.get_subtitle_selectable()


def test_a_path_with_markup_characters_is_shown_as_it_is(env, system):
    system.status = EngineStatus("/opt/R&D <tools>/tesseract", "5.3.4")
    row, _ = make_row(env)
    assert row.get_subtitle() == "Version 5.3.4 · /opt/R&D <tools>/tesseract"
    assert not row.get_use_markup()


def test_a_tesseract_that_is_not_found_on_windows_says_where_it_looked(env, system):
    system.status = EngineStatus(
        None, None, searched=("C:/App/tesseract/tesseract.exe", "PATH", "C:/Program Files/Tesseract-OCR/tesseract.exe")
    )
    row, _ = make_row(env)
    assert row.get_subtitle().endswith(
        "Looked in: C:/App/tesseract/tesseract.exe; PATH; C:/Program Files/Tesseract-OCR/tesseract.exe"
    )
    system.status = EngineStatus("C:/App/tesseract.exe", None, "it did not answer in time", searched=("PATH",))
    row.update()
    assert "Looked in" not in row.get_subtitle()  # Only for a program that was not found at all.


# --- A slow start of Tesseract ---

def test_a_slow_check_does_not_freeze_the_row_and_the_answer_is_shown_when_it_comes(env, monkeypatch):
    release = threading.Event()

    def slow():
        release.wait(10)
        return OK

    monkeypatch.setattr("karamvt.gui.ocr_engine_row.check_engine", slow)
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.engine_install_command", lambda: COMMAND)
    try:
        started = time.monotonic()
        row, _ = make_row(env)
        assert time.monotonic() - started < 3  # It did not wait for the program.
        assert row.checking and row.get_title().startswith("Checking for Tesseract")
        assert not row._copy.get_visible() and not row._recheck.get_visible()
    finally:
        release.set()
    assert pump(3.0, until=lambda: not row.checking)
    assert (
        row.get_title() == "Tesseract is installed"
        and row.get_subtitle() == "Version 5.3.4 \u00b7 /usr/bin/tesseract"
    )


def test_an_old_answer_never_replaces_a_newer_one(env, monkeypatch):
    first_release = threading.Event()
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 1:
            first_release.wait(10)
            # The first question is answered last, and wrongly by now.
            return MISSING
        return OK

    monkeypatch.setattr("karamvt.gui.ocr_engine_row.check_engine", check)
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.engine_install_command", lambda: COMMAND)
    try:
        row, _ = make_row(env)
        assert row.checking
        row.update()  # Asked again: answered at once.
        assert not row.checking and row.get_title() == "Tesseract is installed"
    finally:
        first_release.set()
    pump(0.5)
    assert row.get_title() == "Tesseract is installed" and not row.checking


# --- Where it appears ---

def test_ocr_settings_starts_with_the_tesseract_group(env, system):
    dialog = OcrSettingsDialog()
    rows = [w for w in walk(dialog.get_child()) if isinstance(w, OcrEngineRow)]
    assert len(rows) == 1 and rows[0].get_title() == "Tesseract is not installed"
    groups = [w for w in walk(dialog.get_child()) if isinstance(w, Adw.PreferencesGroup)]
    assert groups[0].get_title() == "Tesseract"
    assert rows[0].get_ancestor(Adw.PreferencesGroup) is groups[0]


def test_ocr_settings_shows_its_toasts_inside_itself(env, system):
    dialog = OcrSettingsDialog()
    shown = capture_toasts(dialog)
    row = next(w for w in walk(dialog.get_child()) if isinstance(w, OcrEngineRow))
    row._copy.emit("clicked")
    assert len(shown) == 1


def test_the_welcome_guides_last_page_has_the_row_and_refreshes_it_when_the_page_opens(env, system):
    app, _window = env
    app.activate_action("welcome", None)
    pump(0.2)
    dialog = app.welcome_dialog
    assert dialog._ocr_row.get_title() == "Tesseract is not installed"

    system.status = OK  # Installed while the guide was open.
    dialog._go(2)
    assert dialog._ocr_row.get_title() == "Tesseract is installed"
    assert dialog._ocr_row.get_ancestor(Adw.PreferencesGroup).get_title() == "Text from images"


def test_the_welcome_guide_shows_the_copy_toast_inside_itself(env, system):
    app, window = env
    app.activate_action("welcome", None)
    pump(0.2)
    dialog = app.welcome_dialog
    in_guide, in_window = capture_toasts(dialog), capture_toasts(window._toast_overlay)
    dialog._ocr_row._copy.emit("clicked")
    assert len(in_guide) == 1 and in_window == []
