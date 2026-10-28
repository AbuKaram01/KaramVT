# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bridge between background threads and the GTK main loop."""

from __future__ import annotations

from collections.abc import Callable

from gi.repository import GLib


def run_on_main_thread(callback: Callable[[], None]) -> None:
    """Schedule ``callback`` on the GLib main loop; safe to call from any thread."""

    def run_once() -> bool:
        callback()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(run_once)
