# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A row that says whether Tesseract, the program behind "Import from image", is installed.

Tesseract is a system program, not a model, so KaramVT cannot download it the way it downloads
language data. What it can do is tell the user plainly whether it is there, and when it is not,
show the install command for their distribution and copy it with one click. "Check Again" then
re-reads the system, so nothing needs restarting after the install.

Used in OCR Settings and on the last page of the welcome guide.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from ..core.ocr import EngineStatus, check_engine, engine_install_command
from .dispatch import run_on_main_thread

# Shown at the start of every "cannot be used" message.
_WHY = "Needed to read text from images."
# How long the row waits for the answer before it says it is checking and carries on with the rest of the window.
# Starting Tesseract is normally instant; the first time on a machine it can take seconds.
_QUICK_ANSWER = 0.25


class OcrEngineRow(Adw.ActionRow):
    """Row that reports whether Tesseract is usable and, if not, how to install it.

    Args:
        add_toast: Shows a toast in the window that hosts the row.

    Attributes:
        checking: True while the answer of a slow check is pending and the row shows "Checking".
    """

    def __init__(self, add_toast: Callable[[Adw.Toast], None]) -> None:
        super().__init__(subtitle_lines=0, use_markup=False, subtitle_selectable=True)
        self._add_toast = add_toast
        self._command: str | None = None
        self._generation = 0
        self.checking = False

        self._icon = Gtk.Image(icon_size=Gtk.IconSize.LARGE)
        self.add_prefix(self._icon)

        self._copy = Gtk.Button(label="Copy Command", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        self._copy.connect("clicked", self._on_copy)
        self._recheck = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        self._recheck.set_tooltip_text("Check Again")
        self._recheck.update_property([Gtk.AccessibleProperty.LABEL], ["Check Again"])
        self._recheck.connect("clicked", lambda _button: self.update())
        self.add_suffix(self._copy)
        self.add_suffix(self._recheck)

        self.update()

    def update(self) -> None:
        """Ask the system again and show the result.

        The question is asked in the background, so a slow start of Tesseract never freezes the window: when the
        answer comes within a moment (the usual case) it is shown at once, otherwise the row says it is checking
        and shows the answer when it arrives. An answer to an older question is never shown over a newer one.
        """
        self._generation += 1
        generation = self._generation
        answer: dict[str, object] = {}
        ready = threading.Event()
        lock = threading.Lock()
        late = [False]

        def ask() -> None:
            status, command = check_engine(), engine_install_command()
            with lock:
                answer["status"], answer["command"] = status, command
                ready.set()
                arrived_late = late[0]
            if arrived_late:
                run_on_main_thread(lambda: self._show(generation, status, command))

        threading.Thread(target=ask, name="tesseract-check", daemon=True).start()
        if ready.wait(_QUICK_ANSWER):
            self._show(generation, answer["status"], answer["command"])          # type: ignore[arg-type]
            return
        # The answer may have arrived after the wait timed out. Under the lock, either it is already there and is
        # shown now, or the worker is told to deliver it on the main thread when it is done; it cannot be missed.
        with lock:
            if not ready.is_set():
                late[0] = True
                self._show_checking()
                return
        self._show(generation, answer["status"], answer["command"])              # type: ignore[arg-type]

    def _show_checking(self) -> None:
        self.checking = True
        for css in ("success", "warning"):
            self._icon.remove_css_class(css)
        self._icon.set_from_icon_name("content-loading-symbolic")
        self.set_title("Checking for Tesseract\u2026")
        self.set_subtitle("Starting it for the first time can take a few seconds.")
        self._copy.set_visible(False)
        self._recheck.set_visible(False)

    def _show(self, generation: int, status: EngineStatus, command: str | None) -> None:
        if generation != self._generation:
            return                      # A newer question has been asked since; its answer is the one to show.
        self.checking = False
        self._command = command
        for css in ("success", "warning"):
            self._icon.remove_css_class(css)

        if status.ok:
            self._icon.set_from_icon_name("object-select-symbolic")
            self._icon.add_css_class("success")
            self.set_title("Tesseract is installed")
            self.set_subtitle(f"Version {status.version} \u00b7 {status.path}")
        else:
            self._icon.set_from_icon_name("dialog-warning-symbolic")
            self._icon.add_css_class("warning")
            self.set_title("Tesseract is not installed" if status.path is None else "Tesseract can't be used")
            self.set_subtitle(self._how_to_fix(status))

        self._copy.set_visible(not status.ok and self._command is not None)
        self._recheck.set_visible(not status.ok)

    def _how_to_fix(self, status: EngineStatus) -> str:
        """Return the text that says why Tesseract cannot be used and what to do about it."""
        if status.path is None:
            lead = _WHY
        else:
            lead = f"{_WHY} It was found at {status.path}, but {status.problem}."
        if self._command:
            text = f"{lead} Install it with:\n{self._command}"
        else:
            text = f"{lead} Install it with your system's package manager, then press Check Again."
        if status.path is None and status.searched:
            text += "\nLooked in: " + "; ".join(status.searched)
        return text

    def _on_copy(self, _button: Gtk.Button) -> None:
        if self._command is None:
            return
        Gdk.Display.get_default().get_clipboard().set(self._command)
        self._add_toast(Adw.Toast(title="Install command copied. Paste it in a terminal."))
