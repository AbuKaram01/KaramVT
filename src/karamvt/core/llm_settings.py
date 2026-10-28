# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""User-tunable sampling settings for the translation model.

The settings are deliberately limited to *how the model picks words* (temperature, top-p, ...). The prompt, the
language handling and the prompt-injection defenses (core.llm_safety) are fixed, so no setting here can turn the app
into something other than a translator. The speech-recognition (Whisper) model has no settings at all; it is tuned
once, in core.transcription.

The defaults reproduce what the app did before these settings existed: temperature 0.1 (close to deterministic,
which suits translation) and llama-cpp-python's own defaults for everything else.
"""

from __future__ import annotations

import math

# One row per control in Preferences, and the single source of truth for clamping values read back from the config:
# (AppConfig field, create_chat_completion keyword, title, subtitle, minimum, maximum, step, digits, default).
LLM_PARAMS: list[tuple[str, str, str, str, float, float, float, int, float]] = [
    ("llm_temperature", "temperature", "Temperature",
     "Lower is more literal and consistent; higher is freer but riskier. 0 always picks the likeliest word",
     0.0, 1.5, 0.05, 2, 0.1),
    ("llm_top_p", "top_p", "Top-p",
     "Only consider the smallest set of likely words whose probabilities add up to this value",
     0.05, 1.0, 0.05, 2, 0.95),
    ("llm_top_k", "top_k", "Top-k",
     "Only consider this many likeliest words at each step. 0 turns the limit off",
     0, 200, 1, 0, 40),
    ("llm_min_p", "min_p", "Min-p",
     "Ignore words less than this fraction as likely as the best one. 0 turns it off",
     0.0, 1.0, 0.01, 2, 0.05),
    ("llm_repeat_penalty", "repeat_penalty", "Repeat penalty",
     "Discourages repeating words. 1.0 means no penalty; values above about 1.2 can distort translations",
     1.0, 1.5, 0.01, 2, 1.0),
    ("llm_seed", "seed", "Seed",
     "A fixed number makes results repeatable. 0 means a different random seed every time",
     0, 2_147_483_647, 1, 0, 0),
]


def _clean(value, minimum: float, maximum: float, default: float) -> float:
    """Return *value* as a float clamped to the allowed range, or *default* if it is not a usable number.

    Unusable values (None, non-numeric text, NaN, infinity) can come from a hand-edited config file.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return min(max(number, minimum), maximum)


def sampling_from_config(cfg) -> dict:
    """Return the ``create_chat_completion`` keyword arguments implied by the saved settings.

    The result is always valid, whatever the config file contains. A seed of 0 becomes ``None`` (a random seed).
    """
    out: dict = {}
    for field, kwarg, _title, _sub, minimum, maximum, _step, _digits, default in LLM_PARAMS:
        value = _clean(getattr(cfg, field, default), minimum, maximum, default)
        if kwarg in ("top_k", "seed"):
            value = int(value)
        out[kwarg] = value
    if out["seed"] == 0:
        # llama-cpp-python expects None, not 0, for a random seed.
        out["seed"] = None
    return out


def default_sampling() -> dict:
    """Return the built-in settings in the same form as sampling_from_config()."""
    out = {kwarg: default for _f, kwarg, _t, _s, _mn, _mx, _st, _d, default in LLM_PARAMS}
    out["top_k"] = int(out["top_k"])
    out["seed"] = None
    return out
