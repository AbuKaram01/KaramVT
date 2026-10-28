# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""OCR text extraction, the engine behind the Text tab's "from image" input.

Wraps pytesseract, which itself starts the system ``tesseract`` program. It is not a pure-Python library: it needs the
``tesseract-ocr`` system package (and per-language data packages) to be installed. See the README for the exact
package names.

Everything the user can tune lives in the gear button's "OCR Settings" dialog (gui/ocr_settings_dialog.py) and is
stored on AppConfig. This module runs Tesseract. The available choices (PSM_OPTIONS, OEM_OPTIONS, TESS_TOGGLES) and the
conversion of saved values into a command line are in ocr_options.py.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

from . import ascii_paths
from .languages import Language, detect_language_from_text
from .ocr_options import (  # noqa: F401  (re-exported: the options were first defined in this module)
    DEFAULT_OEM,
    DEFAULT_PSM,
    OEM_OPTIONS,
    PSM_NEEDS_OSD,
    PSM_OPTIONS,
    TESS_TOGGLES,
    build_config,
    normalize_psm,
    oem_index,
    parse_custom_params,
    psm_index,
    psm_needs_osd,
    tess_vars_from_config,
)
from .paths import data_dir, installed_data_dir, is_windows

# Maps a script family to one representative Tesseract language pack. Tesseract can tell the *script* an image is
# written in (via OSD, orientation and script detection) but not a specific language the way langdetect can for plain
# text: Latin script alone covers dozens of languages. The mapping gives OCR something better than a silent "eng"
# default when no source language was chosen. It is a starting guess, not real language detection.
#
# The keys are the script names OSD actually reports (checked against Tesseract 5.3 output: Latin, Arabic, Cyrillic,
# Greek, Hebrew, Han, Japanese, Korean, Thai, Bengali). Scripts missing from this table fall back to "eng" and say so
# (see OcrResult.notice).
_SCRIPT_TO_TESSERACT = {
    "Latin": "eng",
    "Arabic": "ara",
    "Cyrillic": "rus",
    "Han": "chi_sim",
    "Hiragana": "jpn",
    "Katakana": "jpn",
    "Japanese": "jpn",
    "Hangul": "kor",
    "Korean": "kor",
    "Devanagari": "hin",
    "Bengali": "ben",
    "Greek": "ell",
    "Hebrew": "heb",
    "Thai": "tha",
}


class OcrUnsupportedError(ValueError):
    """Raised when the requested language has no Tesseract language code in the language table."""


@dataclass
class OcrResult:
    """The outcome of one OCR run.

    Attributes:
        text: The extracted text, stripped; empty when nothing was recognised.
        confidence: Mean word confidence from 0 to 100; None when it is unavailable or no text was found.
        detected_language: The language guessed from the text; only set when no language was passed in.
        notice: A caveat the user should see even though OCR succeeded (for example, script detection could not run,
            so English was assumed). None when all went well.
    """

    text: str
    confidence: Optional[float] = None
    detected_language: Optional[Language] = None
    notice: Optional[str] = None


class _PackageManager(NamedTuple):
    """How one system package manager is recognised and how it installs Tesseract and its data."""

    # The program whose presence on PATH says this is the system's package manager.
    command: str
    # Installs Tesseract itself.
    tesseract: str
    # Installs one language pack; "{lang}" stands for the pack's code (without it, all languages at once).
    language: str
    # Installs the osd data. It is named differently from the language packs on some distributions (Fedora's is
    # "tesseract-osd", not "tesseract-langpack-osd"), so it cannot reuse `language`.
    osd: str
    # Installs a working OCR in one go: the program, English and the osd data. These are the packages that KaramVT's
    # own .deb, .rpm and Arch packages depend on (Arch ships even English separately).
    engine_setup: str


