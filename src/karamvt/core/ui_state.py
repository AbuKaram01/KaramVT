# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the interface looked like when the app was last used, so that it opens the same way next time.

This covers the window size, the tab that was open, the choices made on each tab and the text that was in the boxes.
It is not the same thing as AppConfig: the settings are what the user decided in Preferences, while this is the app
remembering where they left off. It lives in its own file, under the XDG *state* folder, so clearing one never
touches the other.

Nothing here can get in the way of starting the app. A missing, unreadable or hand-edited file gives the defaults (a
broken file is set aside as state.json.broken rather than silently overwritten), a single wrong value falls back to
its own default instead of discarding the rest, and saving never raises: remembering is a convenience, and a full
disk must not take the window down with it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import state_dir

log = logging.getLogger(__name__)

# Written into the file, so a later version of the app that changes the layout can tell which one it is reading.
STATE_VERSION = 1
STATE_FILE_NAME = "state.json"

TABS = ("video", "text")
VIDEO_MODES = ("transcribe", "translate")
OUTPUT_FORMATS = ("srt", "vtt")

DEFAULT_WINDOW_WIDTH = 760
DEFAULT_WINDOW_HEIGHT = 720
# A saved size outside these bounds is a damaged file or a different screen, not something worth honouring.
MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT = 360, 300
MAX_WINDOW_SIZE = 16384
# The Text tab accepts 5000 characters; room for more means a file edited by hand cannot make the app read megabytes.
MAX_TEXT_CHARS = 20_000
MAX_LANGUAGES = 200
MAX_PATH_CHARS = 4096


@dataclass
class WindowState:
    """The window geometry.

    Attributes:
        width: Width of the window when it is not maximised; what it returns to when un-maximised.
        height: Height of the window when it is not maximised.
        maximized: Whether the window was maximised.
    """

    width: int = DEFAULT_WINDOW_WIDTH
    height: int = DEFAULT_WINDOW_HEIGHT
    maximized: bool = False


@dataclass
class VideoState:
    """The choices made on the Video tab.

    Attributes:
        batch: Whether batch mode (a folder instead of a single file) is on.
        input_path: The chosen file, or the chosen folder in batch mode.
        mode: One of VIDEO_MODES.
        source_language: A name from the language picker; empty means nothing is remembered.
        target_languages: Names of the chosen target languages.
        output_format: One of OUTPUT_FORMATS.
        output_dir: Folder for the output files; empty means next to each input file.
    """

    batch: bool = False
    input_path: str = ""
    mode: str = "transcribe"
    source_language: str = ""
    target_languages: list[str] = field(default_factory=list)
    output_format: str = "srt"
    output_dir: str = ""


@dataclass
class TextState:
    """The choices and contents of the Text tab.

    Attributes:
        source_language: A name from the language picker; empty means nothing is remembered.
        target_language: A name from the language picker; empty means nothing is remembered.
        input_text: Contents of the input box. Like output_text, it is only written while saving translations is on
            (Preferences > History): the text the user translates stays on this computer only if they allow it,
            the same promise the history makes.
        output_text: Contents of the output box; see input_text.
        image_dir: The folder the last image was imported from.
    """

    source_language: str = ""
    target_language: str = ""
    input_text: str = ""
    output_text: str = ""
    image_dir: str = ""


