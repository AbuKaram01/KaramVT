# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The download manager (karamvt.services): one download at a time, and its result saved as the selected model."""

import threading
import time
from pathlib import Path

import pytest

from karamvt.config import AppConfig
from karamvt.core.catalog import LLMS, WHISPERS, option_by_key
from karamvt.core.downloads import DownloadCancelled, DownloadProgress
from karamvt.services import DownloadBusyError, DownloadEventKind, DownloadManager, use_model

WHISPER = WHISPERS[0]
LLM = LLMS[0]


class Harness:
    """Runs the manager with a fake downloader; callbacks queue up and run when ``flush`` is called."""

    def __init__(self, downloader, models_dir="/models"):
        self.queue = []
        self.events = []
        self.installed = []
        self.manager = DownloadManager(
            lambda: models_dir, self.queue.append,
            on_installed=lambda option, path: self.installed.append((option.key, path)),
            downloader=downloader,
        )
        self.manager.subscribe(self.events.append)

    def finish(self):
        """Wait for the download thread to end, then run the callbacks it queued."""
        self.manager.join(5)
        self.flush()

    def flush(self):
        """Run the callbacks that the manager queued for the main thread."""
        while self.queue:
            self.queue.pop(0)()

    def kinds(self):
        return [event.kind for event in self.events]


def instant(option, models_dir, *, progress, cancel):
    """A downloader that reports two progress updates and returns at once."""
    progress(DownloadProgress(5, 10, "a"))
    progress(DownloadProgress(10, 10, "a"))
    return Path(models_dir) / option.directory


def test_a_download_runs_in_the_configured_folder_and_installs_the_result_before_finishing():
    seen = {}

    def downloader(option, models_dir, *, progress, cancel):
        seen.update(option=option, models_dir=models_dir)
        return Path(models_dir) / "result"

    harness = Harness(downloader)
    harness.manager.start(WHISPER)
    assert harness.manager.active and harness.kinds() == [DownloadEventKind.STARTED]
    harness.finish()

    assert seen == {"option": WHISPER, "models_dir": "/models"}
    assert harness.installed == [("whisper.large-v3-turbo", Path("/models/result"))]
    assert harness.kinds()[-1] is DownloadEventKind.FINISHED and not harness.manager.active
    assert harness.manager.current is None and harness.manager.progress is None


def test_the_folder_is_read_again_for_every_download():
    folders = iter(["/one", "/two"])
    seen = []
    manager = DownloadManager(
        lambda: next(folders), lambda callback: callback(),
        downloader=lambda option, models_dir, *, progress, cancel: seen.append(models_dir) or Path(models_dir),
    )
    for _ in range(2):
        manager.start(WHISPER)
        manager.join(5)
    assert seen == ["/one", "/two"]


def test_progress_is_forwarded_and_the_last_update_is_never_dropped():
    harness = Harness(instant)
    harness.manager.start(WHISPER)
    harness.finish()

    progress = [e.progress for e in harness.events if e.kind is DownloadEventKind.PROGRESS]
    assert progress and progress[-1].done_bytes == 10


def test_progress_is_throttled_so_the_interface_is_not_flooded():
    def chatty(option, models_dir, *, progress, cancel):
        for done in range(1, 5001):
            progress(DownloadProgress(done, 10_000, "a"))
        return Path(models_dir)

    harness = Harness(chatty)
    harness.manager.start(WHISPER)
    harness.finish()
    assert len([e for e in harness.events if e.kind is DownloadEventKind.PROGRESS]) < 200


def test_only_one_download_runs_at_a_time():
    release = threading.Event()

    def slow(option, models_dir, *, progress, cancel):
        release.wait(5)
        return Path(models_dir)

    harness = Harness(slow)
    harness.manager.start(WHISPER)
    with pytest.raises(DownloadBusyError):
        harness.manager.start(LLM)
    release.set()
    harness.finish()
    harness.manager.start(LLM)  # Free again once the first has finished.
    harness.finish()


