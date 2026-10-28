# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The in-app downloader of Tesseract language data (core/tessdata_manager.py): what counts as downloaded, and that
a download either ends with a whole file or leaves nothing behind."""

import threading
import urllib.error

import pytest

from karamvt.core import tessdata_manager as manager

ENOUGH = b"x" * manager._MIN_VALID_SIZE


class FakeResponse:
    """What the server gives back: a status, an optional length, and the body in pieces."""

    def __init__(self, chunks, status=200, length=True):
        self.chunks, self.status = list(chunks), status
        self.length = str(sum(len(chunk) for chunk in chunks)) if length is True else length

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def getheader(self, name):
        return self.length if name == "Content-Length" else None

    def read(self, amount):
        return self.chunks.pop(0) if self.chunks else b""


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, "APP_DATA_DIR", tmp_path)
    return tmp_path / "tessdata" / "fast"


@pytest.fixture
def server(monkeypatch):
    """Replaces the network: `server.answer` is what the next request gets (a response, or an exception to raise)."""
    state = type("Server", (), {"urls": [], "timeouts": [], "answer": None})()

    def urlopen(url, timeout=None):
        state.urls.append(url)
        state.timeouts.append(timeout)
        if isinstance(state.answer, Exception):
            raise state.answer
        return state.answer

    monkeypatch.setattr(manager.urllib.request, "urlopen", urlopen)
    return state


def left_in(folder):
    return sorted(entry.name for entry in folder.iterdir())


# --- What is there ---

def test_the_folder_for_a_quality_is_made_when_asked_for(folder):
    assert not folder.exists()
    assert manager.local_tessdata_dir("fast") == str(folder) and folder.is_dir()


@pytest.mark.parametrize("size, downloaded", [(manager._MIN_VALID_SIZE - 1, False), (manager._MIN_VALID_SIZE, True)])
def test_a_file_that_is_too_small_to_be_language_data_does_not_count(folder, size, downloaded):
    manager.local_tessdata_dir("fast")
    (folder / "ara.traineddata").write_bytes(b"x" * size)
    assert manager.is_downloaded("fast", "ara") is downloaded
    assert manager.is_downloaded("fast", "eng") is False


def test_the_languages_downloaded_are_listed_without_the_support_files(folder):
    manager.local_tessdata_dir("fast")
    for name in ("eng", "ara", "osd", "equ"):
        (folder / f"{name}.traineddata").write_bytes(ENOUGH)
    (folder / "deu.traineddata").write_bytes(b"too small")
    (folder / "fra.traineddata.part").write_bytes(ENOUGH)
    (folder / "notes.txt").write_bytes(ENOUGH)
    assert manager.list_downloaded("fast") == ["ara", "eng"]
    assert manager.list_downloaded("best") == []


def test_the_orientation_data_is_found_when_it_is_there(folder):
    assert manager.has_osd("fast") is False
    (folder / "osd.traineddata").write_bytes(ENOUGH)
    assert manager.has_osd("fast") is True


def test_a_language_can_be_removed_and_removing_what_is_not_there_is_fine(folder):
    manager.local_tessdata_dir("fast")
    (folder / "ara.traineddata").write_bytes(ENOUGH)
    manager.delete("fast", "ara")
    manager.delete("fast", "ara")
    assert left_in(folder) == []


# --- Downloading ---

def test_a_language_is_downloaded_from_the_repository_of_its_quality(folder, server):
    server.answer = FakeResponse([ENOUGH[:10_000], ENOUGH[10_000:]])
    progress = []
    manager.download("fast", "ara", progress_cb=progress.append)
    assert server.urls == ["https://github.com/tesseract-ocr/tessdata_fast/raw/refs/heads/main/ara.traineddata"]
    assert server.timeouts == [30]
    assert (folder / "ara.traineddata").read_bytes() == ENOUGH and left_in(folder) == ["ara.traineddata"]
    assert progress == [50, 100] and manager.is_downloaded("fast", "ara")


def test_each_quality_has_its_own_repository(folder, server, tmp_path):
    for quality, repository in (("standard", "tessdata"), ("best", "tessdata_best")):
        server.answer = FakeResponse([ENOUGH])
        manager.download(quality, "eng")
        assert server.urls[-1] == f"https://github.com/tesseract-ocr/{repository}/raw/refs/heads/main/eng.traineddata"


def test_a_server_that_does_not_say_how_long_the_file_is_gives_no_percentages(folder, server):
    server.answer = FakeResponse([ENOUGH[:15_000], ENOUGH[15_000:]], length=None)
    progress = []
    manager.download("fast", "ara", progress_cb=progress.append)
    assert progress == [None, None] and (folder / "ara.traineddata").stat().st_size == len(ENOUGH)


def test_nobody_has_to_watch_the_progress(folder, server):
    server.answer = FakeResponse([ENOUGH])
    manager.download("fast", "ara")
    assert manager.is_downloaded("fast", "ara")


def test_a_download_replaces_what_was_there(folder, server):
    manager.local_tessdata_dir("fast")
    (folder / "ara.traineddata").write_bytes(b"old" * 10_000)
    server.answer = FakeResponse([ENOUGH])
    manager.download("fast", "ara")
    assert (folder / "ara.traineddata").read_bytes() == ENOUGH


def test_a_quality_that_does_not_exist_is_refused_before_anything_is_asked(folder, server):
    with pytest.raises(RuntimeError, match="Unknown OCR quality tier: 'ultra'"):
        manager.download("ultra", "ara")
    assert server.urls == []


def test_the_user_can_stop_a_download_and_nothing_is_left(folder, server):
    stop = threading.Event()
    server.answer = FakeResponse([ENOUGH[:10_000], ENOUGH[10_000:]])
    with pytest.raises(RuntimeError, match="Cancelled by user"):
        manager.download("fast", "ara", progress_cb=lambda _percent: stop.set(), cancel_event=stop)
    assert left_in(folder) == []


def test_a_stop_pressed_before_the_first_chunk_leaves_nothing(folder, server):
    stop = threading.Event()
    stop.set()
    server.answer = FakeResponse([ENOUGH])
    with pytest.raises(RuntimeError, match="Cancelled by user"):
        manager.download("fast", "ara", cancel_event=stop)
    assert left_in(folder) == []


def test_a_reply_that_is_not_the_file_is_refused(folder, server):
    server.answer = FakeResponse([ENOUGH], status=204)
    with pytest.raises(RuntimeError, match="Server returned HTTP 204"):
        manager.download("fast", "ara")
    assert left_in(folder) == []


def test_a_language_that_does_not_exist_is_reported_as_a_wrong_code(folder, server):
    server.answer = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    with pytest.raises(RuntimeError) as caught:
        manager.download("fast", "xx")
    assert str(caught.value) == "No 'fast' language data found for code 'xx' — double-check the language code"
    assert isinstance(caught.value.__cause__, urllib.error.HTTPError)


def test_any_other_answer_of_the_server_is_reported_with_its_number(folder, server):
    server.answer = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
    with pytest.raises(RuntimeError, match=r"^Download failed: HTTP 503$"):
        manager.download("fast", "ara")


def test_no_connection_is_reported_with_its_reason(folder, server):
    server.answer = urllib.error.URLError("Name or service not known")
    with pytest.raises(RuntimeError, match=r"^Download failed: Name or service not known$"):
        manager.download("fast", "ara")
    assert left_in(folder) == []


class Failing(FakeResponse):
    """A response that fails with `error` once its first piece has been received."""

    def __init__(self, error):
        super().__init__([ENOUGH[:10_000], ENOUGH[10_000:]])
        self.error = error

    def read(self, amount):
        if len(self.chunks) == 1:
            raise self.error
        return super().read(amount)


def test_an_error_of_the_server_half_way_leaves_nothing(folder, server):
    server.answer = Failing(urllib.error.HTTPError("u", 500, "Server Error", {}, None))
    with pytest.raises(RuntimeError, match=r"^Download failed: HTTP 500$"):
        manager.download("fast", "ara")
    assert left_in(folder) == []


def test_a_connection_that_fails_half_way_with_a_reason_leaves_nothing(folder, server):
    server.answer = Failing(urllib.error.URLError("connection broken"))
    with pytest.raises(RuntimeError, match=r"^Download failed: connection broken$"):
        manager.download("fast", "ara")
    assert left_in(folder) == []


def test_a_connection_lost_half_way_leaves_nothing_and_is_not_disguised(folder, server):
    server.answer = Failing(ConnectionResetError("reset by peer"))
    with pytest.raises(ConnectionResetError):
        manager.download("fast", "ara")
    assert left_in(folder) == []


def test_a_file_too_small_to_be_language_data_is_refused_and_not_kept(folder, server):
    manager.local_tessdata_dir("fast")
    (folder / "ara.traineddata").write_bytes(ENOUGH)  # What was there stays.
    server.answer = FakeResponse([b"<html>Rate limited</html>"])
    with pytest.raises(RuntimeError, match="Downloaded file looks incomplete or corrupt"):
        manager.download("fast", "ara")
    assert left_in(folder) == ["ara.traineddata"] and (folder / "ara.traineddata").read_bytes() == ENOUGH


def test_the_same_language_cannot_be_downloaded_twice_at_once_but_can_be_again_after(folder, server):
    class Greedy(FakeResponse):
        def read(self, amount):
            if self.chunks:
                with pytest.raises(RuntimeError, match=r"Already downloading 'ara' \(fast\)"):
                    manager.download("fast", "ara")
            return super().read(amount)

    server.answer = Greedy([ENOUGH])
    manager.download("fast", "ara")
    server.answer = FakeResponse([ENOUGH])
    manager.download("fast", "ara")  # The first one is over: nothing holds the name any more.


def test_a_failed_download_does_not_hold_the_name_either(folder, server):
    server.answer = urllib.error.URLError("down")
    with pytest.raises(RuntimeError):
        manager.download("fast", "ara")
    server.answer = FakeResponse([ENOUGH])
    manager.download("fast", "ara")
    assert manager.is_downloaded("fast", "ara")


def test_the_data_for_orientation_is_fetched_only_when_missing(folder, server):
    server.answer = FakeResponse([ENOUGH])
    progress = []
    manager.ensure_osd("fast", progress_cb=progress.append)
    assert server.urls[-1].endswith("/osd.traineddata") and manager.has_osd("fast") and progress == [100]
    server.urls.clear()
    manager.ensure_osd("fast")
    assert server.urls == []