@dataclass
class UiState:
    """Everything the app remembers about the interface between runs.

    Attributes:
        window: The window geometry.
        tab: The open tab; one of TABS.
        video: The choices made on the Video tab.
        text: The choices and contents of the Text tab.
    """

    window: WindowState = field(default_factory=WindowState)
    tab: str = "video"
    video: VideoState = field(default_factory=VideoState)
    text: TextState = field(default_factory=TextState)

    def to_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-serialisable dict that includes the format version."""
        return {
            "version": STATE_VERSION,
            "window": _dump(self.window),
            "tab": self.tab,
            "video": _dump(self.video),
            "text": _dump(self.text),
        }

    @classmethod
    def from_dict(cls, data: object) -> "UiState":
        """Return the state that the contents of a state file describe.

        Never raises: whatever is missing or of the wrong kind is replaced by its default, field by field, so one bad
        value costs one setting and not all of them.
        """
        source = data if isinstance(data, dict) else {}
        state = cls(
            window=_read(WindowState, source.get("window")),
            tab=source["tab"] if isinstance(source.get("tab"), str) else "video",
            video=_read(VideoState, source.get("video")),
            text=_read(TextState, source.get("text")),
        )
        return _clean(state)


class StateStore:
    """Reads and writes the state file.

    The location is settled when the store is made (the XDG state folder as it is then, or the ``path`` given), so
    one store always means one file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else state_dir() / STATE_FILE_NAME
        # What the file holds (as far as this object knows), so saving an unchanged state writes nothing.
        self._written: str | None = None

    @property
    def path(self) -> Path:
        """The location of the state file."""
        return self._path

    def load(self) -> UiState:
        """Return the saved state, or the defaults if there is none or the file cannot be used.

        A file that is not valid JSON, or not a JSON object, is set aside as ``<name>.broken``.
        """
        path = self.path
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Nothing saved yet means the defaults, so there is no need to write them.
            self._written = _serialise(UiState())
            return UiState()
        except (OSError, UnicodeError) as error:
            log.warning("Could not read %s: %s", path, error)
            return UiState()
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            self._set_aside(path)
            return UiState()
        state = UiState.from_dict(data)
        self._written = _serialise(state)
        return state

    def save(self, state: UiState) -> bool:
        """Write *state* and return whether the file now holds it; a failure is logged, never raised."""
        text = _serialise(_clean(state))
        if text == self._written:
            return True
        path = self.path
        temporary = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Private from the first byte: the file can hold text the user was translating.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            # Atomic replacement: a crash never leaves half a file.
            os.replace(temporary, path)
        except (OSError, ValueError) as error:  # Text that cannot be encoded raises ValueError.
            log.warning("Could not save %s: %s", path, error)
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return False
        self._written = text
        return True

    @staticmethod
    def _set_aside(path: Path) -> None:
        """Rename an unusable state file to ``<name>.broken`` so that it is kept instead of overwritten."""
        try:
            os.replace(path, path.with_name(path.name + ".broken"))
        except OSError:
            pass


# --- Reading and checking ---

def _serialise(state: UiState) -> str:
    return json.dumps(state.to_dict(), indent=2, ensure_ascii=False)


def _dump(section: Any) -> dict[str, Any]:
    return {f.name: getattr(section, f.name) for f in fields(section)}


def _default_of(item: Any) -> Any:
    return item.default if item.default is not MISSING else item.default_factory()


def _usable(value: object, default: object) -> bool:
    """Return True if *value* is the kind of thing a field whose default is *default* holds.

    bool is checked before int because it is a subclass of int.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def _read(cls: type, data: object) -> Any:
    """Build a section dataclass from parsed JSON, keeping the usable values and defaulting the rest."""
    source = data if isinstance(data, dict) else {}
    values = {}
    for item in fields(cls):
        if item.name in source and _usable(source[item.name], _default_of(item)):
            values[item.name] = source[item.name]
    return cls(**values)


def _choice(value: str, allowed: tuple[str, ...], default: str) -> str:
    return value if value in allowed else default


def _clean(state: UiState) -> UiState:
    """Return the same state with every value brought inside what the interface can use.

    Sizes are limited to their bounds, choices must exist, and text and lists are cut to a sane length.
    """
    window, video, text = state.window, state.video, state.text
    return UiState(
        window=WindowState(
            width=min(max(window.width, MIN_WINDOW_WIDTH), MAX_WINDOW_SIZE),
            height=min(max(window.height, MIN_WINDOW_HEIGHT), MAX_WINDOW_SIZE),
            maximized=window.maximized,
        ),
        tab=_choice(state.tab, TABS, "video"),
        video=VideoState(
            batch=video.batch,
            input_path=video.input_path[:MAX_PATH_CHARS],
            mode=_choice(video.mode, VIDEO_MODES, "transcribe"),
            source_language=video.source_language[:MAX_PATH_CHARS],
            target_languages=list(dict.fromkeys(video.target_languages))[:MAX_LANGUAGES],
            output_format=_choice(video.output_format, OUTPUT_FORMATS, "srt"),
            output_dir=video.output_dir[:MAX_PATH_CHARS],
        ),
        text=TextState(
            source_language=text.source_language[:MAX_PATH_CHARS],
            target_language=text.target_language[:MAX_PATH_CHARS],
            input_text=text.input_text[:MAX_TEXT_CHARS],
            output_text=text.output_text[:MAX_TEXT_CHARS],
            image_dir=text.image_dir[:MAX_PATH_CHARS],
        ),
    )
