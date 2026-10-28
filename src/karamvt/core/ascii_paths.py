# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep non-ASCII paths away from tesseract.exe on Windows.

Tesseract's Windows builds are ANSI programs: the paths on their command line, in their environment and the
folder they are started from reach them converted to the system's ANSI code page. Arabic letters survive that only
on a system whose code page has them (1256); on any other they become "?", and a user name in Arabic (so a TEMP
folder, the program's folder and the language-data folder in Arabic) then makes Tesseract fail with "did not
produce any output file" or "Failed loading language".

pytesseract puts its working files in TEMP and gives their full paths to Tesseract, so it cannot be used in that
situation. This module runs Tesseract itself instead (Python has no such problem with non-ASCII paths):

* Tesseract is started *inside* a temporary working folder and is given file names relative to it
  ("input.png", "result"): the working folder is handed to Windows as UTF-16 and never goes through
  Tesseract's command line, so it can hold any letters;
* the program is started by its 8.3 short name when its own path has non-ASCII letters (Tesseract works out where
  its language data is from the path it was started with);
* the language-data folder travels in TESSDATA_PREFIX as its 8.3 short name or, when the volume keeps none, as a
  directory junction inside the working folder.

Nothing here applies outside Windows, and nothing for paths that are already ASCII: :func:`must_guard` says when.
"""
from __future__ import annotations

import contextlib
import errno
import os
import shlex
import subprocess
import sys
import tempfile
from typing import Optional

from .paths import is_windows

# Win32 CREATE_NO_WINDOW process-creation flag: starts the child process without a console window.
_CREATE_NO_WINDOW = 0x08000000

# Fixed ASCII names inside the working folder: the junction to the language data, the input picture and the base
# name of Tesseract's output (Tesseract appends the extension).
_JUNCTION_NAME = "tessdata"
_INPUT_NAME = "input.png"
_OUTPUT_BASE = "result"

# Output kinds that pytesseract does not name as an argument on the command line; for these the switch travels in
# the options string instead.
_NOT_AN_ARGUMENT = {"box", "osd", "tsv", "xml"}


class ProgramNotFound(Exception):
    """The Tesseract program could not be started because it does not exist; its path is the exception argument."""


class TesseractFailed(Exception):
    """Tesseract ran and did not produce what was asked for.

    Carries the process exit status (``returncode``) and Tesseract's error output (``message``).
    """

    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(f"{returncode} {message}")
        self.returncode = returncode
        self.message = message


def is_ascii(path: Optional[str]) -> bool:
    """Return whether *path* is pure ASCII; None counts as ASCII, so an absent path never triggers the guard."""
    return path is None or path.isascii()


def must_guard(program: Optional[str], tessdata_dir: Optional[str]) -> bool:
    """Return whether Tesseract would be given a non-ASCII path on this system.

    That is the case on Windows when the program, the language-data folder or the TEMP folder (where pytesseract
    writes its files) contains such letters. Always False on other systems.
    """
    if not is_windows():
        return False
    return not all(is_ascii(path) for path in (program, tessdata_dir, tempfile.gettempdir()))


def short_path(path: str) -> Optional[str]:
    """Return the 8.3 short form of the existing *path* if that is pure ASCII, else None.

    None is also returned off Windows and when the volume keeps no short names: Windows then returns the long
    name, which is no use here.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short_path_name.restype = wintypes.DWORD
        # Called without a buffer, the function returns the required size, including the terminating NUL.
        size = get_short_path_name(path, None, 0)
        if size == 0:
            return None
        buffer = ctypes.create_unicode_buffer(size)
        length = get_short_path_name(path, buffer, size)
        # 0 is a failure; a length that is not smaller than the buffer means the buffer was too small.
        if length == 0 or length >= size:
            return None
    except (AttributeError, OSError, ValueError):
        return None
    return buffer.value if is_ascii(buffer.value) else None


def launchable(program: str) -> str:
    """Return *program* in a form Windows can start while Tesseract still sees only ASCII.

    Tesseract works out where its own folder (and so its language data) is from the path it was started with.
    The 8.3 short name is therefore used when the path has non-ASCII letters; otherwise, or when no short name
    exists, the path itself is returned.
    """
    if is_ascii(program):
        return program
    return short_path(program) or program


def make_junction(target: str, link: str) -> bool:
    """Make the folder *link* a directory junction to *target*.

    Unlike a symbolic link, a junction needs no administrator rights. Return False if it cannot be created.
    """
    if sys.platform != "win32":
        return False
    try:
        import _winapi

        _winapi.CreateJunction(target, link)
        return True
    except (ImportError, AttributeError, OSError):
        pass
    # _winapi is a private module and may lack CreateJunction, so ``mklink /J`` through cmd is the fallback.
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True, check=False, timeout=20, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and os.path.isdir(link)


def _remove_link(link: str) -> None:
    """Remove the junction *link* only, never what it points to.

    ``os.rmdir`` removes a junction without touching its target; ``os.unlink`` is the fallback for any other kind
    of link.
    """
    try:
        os.rmdir(link)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(link)


def _safe_tessdata(program: str, tessdata_dir: Optional[str], work_dir: str, junctions: list[str]) -> Optional[str]:
    """Return the value for TESSDATA_PREFIX for this run, or None to leave the environment as it is.

    The folder is the one asked for; failing that, when the program's own path has non-ASCII letters, the
    ``tessdata`` folder beside it (where Tesseract would have looked, had it been able to read its own path).
    A folder with non-ASCII letters is replaced by its 8.3 short name or, when the volume keeps none, by a
    directory junction created inside *work_dir*. The junction is returned as a name relative to *work_dir*,
    where Tesseract is started, and its path is appended to *junctions* so that the caller can remove it.
    """
    folder = tessdata_dir
    if folder is None and not is_ascii(program):
        beside = os.path.join(os.path.dirname(program), "tessdata")
        folder = beside if os.path.isdir(beside) else None
    if folder is None or is_ascii(folder):
        return folder
    short = short_path(folder)
    if short:
        return short
    link = os.path.join(work_dir, _JUNCTION_NAME)
    if make_junction(folder, link):
        junctions.append(link)
        return _JUNCTION_NAME
    return folder


def _save_picture(image, path: str) -> None:
    """Write the PIL *image* as a PNG at *path*, replacing transparency with white as pytesseract does."""
    from PIL import Image

    if "A" in image.getbands():
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, (0, 0), image.getchannel("A"))
        image = background
    image.save(path, format="PNG")


