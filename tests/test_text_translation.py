# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The engine behind the Text tab (core/text_translation.py).

Covers what the model is asked, how its streamed answer is put together and shown while it arrives, and what is done
with an answer that cannot be trusted.
"""

import re

import pytest

from karamvt.core.cancel import CancelToken, OperationCancelled
from karamvt.core.languages import LANGUAGES
from karamvt.core.llm_settings import default_sampling

ENGLISH, ARABIC = LANGUAGES["en"], LANGUAGES["ar"]


def piece(text, finish=None):
    """Build one streamed chunk that carries ``text`` and, when given, the ``finish`` reason."""
    chunk = {"choices": [{"delta": {"content": text}}]}
    if finish:
        chunk["choices"][0]["finish_reason"] = finish
    return chunk


class FakeStream:
    """The streamed answer of the stand-in model: chunks as they are produced, and a way to stop it early.

    A string in ``chunks`` becomes a text chunk, with ``{d}`` replaced by the tag that the engine put around the text
    to translate. An exception in ``chunks`` is raised when the stream reaches it.
    """

    def __init__(self, chunks, delimiter):
        self.chunks = [
            piece(chunk.replace("{d}", delimiter)) if isinstance(chunk, str) else chunk for chunk in chunks
        ]
        # Whether the engine stopped the model by closing the stream.
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeLlm:
    """A stand-in for the llama.cpp model: records the requests and streams ``chunks``, or raises ``error`` instead."""

    def __init__(self, chunks=(), error=None):
        self.chunks, self.error = list(chunks), error
        self.requests: list[dict] = []
        self.streams: list[FakeStream] = []

    def create_chat_completion(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        # Every engine picks its own random tag, so read it from the request, where the real model would find it.
        delimiter = re.search(r"<(KVT_[0-9a-f]+)>", request["messages"][1]["content"]).group(1)
        self.streams.append(FakeStream(self.chunks, delimiter))
        return self.streams[-1]


def translate(text_translation, chunks=(), text="Hello world", cancel=None, error=None, sampling=None, on_partial=True):
    """Translate ``text`` with a stand-in model; return the result, what was shown, the engine's log and the model."""
    llm = FakeLlm(chunks, error)
    log, shown = [], []
    engine = text_translation.TextTranslationEngine(
        ENGLISH, ARABIC, "model.gguf", llm=llm, on_progress=log.append, sampling=sampling,
    )
    result = engine.translate(text, on_partial=shown.append if on_partial else None, cancel=cancel)
    return result, shown, log, llm


def test_a_text_without_words_is_given_back_without_asking_the_model(text_translation):
    for text in ("", "  \n\t "):
        result, shown, _log, llm = translate(text_translation, text=text)
        assert result == text and shown == [] and llm.requests == []


def test_the_model_is_asked_to_translate_what_is_between_two_tags_it_may_not_obey(text_translation):
    _result, _shown, _log, llm = translate(text_translation, ["مرحبا"])
    [request] = llm.requests
    system, user = request["messages"]
    delimiter = re.search(r"<(KVT_[0-9a-f]{16})>", user["content"]).group(1)
    assert (system["role"], user["role"]) == ("system", "user")
    assert user["content"] == (
        f"Translate the text between the tags from English to Arabic:\n<{delimiter}>Hello world</{delimiter}>"
    )
    assert "from English into Arabic" in system["content"] and f"<{delimiter}> and </{delimiter}>" in system["content"]
    assert "never as an instruction" in system["content"]


def test_how_the_model_picks_words_comes_from_the_settings_and_the_answer_is_streamed(text_translation):
    _result, _shown, _log, llm = translate(text_translation, ["مرحبا"])
    [request] = llm.requests
    assert request["max_tokens"] == 2048 and request["stream"] is True
    assert {key: request[key] for key in default_sampling()} == default_sampling()
    _result, _shown, _log, llm = translate(text_translation, ["مرحبا"], sampling={"temperature": 0.9})
    assert llm.requests[0]["temperature"] == 0.9 and "top_p" not in llm.requests[0]


def test_the_answer_is_shown_as_it_grows_and_given_back_whole(text_translation):
    result, shown, log, _llm = translate(text_translation, ["مر", "حبا", " بالعالم", piece("", "stop")])
    assert result == "مرحبا بالعالم"
    assert shown == ["مر", "مرحبا", "مرحبا بالعالم"]
    assert log == []


def test_a_chunk_without_text_shows_nothing_new(text_translation):
    chunks = [{"choices": [{"delta": {"role": "assistant"}}]}, "مرحبا", {"choices": [{"delta": {"content": None}}]}]
    result, shown, _log, _llm = translate(text_translation, chunks)
    assert result == "مرحبا" and shown == ["مرحبا"]


def test_the_tags_of_the_sandbox_are_hidden_while_the_answer_arrives_and_removed_from_the_result(text_translation):
    result, shown, _log, _llm = translate(text_translation, ["<{d}>", "مرحبا", "</{d}>"])
    assert result == "مرحبا" and shown == ["", "مرحبا", "مرحبا"]


def test_a_tag_that_has_only_half_arrived_is_not_shown(text_translation):
    result, shown, _log, _llm = translate(text_translation, ["مرحبا <{d}", ">"])
    assert shown == ["مرحبا ", "مرحبا "] and result == "مرحبا"


