# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Downloading the models of ``catalog`` from Hugging Face, with resume and cancellation.

Only the standard library is used. Each file is written to ``<name>.part`` and renamed
into place when complete, so a file that exists is a finished file, and an interrupted
download continues from where it stopped (HTTP ``Range``). Redirects, which Hugging Face
uses to hand large files to its CDN, are followed only when they stay on HTTPS.

Nothing here touches the interface; failures are exceptions the interface describes.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from .. import __version__
from .catalog import ModelOption, RemoteFile

# Polled at checkpoints; returns True when the caller wants the download to stop.
CancelCheck = Callable[[], bool]

HUGGING_FACE = "https://huggingface.co"
_CHUNK_BYTES = 1024 * 1024
# Free space that must remain on the disk beyond the download itself.
_SPACE_MARGIN_BYTES = 256 * 1024 * 1024
_TIMEOUT_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 30.0
# Matches "bytes 0-99/1234" and "bytes */1234"; group 1 is the full size of the file.
_CONTENT_RANGE = re.compile(r"bytes\s+(?:\d+-\d+|\*)/(\d+)")
# Statuses that are usually temporary: timeout, too early, rate limit and server errors.
_RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException)


class DownloadError(Exception):
    """A download could not be completed."""


class DownloadCancelled(Exception):
    """The caller asked to stop. Partial files are kept so the download can resume."""


class NotEnoughSpaceError(DownloadError):
    """The disk cannot hold the remaining download.

    Attributes:
        needed: Bytes that the download still needs.
        available: Free bytes on the disk.
    """

    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"{needed} bytes needed, {available} available")
        self.needed = needed
        self.available = available


@dataclass(frozen=True)
class DownloadProgress:
    """A progress report for one download.

    Attributes:
        done_bytes: Bytes received so far, counting files that were already complete.
        total_bytes: Total bytes of the download; an estimate until the real size of every file is known.
        file: Path, inside the model folder, of the file being fetched.
    """

    done_bytes: int
    total_bytes: int
    file: str

    @property
    def fraction(self) -> float:
        """The completed share of the download, from 0.0 to 1.0."""
        return min(1.0, self.done_bytes / self.total_bytes) if self.total_bytes else 0.0


