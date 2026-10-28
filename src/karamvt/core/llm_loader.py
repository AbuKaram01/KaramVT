# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared GGUF model loading, used by every translation engine.

Loading a model is the expensive step (sometimes several seconds), so it lives in one function that behaves the same
everywhere. Callers can then load a model once and reuse it across many translate() calls, target languages or files
in a batch, instead of reloading it each time.
"""

from __future__ import annotations

import os
from typing import Callable

from llama_cpp import Llama


def load_llm(
    model_path: str,
    context_window: int,
    cpu_threads: int = 4,
    n_gpu_layers: int = 0,
    on_progress: Callable[[str], None] = print,
) -> Llama:
    """Load a GGUF model with llama-cpp-python and return it.

    Args:
        model_path: Path of the ``.gguf`` file.
        context_window: Context size in tokens (``n_ctx``).
        cpu_threads: Number of CPU threads; values below 1 are raised to 1.
        n_gpu_layers: Layers to offload to the GPU; 0 keeps the model on the CPU, any other value uses the GPU.
        on_progress: Receives a status line before the (slow) load starts.
    """
    model_name = os.path.basename(model_path)
    device_note = "GPU" if n_gpu_layers != 0 else "CPU"
    on_progress(f"Loading LLM: {model_name} ({device_note})...")
    return Llama(
        model_path=model_path,
        n_ctx=context_window,
        n_threads=max(1, cpu_threads),
        n_gpu_layers=n_gpu_layers,
        verbose=False,
    )
