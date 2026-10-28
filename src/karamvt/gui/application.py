# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Adw.Application of KaramVT and the ``main`` entry point of the graphical interface.

The application owns the state that outlives a single window (model downloads, saved window state, translation
history) and the actions of the main menu, so that every window and dialog reaches them in the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .. import APP_ID, __version__, winsupport

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ImportError, ValueError) as error:
    winsupport.report_missing_toolkit(error)
    raise

from ..config import AppConfig
from ..core.history import HistoryStore
from ..core.ui_state import StateStore
from ..services import DownloadManager, use_model
from .dispatch import run_on_main_thread
from .window import MainWindow

_PACKAGE_DIR = Path(__file__).resolve().parent


class KaramVTApplication(Adw.Application):
    """The KaramVT application: creates the main window and owns what is shared between windows.

    Attributes:
        downloads: Manager of the model downloads.
        state_store: Storage of the window and tab state remembered between sessions.
        history: Storage of the translation history.
        preferences_window: The Preferences window while it is open, otherwise None.
        welcome_dialog: The welcome guide while it is open, otherwise None.
    """

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        # Owned here, not by the Preferences window, so a download keeps running (and its
        # result is still selected) after Preferences is closed.
        self.downloads = DownloadManager(
            lambda: AppConfig.load().models_folder(), run_on_main_thread, on_installed=use_model,
        )
        # What the window looked like last time, and the translation history. Both only touch the disk when they
        # are first read or written, and both live here so every window shares them.
        self.state_store = StateStore()
        self.history = HistoryStore()
        # The open Preferences window, if any. It is a transient window, not one of the application's own
        # (get_windows() does not list it), so it is tracked here.
        self.preferences_window = None
        # The open welcome guide, if any (a dialog on the main window, not a window of its own).
        self.welcome_dialog = None
        self._create_action("quit", lambda *_: self.quit(), ["<primary>q"])
        self._create_action("about", self._on_about_action)
        self._create_action("preferences", self._on_preferences_action, ["<primary>comma"])
        self._create_action("download-models", self._on_download_models_action)
        self._create_action("welcome", self._on_welcome_action)
        self._create_action("history", self._on_history_action, ["<primary>h"])
        self._create_action("ocr-settings", self._on_ocr_settings_action)
        # What clicking a "job finished" notification runs: bring the window back.
        self._create_action("show-window", lambda *_: self.activate())

    def do_startup(self) -> None:
        """Make the application icon findable by the icon theme."""
        Adw.Application.do_startup(self)
        # The icon is not installed system-wide when running from a source checkout, and Windows has no
        # system-wide icon folders at all: look next to the sources and in the environment's own share/icons.
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            for icons in (_PACKAGE_DIR.parents[2] / "data" / "icons", Path(sys.prefix) / "share" / "icons"):
                if icons.is_dir():
                    theme.add_search_path(str(icons))

    def do_activate(self) -> None:
        """Show the main window, creating it first if needed; on the first start, also offer the welcome guide."""
        window = self.props.active_window
        first_start = window is None
        if not window:
            window = MainWindow(application=self)
        window.present()
        if first_start:
            self._show_welcome_if_new()

    def _show_welcome_if_new(self) -> None:
        """Open the welcome guide on the very first start (no models and the guide never seen)."""
        from .welcome import should_show_welcome

        if should_show_welcome():
            def open_once() -> bool:
                self._open_welcome()
                return GLib.SOURCE_REMOVE

            # After the main window is on screen, so the guide opens on top of it.
            GLib.idle_add(open_once)

    def _open_welcome(self):
        """Return the welcome guide: the one already open, or a new one on top of the main window."""
        from .welcome import WelcomeDialog

        if self.welcome_dialog is None:
            dialog = WelcomeDialog(self.downloads, app_icon=APP_ID)
            dialog.connect("closed", lambda _dialog: setattr(self, "welcome_dialog", None))
            self.welcome_dialog = dialog
            dialog.present(self.props.active_window)
        return self.welcome_dialog

    def _on_welcome_action(self, *_args) -> None:
        self._open_welcome()

    def _on_history_action(self, *_args) -> None:
        window = next((w for w in self.get_windows() if isinstance(w, MainWindow)), None)
        if window is not None:
            window.show_history()

    def do_shutdown(self) -> None:
        """Save the window state and close the history store before the application exits.

        Quitting from the menu or the keyboard closes the windows without asking them, so the state is saved here
        as well as when a window is closed.
        """
        for window in self.get_windows():
            if isinstance(window, MainWindow):
                window.save_state()
        self.history.close()
        Adw.Application.do_shutdown(self)

    def _create_action(self, name: str, callback, shortcuts: list[str] | None = None) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)
        if shortcuts:
            self.set_accels_for_action(f"app.{name}", shortcuts)

    def _on_about_action(self, *_args) -> None:
        about = Adw.AboutWindow(
            transient_for=self.props.active_window,
            application_name="KaramVT",
            application_icon=APP_ID,
            version=__version__,
            developer_name="AbuKaram01",
            comments="Translate video subtitles and text, and extract text from images — fully offline.",
            license_type=Gtk.License.AGPL_3_0,
        )
        about.present()

    def _on_ocr_settings_action(self, *_args) -> None:
        """Open the OCR settings dialog, unless a text job is running.

        This is the one entry point for the main menu and the Preferences row. The Text tab's gear button is already
        locked while a job runs, so it can open the dialog directly. The same rule applies here: the OCR engine is
        not changed in the middle of a job.
        """
        from .ocr_settings_dialog import present_ocr_settings

        main = next((w for w in self.get_windows() if isinstance(w, MainWindow)), None)
        if main is not None and main.text_job_running:
            main.show_toast("OCR Settings are unavailable while a job is running.")
            return
        present_ocr_settings(self.props.active_window)

    def _open_preferences(self):
        """Return the Preferences window: the one already open, or a new one."""
        from .preferences import PreferencesWindow

        if self.preferences_window is None:
            prefs = PreferencesWindow(
                transient_for=self.props.active_window, downloads=self.downloads, history=self.history,
            )
            prefs.connect("destroy", lambda _window: setattr(self, "preferences_window", None))
            self.preferences_window = prefs
        return self.preferences_window

    def _on_preferences_action(self, *_args) -> None:
        self._open_preferences().present()

    def _on_download_models_action(self, *_args) -> None:
        """Open Preferences with the "Download models" menu expanded and in view."""
        prefs = self._open_preferences()
        prefs.show_downloads()
        prefs.present()


def main() -> int:
    """Run the application and return its exit status."""
    app = KaramVTApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