# Receives a DownloadProgress after every chunk written.
ProgressSink = Callable[[DownloadProgress], None]


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect that would leave HTTPS (the ``Range`` header survives a redirect)."""

    def __init__(self, allow_http: bool) -> None:
        self._allow_http = allow_http

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - stdlib signature
        scheme = urlparse(newurl).scheme
        if scheme != "https" and not self._allow_http:
            raise urllib.error.URLError(f"refusing a redirect to a {scheme or 'relative'} address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _total_from_headers(headers: http.client.HTTPMessage, offset: int, status: int) -> int | None:
    """Return the full file size from ``Content-Range`` (206) or ``Content-Length`` (200); None if neither is usable."""
    if status == http.client.PARTIAL_CONTENT:
        match = _CONTENT_RANGE.match(headers.get("Content-Range", ""))
        if match:
            return int(match.group(1))
        length = headers.get("Content-Length")
        return offset + int(length) if length and length.isdigit() else None
    length = headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def _size_of(path: Path) -> int:
    """Return the size of *path* in bytes, or 0 if it does not exist."""
    return path.stat().st_size if path.exists() else 0


def _refuse_what_cannot_be_retried(error: Exception, file: RemoteFile) -> None:
    """Raise DownloadError for a server answer that retrying cannot change (not found, forbidden, ...)."""
    if isinstance(error, urllib.error.HTTPError) and error.code not in _RETRYABLE_HTTP_CODES:
        raise DownloadError(f"{error.code} {error.reason}: {file.path}") from error


class _Download:
    """The state of one ``download_option`` call."""

    def __init__(
        self,
        option: ModelOption,
        folder: Path,
        progress: ProgressSink | None,
        cancel: CancelCheck | None,
        base_url: str,
        retries: int,
        sleep: Callable[[float], None],
    ) -> None:
        self._option = option
        self._folder = folder
        self._progress = progress
        self._cancel = cancel
        self._base_url = base_url.rstrip("/")
        self._retries = retries
        self._sleep = sleep
        self._opener = urllib.request.build_opener(_HttpsOnlyRedirects(base_url.startswith("http://")))
        # Real sizes of the files whose size is known.
        self._sizes: dict[str, int] = {}
        # Bytes in files that are already complete.
        self._finished_bytes = 0

    # --- Driver ---

    def run(self) -> Path:
        """Fetch every file that is not complete yet and return the model folder."""
        self._folder.mkdir(parents=True, exist_ok=True)
        self._ensure_space(self._remaining_estimate())
        for file in self._option.files:
            target = self._folder / file.path
            if target.is_file():
                self._finish_file(file, target.stat().st_size)
            else:
                self._download_file(file, target)
        return self._folder

    def _finish_file(self, file: RemoteFile, size: int) -> None:
        self._sizes[file.path] = size
        self._finished_bytes += size

    def _download_file(self, file: RemoteFile, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = self._url(file)
        failures = 0
        while True:
            self._check_cancelled()
            before = _size_of(part)
            try:
                size = self._attempt(file, url, part)
            except _Restart:
                continue
            except (urllib.error.HTTPError, *_TRANSIENT_ERRORS) as error:
                _refuse_what_cannot_be_retried(error, file)
                # A connection that keeps making progress does not count towards the retry limit.
                failures = 0 if _size_of(part) > before else failures + 1
                self._wait_or_give_up(error, failures)
            else:
                os.replace(part, target)
                self._finish_file(file, size)
                return

    def _wait_or_give_up(self, failure: Exception, failures: int) -> None:
        """Raise DownloadError once the retries are used up; otherwise sleep with an exponentially growing delay."""
        if failures > self._retries:
            raise DownloadError(f"the connection failed: {failure}") from failure
        self._sleep(min(2.0 ** failures, _MAX_BACKOFF_SECONDS))

    def _attempt(self, file: RemoteFile, url: str, part: Path) -> int:
        """Fetch the rest of one file into *part* and return its final size.

        Raises on any interruption, so that the caller decides whether to retry.
        """
        offset = _size_of(part)
        try:
            response = self._open(url, offset)
        except urllib.error.HTTPError as error:
            if error.code == http.client.REQUESTED_RANGE_NOT_SATISFIABLE and offset:
                self._settle_unsatisfiable_range(error, offset, part)
                return offset
            raise

        with response:
            status = response.status
            total = _total_from_headers(response.headers, offset, status)
            resumed = status == http.client.PARTIAL_CONTENT and offset > 0
            if not resumed:
                # The server sent the whole file, so the partial data is overwritten.
                offset = 0
            if total is not None:
                self._sizes[file.path] = total
                self._ensure_space(total - offset)
            written = self._write_body(response, file, part, offset, resumed)
        if total is not None and written != total:
            raise ConnectionError(f"received {written} of {total} bytes")
        return written

    def _open(self, url: str, offset: int):
        """Open *url*, asking for the bytes from *offset* on when resuming."""
        headers = {"User-Agent": f"KaramVT/{__version__}"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        return self._opener.open(urllib.request.Request(url, headers=headers), timeout=_TIMEOUT_SECONDS)

    def _write_body(self, response, file: RemoteFile, part: Path, offset: int, resumed: bool) -> int:
        """Write the response body to the partial file and return the length the file now has.

        When resuming, the body is appended after the data that is already there.
        """
        written = offset
        with open(part, "ab" if resumed else "wb") as handle:
            while chunk := response.read(_CHUNK_BYTES):
                self._check_cancelled()
                handle.write(chunk)
                written += len(chunk)
                self._report(file, written)
        return written

    @staticmethod
    def _settle_unsatisfiable_range(error: urllib.error.HTTPError, offset: int, part: Path) -> None:
        """Handle a 416 answer, which means the partial file is already as long as the file, or longer (stale).

        Returns normally if the partial file is complete, so that only the rename is missing. Otherwise the stale
        partial file is deleted and _Restart is raised.
        """
        match = _CONTENT_RANGE.match(error.headers.get("Content-Range", "") if error.headers else "")
        if match and int(match.group(1)) == offset:
            return
        part.unlink(missing_ok=True)
        raise _Restart

    # --- Helpers ---

    def _url(self, file: RemoteFile) -> str:
        return f"{self._base_url}/{self._option.repo}/resolve/{self._option.revision}/{quote(file.path)}"

    def _check_cancelled(self) -> None:
        if self._cancel is not None and self._cancel():
            raise DownloadCancelled

    def _estimate_total(self) -> int:
        """Total download size: the real size where it is known, the catalog estimate for the other files."""
        return sum(self._sizes.get(f.path, f.approx_bytes) for f in self._option.files)

    def _remaining_estimate(self) -> int:
        """Estimate the bytes still to fetch, from the catalog sizes minus what partial files already hold."""
        remaining = 0
        for file in self._option.files:
            target = self._folder / file.path
            if target.is_file():
                continue
            part = target.with_name(target.name + ".part")
            remaining += max(file.approx_bytes - _size_of(part), 0)
        return remaining

    def _report(self, file: RemoteFile, written: int) -> None:
        if self._progress is not None:
            done = self._finished_bytes + written
            self._progress(DownloadProgress(done, max(self._estimate_total(), done), file.path))

    def _ensure_space(self, needed: int) -> None:
        """Raise NotEnoughSpaceError unless *needed* bytes plus a safety margin fit on the disk."""
        available = shutil.disk_usage(self._folder).free
        if needed + _SPACE_MARGIN_BYTES > available:
            raise NotEnoughSpaceError(needed, available)


class _Restart(Exception):
    """Internal: the partial file was discarded; fetch the file again from the start."""


def download_option(
    option: ModelOption,
    models_dir: str | Path,
    *,
    progress: ProgressSink | None = None,
    cancel: CancelCheck | None = None,
    base_url: str = HUGGING_FACE,
    retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Download every file of ``option`` into ``<models_dir>/<option.directory>``.

    Files that are already complete are skipped, partial files are resumed, and short connection drops are retried
    with a growing delay.

    Args:
        option: The model to download.
        models_dir: The folder that holds all models.
        progress: Receives a DownloadProgress after every chunk.
        cancel: Polled at checkpoints; returning True stops the download.
        base_url: The server to download from; replaced only by tests.
        retries: Failed attempts without progress that are retried per file before giving up.
        sleep: The delay function used between retries; replaced only by tests.

    Returns:
        The path the settings should point to (``option.local_path``).

    Raises:
        DownloadCancelled: ``cancel()`` returned True. Partial files stay for the next attempt.
        NotEnoughSpaceError: The disk cannot hold the remaining download.
        DownloadError: The server refused, or the connection kept failing.
    """
    _Download(option, Path(models_dir) / option.directory, progress, cancel, base_url, retries, sleep).run()
    return option.local_path(models_dir)
