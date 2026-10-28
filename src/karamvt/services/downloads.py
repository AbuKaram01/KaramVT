# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Model downloads as a background service shared by every window.

One download runs at a time, on its own thread, so the user can keep translating while a large model downloads.
Listeners are called on the interface thread through ``dispatch`` and may touch widgets. The service outlives the
Preferences window: closing and reopening it shows the download that is still running.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from pathlib import Path

from ..config import AppConfig
from ..core.catalog import ModelOption
from ..core.downloads import DownloadCancelled, DownloadProgress, download_option

# Runs the given callable on the interface thread; this is how worker-thread results reach the listeners.
Dispatch = Callable[[Callable[[], None]], None]

# Has the call shape of core.downloads.download_option, and can be replaced, for example by a fake in tests.
Downloader = Callable[..., Path]

# Minimum time between two progress reports passed on to the interface thread.
_PROGRESS_INTERVAL_SECONDS = 0.25


def use_model(option: ModelOption, path: Path | None = None) -> Path:
    """Select a downloaded model in the saved settings and return the path that was saved.

    This is a read-modify-write on the *current* settings file, so it never undoes a change made elsewhere (the
    Preferences window and the OCR Settings dialog write to the same file).

    Args:
        option: The model to select.
        path: Location of the model; defaults to the standard location of ``option`` in the configured models folder.

    Returns:
        The path that was stored in the settings.
    """
    config = AppConfig.load()
    chosen = path or option.local_path(config.models_folder())
    setattr(config, option.kind.config_field, str(chosen))
    config.save()
    return chosen


class DownloadBusyError(RuntimeError):
    """Another download is already running."""


class DownloadEventKind(str, Enum):
    """The stage of a download that a DownloadEvent reports."""

    STARTED = "started"
    PROGRESS = "progress"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadEvent:
    """A change in the state of the running download, as delivered to listeners.

    Attributes:
        kind: What happened.
        option: The model that is being downloaded.
        progress: The latest progress report; set only for PROGRESS events.
        error: The exception that stopped the download; set only for FAILED events.
    """

    kind: DownloadEventKind
    option: ModelOption
    progress: DownloadProgress | None = None
    error: Exception | None = None


# Called on the interface thread for every event.
Listener = Callable[[DownloadEvent], None]


class DownloadManager:
    """Runs one model download at a time on a worker thread and reports its progress to listeners.

    All public methods are meant to be called from the interface thread. Listeners are called on that thread too,
    because every result of the worker thread is passed through ``dispatch`` first.

    Args:
        models_dir: Returns the models folder. It is asked each time a download starts, so a folder changed in
            Preferences applies to the next download.
        dispatch: Runs a callable on the interface thread.
        on_installed: Runs on the interface thread when a download has finished, before listeners hear about it.
        downloader: Performs the download; defaults to core.downloads.download_option.

    Attributes:
        current: The model being downloaded, or None when the manager is idle.
        progress: The latest progress report of the running download, or None.
    """

    def __init__(
        self,
        models_dir: Callable[[], str | Path],
        dispatch: Dispatch,
        *,
        on_installed: Callable[[ModelOption, Path], None] | None = None,
        downloader: Downloader = download_option,
    ) -> None:
        self._models_dir = models_dir
        self._dispatch = dispatch
        self._on_installed = on_installed
        self._downloader = downloader
        self._listeners: list[Listener] = []
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: ModelOption | None = None
        self.progress: DownloadProgress | None = None

    @property
    def active(self) -> bool:
        """Whether a download is running."""
        return self.current is not None

    # --- Listening ---

    def subscribe(self, listener: Listener) -> None:
        """Register a listener for all later events.

        Earlier events are not replayed; the present state is available from ``current`` and ``progress``.
        """
        self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        """Remove a listener; a listener that is not registered is ignored."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, event: DownloadEvent) -> None:
        # Iterates over a copy, so that a listener can unsubscribe while it handles the event.
        for listener in list(self._listeners):
            listener(event)

    # --- Control (call from the interface thread) ---

    def start(self, option: ModelOption) -> None:
        """Begin downloading ``option`` into the configured models folder.

        Raises:
            DownloadBusyError: A download is already running; the exception argument is the key of that model.
        """
        if self.current is not None:
            raise DownloadBusyError(self.current.key)
        # Asked before any state changes, so that a failure here leaves the manager idle instead of busy for good.
        models_dir = str(self._models_dir())
        self.current, self.progress = option, None
        self._cancelled.clear()
        self._thread = threading.Thread(
            target=self._run, args=(option, models_dir), name="karamvt-download", daemon=True,
        )
        # STARTED is emitted before the thread starts, so that no later event can overtake it.
        self._emit(DownloadEvent(DownloadEventKind.STARTED, option))
        self._thread.start()

    def cancel(self) -> None:
        """Ask the running download to stop; what was downloaded so far is kept for resuming.

        The worker stops at its next cancellation check, and a CANCELLED event follows.
        """
        self._cancelled.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the download thread to end (used by tests and at shutdown)."""
        if self._thread is not None:
            self._thread.join(timeout)

    # --- Worker thread ---

    def _run(self, option: ModelOption, models_dir: str) -> None:
        """Download on the worker thread and hand the outcome to the interface thread through ``dispatch``."""
        last_report = 0.0

        def report(progress: DownloadProgress) -> None:
            # Throttled so that the interface thread is not flooded; the report that completes the download always
            # goes through.
            nonlocal last_report
            now = time.monotonic()
            if now - last_report >= _PROGRESS_INTERVAL_SECONDS or progress.done_bytes >= progress.total_bytes:
                last_report = now
                self._dispatch(lambda: self._on_progress(option, progress))

        try:
            path = self._downloader(option, models_dir, progress=report, cancel=self._cancelled.is_set)
        except DownloadCancelled:
            self._dispatch(partial(self._finish, DownloadEvent(DownloadEventKind.CANCELLED, option)))
        except Exception as error:  # noqa: BLE001 - shown to the user, never swallowed
            self._dispatch(partial(self._finish, DownloadEvent(DownloadEventKind.FAILED, option, error=error)))
        else:
            self._dispatch(partial(self._complete, option, path))

    # --- Interface thread (through dispatch) ---

    def _on_progress(self, option: ModelOption, progress: DownloadProgress) -> None:
        # A report that was queued before its download ended can arrive afterwards; it is dropped.
        if self.current is option:
            self.progress = progress
            self._emit(DownloadEvent(DownloadEventKind.PROGRESS, option, progress))

    def _complete(self, option: ModelOption, path: Path) -> None:
        # on_installed runs first, so that listeners that react to FINISHED already see the model selected.
        if self._on_installed is not None:
            self._on_installed(option, path)
        self._finish(DownloadEvent(DownloadEventKind.FINISHED, option))

    def _finish(self, event: DownloadEvent) -> None:
        """End the download: reset the state first, so that listeners see the manager as idle, then report the event."""
        self.current, self.progress = None, None
        self._emit(event)
