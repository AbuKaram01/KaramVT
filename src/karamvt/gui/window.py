# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The main window: the Video and Text tabs, the header-bar menu and the notifications about finished jobs.

The window also remembers how the user left it (size, tab and each tab's choices) and keeps a running job from
being killed silently when it is closed.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from .. import winsupport
from ..config import AppConfig, is_model_problem
from ..core.downloads import NotEnoughSpaceError
from ..core.history import HistoryEntry
from ..core.ui_state import StateStore, UiState, WindowState
from ..services import DownloadEvent, DownloadEventKind
from .formatting import size_label
from .history_dialog import HistoryDialog
from .model_downloads import model_name
from .tabs.text_tab import TextTabView
from .tabs.video_tab import VideoTabView


# A change is written this long after the last one, so typing or resizing does not write the file on every step.
_STATE_SAVE_DELAY_MS = 800

# The entries of the header-bar menu as (label, application action). The application defines the actions.
_MENU_ITEMS = [
    ("OCR Settings", "app.ocr-settings"),
    ("Download Models…", "app.download-models"),
    ("Translation History", "app.history"),
    ("Preferences", "app.preferences"),
    ("Welcome Guide", "app.welcome"),
    ("About", "app.about"),
]

# How long closing waits for a running job to unwind before closing anyway. A model that is still loading cannot
# be interrupted, so the wait has to be bounded.
_CLOSE_WAIT_SECONDS = 15.0


class MainWindow(Adw.ApplicationWindow):
    """Application window that hosts the Video and Text tabs in an Adw.ViewStack.

    Besides the tabs it owns the header-bar menu and the toasts, saves the window state shortly after it changes
    (and once more when the window closes), and asks the user before a close would stop a running job.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._closing = False
        # True while the saved state is being applied, so that applying it is not mistaken for a change to save.
        self._restoring = True
        self._destroyed = False
        self._state_save_source = 0
        self._history_dialog: HistoryDialog | None = None
        app = self.get_application()
        self._state_store: StateStore | None = getattr(app, "state_store", None)
        saved = self._state_store.load() if self._state_store is not None else UiState()
        self._set_up_window(saved.window)

        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)
        self._view_stack = Adw.ViewStack()
        self._toast_overlay.set_child(self._build_toolbar_view())
        self._restore_state(saved)

        # Warn instead of silently killing a running job if the window is closed.
        self.connect("close-request", self._on_close_request)
        self._follow_downloads()
        self.connect("destroy", self._on_destroy)

    def _set_up_window(self, saved: WindowState) -> None:
        self.set_title("KaramVT")
        # GTK 4 has no window icon API on Windows; this sets it there once the window is mapped (no-op elsewhere).
        self.connect("map", lambda *_: winsupport.apply_icon_to_own_windows())
        self.set_default_size(saved.width, saved.height)
        if saved.maximized:
            self.maximize()

    def _build_toolbar_view(self) -> Adw.ToolbarView:
        toolbar_view = Adw.ToolbarView()
        self._add_tabs()
        switcher_title = Adw.ViewSwitcherTitle(stack=self._view_stack, title="KaramVT")
        toolbar_view.add_top_bar(self._build_header(switcher_title))

        # A second bar with the tab switcher, shown only when the window is narrow enough that the switcher does not
        # fit in the header bar (the usual companion of Adw.ViewSwitcherTitle).
        switcher_bar = Adw.ViewSwitcherBar(stack=self._view_stack)
        switcher_title.bind_property("title-visible", switcher_bar, "reveal", 0)
        toolbar_view.add_bottom_bar(switcher_bar)

        toolbar_view.set_content(self._view_stack)
        return toolbar_view

    def _add_tabs(self) -> None:
        self._video_tab = VideoTabView()
        self._video_tab.connect("toast-requested", self._on_toast_requested)
        self._video_tab.connect("job-finished", self._on_job_finished, "video-finished")
        self._video_tab.connect("state-changed", self._on_state_changed)
        self._view_stack.add_titled_with_icon(self._video_tab, "video", "Video", "video-x-generic-symbolic")

        self._text_tab = TextTabView()
        self._text_tab.connect("toast-requested", self._on_toast_requested)
        self._text_tab.connect("job-finished", self._on_job_finished, "text-finished")
        self._text_tab.connect("state-changed", self._on_state_changed)
        self._text_tab.connect("translation-done", self._on_translation_done)
        self._view_stack.add_titled_with_icon(self._text_tab, "text", "Text", "text-x-generic-symbolic")

    @staticmethod
    def _build_header(switcher_title: Adw.ViewSwitcherTitle) -> Adw.HeaderBar:
        menu = Gio.Menu()
        for label, action in _MENU_ITEMS:
            menu.append(label, action)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu_button.set_menu_model(menu)
        header = Adw.HeaderBar()
        header.set_title_widget(switcher_title)
        header.pack_end(menu_button)
        return header

    def _restore_state(self, saved: UiState) -> None:
        """Open the way the user left it: the tab, then each tab's own choices.

        Nothing is announced while this runs, because the state is what has just been read. The change handlers
        are connected only afterwards, so the first change after it is what gets saved.
        """
        self._view_stack.set_visible_child_name(saved.tab)
        self._video_tab.apply_state(saved.video)
        self._text_tab.apply_state(saved.text)
        self._view_stack.connect("notify::visible-child-name", self._on_state_changed)
        for name in ("default-width", "default-height", "maximized"):
            self.connect(f"notify::{name}", self._on_state_changed)
        self._restoring = False

    def _follow_downloads(self) -> None:
        """Tell the user how a model download ended, whether or not Preferences is still open."""
        downloads = getattr(self.get_application(), "downloads", None)
        if downloads is not None:
            downloads.subscribe(self._on_download_event)
            self.connect("destroy", lambda _window: downloads.unsubscribe(self._on_download_event))

    # --- Used by the application's actions ---

    @property
    def text_job_running(self) -> bool:
        """Whether a job of the Text tab is running."""
        return self._text_tab.is_busy

    @property
    def video_job_running(self) -> bool:
        """Whether a job of the Video tab is running."""
        return self._video_tab.is_busy

    def show_toast(self, message: str) -> None:
        """Show a short message at the bottom of the window."""
        self._toast_overlay.add_toast(Adw.Toast(title=message))

    # --- Remembering where the user left off ---

    def capture_state(self) -> UiState:
        """Collect everything to remember about how the window looks right now."""
        # The size when not maximised, which is the one to go back to.
        width, height = self.get_default_size()
        keep_text = AppConfig.load().history_enabled
        return UiState(
            window=WindowState(width=max(width, 1), height=max(height, 1), maximized=self.is_maximized()),
            tab=self._view_stack.get_visible_child_name() or "video",
            video=self._video_tab.capture_state(),
            text=self._text_tab.capture_state(keep_text=keep_text),
        )

    def save_state(self) -> bool:
        """Write the state now and cancel any save that was waiting.

        Closing the window calls this, so the last changes are never lost.

        Returns:
            Whether the state was written.
        """
        self._cancel_pending_state_save()
        if self._state_store is None or self._destroyed:
            return False
        return self._state_store.save(self.capture_state())

    def _on_state_changed(self, *_args) -> None:
        """Schedule a save shortly after the last change, so that a burst of changes is written once."""
        if self._restoring or self._destroyed or self._state_save_source or self._state_store is None:
            return
        self._state_save_source = GLib.timeout_add(_STATE_SAVE_DELAY_MS, self._save_due)

    def _save_due(self) -> bool:
        """Timer callback of the delayed save; runs once."""
        self._state_save_source = 0
        self.save_state()
        return GLib.SOURCE_REMOVE

    def _cancel_pending_state_save(self) -> None:
        if self._state_save_source:
            GLib.source_remove(self._state_save_source)
            self._state_save_source = 0

    def _on_destroy(self, _window) -> None:
        # The widgets are going away: a save that was still waiting would record whatever is left of them.
        self._destroyed = True
        self._cancel_pending_state_save()

    # --- Translation history ---

    def show_history(self) -> None:
        """Open the translation history (nothing happens if it is already open)."""
        history = getattr(self.get_application(), "history", None)
        if history is None:
            return
        if self._history_dialog is None:
            dialog = HistoryDialog(history, self._use_translation)
            dialog.connect("closed", lambda _dialog: setattr(self, "_history_dialog", None))
            self._history_dialog = dialog
            dialog.present(self)

    def _use_translation(self, entry: HistoryEntry) -> bool:
        """Handle "Use again" in the history: put the translation back into the Text tab and show that tab.

        Returns:
            False when the Text tab is busy and the translation was not applied.
        """
        if self._text_tab.is_busy:
            self.show_toast("Wait for the current text job to finish first.")
            return False
        self._text_tab.show_translation(
            entry.source_language, entry.target_language, entry.source_text, entry.translated_text,
        )
        self._view_stack.set_visible_child_name("text")
        return True

    def _on_translation_done(
        self, _tab, source_language: str, target_language: str, source_text: str, translated: str,
        model: str, seconds: float,
    ) -> None:
        """Store a finished translation in the history, unless the user turned the history off."""
        config = AppConfig.load()
        history = getattr(self.get_application(), "history", None)
        if history is None or not config.history_enabled:
            return
        entry = history.add(
            source_language, target_language, source_text, translated,
            model=model, seconds=seconds, keep=config.history_keep,
        )
        if entry is not None and self._history_dialog is not None:
            self._history_dialog.refresh()

    def notify_finished(self, notification_id: str, title: str, body: str) -> None:
        """Tell the user, with a desktop notification, that a job ended while the app is not in front.

        Nothing is sent while the user is in the app (the toast says it there), or when they have turned
        the notifications off. A later notification with the same id replaces the earlier one.
        """
        if not AppConfig.load().notify_when_done or self._app_is_in_front():
            return
        # GLib has no notification backend on Windows, so a Windows toast is used there.
        if winsupport.send_toast(title, body):
            return
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        # Clicking the notification brings the window forward.
        notification.set_default_action("app.show-window")
        self.get_application().send_notification(notification_id, notification)

    def _app_is_in_front(self) -> bool:
        """Return whether the user is in KaramVT: this window or Preferences (a window of its own) has the focus."""
        preferences = getattr(self.get_application(), "preferences_window", None)
        return self.is_active() or (preferences is not None and preferences.is_active())

    # --- Handlers ---

    def _on_toast_requested(self, _sender, message: str) -> None:
        toast = Adw.Toast(title=message)
        if is_model_problem(message):
            # A missing or moved model: offer the way to get one, right on the message.
            toast.set_button_label("Download Models")
            toast.set_action_name("app.download-models")
        self._toast_overlay.add_toast(toast)

    def _on_job_finished(self, _tab, title: str, body: str, notification_id: str) -> None:
        self.notify_finished(notification_id, title, body)

    def _on_download_event(self, event: DownloadEvent) -> None:
        name = model_name(event.option)
        if event.kind is DownloadEventKind.FINISHED:
            toast = Adw.Toast(title=f"{name} downloaded and selected.")
        elif event.kind is DownloadEventKind.CANCELLED:
            toast = Adw.Toast(title="Download stopped. Press Download to continue later.")
        elif event.kind is DownloadEventKind.FAILED:
            error = event.error
            if isinstance(error, NotEnoughSpaceError):
                title = (f"Not enough free disk space: {size_label(error.needed)} needed, "
                         f"{size_label(error.available)} available.")
            else:
                title = f"Download failed: {error}"
            toast = Adw.Toast(title=title, timeout=10)
        else:
            return
        # The welcome guide and Preferences sit on top of this window: show the message where the user is looking.
        app = self.get_application()
        on_top = getattr(app, "welcome_dialog", None) or getattr(app, "preferences_window", None)
        (on_top or self._toast_overlay).add_toast(toast)

    def _on_close_request(self, *_args) -> bool:
        """Warn before closing under a running job instead of killing it silently.

        Returns:
            True to block the close (the job is still unwinding, or the user has yet to answer), False to allow it.
        """
        if self._closing:
            return True  # Already shutting down: wait for the job to unwind.
        if not (self._video_tab.is_busy or self._text_tab.is_busy):
            self.save_state()
            return False  # Allow the window to close normally.

        video_running = self._video_tab.is_busy
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Job still running",
            body=(
                "Closing now will stop the current job. "
                + (
                    "Your progress is saved, so you can continue it next time. "
                    if video_running else ""
                )
                + "Close anyway?"
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("close", "Close Anyway")
        dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_close_dialog_response)
        dialog.present()
        return True  # Block the close until the user responds.

    def _on_close_dialog_response(self, _dialog: Adw.MessageDialog, response: str) -> None:
        if response != "close":
            return
        # Do not destroy the window under a running job: a model call that is in the middle of native code
        # (llama.cpp, Whisper) can crash the process on exit. Ask the job to stop and wait for it to unwind (it only
        # needs to finish the current subtitle line or segment), then close. Everything it had produced is already
        # saved.
        self._closing = True
        self.set_sensitive(False)
        self._video_tab.stop_for_quit()
        self._text_tab.stop_for_quit()
        deadline = time.monotonic() + _CLOSE_WAIT_SECONDS

        def _check() -> bool:
            if not (self._video_tab.is_busy or self._text_tab.is_busy) or time.monotonic() > deadline:
                self.save_state()
                self.destroy()
                return False
            return True

        if _check():
            GLib.timeout_add(100, _check)
