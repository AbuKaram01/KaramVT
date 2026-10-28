# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persistent application configuration.

Model paths are machine-specific, so they are never hard-coded in the
source tree. They live in a small JSON file under the user's config
directory and can be edited from the GUI's Preferences window.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .core.history import DEFAULT_LIMIT, MAX_LIMIT, MIN_LIMIT
from .core.paths import config_dir, default_models_dir

APP_CONFIG_DIR = config_dir()
APP_CONFIG_FILE = APP_CONFIG_DIR / "config.json"


@dataclass
class AppConfig:
    """The user's saved settings, as one flat record serialised to ``config.json``.

    Use ``load()`` to read the file and ``save()`` to write it. To change a few settings from a window,
    prefer ``update_saved_config()``, which re-reads the file first so that concurrent edits are not lost.
    Fields are grouped below by feature; the comments above them describe the non-obvious ones.
    """

    whisper_model_path: str = ""
    llm_model_path: str = ""
    # Where "Download models" (Preferences) puts new models; empty = the default folder
    # (core.paths.default_models_dir). Models already on disk can live anywhere: the two
    # paths above are what the app actually loads.
    models_dir: str = ""
    # Set once the first-run welcome guide has been shown (or was not needed), so it never reappears.
    welcome_done: bool = False
    # A desktop notification when a video job or a text translation ends while the window is
    # not in front (Preferences → Notifications). On by default: a long job is the very case
    # where nobody is watching the window.
    notify_when_done: bool = True
    # Keep every finished text translation (Preferences → History), up to this many. Turning it off also
    # stops the text in the two boxes from being remembered between sessions.
    history_enabled: bool = True
    history_limit: int = DEFAULT_LIMIT
    cpu_threads: int = 4
    processing_device: str = "auto"  # "auto" | "cpu" | "gpu"
    # --- Saved progress ---
    # Continue an interrupted *transcription* from where it stopped (core.transcription).
    # Off by default: the join point can occasionally cut or repeat a word, so it is an
    # explicit opt-in (Preferences → Saved Progress), unlike translation resume, which
    # is exact.
    resume_transcription: bool = False

    # --- Translation model: advanced sampling (core.llm_settings; Preferences) ---
    # The defaults are the app's original behaviour. There is intentionally no
    # setting for the prompt, and none for the Whisper model.
    llm_temperature: float = 0.1
    llm_top_p: float = 0.95
    llm_top_k: int = 40
    llm_min_p: float = 0.05
    llm_repeat_penalty: float = 1.0
    llm_seed: int = 0  # 0 = random every time

    # --- OCR (Import from image): edited from the gear button's "OCR Settings" ---
    ocr_psm: int = 6  # Tesseract page segmentation mode — see core.ocr_options.PSM_OPTIONS
    ocr_oem: int = 3  # Tesseract engine mode — see core.ocr_options.OEM_OPTIONS
    ocr_language_source: str = "system"  # "system" | one of core.tessdata_manager.QUALITY_TIERS' ids
    # Named Tesseract config variables (core.ocr_options.TESS_TOGGLES) — each is only
    # passed to Tesseract when switched on; off leaves Tesseract's own default.
    ocr_preserve_spaces: bool = False
    ocr_chop_enable: bool = False
    ocr_use_new_state_cost: bool = False
    ocr_segment_segcost_rating: bool = False
    ocr_enable_new_segsearch: bool = False
    ocr_language_model_ngram_on: bool = False
    ocr_textord_force_make_prop_words: bool = False
    ocr_edges_max_children_enabled: bool = False
    ocr_edges_max_children_value: int = 40
    ocr_custom_params: str = ""  # raw "key=value; key2=value2" pairs

    def models_folder(self) -> Path:
        """The folder downloaded models go to: the saved one, or the default."""
        return Path(self.models_dir).expanduser() if self.models_dir.strip() else default_models_dir()

    @property
    def history_keep(self) -> int:
        """How many translations to keep: the saved limit, clamped to the allowed range.

        Clamping means that a hand-edited file cannot make the history delete everything.
        """
        return min(max(self.history_limit, MIN_LIMIT), MAX_LIMIT)

    @property
    def whisper_ready(self) -> bool:
        """Whether the chosen speech-recognition model exists on disk."""
        return bool(self.whisper_model_path) and os.path.isdir(self.whisper_model_path)

    @property
    def llm_ready(self) -> bool:
        """Whether the chosen translation model exists on disk."""
        return bool(self.llm_model_path) and os.path.isfile(self.llm_model_path)

    @property
    def models_ready(self) -> bool:
        """Whether both models exist on disk, so a video or a text can be translated."""
        return self.whisper_ready and self.llm_ready

    @property
    def is_configured(self) -> bool:
        """Whether both model paths have been set (not whether they're valid — see validation_errors)."""
        return bool(self.whisper_model_path) and bool(self.llm_model_path)

    def validation_errors(self) -> list[str]:
        """Human-readable problems with the current model paths. Empty list = OK to run.

        Catches the case where a path was set (e.g. by hand-editing the config
        file, or after the referenced files were moved/deleted) but doesn't
        actually exist on disk — better to fail here than after a model has
        already started loading.
        """
        errors: list[str] = []

        if not self.whisper_model_path:
            errors.append("No Faster-Whisper model selected.")
        elif not os.path.isdir(self.whisper_model_path):
            errors.append(f"Whisper model directory not found: {self.whisper_model_path}")

        if not self.llm_model_path:
            errors.append("No translation model selected.")
        elif not os.path.isfile(self.llm_model_path):
            errors.append(f"LLM model file not found: {self.llm_model_path}")

        return errors

    @classmethod
    def load(cls) -> "AppConfig":
        """Return the saved settings.

        Never raises and never returns a value of the wrong type. A missing or unreadable file gives the
        defaults; a file whose content is not a JSON object is also set aside as ``config.json.broken`` rather
        than silently overwritten. Any single field holding the wrong kind of value (for example in a
        hand-edited file) falls back to its default instead of crashing whatever window reads it next.
        """
        defaults = asdict(cls())
        try:
            text = APP_CONFIG_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except OSError:
            return cls()
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            try:
                os.replace(APP_CONFIG_FILE, APP_CONFIG_FILE.with_name(APP_CONFIG_FILE.name + ".broken"))
            except OSError:
                pass
            return cls()
        for key, value in data.items():
            if key in defaults and _same_kind(value, defaults[key]):
                defaults[key] = float(value) if isinstance(defaults[key], float) else value
        return cls(**defaults)

    def save(self) -> None:
        """Write the settings to disk atomically.

        The data goes to a temporary file that is then renamed over the real one, so a crash or power cut
        mid-save can never leave a half-written (unreadable) settings file.
        """
        APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = APP_CONFIG_FILE.with_name(APP_CONFIG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, APP_CONFIG_FILE)


def update_saved_config(**changes) -> None:
    """Change some settings in the file *as it is now* and save it.

    Reading again just before writing means a window never undoes a change made somewhere else
    (Preferences and the OCR Settings dialog write to the same file). A name that is not a
    setting is a bug in the caller, so it raises instead of quietly adding an attribute.
    """
    config = AppConfig.load()
    for name, value in changes.items():
        if not hasattr(config, name):
            raise AttributeError(f"{name!r} is not a setting")
        setattr(config, name, value)
    config.save()


# The start of every message validation_errors() can return, so the window can offer a
# "Download Models" button on the toast that shows one of them.
_MODEL_ERROR_PREFIXES = ("No Faster-Whisper model", "No translation model", "Whisper model directory not found",
                         "LLM model file not found")


def is_model_problem(message: str) -> bool:
    """Whether *message* (one of validation_errors()) is about a missing model."""
    return message.startswith(_MODEL_ERROR_PREFIXES)


def _same_kind(value, default) -> bool:
    """Return whether *value* is an acceptable replacement for a field whose default is *default*.

    bool is checked first because it is a subclass of int; ints are accepted for float fields.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value  # not NaN
    if isinstance(default, str):
        return isinstance(value, str)
    return True
