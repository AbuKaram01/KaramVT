# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The downloader, exercised against a real HTTP server on localhost that behaves like Hugging Face."""

import re
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import pytest

from karamvt.core import downloads
from karamvt.core.catalog import ModelKind, ModelOption, RemoteFile
from karamvt.core.downloads import (
    DownloadCancelled,
    DownloadError,
    NotEnoughSpaceError,
    _HttpsOnlyRedirects,
    download_option,
)

MIB = 1024 * 1024
BIG = bytes(range(256)) * (3 * MIB // 256)  # 3 MiB, so that a download takes several chunks.
SMALL = b'{"hello": "world"}'
OPTION = ModelOption(
    "test", ModelKind.WHISPER, "owner/repo", "model-dir",
    (RemoteFile("config.json", len(SMALL)), RemoteFile("1_Pooling/config.json", len(SMALL)),
     RemoteFile("weights.bin", len(BIG))),
)
ROOT = "/owner/repo/resolve/main/"


class Site:
    """What the fake server does; tests change these fields to simulate trouble."""

    def __init__(self):
        self.files = {ROOT + "config.json": SMALL, ROOT + "1_Pooling/config.json": SMALL, ROOT + "weights.bin": BIG}
        self.requests: list[tuple[str, str | None]] = []
        self.supports_range = True
        self.drop_after: int | None = None  # Cut the connection after this many body bytes, once.
        self.always_drop = False
        self.drop_every: int | None = None  # Cut every attempt after this many bytes (the last one is whole).
        self.drop_path = ROOT + "weights.bin"  # The connection trouble applies to this file only.
        self.failures: dict[str, list[int]] = {}  # Status codes to answer per path before succeeding.
        self.redirects: dict[str, str] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        site: Site = self.server.site
        path = unquote(self.path)
        site.requests.append((path, self.headers.get("Range")))
        if site.failures.get(path):
            return self._empty(site.failures[path].pop(0))
        if path in site.redirects:
            self.send_response(302)
            self.send_header("Location", site.redirects[path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = site.files.get(path)
        if body is None:
            return self._empty(404)

        start, status = 0, 200
        wanted = self.headers.get("Range")
        if wanted and site.supports_range:
            start = int(re.match(r"bytes=(\d+)-", wanted).group(1))
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        chunk = body[start:]
        self.send_response(status)
        self.send_header("Content-Length", str(len(chunk)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()

        if site.drop_every is not None and path == site.drop_path and len(chunk) > site.drop_every:
            self.wfile.write(chunk[:site.drop_every])
            self.wfile.flush()
            self.close_connection = True
            return
        cut = 0 if site.always_drop else site.drop_after
        if cut is not None and path == site.drop_path:
            site.drop_after = None
            self.wfile.write(chunk[:cut])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(chunk)

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def site():
    """Serve the fake site over HTTP on a free localhost port; the test gets the Site it can change."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.site = Site()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    server.site.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server.site
    server.shutdown()
    server.server_close()


def fetch(site, tmp_path, **options):
    """Download OPTION from the fake site into ``tmp_path``, without waiting between retries unless asked to."""
    options.setdefault("sleep", lambda _seconds: None)
    return download_option(OPTION, tmp_path, base_url=site.url, **options)


def read(tmp_path, name):
    return (tmp_path / "model-dir" / name).read_bytes()


# --- The normal path ---

def test_every_file_is_downloaded_into_the_models_folder_and_the_result_is_the_settings_path(site, tmp_path):
    result = fetch(site, tmp_path)

    assert result == tmp_path / "model-dir" == OPTION.local_path(tmp_path)
    assert read(tmp_path, "config.json") == SMALL and read(tmp_path, "weights.bin") == BIG
    assert read(tmp_path, "1_Pooling/config.json") == SMALL
    assert not list(tmp_path.rglob("*.part")) and OPTION.is_downloaded(tmp_path)


def test_progress_grows_to_the_exact_total_and_names_the_current_file(site, tmp_path):
    seen = []
    fetch(site, tmp_path, progress=seen.append)

    done = [p.done_bytes for p in seen]
    assert done == sorted(done) and seen[-1].done_bytes == seen[-1].total_bytes == 2 * len(SMALL) + len(BIG)
    assert seen[-1].fraction == 1.0
    assert {p.file for p in seen} == {"config.json", "1_Pooling/config.json", "weights.bin"}


def test_complete_files_are_not_downloaded_again(site, tmp_path):
    fetch(site, tmp_path)
    site.requests.clear()
    fetch(site, tmp_path)
    assert site.requests == []


def test_a_request_names_the_repository_revision_and_encoded_path(site, tmp_path):
    fetch(site, tmp_path)
    assert {path for path, _ in site.requests} == set(site.files)


# --- Resume ---

def test_a_partial_file_is_resumed_with_a_range_request(site, tmp_path):
    folder = tmp_path / "model-dir"
    folder.mkdir()
    (folder / "weights.bin.part").write_bytes(BIG[:MIB + 5])

    fetch(site, tmp_path)

    assert read(tmp_path, "weights.bin") == BIG
    assert (ROOT + "weights.bin", f"bytes={MIB + 5}-") in site.requests


def test_a_server_that_ignores_range_makes_the_file_start_over(site, tmp_path):
    site.supports_range = False
    folder = tmp_path / "model-dir"
    folder.mkdir()
    (folder / "weights.bin.part").write_bytes(b"stale garbage that is not a prefix")

    fetch(site, tmp_path)
    assert read(tmp_path, "weights.bin") == BIG


def test_a_complete_partial_file_answered_with_416_is_just_renamed(site, tmp_path):
    folder = tmp_path / "model-dir"
    folder.mkdir()
    (folder / "weights.bin.part").write_bytes(BIG)

    fetch(site, tmp_path)
    assert read(tmp_path, "weights.bin") == BIG


def test_a_stale_partial_file_longer_than_the_real_file_is_discarded(site, tmp_path):
    folder = tmp_path / "model-dir"
    folder.mkdir()
    (folder / "weights.bin.part").write_bytes(BIG + b"extra")

    fetch(site, tmp_path)
    assert read(tmp_path, "weights.bin") == BIG


def test_cancelling_keeps_the_partial_file_and_the_next_run_continues_it(site, tmp_path):
    seen = []
    with pytest.raises(DownloadCancelled):
        fetch(site, tmp_path, progress=seen.append, cancel=lambda: any(p.file == "weights.bin" for p in seen))

    part = tmp_path / "model-dir" / "weights.bin.part"
    assert part.exists() and 0 < part.stat().st_size < len(BIG) and not OPTION.is_downloaded(tmp_path)

    fetch(site, tmp_path)
    assert read(tmp_path, "weights.bin") == BIG and not part.exists()


# --- Trouble ---

def test_a_dropped_connection_is_retried_and_resumes_where_it_stopped(site, tmp_path):
    site.drop_after = MIB + 7
    sleeps = []
    fetch(site, tmp_path, sleep=sleeps.append)

    assert read(tmp_path, "weights.bin") == BIG
    ranges = [r for path, r in site.requests if path.endswith("weights.bin")]
    assert ranges[0] is None and ranges[1] is not None and sleeps  # The retry used Range and waited.


def test_a_link_that_keeps_dropping_but_keeps_delivering_is_not_given_up_on(site, tmp_path):
    site.drop_every = MIB // 2 + 5  # Five cuts for a file of 3 MiB, with one retry allowed.
    fetch(site, tmp_path, retries=1)
    assert read(tmp_path, "weights.bin") == BIG
    assert sum(p.endswith("weights.bin") for p, _ in site.requests) == 6


def test_a_link_that_never_delivers_anything_gives_up_after_the_retries(site, tmp_path):
    site.always_drop = True
    sleeps = []
    with pytest.raises(DownloadError, match="connection"):
        fetch(site, tmp_path, retries=3, sleep=sleeps.append)

    assert len(sleeps) == 3 and sleeps == sorted(sleeps)                   # Backing off.
    assert sum(p.endswith("weights.bin") for p, _ in site.requests) == 4   # One try plus three retries.


def test_the_wait_between_retries_grows_but_not_beyond_a_limit(site, tmp_path):
    site.always_drop = True
    sleeps = []
    with pytest.raises(DownloadError, match="connection"):
        fetch(site, tmp_path, retries=7, sleep=sleeps.append)
    assert sleeps[:4] == [2.0, 4.0, 8.0, 16.0] and max(sleeps) == downloads._MAX_BACKOFF_SECONDS == 30.0


def test_cancelling_while_waiting_to_retry_stops_before_asking_the_server_again(site, tmp_path):
    site.always_drop = True

    def cancel():
        return sum(p.endswith("weights.bin") for p, _ in site.requests) >= 1

    with pytest.raises(DownloadCancelled):
        fetch(site, tmp_path, cancel=cancel)
    assert sum(p.endswith("weights.bin") for p, _ in site.requests) == 1


def test_a_range_refused_for_a_file_that_was_not_started_is_an_error(site, tmp_path):
    site.failures[ROOT + "weights.bin"] = [416]
    with pytest.raises(DownloadError, match="416"):
        fetch(site, tmp_path)


def test_a_missing_file_fails_at_once_without_retrying(site, tmp_path):
    del site.files[ROOT + "weights.bin"]
    sleeps = []
    with pytest.raises(DownloadError, match="404"):
        fetch(site, tmp_path, sleep=sleeps.append)
    assert sleeps == [] and sum(p.endswith("weights.bin") for p, _ in site.requests) == 1


def test_a_temporary_server_error_is_retried(site, tmp_path):
    site.failures[ROOT + "config.json"] = [503, 429]
    fetch(site, tmp_path)
    assert read(tmp_path, "config.json") == SMALL


def test_a_full_disk_is_reported_before_anything_is_downloaded(site, tmp_path, monkeypatch):
    class Usage:
        free = 10 * MIB

    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda _path: Usage)
    with pytest.raises(NotEnoughSpaceError) as caught:
        fetch(site, tmp_path)
    assert caught.value.available == 10 * MIB and caught.value.needed >= len(BIG)
    assert site.requests == []


# --- Redirects (Hugging Face hands big files to a CDN) ---

def test_a_redirect_is_followed_and_keeps_the_range_header(site, tmp_path):
    folder = tmp_path / "model-dir"
    folder.mkdir()
    (folder / "weights.bin.part").write_bytes(BIG[:100])
    site.files["/cdn/weights.bin"] = BIG
    site.redirects[ROOT + "weights.bin"] = f"{site.url}/cdn/weights.bin"

    fetch(site, tmp_path)

    assert read(tmp_path, "weights.bin") == BIG
    assert ("/cdn/weights.bin", "bytes=100-") in site.requests


@pytest.mark.parametrize("target", ["http://cdn.example/f", "ftp://cdn.example/f", "file:///etc/passwd", "/relative"])
def test_a_redirect_away_from_https_is_refused(target):
    handler = _HttpsOnlyRedirects(allow_http=False)
    with pytest.raises(urllib.error.URLError, match="refusing"):
        handler.redirect_request(_request(), None, 302, "Found", {}, target)


def test_a_redirect_to_https_is_allowed():
    handler = _HttpsOnlyRedirects(allow_http=False)
    redirected = handler.redirect_request(_request(), None, 302, "Found", {}, "https://cdn.example/f")
    assert redirected.full_url == "https://cdn.example/f" and redirected.get_header("Range") == "bytes=5-"


def _request():
    import urllib.request

    return urllib.request.Request("https://huggingface.co/x", headers={"Range": "bytes=5-"})
