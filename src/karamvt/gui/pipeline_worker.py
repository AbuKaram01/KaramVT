# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background-thread runner for the file and folder pipelines (transcription and translation jobs).

The model and inference calls are blocking, so they run on a worker thread; the results come back to the GTK main
thread as signals.
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from ..core.cancel import CancelToken, OperationCancelled


class _Pipeline(Protocol):
    """What the worker needs from a pipeline: a blocking ``run`` that raises ``OperationCancelled`` when stopped."""

    def run(self) -> None: ...


# Builds the pipeline from the log callback, the progress callback (a fraction) and the cancellation token.
PipelineFactory = Callable[[Callable[[str], None], Callable[[float], None], CancelToken], _Pipeline]


class PipelineWorker(GObject.Object):
    """Runs a pipeline on a background thread and reports back through signals.

    Model and inference calls (faster-whisper, llama.cpp) are blocking, so they must never run on the GTK main
    thread or the window would freeze. Every signal emission is marshalled back onto the main loop with
    ``GLib.idle_add``, so connected handlers can safely touch widgets directly.

    Stopping is cooperative: ``cancel()`` only raises a flag that the pipeline checks at its checkpoints, and
    "cancelled" (instead of "finished") is emitted once the thread has actually unwound, so the UI can wait for it
    before allowing a new job.

    The signals are ``log`` (a status line), ``progress`` (the fraction of the work done), ``finished`` (whether the
    pipeline succeeded and, if it did not, the error message; empty on success) and ``cancelled``.

    Args:
        pipeline_factory: Builds the pipeline on the worker thread, given the log and progress callbacks and the
            cancellation token.
    """

    __gsignals__ = {
        "log": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "finished": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
        "cancelled": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, pipeline_factory: PipelineFactory) -> None:
        super().__init__()
        self._pipeline_factory = pipeline_factory
        self._thread: threading.Thread | None = None
        self._cancel_token = CancelToken()

    def start(self) -> None:
        """Start the pipeline on a new daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Ask the pipeline to stop at its next checkpoint."""
        self._cancel_token.cancel()

    def _emit_log(self, message: str) -> None:
        GLib.idle_add(self.emit, "log", message)

    def _emit_progress(self, fraction: float) -> None:
        GLib.idle_add(self.emit, "progress", fraction)

    def _run(self) -> None:
        try:
            pipeline = self._pipeline_factory(self._emit_log, self._emit_progress, self._cancel_token)
            pipeline.run()
        except OperationCancelled:
            GLib.idle_add(self.emit, "cancelled")
        except Exception as exc:  # Any failure is reported to the UI through the "finished" signal.
            GLib.idle_add(self.emit, "finished", False, str(exc))
        else:
            GLib.idle_add(self.emit, "finished", True, "")
