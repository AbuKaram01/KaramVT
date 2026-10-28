# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Support for the tests that drive the real interface (GTK 4, libadwaita 1.5 and a display).

On a machine without a display, run them with ``xvfb-run -a pytest``. The speech and translation libraries are
replaced by stand-ins, since nothing here loads a model, and downloads go through a fake downloader that the test
releases, fails or cancels.
"""

# Most names are re-exported for the test modules that import them from here.
# ruff: noqa: F401

import importlib
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The tests must never reach a real D-Bus session bus.
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk
except (ValueError, ImportError):
    pytest.skip("GTK 4 / libadwaita are not installed", allow_module_level=True)

# Gtk.init_check() answers True even when there is no display, so ask GDK for the default display directly.
if Gdk.Display.get_default() is None:
    pytest.skip("no display available", allow_module_level=True)

# Libraries that are not installed are replaced by mocks, so that the modules importing them still load.
for _name in ("faster_whisper", "faster_whisper.audio", "llama_cpp", "langdetect", "pytesseract", "PIL", "PIL.Image"):
    try:
        importlib.import_module(_name)
    except ImportError:
        sys.modules[_name] = MagicMock()

from karamvt.core.ocr import EngineStatus  # noqa: E402
from karamvt.config import AppConfig  # noqa: E402
from karamvt.core.catalog import LLMS, WHISPERS, option_by_key  # noqa: E402
from karamvt.core.downloads import DownloadCancelled, DownloadProgress, NotEnoughSpaceError  # noqa: E402
from karamvt.gui.application import KaramVTApplication  # noqa: E402
from karamvt.gui.dispatch import run_on_main_thread  # noqa: E402
from karamvt.gui.formatting import size_label  # noqa: E402
from karamvt.gui.model_downloads import RowState  # noqa: E402
from karamvt.gui.preferences import PreferencesWindow  # noqa: E402
from karamvt.gui.window import MainWindow  # noqa: E402
from karamvt.services import DownloadManager, use_model  # noqa: E402


def pump(seconds: float = 0.3, until=None) -> bool:
    """Run the GLib main loop for up to ``seconds`` (or until ``until()`` is true)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.iteration(False):
            pass
        if until is not None and until():
            return True
        time.sleep(0.005)
    return until() if until is not None else True


class FakeDownloader:
    """Pretends to download: reports some progress, then waits to be released, failed or cancelled."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.failure = None

    def __call__(self, option, models_dir, *, progress, cancel):
        self.calls.append((option.key, models_dir))
        progress(DownloadProgress(option.approx_bytes // 4, option.approx_bytes, option.files[0].path))
        while not self.release.is_set():
            if cancel():
                raise DownloadCancelled
            time.sleep(0.005)
        if self.failure:
            raise self.failure
        for file in option.files:
            target = Path(models_dir) / option.directory / file.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        return option.local_path(models_dir)


@pytest.fixture
def downloader():
    """A FakeDownloader that is released at the end of the test, so that no download thread is left waiting."""
    fake = FakeDownloader()
    yield fake
    fake.release.set()


@pytest.fixture
def env(tmp_path, monkeypatch, config_file, downloader):
    """A real application and main window whose downloads go through the fake downloader.

    Yields:
        The ``(application, window)`` pair. Both are torn down after the test.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # The result must not depend on whether Tesseract is installed on the machine running the tests.
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.check_engine", lambda: EngineStatus("/usr/bin/tesseract", "5.3.4"))
    monkeypatch.setattr("karamvt.gui.ocr_engine_row.engine_install_command", lambda: "sudo apt install tesseract-ocr")
    app = KaramVTApplication()
    app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
    assert app.register(None)
    app.downloads = DownloadManager(
        lambda: AppConfig.load().models_folder(), run_on_main_thread, on_installed=use_model, downloader=downloader,
    )
    window = MainWindow(application=app)
    window.present()
    pump(0.1)
    yield app, window
    app.downloads.cancel()
    app.downloads.join(2)
    pump(0.1)
    for opened in list(app.get_windows()):
        opened.destroy()


def open_preferences(app, window) -> PreferencesWindow:
    """Open the Preferences window through the application's action and return it."""
    app.activate_action("preferences", None)
    pump(0.1)
    assert isinstance(app.preferences_window, PreferencesWindow)
    return app.preferences_window


def rows_of(prefs):
    """The model rows of a Preferences window, keyed by model key."""
    return prefs._downloads_group._rows


def capture_toasts(target) -> list:
    """Collect the toasts sent to an Adw.ToastOverlay or Adw.PreferencesWindow instead of showing them."""
    seen: list = []
    target.add_toast = seen.append
    return seen


def walk(widget):
    """Yield a widget and all of its descendants, depth first."""
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


def ready_models(tmp_path):
    """Save a configuration whose Whisper folder and LLM file exist, so that both models count as downloaded."""
    whisper = tmp_path / "whisper"
    whisper.mkdir()
    llm = tmp_path / "model.gguf"
    llm.write_bytes(b"x")
    AppConfig(whisper_model_path=str(whisper), llm_model_path=str(llm)).save()


def fake_translation(monkeypatch, error=None, result="Hola"):
    """Replace the Text tab's model loading and engine; the engine returns ``result`` or raises ``error``."""
    class FakeEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def translate(self, text, on_partial=None, cancel=None):
            if error is not None:
                raise error
            return result

    monkeypatch.setattr("karamvt.gui.tabs.text_tab.load_llm", lambda *_a, **_k: object())
    monkeypatch.setattr("karamvt.gui.tabs.text_tab.TextTranslationEngine", FakeEngine)
    monkeypatch.setattr("karamvt.gui.tabs.text_tab.resolve_llm_gpu_layers", lambda _device: 0)


def run_text_job(tab, text="Hello"):
    """Translate ``text`` from English to Spanish in the Text tab and wait until the job has finished."""
    tab._input_buffer.set_text(text)
    tab._source_picker.set_selected("English")
    tab._target_picker.set_selected("Spanish")
    tab._translate_button.emit("clicked")
    assert pump(3.0, until=lambda: tab._worker is not None and not tab.is_busy)
    pump(0.1)
