# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""In-app downloader for Tesseract language data (``.traineddata`` files).

Lets the user fetch official ``.traineddata`` files directly from Tesseract's own GitHub data repositories, at one
of three quality tiers, without touching the system package manager: no sudo/root is needed, and the files are
independent of whatever tesseract-ocr-<lang> packages (if any) are installed system-wide.

Quality tiers (they mirror Tesseract's three official data repositories):

* ``fast``: smallest and fastest, lower accuracy.
* ``standard``: Tesseract's traditional default balance of speed and accuracy.
* ``best``: LSTM models trained for maximum accuracy; slower and larger.

The module has no GTK dependency and needs internet access only for the ``download()`` call itself; nothing else in
OCR needs a network connection.
"""
from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request

from .paths import data_dir

APP_DATA_DIR = data_dir()

# One URL template per quality tier; the language code replaces ``%s``.
_DOWNLOAD_URLS = {
    "fast":     "https://github.com/tesseract-ocr/tessdata_fast/raw/refs/heads/main/%s.traineddata",
    "standard": "https://github.com/tesseract-ocr/tessdata/raw/refs/heads/main/%s.traineddata",
    "best":     "https://github.com/tesseract-ocr/tessdata_best/raw/refs/heads/main/%s.traineddata",
}

# Ordered (id, display name) pairs, for UI lists.
QUALITY_TIERS: list[tuple[str, str]] = [
    ("fast",     "Fast — smaller and quicker, lower accuracy"),
    ("standard", "Standard — Tesseract's default balance"),
    ("best",     "Best — most accurate, larger and slower"),
]

# Support files that live next to the language packs but are not OCR
# languages. Never offered as selectable languages (see list_downloaded).
OSD_CODE = "osd"
NON_LANGUAGE_CODES = frozenset({OSD_CODE, "equ"})

# A .traineddata file smaller than this is almost certainly a truncated
# connection or a GitHub error page, not real model data — even the smallest
# real Tesseract language packs are several hundred KB.
_MIN_VALID_SIZE = 20_000
_CHUNK_BYTES = 65536

# Guards against the same (quality, code) being downloaded twice at once, for example two callers (a manual
# download and an automatic ensure_osd()) racing for the same file and both writing to the same ".part" path.
_download_lock = threading.Lock()
_downloading: set[tuple[str, str]] = set()


def local_tessdata_dir(quality: str) -> str:
    """Return the folder holding downloaded packs for *quality*, creating it if needed."""
    path = APP_DATA_DIR / "tessdata" / quality
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _model_path(quality: str, code: str) -> str:
    return os.path.join(local_tessdata_dir(quality), f"{code}.traineddata")


def is_downloaded(quality: str, code: str) -> bool:
    """Return whether *code* has a plausibly valid downloaded pack for *quality*.

    "Plausibly valid" means the file exists and is not smaller than a truncated download would be.
    """
    path = _model_path(quality, code)
    return os.path.isfile(path) and os.path.getsize(path) >= _MIN_VALID_SIZE


def has_osd(quality: str) -> bool:
    """Return whether osd.traineddata (script/orientation detection) is downloaded for *quality*."""
    return is_downloaded(quality, OSD_CODE)


def list_downloaded(quality: str) -> list[str]:
    """Return the sorted language codes downloaded for *quality*.

    Support files such as "osd" are excluded because they are not selectable OCR languages.
    """
    folder = local_tessdata_dir(quality)
    codes = []
    try:
        for fname in os.listdir(folder):
            if fname.endswith(".traineddata"):
                code = fname[: -len(".traineddata")]
                if code not in NON_LANGUAGE_CODES and is_downloaded(quality, code):
                    codes.append(code)
    except OSError:
        pass
    return sorted(codes)


def delete(quality: str, code: str) -> None:
    """Remove a downloaded language pack. Does nothing, silently, if it is absent or cannot be removed."""
    try:
        os.remove(_model_path(quality, code))
    except OSError:
        pass


def download(quality: str, code: str, progress_cb=None, cancel_event=None) -> None:
    """Download one language's ``.traineddata`` file for the given quality tier.

    Any partial file left behind by a failure is always removed, so a later retry never sees a corrupt
    half-downloaded model sitting there looking valid.

    Args:
        quality: A tier id from QUALITY_TIERS.
        code: The Tesseract language code, for example "eng".
        progress_cb: Optional ``callable(percent: int | None)``. None means "downloading, but total size unknown"
            (no Content-Length header); the caller should show an indeterminate spinner in that case.
        cancel_event: Optional ``threading.Event``, polled between chunks.

    Raises:
        RuntimeError: The tier is unknown, the same file is already being downloaded, the language code does not
            exist, a network error occurred, or the download was cancelled.
    """
    if quality not in _DOWNLOAD_URLS:
        raise RuntimeError(f"Unknown OCR quality tier: {quality!r}")

    key = (quality, code)
    with _download_lock:
        if key in _downloading:
            raise RuntimeError(
                f"Already downloading {code!r} ({quality}) \u2014 please wait for it to finish"
            )
        _downloading.add(key)

    try:
        _do_download(quality, code, progress_cb, cancel_event)
    finally:
        with _download_lock:
            _downloading.discard(key)


def _do_download(quality: str, code: str, progress_cb, cancel_event) -> None:
    """Download to a ".part" file and move it into place only once it looks complete."""
    url        = _DOWNLOAD_URLS[quality] % code
    final_path = _model_path(quality, code)
    part_path  = final_path + ".part"

    try:
        _fetch(url, part_path, progress_cb, cancel_event)
    except urllib.error.HTTPError as exc:
        _cleanup_partial(part_path)
        raise _http_failure(exc, quality, code) from exc
    except urllib.error.URLError as exc:
        _cleanup_partial(part_path)
        raise RuntimeError(f"Download failed: {exc.reason}") from exc
    except Exception:
        _cleanup_partial(part_path)
        raise

    if os.path.getsize(part_path) < _MIN_VALID_SIZE:
        _cleanup_partial(part_path)
        raise RuntimeError("Downloaded file looks incomplete or corrupt — try again")

    os.replace(part_path, final_path)


def _http_failure(exc: urllib.error.HTTPError, quality: str, code: str) -> RuntimeError:
    """Turn an HTTP error into a RuntimeError with a message the user can act on."""
    if exc.code == 404:
        return RuntimeError(
            f"No {quality!r} language data found for code '{code}' "
            "— double-check the language code"
        )
    return RuntimeError(f"Download failed: HTTP {exc.code}")


def _fetch(url: str, part_path: str, progress_cb, cancel_event) -> None:
    """Write what the server sends to *part_path*, reporting progress and stopping when cancelled."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Server returned HTTP {resp.status}")
        length_header = resp.getheader("Content-Length")
        total = int(length_header) if length_header else None
        written = 0
        with open(part_path, "wb") as f:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("Cancelled by user")
                chunk = resp.read(_CHUNK_BYTES)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress_cb:
                    progress_cb(int(written * 100 / total) if total else None)


def ensure_osd(quality: str, progress_cb=None) -> None:
    """Make sure osd.traineddata is present for *quality*, downloading it when it is missing.

    The file holds the orientation/script detection data that is needed whenever OCR has no explicit source
    language to fall back on. A tessdata directory built purely by downloading specific languages one at a time
    does not have it unless it is fetched separately, since it is not tied to any one language.

    Raises:
        RuntimeError: The download failed, as for download().
    """
    if not has_osd(quality):
        download(quality, OSD_CODE, progress_cb=progress_cb)


def _cleanup_partial(part_path: str) -> None:
    try:
        os.remove(part_path)
    except OSError:
        pass
