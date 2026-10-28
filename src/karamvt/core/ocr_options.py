# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The choices the OCR Settings dialog offers, and how saved settings become a Tesseract command line.

The module holds pure data and pure functions; nothing here runs Tesseract. core/ocr.py runs it (and re-exports
these names for the code that already imports them from there); gui/ocr_settings_dialog.py builds its rows from the
tables below.
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

# --- Engine mode (OEM) ---
# Each entry is (value, short_label, full_description). The short label is what
# the combo row shows; the description appears as its subtitle once selected.
OEM_OPTIONS: list[tuple[int, str, str]] = [
    (0, "Legacy only", (
        "Legacy engine only \u2014 the older, non-neural engine. Needs legacy-capable data (the Standard source); "
        "Fast and Best data are neural-only"
    )),
    (1, "Neural net (LSTM)", "Neural net (LSTM) only \u2014 usually the most accurate option"),
    (2, "Legacy + LSTM", (
        "Legacy + LSTM combined \u2014 runs both and merges results. "
        "Needs legacy-capable data (the Standard source)"
    )),
    (3, "Default", "Default \u2014 whichever engine is available on this system"),
]
DEFAULT_OEM = 3

# --- Page segmentation mode (PSM) ---
# PSM 0 ("OSD only") and PSM 2 ("Auto, no OSD/OCR") are deliberately absent:
# neither one produces any text, so offering them only led to empty or failed
# runs. PSM 6 is this app's default rather than Tesseract's own PSM 3 — for a
# cropped screenshot or photo of a single paragraph (this feature's actual,
# typical use case), PSM 3's layout analysis has no real page structure to
# work with and reliably does worse than just being told "it's one block".
PSM_OPTIONS: list[tuple[int, str, str]] = [
    (1, "Auto + OSD", "Automatic page segmentation with orientation detection (needs osd data)"),
    (3, "Fully automatic", "Fully automatic page segmentation \u2014 best for a full page or complex layout"),
    (4, "Single column", "Assume a single column of text of variable sizes"),
    (5, "Single vertical block", "Assume a single uniform block of vertically aligned text"),
    (6, "Single block", (
        "Assume a single uniform block of text \u2014 best for a cropped paragraph or screenshot (default)"
    )),
    (7, "Single line", "Treat the image as a single text line"),
    (8, "Single word", "Treat the image as a single word"),
    (9, "Single word (circle)", "Treat the image as a single word in a circle"),
    (10, "Single character", "Treat the image as a single character"),
    (11, "Sparse text", "Sparse text \u2014 find as much text as possible, in no particular order"),
    (12, "Sparse text + OSD", "Sparse text with orientation detection (needs osd data)"),
    (13, "Raw line", "Raw line \u2014 bypass Tesseract-specific heuristics"),
]
DEFAULT_PSM = 6

# Modes that make Tesseract itself load osd.traineddata. (Auto-detect of the
# *source language* needs osd too, but through a separate call — see
# OcrEngine._guess_tesseract_lang.)
PSM_NEEDS_OSD = frozenset({1, 12})
_VALID_PSM_VALUES = frozenset(v for v, _l, _d in PSM_OPTIONS)


def psm_needs_osd(value: int) -> bool:
    """True if this page segmentation mode requires osd.traineddata."""
    return value in PSM_NEEDS_OSD


def normalize_psm(value) -> int:
    """*value* if it is a selectable PSM, otherwise DEFAULT_PSM."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PSM
    return value if value in _VALID_PSM_VALUES else DEFAULT_PSM


def psm_index(value) -> int:
    """Index of *value* (normalized) in PSM_OPTIONS — for UI combo rows."""
    normalized = normalize_psm(value)
    return next(i for i, (v, _l, _d) in enumerate(PSM_OPTIONS) if v == normalized)


def oem_index(value) -> int:
    """Index of *value* in OEM_OPTIONS, falling back to the default mode."""
    return next(
        (i for i, (v, _l, _d) in enumerate(OEM_OPTIONS) if v == value),
        next(i for i, (v, _l, _d) in enumerate(OEM_OPTIONS) if v == DEFAULT_OEM),
    )


# --- Named Tesseract variables ---
# (AppConfig field, Tesseract variable). Each is passed as "-c var=1" only when
# its switch is on; left off, Tesseract's own built-in default applies. Kept as
# raw variable names (not friendly prose) since that's exactly what someone
# tuning these already knows to look for.
TESS_TOGGLES: list[tuple[str, str]] = [
    ("ocr_chop_enable", "chop_enable"),
    ("ocr_use_new_state_cost", "use_new_state_cost"),
    ("ocr_segment_segcost_rating", "segment_segcost_rating"),
    ("ocr_enable_new_segsearch", "enable_new_segsearch"),
    ("ocr_language_model_ngram_on", "language_model_ngram_on"),
    ("ocr_textord_force_make_prop_words", "textord_force_make_prop_words"),
]

_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_custom_params(raw: str) -> list[tuple[str, str]]:
    """Parse the free-text "key=value" list into (key, value) pairs, one -c flag each.

    Entries are separated by semicolons and/or newlines. Blank or malformed entries (no "=", or a name that is not
    a plain identifier) are skipped rather than raising, because this is hand-typed advanced configuration.
    """
    pairs: list[tuple[str, str]] = []
    if not raw:
        return pairs
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key, value = key.strip(), value.strip()
        if _VAR_NAME_RE.match(key):
            pairs.append((key, value))
    return pairs


def tess_vars_from_config(cfg) -> list[tuple[str, str]]:
    """Every "-c" variable implied by the saved OCR settings, in order.

    Custom parameters go last on purpose: Tesseract uses the last value it
    sees for a repeated variable, so typing "chop_enable=0" there can force
    off something a toggle above would otherwise switch on.
    """
    out: list[tuple[str, str]] = []
    if cfg.ocr_preserve_spaces:
        out.append(("preserve_interword_spaces", "1"))
    for field, var_name in TESS_TOGGLES:
        if getattr(cfg, field):
            out.append((var_name, "1"))
    if cfg.ocr_edges_max_children_enabled:
        out.append(("edges_max_children_per_outline", str(int(cfg.ocr_edges_max_children_value))))
    out.extend(parse_custom_params(cfg.ocr_custom_params))
    return out


def build_config(psm: int, oem: int, tessdata_dir: Optional[str], tess_vars) -> str:
    """The single ``config`` string handed to pytesseract, which splits it again with shlex."""
    args = ["--psm", str(psm), "--oem", str(oem)]
    if tessdata_dir:
        args += ["--tessdata-dir", tessdata_dir]
    for name, value in tess_vars:
        args += ["-c", f"{name}={value}"]
    return shlex.join(args)
