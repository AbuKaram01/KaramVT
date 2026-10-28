# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guards the dependency cap that keeps video transcription working."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyav_is_capped_below_19():
    # faster-whisper 1.2.1 calls av.open(..., metadata_errors="ignore"), and PyAV 19 removed that argument, so an
    # uncapped install breaks every video transcription.
    for name in ("pyproject.toml", "requirements.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"""["']?av>=11,<19["']?""", text), f"{name} must pin av>=11,<19"