# Checked in order; the first whose command is on PATH wins. On Homebrew the base "tesseract" formula already bundles
# eng and osd.
_PACKAGE_MANAGERS = [
    _PackageManager(
        "apt",
        "sudo apt install tesseract-ocr",
        "sudo apt install tesseract-ocr-{lang}",
        "sudo apt install tesseract-ocr-osd",
        "sudo apt install tesseract-ocr tesseract-ocr-osd",
    ),
    _PackageManager(
        "dnf",
        "sudo dnf install tesseract",
        "sudo dnf install tesseract-langpack-{lang}",
        "sudo dnf install tesseract-osd",
        "sudo dnf install tesseract tesseract-osd",
    ),
    _PackageManager(
        "pacman",
        "sudo pacman -S tesseract",
        "sudo pacman -S tesseract-data-{lang}",
        "sudo pacman -S tesseract-data-osd",
        "sudo pacman -S tesseract tesseract-data-eng tesseract-data-osd",
    ),
    _PackageManager(
        "brew",
        "brew install tesseract",
        "brew install tesseract-lang",
        "brew install tesseract",
        "brew install tesseract",
    ),
    # Windows: the UB Mannheim build, whose installer brings English and the orientation data (osd). Other languages
    # are downloaded in OCR Settings, so the language and osd entries repeat the command that installs the program.
    _PackageManager("winget", *(["winget install UB-Mannheim.TesseractOCR"] * 4)),
]
# The command that installs a working OCR setup, by package manager command.
_ENGINE_SETUP = {manager.command: manager.engine_setup for manager in _PACKAGE_MANAGERS}


def _first_package_manager() -> Optional[_PackageManager]:
    """Return the first known package manager whose command is on PATH, or None."""
    return next((manager for manager in _PACKAGE_MANAGERS if shutil.which(manager.command)), None)


def _install_hint(lang: Optional[str] = None) -> str:
    """Return the hint for installing Tesseract itself (lang=None) or one language pack.

    The command is chosen by the package manager that is actually on PATH, rather than always assuming Debian or
    Ubuntu.
    """
    manager = _first_package_manager()
    if manager is None:
        return "Install Tesseract using whatever package manager your system provides."
    if lang is None:
        return f"Run: {manager.tesseract}"
    if "{lang}" in manager.language:
        return f"Run: {manager.language.format(lang=lang)}"
    return f"Run: {manager.language}"  # Homebrew, for example, installs all languages together.


def osd_install_hint() -> str:
    """Return a one-line, distro-appropriate hint for installing the osd data file."""
    manager = _first_package_manager()
    if manager is None:
        return "Install the Tesseract orientation data (osd) using your system's package manager."
    return f"Run: {manager.osd}"


def engine_install_command() -> Optional[str]:
    """Return the command that installs Tesseract (with English and osd), or None.

    None means that none of the known package managers is on PATH.
    """
    manager = _first_package_manager()
    return manager.engine_setup if manager else None


@dataclass(frozen=True)
class EngineStatus:
    """Whether the ``tesseract`` program can be used, and if not, why.

    Attributes:
        path: Where the program was found; None when it is not installed.
        version: The version, for example "5.3.4"; None when the program is missing or would not run.
        problem: Why a program that was found could not be used.
        searched: Where the program was looked for when it was not found (Windows only: elsewhere PATH is the only
            place). It is what the user is shown, so that "not installed" says what was checked. It is not part of
            what makes two statuses equal.
    """

    path: Optional[str]
    version: Optional[str]
    problem: Optional[str] = None
    searched: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ok(self) -> bool:
        """Whether the program was found and reported a version."""
        return self.version is not None


_VERSION_LINE = re.compile(r"^tesseract\s+v?(\d+(?:\.\d+)*)", re.IGNORECASE)


def _windows_tesseract_candidates() -> list[str]:
    """Return where the Windows installers put tesseract.exe; none of them adds it to PATH."""
    folders = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.path.join(os.environ["LOCALAPPDATA"], "Programs") if os.environ.get("LOCALAPPDATA") else None,
    ]
    return [os.path.join(folder, "Tesseract-OCR", "tesseract.exe") for folder in folders if folder]


def _bundled_tesseract_candidates() -> list[str]:
    r"""Return the copies of tesseract.exe that belong to KaramVT.

    These are the one that the Windows installer puts beside the program and the one that the install script unpacks
    into the data folder.

    The script always unpacks into ``%LOCALAPPDATA%\karamvt``, while the data folder follows ``XDG_DATA_HOME`` when it
    is set (a portable setup). On Windows the script's folder is therefore the last candidate.
    """
    program_folder = Path(__file__).resolve().parents[3]
    folders = [program_folder, data_dir()]
    if is_windows():
        installed = installed_data_dir()
        if installed is not None:
            folders.append(installed)
    # Without XDG_DATA_HOME the last two are the same folder; dict.fromkeys lists it once and keeps the order.
    return list(dict.fromkeys(str(folder / "tesseract" / "tesseract.exe") for folder in folders))