def test_a_folder_that_cannot_be_resolved_leaves_the_manager_free():
    asked = []

    def models_dir():
        asked.append(True)
        if len(asked) == 1:
            raise OSError("the models folder is not available")
        return "/models"

    queue = []
    manager = DownloadManager(models_dir, queue.append, downloader=instant)
    with pytest.raises(OSError):
        manager.start(WHISPER)
    assert manager.current is None and not manager.active
    manager.start(LLM)  # Not refused as busy: the failed start left nothing behind.
    manager.join(5)
    assert manager.current is LLM


def test_cancelling_reaches_the_downloader_and_installs_nothing():
    def waits_for_cancel(option, models_dir, *, progress, cancel):
        while not cancel():
            time.sleep(0.005)
        raise DownloadCancelled

    harness = Harness(waits_for_cancel)
    harness.manager.start(LLM)
    harness.manager.cancel()
    harness.finish()

    assert harness.kinds()[-1] is DownloadEventKind.CANCELLED and harness.installed == []
    assert not harness.manager.active


def test_a_failure_is_reported_with_its_error_and_the_manager_is_free_again():
    def broken(option, models_dir, *, progress, cancel):
        raise OSError("disk exploded")

    harness = Harness(broken)
    harness.manager.start(LLM)
    harness.finish()

    failed = harness.events[-1]
    assert failed.kind is DownloadEventKind.FAILED and "disk exploded" in str(failed.error)
    assert harness.installed == [] and not harness.manager.active


def test_a_listener_that_unsubscribes_hears_nothing_more():
    harness = Harness(instant)
    harness.manager.unsubscribe(harness.events.append)
    harness.manager.start(WHISPER)
    harness.finish()
    assert harness.events == []


def test_a_restarted_download_forgets_the_previous_progress():
    harness = Harness(instant)
    harness.manager.start(WHISPER)
    harness.finish()
    harness.manager.start(WHISPER)
    assert harness.manager.progress is None
    harness.finish()


# --- Selecting a model in the saved settings ---

def test_use_model_sets_the_field_for_its_kind_only(config_file, tmp_path):
    AppConfig(models_dir=str(tmp_path / "models")).save()

    use_model(option_by_key("whisper.large-v3"))
    loaded = AppConfig.load()
    assert loaded.whisper_model_path == str(tmp_path / "models" / "faster-whisper-large-v3")
    assert loaded.llm_model_path == ""

    use_model(option_by_key("llm.e4b.q6_k_xl"))
    loaded = AppConfig.load()
    assert loaded.llm_model_path == str(tmp_path / "models" / "gemma-4-E4B-it" / "gemma-4-E4B-it-UD-Q6_K_XL.gguf")
    assert loaded.whisper_model_path == str(tmp_path / "models" / "faster-whisper-large-v3")


def test_use_model_without_a_chosen_folder_uses_the_default_one(config_file, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    chosen = use_model(option_by_key("whisper.large-v3-turbo"))
    assert chosen == tmp_path / "karamvt" / "models" / "faster-whisper-large-v3-turbo"
    assert AppConfig.load().whisper_model_path == str(chosen)


def test_use_model_keeps_every_other_saved_setting(config_file):
    AppConfig(cpu_threads=12, llm_temperature=0.4, ocr_psm=3).save()
    use_model(option_by_key("whisper.large-v3-turbo"))
    loaded = AppConfig.load()
    assert (loaded.cpu_threads, loaded.llm_temperature, loaded.ocr_psm) == (12, 0.4, 3)


def test_a_finished_download_becomes_the_selected_model_and_is_saved(config_file, tmp_path):
    queue = []
    manager = DownloadManager(
        lambda: tmp_path / "models", queue.append,
        on_installed=lambda option, path: use_model(option, path),
        downloader=lambda option, models_dir, *, progress, cancel: option.local_path(models_dir),
    )
    manager.start(LLMS[4])  # The E4B model in the Q6_K_XL quantization.
    manager.join(5)
    while queue:
        queue.pop(0)()

    expected = str(tmp_path / "models" / "gemma-4-E4B-it" / "gemma-4-E4B-it-UD-Q6_K_XL.gguf")
    assert AppConfig.load().llm_model_path == expected
