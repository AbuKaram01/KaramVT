# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Small value types shared by the core pipelines and the GUI.

Holds the enums that describe a job (mode, processing device, output format), the caption record and the single
function that decides where an output subtitle file is written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, auto


class PipelineMode(Enum):
    """What a subtitle job produces: a transcript only, or a transcript plus a translation."""

    TRANSCRIBE_ONLY = auto()
    TRANSCRIBE_AND_TRANSLATE = auto()


class ProcessingDevice(Enum):
    """The hardware a model should run on, as stored in the configuration file.

    The values are the strings persisted in ``processing_device``; core.hardware maps them to concrete backends.
    """

    AUTO = "auto"
    CPU = "cpu"
    GPU = "gpu"

    @property
    def label(self) -> str:
        """The name shown in the Preferences device selector."""
        return {"auto": "Auto", "cpu": "CPU", "gpu": "GPU"}[self.value]


class OutputFormat(Enum):
    """A supported subtitle file format; the value is the file extension including the leading dot."""

    SRT = ".srt"
    VTT = ".vtt"

    @property
    def extension(self) -> str:
        """The file extension, including the leading dot."""
        return self.value

    @property
    def label(self) -> str:
        """The upper-case format name without the dot, for display (".srt" becomes "SRT")."""
        return self.value[1:].upper()


# Extensions recognised as subtitle (caption) input files.
SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({".srt", ".vtt"})


@dataclass
class Caption:
    """One timed subtitle cue.

    Attributes:
        start: Cue start, in seconds from the beginning of the media.
        end: Cue end, in seconds from the beginning of the media.
        text: The cue text; line breaks inside a cue are kept.
    """

    start: float
    end: float
    text: str


def build_output_path(input_path: str, language_code: str, output_format: OutputFormat) -> str:
    """Return the output subtitle path: the input path with ``_<language_code>`` and the format extension.

    This is the only place that decides output file names. It is shared by the pipelines, which call it once the
    actual resolved language is known, and by the GUI, which calls it ahead of time where possible to warn before an
    existing file is silently overwritten.
    """
    base, _ = os.path.splitext(input_path)
    return f"{base}_{language_code}{output_format.extension}"