def _windows_program_search(name: str) -> Optional[str]:
    """Return where Windows itself finds a program given by name only, or None (always None off Windows).

    This is what ``CreateProcess`` does, and so what pytesseract's plain ``tesseract`` relies on when nothing else
    tells it where the program is: the folder of the running program, the current folder, the system folders and then
    PATH. PATH is read the way Windows reads it, which Python's ``shutil.which`` does not always do (a folder written
    in quotes, say).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        search = ctypes.windll.kernel32.SearchPathW
        search.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPWSTR,
                           ctypes.POINTER(wintypes.LPWSTR)]
        search.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = search(None, name, ".exe", len(buffer), buffer, None)
    except (AttributeError, OSError, ValueError):
        return None
    return buffer.value if 0 < length < len(buffer) else None


def find_tesseract() -> Optional[str]:
    """Return the path of the tesseract program, or None if it cannot be found.

    On Windows the search order is: KaramVT's own copy (a known version, whatever else is on the machine), PATH, the
    folders the Tesseract installers use, and finally wherever Windows itself would find a program called
    ``tesseract``, so that this finds it whenever running OCR would. Elsewhere it is whatever is on PATH.
    """
    if is_windows():
        bundled = next((path for path in _bundled_tesseract_candidates() if os.path.isfile(path)), None)
        if bundled is not None:
            return bundled
    found = shutil.which("tesseract")
    if found is None and is_windows():
        found = next((path for path in _windows_tesseract_candidates() if os.path.isfile(path)), None)
    if found is None and is_windows():
        found = _windows_program_search("tesseract")
    return found


def _use_found_tesseract(pytesseract_module) -> None:
    """Point pytesseract at the program that `find_tesseract` found (Windows only; elsewhere PATH decides)."""
    path = find_tesseract() if is_windows() else None
    if path:
        pytesseract_module.pytesseract.tesseract_cmd = path


# Held for the whole OCR run on Windows: TESSDATA_PREFIX is process-wide, so two runs that use different language
# folders must not overlap.
_TESSDATA_LOCK = threading.Lock()


@contextlib.contextmanager
def _tessdata_environment(language_source: str) -> Iterator[None]:
    r"""Make Tesseract read the downloaded language packs of *language_source*.

    Outside Windows the folder is passed as the ``--tessdata-dir`` option (see `_tessdata_option`). On Windows
    pytesseract splits the options string without understanding quotes, so a folder that needs them (any path with a
    space, such as C:\Users\Ali Hassan\...) arrives with the quote characters inside it. The environment variable
    carries the folder unquoted instead.

    The variable is process-wide, so it is set under a lock for the duration of the run and the previous value is
    restored afterwards. Nothing is changed for the "system" language source or outside Windows.
    """
    if not is_windows() or language_source == "system":
        yield
        return
    from . import tessdata_manager

    folder = tessdata_manager.local_tessdata_dir(language_source)
    with _TESSDATA_LOCK:
        previous = os.environ.get("TESSDATA_PREFIX")
        os.environ["TESSDATA_PREFIX"] = folder
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("TESSDATA_PREFIX", None)
            else:
                os.environ["TESSDATA_PREFIX"] = previous


def _tessdata_option(tessdata_dir: Optional[str]) -> Optional[str]:
    """Return the folder to pass as ``--tessdata-dir``; on Windows it travels in the environment instead."""
    return None if is_windows() else tessdata_dir


# --- Calling pytesseract ---
#
# tesseract.exe cannot take non-ASCII letters in the paths it is given (see ascii_paths), and pytesseract writes its
# working files into TEMP and gives their full paths to it. On Windows, when TEMP, the program or the language folder
# has such letters (a user name in Arabic), the three calls below run Tesseract through ascii_paths instead, and read
# its output with pytesseract's own parsers. In every other case they are pytesseract's own calls.

def _guarded(pytesseract_module, tessdata_dir: Optional[str]) -> bool:
    """Return whether this run must go through ascii_paths because Tesseract would be given a non-ASCII path."""
    program = getattr(getattr(pytesseract_module, "pytesseract", None), "tesseract_cmd", None)
    return ascii_paths.must_guard(program, tessdata_dir)


def _run_in_work_folder(
    pytesseract_module, image, extension: str, lang: Optional[str], config: str, tessdata_dir: Optional[str],
) -> str:
    """Return the text of Tesseract's *extension* output, raising pytesseract's exceptions.

    The callers' messages for a missing program, missing language data and so on therefore apply unchanged.
    """
    try:
        return ascii_paths.run_tesseract(
            pytesseract_module.pytesseract.tesseract_cmd, image, extension, lang, config, tessdata_dir,
        )
    except ascii_paths.ProgramNotFound as exc:
        raise pytesseract_module.TesseractNotFoundError() from exc
    except ascii_paths.TesseractFailed as exc:
        raise pytesseract_module.TesseractError(exc.returncode, exc.message) from exc


def _image_to_string(pytesseract_module, image, lang: Optional[str], config: str, tessdata_dir: Optional[str]) -> str:
    if not _guarded(pytesseract_module, tessdata_dir):
        return pytesseract_module.image_to_string(image, lang=lang, config=config)
    return _run_in_work_folder(pytesseract_module, image, "txt", lang, config, tessdata_dir)


def _image_to_data(pytesseract_module, image, lang: Optional[str], config: str, tessdata_dir: Optional[str]) -> dict:
    if not _guarded(pytesseract_module, tessdata_dir):
        return pytesseract_module.image_to_data(
            image, lang=lang, config=config, output_type=pytesseract_module.Output.DICT
        )
    raw = _run_in_work_folder(
        pytesseract_module, image, "tsv", lang, f"-c tessedit_create_tsv=1 {config.strip()}", tessdata_dir,
    )
    return pytesseract_module.pytesseract.file_to_dict(raw, "\t", -1)


def _image_to_osd(pytesseract_module, image, config: str, tessdata_dir: Optional[str]) -> dict:
    if not _guarded(pytesseract_module, tessdata_dir):
        return pytesseract_module.image_to_osd(image, config=config, output_type=pytesseract_module.Output.DICT)
    raw = _run_in_work_folder(pytesseract_module, image, "osd", "osd", f"--psm 0 {config.strip()}", tessdata_dir)
    return pytesseract_module.pytesseract.osd_to_dict(raw)


# Asking for the version starts the program, which the first time on a machine (antivirus scanning a program it has
# not seen, a cold disk) can take much longer than it does afterwards. Giving up early reported a working Tesseract as
# unusable, so this timeout is generous; the question is asked in the background (gui/ocr_engine_row.py).
ENGINE_CHECK_TIMEOUT = 30.0
# The Windows process-creation flag CREATE_NO_WINDOW; subprocess defines it by name on Windows only.
_CREATE_NO_WINDOW = 0x08000000


def _search_places() -> tuple[str, ...]:
    """Return where `find_tesseract` looks, in order (Windows only; elsewhere PATH alone decides)."""
    if not is_windows():
        return ()
    return (*_bundled_tesseract_candidates(), "PATH", *_windows_tesseract_candidates(), "Windows' own program search")


def check_engine(timeout: float = ENGINE_CHECK_TIMEOUT) -> EngineStatus:
    """Look for the ``tesseract`` program and ask it for its version.

    The program is looked for on PATH or, on Windows, where its installers put it. The check needs no image and no
    language data.

    Args:
        timeout: Seconds to wait for the program to answer.
    """
    path = find_tesseract()
    if path is None:
        return EngineStatus(None, None, searched=_search_places())
    try:
        # Started the way the OCR itself starts it: no window of its own (the application has no console, and
        # Windows would give the program one), no input to wait for, and its output read as UTF-8 whatever the
        # system's code page is.
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return EngineStatus(path, None, "it did not answer in time")
    except OSError as exc:
        return EngineStatus(path, None, exc.strerror or str(exc))
    # Older releases print the version on stderr, newer ones on stdout.
    first_line = ((result.stdout or "").strip() or (result.stderr or "").strip()).split("\n")[0].strip()
    match = _VERSION_LINE.match(first_line)
    if result.returncode != 0 or match is None:
        return EngineStatus(path, None, first_line or f"it exited with code {result.returncode}")
    return EngineStatus(path, match.group(1))


def _osd_missing_error(custom_dir: bool, reason: str = "") -> RuntimeError:
    """Return the error for when osd.traineddata is needed but cannot be loaded, with the fix in its message."""
    prefix = f"{reason}\n" if reason else ""
    if custom_dir:
        return RuntimeError(
            prefix
            + "Script/orientation data (osd) isn't downloaded for this language source.\n"
            "Download it from OCR Settings \u2192 Manage downloaded languages \u2014 no admin rights needed."
        )
    return RuntimeError(
        prefix
        + "Script/orientation data (osd) isn't installed on this system.\n"
        + osd_install_hint()
        + "\nOr switch to a downloaded language source in OCR Settings \u2014 no admin rights needed."
    )


def _import_ocr_libraries():
    """Import and return pytesseract, PIL's Image module and PIL's UnidentifiedImageError.

    The import happens when OCR is first used, so a missing package surfaces at that point as an ImportError that
    says how to install it.

    Raises:
        ImportError: pytesseract or Pillow is not installed.
    """
    try:
        import pytesseract
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise ImportError(
            "OCR requires the 'pytesseract' and 'Pillow' Python packages. "
            "See the README for install steps."
        ) from exc
    return pytesseract, Image, UnidentifiedImageError


def _tesseract_missing_error() -> RuntimeError:
    return RuntimeError("The 'tesseract' program isn't installed on this system.\n" + _install_hint())


def _local_tessdata_dir(language_source: str, psm: int) -> Optional[str]:
    """Return the folder of language data downloaded for *language_source* ("system" has none).

    Raises:
        RuntimeError: *psm* needs osd data that this folder lacks.
    """
    if language_source == "system":
        return None
    from . import tessdata_manager
    tessdata_dir = tessdata_manager.local_tessdata_dir(language_source)
    # With a custom --tessdata-dir, PSM 1 and 12 do not fail when osd is missing: Tesseract only warns and silently
    # skips orientation detection. Check up front rather than return degraded text.
    if psm_needs_osd(psm) and not tessdata_manager.has_osd(language_source):
        raise _osd_missing_error(custom_dir=True)
    return tessdata_dir


def _open_image(image_module, unidentified_image_error: type, image_path: str):
    """Open the image, turning PIL's unrecognised-format error into a ValueError that names the file."""
    try:
        return image_module.open(image_path)
    except unidentified_image_error as exc:
        raise ValueError(
            f"'{os.path.basename(image_path)}' doesn't look like a valid image file."
        ) from exc


