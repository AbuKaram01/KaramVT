# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cooperative cancellation for long-running jobs.

The GUI's Stop button calls ``CancelToken.cancel()`` from the main thread. The job, running on a worker thread,
calls ``raise_if_cancelled()`` at natural checkpoints (between subtitle segments, per streamed token, between
files) and unwinds with ``OperationCancelled``. Nothing is killed from outside, so a stopped job always leaves
models and files in a consistent state: a file is either saved completely or not written at all.
"""
from __future__ import annotations

import threading
from typing import Optional


class OperationCancelled(Exception):
    """Raised inside a job when the user pressed Stop."""


class CancelToken:
    """Thread-safe flag that the GUI sets and a running job polls.

    One token belongs to one job. It is backed by a ``threading.Event``, so ``cancel()`` may be called from any
    thread while the job checks it from another.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation; the job stops at its next checkpoint."""
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        """Whether cancel() has been called."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise OperationCancelled if cancel() was called.

        Jobs call this at natural checkpoints, so that a stopped job never leaves a half-written file.

        Raises:
            OperationCancelled: The user pressed Stop.
        """
        if self._event.is_set():
            raise OperationCancelled()


def check(token: Optional[CancelToken]) -> None:
    """Call ``token.raise_if_cancelled()``, tolerating ``None`` for APIs where passing a token is optional."""
    if token is not None:
        token.raise_if_cancelled()
