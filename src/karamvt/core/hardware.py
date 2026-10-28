# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Translate the user's processing-device setting into engine parameters.

The setting is one of ``"auto"``, ``"cpu"`` or ``"gpu"``. ``"auto"`` selects the GPU only when a CUDA device is
detected, so the same configuration works on machines with and without one.
"""
from __future__ import annotations


def gpu_available() -> bool:
    """Return whether a usable CUDA GPU is present (best effort).

    Reuses ctranslate2's own device detection, which is already a transitive dependency through faster-whisper,
    instead of adding a dependency for this check. Any failure yields False: a machine with no GPU, no drivers or
    a CPU-only build must never crash here.
    """
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def resolve_whisper_device(processing_device: str) -> tuple[str, str]:
    """Map a processing-device setting to the ``(device, compute_type)`` pair for faster-whisper's WhisperModel."""
    if processing_device == "cpu":
        return "cpu", "int8"
    if processing_device == "gpu":
        return "cuda", "float16"
    # "auto": use CUDA only when it is actually available.
    return ("cuda", "float16") if gpu_available() else ("cpu", "int8")


def resolve_llm_gpu_layers(processing_device: str) -> int:
    """Map a processing-device setting to llama.cpp's ``n_gpu_layers``.

    0 keeps every layer on the CPU. -1 is llama.cpp's convention for "offload as many layers as fit on the GPU".
    """
    if processing_device == "cpu":
        return 0
    if processing_device == "gpu":
        return -1
    # "auto": offload only when a GPU is actually available.
    return -1 if gpu_available() else 0