def _legacy_engine_hint(oem: int) -> str:
    """Return what to add to a language-data error when the engine mode needs legacy-capable data."""
    if oem not in (0, 2):
        return ""
    return (
        "\nEngine mode “" + OEM_OPTIONS[oem_index(oem)][1] + "” needs legacy-capable "
        "language data — the Fast and Best sources are neural-only. "
        "Try the Standard source, or set the engine mode to Default."
    )


def _language_data_error(tess_lang: str, language_source: str, legacy_hint: str) -> RuntimeError:
    """Return the error for language data that Tesseract could not load; the advice depends on where it comes from."""
    if language_source == "system":
        return RuntimeError(
            f"Tesseract language pack '{tess_lang}' is not installed.\n"
            + _install_hint(tess_lang)
            + legacy_hint
            + "\nOr switch to a downloaded language source in OCR Settings — no admin rights needed."
        )
    from . import tessdata_manager
    if tessdata_manager.is_downloaded(language_source, tess_lang):
        return RuntimeError(
            f"Could not load the downloaded '{tess_lang}' language data."
            + (
                legacy_hint
                or "\nIt may be corrupt — try deleting and re-downloading it from "
                "OCR Settings → Manage downloaded languages."
            )
        )
    return RuntimeError(
        f"'{tess_lang}' isn't downloaded for the '{language_source}' quality "
        "tier yet.\nDownload it from OCR Settings → Manage downloaded languages."
    )