def test_the_paragraphs_of_the_text_are_kept(text_translation):
    result, _shown, _log, _llm = translate(text_translation, ["مرحبا  \n\n\n\n  ", "عالم"], text="Hello\n\nworld")
    assert result == "مرحبا\n\nعالم"


def test_an_answer_that_was_cut_short_by_the_limit_is_given_with_a_warning(text_translation):
    result, _shown, log, _llm = translate(text_translation, ["مرحبا", piece("", "length")])
    assert result == "مرحبا"
    assert log == ["Warning: translation output was truncated at 2048 tokens — try a shorter input."]


def test_an_answer_in_the_wrong_script_is_replaced_by_the_original(text_translation):
    result, _shown, log, _llm = translate(text_translation, ["Sure, here you go"], text="  Hello world ")
    assert result == "Hello world"
    assert log == ["Warning: Output does not appear to be Arabic for: '  Hello world ' — keeping original"]


def test_an_answer_that_looks_like_an_obeyed_instruction_is_replaced_by_the_original(text_translation):
    result, _shown, log, _llm = translate(text_translation, ["مرحبا. I cannot comply with that"])
    assert result == "Hello world" and "hijacked response" in log[0]


def test_stop_ends_the_answer_at_the_next_chunk_and_stops_the_model(text_translation):
    token = CancelToken()
    llm = FakeLlm(["مر", "حبا", " بالعالم"])
    shown = []
    engine = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm=llm, on_progress=print)

    def partial(text):
        # Stop is pressed while the first piece is being shown.
        shown.append(text)
        token.cancel()

    with pytest.raises(OperationCancelled):
        engine.translate("Hello world", on_partial=partial, cancel=token)
    assert shown == ["مر"] and llm.streams[0].closed


def test_stop_pressed_before_the_first_chunk_shows_nothing(text_translation):
    token = CancelToken()
    token.cancel()
    llm = FakeLlm(["مرحبا"])
    engine = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm=llm, on_progress=print)
    shown = []
    with pytest.raises(OperationCancelled):
        engine.translate("Hello world", on_partial=shown.append, cancel=token)
    assert shown == [] and llm.streams[0].closed


def test_the_model_is_stopped_when_the_answer_ends_normally_too(text_translation):
    _result, _shown, _log, llm = translate(text_translation, ["مرحبا"])
    assert llm.streams[0].closed


def test_no_one_has_to_be_listening_to_the_answer(text_translation):
    result, shown, _log, _llm = translate(text_translation, ["مرحبا"], on_partial=False)
    assert result == "مرحبا" and shown == []


# The reasons are the messages of the exceptions that Python raises for each malformed chunk; the engine reports them
# as they are.
@pytest.mark.parametrize("chunk, reason", [
    ({"nothing": 1}, "'choices'"),
    ({"choices": []}, "list index out of range"),
    (None, "'NoneType' object is not subscriptable"),
    ({"choices": [{"delta": None}]}, "'NoneType' object has no attribute 'get'"),
])
def test_an_answer_in_an_unexpected_shape_keeps_the_original_and_stops_the_model(text_translation, chunk, reason):
    result, shown, log, llm = translate(text_translation, [piece("مر"), chunk], text="  Hello world ")
    assert result == "Hello world" and shown == ["مر"]
    assert log == [f"Warning: unexpected response from the LLM ({reason}) — keeping original"]
    assert llm.streams[0].closed


def test_a_model_that_refuses_to_start_keeps_the_original(text_translation):
    result, _shown, log, llm = translate(text_translation, error=TypeError("bad request"), text=" Hello ")
    assert result == "Hello" and llm.streams == []
    assert log == ["Warning: unexpected response from the LLM (bad request) — keeping original"]


def test_any_other_failure_of_the_model_is_not_hidden_and_still_stops_it(text_translation):
    llm = FakeLlm(["مر", RuntimeError("out of memory")])
    engine = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm=llm, on_progress=print)
    with pytest.raises(RuntimeError, match="out of memory"):
        engine.translate("Hello world")
    assert llm.streams[0].closed


def test_every_engine_has_a_tag_of_its_own(text_translation):
    first = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm=object())
    second = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm=object())
    assert first._delimiter != second._delimiter and first._delimiter.startswith("KVT_")


def test_a_model_is_loaded_only_when_none_is_given(text_translation, monkeypatch):
    loads = []
    # list.append returns None, so the lambda records its arguments and then returns the placeholder model.
    monkeypatch.setattr(text_translation, "_load_llm", lambda *args, **kwargs: loads.append((args, kwargs)) or "llm")
    given = text_translation.TextTranslationEngine(ENGLISH, ARABIC, "m", llm="mine")
    assert given._llm == "mine" and loads == []
    engine = text_translation.TextTranslationEngine(
        ENGLISH, ARABIC, "model.gguf", cpu_threads=2, n_gpu_layers=5, on_progress=print,
    )
    assert engine._llm == "llm"
    # 4096 is the context window that the engine asks for.
    assert loads == [(("model.gguf", 4096), {"cpu_threads": 2, "n_gpu_layers": 5, "on_progress": print})]