def _command_line(program: str, lang: Optional[str], config: str, extension: str) -> list[str]:
    """Build the Tesseract command line from file names relative to the working folder."""
    command = [launchable(program), _INPUT_NAME, _OUTPUT_BASE]
    if lang is not None:
        command += ["-l", lang]
    if config:
        # Non-POSIX splitting on Windows keeps backslashes in the options intact.
        command += shlex.split(config, posix=not is_windows())
    command += [kind for kind in extension.split() if kind not in _NOT_AN_ARGUMENT]
    return command


def _environment(program: str, tessdata_dir: Optional[str], work_dir: str, junctions: list[str]) -> dict[str, str]:
    """Return a copy of the process environment with TESSDATA_PREFIX adjusted for ASCII-only paths."""
    environment = dict(os.environ)
    data = _safe_tessdata(program, tessdata_dir, work_dir, junctions)
    if data is not None:
        environment["TESSDATA_PREFIX"] = data
    return environment


def _start(command: list[str], work_dir: str, environment: dict[str, str], program: str):
    """Run the program to completion and return the finished process; raise ProgramNotFound if it does not exist."""
    try:
        return subprocess.run(
            command, cwd=work_dir, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
        raise ProgramNotFound(program) from exc


def _read_output(work_dir: str, extension: str, returncode: int, message: str) -> str:
    """Return the text of Tesseract's output file; a missing file raises TesseractFailed with its error output."""
    try:
        with open(os.path.join(work_dir, f"{_OUTPUT_BASE}.{extension}"), "rb") as handle:
            return handle.read().decode("utf-8")
    except OSError:
        raise TesseractFailed(returncode, message or "Tesseract did not produce any output file.") from None


def run_tesseract(
    program: str,
    image,
    extension: str,
    lang: Optional[str],
    config: str = "",
    tessdata_dir: Optional[str] = None,
) -> str:
    """Run Tesseract on the PIL *image* and return the text of its ``<extension>`` output.

    Does what ``pytesseract.run_and_get_output`` does, but from inside a private working folder and with relative
    ASCII file names (see the module docstring).

    Args:
        program: Path of the Tesseract executable.
        image: The PIL image to recognise.
        extension: Kind of output to read: "txt", "tsv" or "osd".
        lang: Language code(s) for ``-l``, or None for Tesseract's default.
        config: Options string, split the way pytesseract splits it.
        tessdata_dir: Language-data folder, or None to leave Tesseract's own lookup alone.

    Raises:
        ProgramNotFound: The executable does not exist.
        TesseractFailed: Tesseract exited with an error or produced no output file.
    """
    with tempfile.TemporaryDirectory(prefix="karamvt-ocr-") as work_dir:
        _save_picture(image, os.path.join(work_dir, _INPUT_NAME))
        junctions: list[str] = []
        try:
            command = _command_line(program, lang, config, extension)
            environment = _environment(program, tessdata_dir, work_dir, junctions)
            done = _start(command, work_dir, environment, program)
            message = " ".join(done.stderr.decode("utf-8", errors="replace").split())
            if done.returncode:
                raise TesseractFailed(done.returncode, message)
            return _read_output(work_dir, extension, done.returncode, message)
        finally:
            for link in junctions:
                _remove_link(link)