def _tesseract_error(
    exc: Exception, *, psm: int, oem: int, language_source: str, tess_lang: str, tessdata_dir: Optional[str],
) -> RuntimeError:
    """Return the error the user sees for a failed Tesseract run, with the fix in its message."""
    message = str(exc)
    # Tesseract reports these conditions only as text in its error output, so they are recognised by their wording.
    if psm_needs_osd(psm) and "'osd'" in message:
        return _osd_missing_error(custom_dir=bool(tessdata_dir))
    if "Failed loading language" in message or "Could not initialize" in message:
        return _language_data_error(tess_lang, language_source, _legacy_engine_hint(oem))
    return RuntimeError(f"Tesseract failed: {message}")


class OcrEngine:
    """Extracts the text of an image with Tesseract.

    Args:
        on_progress: Called with a short status message as the work proceeds; the default prints it.
    """

    def __init__(self, on_progress: Callable[[str], None] = print) -> None:
        self.on_progress = on_progress

    def extract_text(
        self,
        image_path: str,
        language: Optional[Language] = None,
        psm: int = DEFAULT_PSM,
        language_source: str = "system",
        oem: int = DEFAULT_OEM,
        tess_vars: Optional[list[tuple[str, str]]] = None,
    ) -> OcrResult:
        """Read the text in an image.

        Args:
            image_path: The image file to read.
            language: The language of the text. None means auto-detect: the script is read from the image (which needs
                osd data) and the language is guessed again from the extracted text.
            psm: Tesseract page segmentation mode.
            language_source: "system" to use whatever tesseract-ocr-<lang> packages are installed through the OS
                package manager, or one of tessdata_manager.QUALITY_TIERS' ids ("fast", "standard", "best") to use a
                language pack downloaded in the app instead (no sudo needed). See OCR Settings.
            oem: Tesseract OCR engine mode.
            tess_vars: Extra "-c name=value" Tesseract variables; build them from the saved settings with
                tess_vars_from_config().

        Returns:
            The text, with its mean confidence and, for auto-detect, the language guessed from it.

        Raises:
            FileNotFoundError: *image_path* does not exist.
            OcrUnsupportedError: *language* has no Tesseract language data.
            ValueError: The file is not a valid image.
            ImportError: pytesseract or Pillow is not installed.
            RuntimeError: Tesseract, the language data or the osd data is missing, or Tesseract failed; the message
                says how to fix it.
        """
        with _tessdata_environment(language_source):
            return self._extract_text(image_path, language, psm, language_source, oem, tess_vars)

    def _extract_text(
        self,
        image_path: str,
        language: Optional[Language] = None,
        psm: int = DEFAULT_PSM,
        language_source: str = "system",
        oem: int = DEFAULT_OEM,
        tess_vars: Optional[list[tuple[str, str]]] = None,
    ) -> OcrResult:
        """Do the work of `extract_text`, which calls this with the language data folder already set up."""
        pytesseract, pil_image, unidentified_image_error = _import_ocr_libraries()
        _use_found_tesseract(pytesseract)

        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"Image not found: {image_path}")

        if language is not None and not language.tesseract_code:
            raise OcrUnsupportedError(
                f"OCR isn't supported for {language.name} (no Tesseract language data for it)."
            )

        psm = normalize_psm(psm)
        tessdata_dir = _local_tessdata_dir(language_source, psm)

        img = _open_image(pil_image, unidentified_image_error, image_path)
        notice: Optional[str] = None
        with img:
            if language is not None:
                tess_lang = language.tesseract_code
                self.on_progress(f"Reading text from image ({language.name})...")
            else:
                tess_lang, notice = self._guess_tesseract_lang(pytesseract, img, language_source, tessdata_dir)

            config = build_config(psm, oem, _tessdata_option(tessdata_dir), tess_vars or [])
            try:
                text = _image_to_string(pytesseract, img, tess_lang, config, tessdata_dir)
                confidence = self._mean_confidence(pytesseract, img, tess_lang, config, tessdata_dir)
            except pytesseract.TesseractNotFoundError as exc:
                raise _tesseract_missing_error() from exc
            except pytesseract.TesseractError as exc:
                raise _tesseract_error(
                    exc, psm=psm, oem=oem, language_source=language_source,
                    tess_lang=tess_lang, tessdata_dir=tessdata_dir,
                ) from exc

        text = text.strip()
        if not text:
            confidence = None
        detected = self._detect_language(text) if language is None else None
        return OcrResult(text=text, confidence=confidence, detected_language=detected, notice=notice)

    def _detect_language(self, text: str) -> Optional[Language]:
        """Return the language that langdetect guesses for the extracted text, or None if it has no guess.

        Now that there is text, this is a much better guess than the script-only heuristic used before it.
        """
        if not text:
            return None
        try:
            detected = detect_language_from_text(text[:500])
        except ImportError:
            return None
        if detected:
            self.on_progress(f"Extracted text looks like {detected.name}.")
        return detected

    @staticmethod
    def _mean_confidence(
        pytesseract_module, image, tess_lang: str, config: str, tessdata_dir: Optional[str] = None,
    ) -> Optional[float]:
        """Return Tesseract's own estimate of how sure it is about the result, or None if there is none.

        The average covers words only: structural rows (page, block, paragraph, line) come back with conf == -1 and are
        excluded, as Tesseract's TSV output is documented to work.
        """
        try:
            data = _image_to_data(pytesseract_module, image, tess_lang, config, tessdata_dir)
        except Exception:
            # The confidence is optional; a failure here must not discard the text that was already read.
            return None
        scores = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
        return sum(scores) / len(scores) if scores else None

    def _ensure_osd_data(self, language_source: str) -> None:
        """Make sure the downloaded language data has osd.traineddata, fetching it when it is missing.

        A tessdata directory built by downloading specific languages one at a time will not have it unless it is
        fetched separately.
        """
        from . import tessdata_manager
        if tessdata_manager.has_osd(language_source):
            return
        self.on_progress("Downloading script-detection data (osd)…")
        # Progress is reported in steps of 10 percent rather than on every callback. A one-item list lets the nested
        # function update the value.
        last_step = [-1]

        def _osd_progress(percent):
            if percent is not None and percent // 10 != last_step[0]:
                last_step[0] = percent // 10
                self.on_progress(f"Downloading script-detection data (osd)… {percent}%")

        try:
            tessdata_manager.ensure_osd(language_source, progress_cb=_osd_progress)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Auto-detect needs script-detection data (osd), which couldn't be downloaded: {exc}\n"
                "Pick a source language instead, or download it later from "
                "OCR Settings → Manage downloaded languages."
            ) from exc

    def _assume_english(self, notice: str) -> tuple[str, str]:
        """Report *notice* as progress and return English with it: the fallback when the script cannot be told."""
        self.on_progress(notice)
        return "eng", notice

    def _guess_tesseract_lang(
        self, pytesseract_module, image, language_source: str, tessdata_dir: Optional[str]
    ) -> tuple[str, Optional[str]]:
        """Auto-detect: pick a Tesseract language pack from the image's *script*.

        Returns:
            (tesseract_lang, notice). *notice* is None when detection worked; otherwise it says why English was
            assumed instead, so that the UI can show it rather than silently producing garbage for non-Latin text.

        Raises:
            RuntimeError: The osd data is missing or cannot be fetched; the message says how to fix it. Guessing
                English then would just mislead. (Choosing a source language skips all of this.)
        """
        osd_config = ""
        if tessdata_dir:
            self._ensure_osd_data(language_source)
            if _tessdata_option(tessdata_dir):
                osd_config = shlex.join(["--tessdata-dir", tessdata_dir])

        self.on_progress("Detecting the image's script…")
        try:
            osd = _image_to_osd(pytesseract_module, image, osd_config, tessdata_dir)
        except pytesseract_module.TesseractNotFoundError as exc:
            raise _tesseract_missing_error() from exc
        except pytesseract_module.TesseractError as exc:
            message = str(exc)
            if "osd" in message and ("Failed loading language" in message or "Error opening data file" in message):
                raise _osd_missing_error(custom_dir=bool(tessdata_dir)) from exc
            if "Too few characters" in message:
                reason = "there isn't enough text in the image to detect its script"
            else:
                reason = "script detection failed"
            return self._assume_english(
                f"Assumed English — {reason}. Pick a source language for better accuracy."
            )

        script = osd.get("script", "")
        guess = _SCRIPT_TO_TESSERACT.get(script)
        if guess is None:
            return self._assume_english(
                f"Detected “{script}” script, which has no built-in mapping — "
                "assumed English. Pick a source language for better accuracy."
            )
        self.on_progress(
            f"No source language chosen — detected “{script}” script, "
            f"using {guess} for OCR. Pick a language above for better accuracy."
        )
        return guess, None
