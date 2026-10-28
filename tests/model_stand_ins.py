# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stand-ins for the models, so the code that drives them can run in a test without loading one.

`pipelines` and `text_translation` are the core modules themselves, imported with the model libraries replaced when
they are not installed. `fakes` puts speech recognition and translation in place of the real ones for a whole job
and keeps a note of what they were asked.
"""

import contextlib
import importlib
import os
import sys
import types
import weakref
from unittest.mock import MagicMock

import pytest

from karamvt.core.languages import LANGUAGES
from karamvt.core.models import Caption

# Third-party model libraries, replaced by mocks when they are not installed.
_HEAVY = ("faster_whisper", "faster_whisper.audio", "llama_cpp")
# The project's modules that import those libraries; they are imported afresh for every test that needs them.
_OURS = ("karamvt.core.pipelines", "karamvt.core.transcription", "karamvt.core.translation",
        "karamvt.core.text_translation", "karamvt.core.llm_loader")

# What FakeTranscriber returns for a video that has no script of its own.
SPEECH = [
    Caption(0.0, 1.0, " Hello there "), Caption(1.0, 2.5, "How are you"),
    Caption(2.5, 4.0, "Goodbye"), Caption(4.0, 5.0, "See you"),
]


class FakeModel:
    """What loading a model gives: something that can be asked whether it is still in memory."""

    def __init__(self, name):
        self.name = name


class FakeTranslator:
    """Answers "[<language>] <the line>", so a test can see which line went to which language."""

    created: list = []
    calls: list = []
    fail_after = None           # The call after this many have been answered raises.
    after_call = None           # Called with the number of lines answered so far.

    def __init__(self, source, target, model_path, on_progress=None, llm=None, sampling=None):
        self.target = target
        type(self).created.append({"source": source, "target": target, "model": model_path, "llm": llm,
                                   "sampling": sampling, "on_progress": on_progress})

    def translate(self, text, previous_translation=None):
        kind = type(self)
        if kind.fail_after is not None and len(kind.calls) >= kind.fail_after:
            raise RuntimeError("the translation model crashed")
        kind.calls.append((self.target.whisper_code, text, previous_translation))
        if kind.after_call is not None:
            kind.after_call(len(kind.calls))
        return f"[{self.target.whisper_code}] {text}"


class FakeTranscriber:
    """Speech recognition: hands out SPEECH for any video, or what `script` has for that file name."""

    script: dict = {}
    crash_after: dict = {}  # File name -> raise after this many segments have been reported.
    calls: list = []
    detected: list = []
    language = LANGUAGES["en"]

    def __init__(self, model_path, model=None, on_progress=print):
        self.model = model

    def detect_language(self, path):
        type(self).detected.append(os.path.basename(path))
        return type(self).language

    def transcribe(self, path, source_lang, cancel=None, on_segment=None, keep=None):
        kind, name = type(self), os.path.basename(path)
        kind.calls.append({"video": name, "language": source_lang.whisper_code, "keep": keep,
                           "model": getattr(self.model, "name", None), "cancel": cancel})
        captions = list(keep or [])
        speech = kind.script.get(name, SPEECH)
        for index in range(len(captions), len(speech)):  # What is kept is not transcribed again.
            if index == kind.crash_after.get(name, -1):
                raise RuntimeError("the computer went to sleep")
            captions.append(speech[index])
            if on_segment is not None:
                on_segment(speech[index])
        return captions


@contextlib.contextmanager
def _core_with_model_libraries_stubbed():
    """Replace the model libraries (Whisper, llama.cpp) with mocks when they are not installed.

    The project's modules that use them are imported afresh inside the block and forgotten after it, so that they
    bind to the stand-ins and never to a copy left over from another test.
    """
    saved = {name: sys.modules.get(name) for name in (*_HEAVY, *_OURS)}
    for name in _HEAVY:
        try:
            importlib.import_module(name)
        except ImportError:
            sys.modules[name] = MagicMock()
    for name in _OURS:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


@pytest.fixture
def pipelines():
    """The pipelines module, with its model loading and translation replaced."""
    with _core_with_model_libraries_stubbed():
        module = importlib.import_module("karamvt.core.pipelines")
        module.load_llm = lambda *_args, **_kwargs: object()
        module.resolve_llm_gpu_layers = lambda _device: 0
        module.TranslationEngine = FakeTranslator
        yield module


@pytest.fixture
def text_translation():
    """The module of the Text tab's translation engine."""
    with _core_with_model_libraries_stubbed():
        yield importlib.import_module("karamvt.core.text_translation")


@pytest.fixture
def fakes(pipelines):
    """Speech recognition and translation as stand-ins, with a note of how they were loaded and what they were asked."""
    state = types.SimpleNamespace(
        translator=type("Translator", (FakeTranslator,), {"created": [], "calls": [], "fail_after": None,
                                                           "after_call": None}),
        transcriber=type("Transcriber", (FakeTranscriber,), {"script": {}, "crash_after": {}, "calls": [],
                                                              "detected": []}),
        whisper_loads=[], llm_loads=[], devices=[], whisper_refs=[], llm_refs=[], whisper_held_when_llm_loads=[],
    )

    def load_whisper(path, **kwargs):
        state.whisper_loads.append(dict(kwargs, path=path))
        model = FakeModel("the whisper model")
        state.whisper_refs.append(weakref.ref(model))
        return model

    def load_llm(path, **kwargs):
        state.llm_loads.append(dict(kwargs, path=path))
        state.whisper_held_when_llm_loads.append(any(ref() is not None for ref in state.whisper_refs))
        model = FakeModel("the translation model")
        state.llm_refs.append(weakref.ref(model))
        return model

    def whisper_device(setting):
        state.devices.append(setting)
        return "cpu", "int8"

    pipelines.load_whisper_model = load_whisper
    pipelines.resolve_whisper_device = whisper_device
    pipelines.TranscriptionEngine = state.transcriber
    pipelines.load_llm = load_llm
    pipelines.resolve_llm_gpu_layers = lambda setting: 7
    pipelines.TranslationEngine = state.translator
    return state
