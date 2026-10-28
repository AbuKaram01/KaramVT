# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background-thread runner for the jobs of the Text tab (translating a string, reading text from an image)."""

from __future__ import annotations

import threading
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from ..core.cancel import CancelToken, OperationCancelled


class JobContext:
    """What a Text-tab job uses to talk to the UI. Its methods are called from the worker thread."""

    def __init__(self, worker: "TextJobWorker") -> None:
        self._worker = worker
        self.cancel: CancelToken = worker.cancel_token

    def log(self, message: str) -> None:
        """Send a status line to the Text tab."""
        GLib.idle_add(self._worker.emit, "log", message)

    def partial(self, text: str) -> None:
        """Send the result so far, for example the translation as it streams in."""
        self._worker._push_partial(text)


class TextJobWorker(GObject.Object):
    """Runs a single callable (translate a string, or OCR an image) on a background thread.

    The result is reported back to the main thread through signals. This is the Text tab's counterpart to
    ``PipelineWorker``, which reports only success or failure because its jobs save a file instead of returning a
    value to display.

    The job receives a ``JobContext``: ``log()`` for status lines, ``partial()`` for live results, and ``.cancel``
    (a ``CancelToken``) to honour the Stop button.

    Args:
        job: The callable to run on the worker thread. Its return value is delivered with the "finished" signal.
    """

    __gsignals__ = {
        "log": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # The result so far, while the job is still running (streaming).
        "partial": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # The result is declared as a generic ``object``, not ``str``. The translate job returns a plain string, but
        # the OCR job returns a richer OcrResult (text, confidence and detected language), and GObject signals are
        # strongly typed: declaring it as ``str`` would reject or mangle anything that is not a string.
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (bool, object)),  # (success, result_or_error)
        "cancelled": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, job: Callable[[JobContext], Any]) -> None:
        super().__init__()
        self._job = job
        self._thread: threading.Thread | None = None
        self.cancel_token = CancelToken()
        # Tokens can arrive far faster than the screen can usefully repaint, so only the newest partial is kept and
        # one UI update is scheduled for however many arrived since the last one.
        self._partial_lock = threading.Lock()
        self._pending_partial: str | None = None
        self._flush_scheduled = False

    def start(self) -> None:
        """Start the job on a new daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Ask the job to stop at its next checkpoint."""
        self.cancel_token.cancel()

    def _push_partial(self, text: str) -> None:
        with self._partial_lock:
            self._pending_partial = text
            if self._flush_scheduled:
                return
            self._flush_scheduled = True
        GLib.idle_add(self._flush_partial)

    def _flush_partial(self) -> bool:
        with self._partial_lock:
            text, self._pending_partial = self._pending_partial, None
            self._flush_scheduled = False
        if text is not None:
            self.emit("partial", text)
        return False

    def _run(self) -> None:
        try:
            result = self._job(JobContext(self))
        except OperationCancelled:
            GLib.idle_add(self.emit, "cancelled")
        except Exception as exc:  # Any failure is reported to the UI through the "finished" signal.
            GLib.idle_add(self.emit, "finished", False, str(exc))
        else:
            GLib.idle_add(self.emit, "finished", True, result)
