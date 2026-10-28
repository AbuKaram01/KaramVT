# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The languages KaramVT offers: only the big ones that the speech and translation models handle very well."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from karamvt.core.languages import (
    _DETECTED_ONLY,
    _ENTRIES,
    LANGUAGES,
    Language,
    available_language_names,
    detect_language_from_text,
    language_for_code,
    ocr_languages,
    resolve_language,
    speech_language_names,
)

# The whole table: translation, the text tab, OCR, and the video tab's target languages.
OFFERED = [
    "Arabic", "Bengali", "Bulgarian", "Chinese", "Chinese (Traditional)", "Croatian", "Danish", "Dutch",
    "English", "French", "German", "Greek", "Hindi", "Indonesian", "Italian", "Japanese", "Korean",
    "Marathi", "Norwegian", "Persian", "Polish", "Portuguese", "Romanian", "Russian", "Spanish",
    "Swedish", "Tagalog", "Tamil", "Telugu", "Thai", "Turkish", "Ukrainian", "Urdu", "Vietnamese",
]
# Whisper hears these reliably: the video tab's source languages.
SPEECH = [
    "Arabic", "Bulgarian", "Chinese", "Chinese (Traditional)", "Croatian", "Dutch", "English", "French",
    "German", "Greek", "Hindi", "Indonesian", "Italian", "Japanese", "Korean", "Norwegian", "Polish",
    "Portuguese", "Romanian", "Russian", "Spanish", "Swedish", "Tagalog", "Turkish", "Ukrainian", "Vietnamese",
]
# Translation only: the model translates them well but Whisper does not hear them reliably.
TEXT_ONLY = ["Bengali", "Danish", "Marathi", "Persian", "Tamil", "Telugu", "Thai", "Urdu"]
# Not offered anywhere: weak translation, or Whisper cannot even write the language (Pashto).
DROPPED = [
    "Swahili", "Hausa", "Serbian", "Georgian", "Uzbek", "Yoruba", "Zulu", "Somali", "Pashto",
    "Kurdish (Sorani)", "Maltese", "Czech", "Finnish", "Catalan", "Hungarian", "Slovak", "Hebrew", "Malay",
    "Punjabi", "Gujarati", "Kannada", "Malayalam",
]


def test_exactly_the_agreed_languages_are_offered():
    assert available_language_names() == OFFERED
    assert [language.name for language in _ENTRIES] == OFFERED


def test_the_video_tabs_spoken_language_list_is_the_speech_subset_of_the_table():
    assert speech_language_names() == SPEECH
    assert set(SPEECH) | set(TEXT_ONLY) == set(OFFERED) and set(SPEECH).isdisjoint(TEXT_ONLY)
    assert [language.name for language in _ENTRIES if not language.speech] == TEXT_ONLY


def test_hindi_can_be_translated_from_and_into_in_every_tab_including_as_the_spoken_language_of_a_video():
    hindi = resolve_language("Hindi")
    assert hindi.speech and "Hindi" in speech_language_names() and "Hindi" in available_language_names()
    assert (hindi.whisper_code, hindi.tesseract_code) == ("hi", "hin")


@pytest.mark.parametrize("name", TEXT_ONLY)
def test_a_translation_only_language_is_not_a_spoken_language_but_everything_else_still_works(name):
    language = resolve_language(name)
    assert language is not None and not language.speech
    assert name in available_language_names() and name not in speech_language_names()
    assert language.whisper_code and language.tesseract_code  # Auto-detect and OCR still know it.


@pytest.mark.parametrize("name", DROPPED)
def test_a_dropped_language_cannot_be_picked_or_looked_up(name):
    assert name not in available_language_names()
    assert resolve_language(name) is None and resolve_language(name.lower()) is None


@pytest.mark.parametrize(
    "code", ["ps", "sw", "ha", "sr", "cs", "fi", "ca", "hu", "sk", "he", "ms", "pa", "gu", "kn", "ml"]
)
def test_a_dropped_language_cannot_be_looked_up_by_code_either(code):
    assert resolve_language(code) is None


@pytest.mark.parametrize("language", _ENTRIES, ids=lambda language: language.name)
def test_every_offered_language_can_be_found_by_name_and_by_code_in_any_case(language):
    assert resolve_language(language.name) is language
    assert resolve_language(language.name.upper()) is language
    assert resolve_language(f"  {language.name.lower()} ") is language
    assert resolve_language(language.whisper_code.upper()).whisper_code == language.whisper_code


def test_names_are_unique_and_the_list_is_sorted_for_the_pickers():
    names = [language.name for language in _ENTRIES]
    assert len(names) == len(set(names)) and names == sorted(names)


def test_both_chinese_scripts_share_whisper_but_differ_for_ocr():
    simplified, traditional = resolve_language("Chinese"), resolve_language("Chinese (Traditional)")
    assert simplified.whisper_code == traditional.whisper_code == "zh"
    assert (simplified.tesseract_code, traditional.tesseract_code) == ("chi_sim", "chi_tra")
    assert resolve_language("zh") is simplified  # The code means the simplified entry.


def test_every_offered_language_works_for_speech_and_for_ocr():
    assert all(language.whisper_code and language.tesseract_code for language in _ENTRIES)


def test_arabic_persian_and_urdu_are_right_to_left_and_the_non_latin_languages_have_a_script_check():
    assert [language.name for language in _ENTRIES if language.is_rtl] == ["Arabic", "Persian", "Urdu"]
    for name in ("Arabic", "Bengali", "Bulgarian", "Chinese", "Chinese (Traditional)", "Greek", "Hindi", "Japanese",
                 "Korean", "Marathi", "Persian", "Russian", "Tamil", "Telugu", "Thai", "Ukrainian", "Urdu"):
        assert resolve_language(name).unicode_ranges, name
    for name in ("English", "French", "German", "Spanish", "Turkish", "Vietnamese", "Indonesian", "Dutch",
                 "Swedish", "Norwegian", "Romanian", "Croatian", "Tagalog", "Danish"):
        assert resolve_language(name).unicode_ranges == [], name


def test_the_pickers_start_on_arabic_for_a_target_with_no_auto_detect():
    assert available_language_names()[0] == "Arabic"


# --- Languages Whisper can still hear ---

def test_a_language_the_pickers_do_not_offer_keeps_a_proper_name_when_whisper_detects_it():
    czech = language_for_code("cs")
    assert (czech.name, czech.whisper_code, czech.tesseract_code) == ("Czech", "cs", "")
    assert language_for_code("SW").name == "Swahili"


def test_an_offered_language_is_returned_as_the_offered_one_and_an_unknown_code_is_still_usable():
    assert language_for_code("ar") is resolve_language("arabic")
    assert language_for_code(" AR ") is resolve_language("arabic")
    unknown = language_for_code("xx")
    assert (unknown.name, unknown.whisper_code) == ("XX", "xx")


def test_a_stand_in_language_is_not_vouched_for_so_the_log_can_warn_about_it():
    assert language_for_code("cs").speech is False and language_for_code("xx").speech is False
    assert language_for_code("en").speech is True


def test_the_name_only_table_never_overlaps_the_offered_languages():
    offered_codes = {language.whisper_code for language in _ENTRIES}
    assert offered_codes.isdisjoint(_DETECTED_ONLY)
    assert all(name and name == name.strip() for name in _DETECTED_ONLY.values())
    assert len(_DETECTED_ONLY) == 47  # 81 languages before, 34 offered now.


def detect_with_engine(monkeypatch, code):
    """What the transcription engine says while auto-detecting a video whose language Whisper hears as ``code``."""
    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())  # Nothing here loads a real model.
    monkeypatch.delitem(sys.modules, "karamvt.core.transcription", raising=False)
    from karamvt.core.transcription import TranscriptionEngine

    model = SimpleNamespace(
        transcribe=lambda *args, **kwargs: (None, SimpleNamespace(language=code, language_probability=0.97))
    )
    messages = []
    detected = TranscriptionEngine("unused", model=model, on_progress=messages.append).detect_language("audio.wav")
    monkeypatch.delitem(sys.modules, "karamvt.core.transcription", raising=False)  # Do not leak the stand-in.
    return detected, messages


def test_the_transcription_engine_names_a_detected_language_that_is_not_offered(monkeypatch):
    detected, messages = detect_with_engine(monkeypatch, "cs")
    assert detected.name == "Czech"
    assert any("Czech (cs) — 97.0% confidence" in message for message in messages)
    assert any("less reliable for Czech" in message for message in messages)


def test_a_translation_only_language_detected_in_a_video_still_works_but_comes_with_a_warning(monkeypatch):
    detected, messages = detect_with_engine(monkeypatch, "fa")
    assert detected is resolve_language("persian")
    assert any("less reliable for Persian" in message for message in messages)


def test_a_language_whisper_hears_reliably_is_detected_without_a_warning(monkeypatch):
    detected, messages = detect_with_engine(monkeypatch, "es")
    assert detected is resolve_language("spanish")
    assert not any("less reliable" in message for message in messages)


# --- Detecting the language of a text ---

def fake_langdetect(monkeypatch, result):
    module = types.ModuleType("langdetect")
    if isinstance(result, Exception):
        def detect(_text):
            raise result
    else:
        def detect(_text):
            return result
    module.detect = detect
    monkeypatch.setitem(sys.modules, "langdetect", module)


@pytest.mark.parametrize("detected, name", [
    ("es", "Spanish"), ("zh-cn", "Chinese"), ("zh-tw", "Chinese"), ("ar", "Arabic"), ("fa", "Persian"), ("ur", "Urdu"),
])
def test_an_offered_language_is_detected_from_text(monkeypatch, detected, name):
    fake_langdetect(monkeypatch, detected)
    assert detect_language_from_text("some text").name == name


@pytest.mark.parametrize("detected", ["cs", "sw", "ps", "he"])
def test_text_in_a_language_that_is_not_offered_is_not_taken_for_another(monkeypatch, detected):
    fake_langdetect(monkeypatch, detected)
    assert detect_language_from_text("some text") is None


def test_text_that_cannot_be_classified_gives_none(monkeypatch):
    fake_langdetect(monkeypatch, RuntimeError("no features in text"))
    assert detect_language_from_text("1234") is None


def test_the_language_table_still_hands_out_frozen_language_objects():
    assert isinstance(LANGUAGES["english"], Language)
    with pytest.raises(AttributeError):
        LANGUAGES["english"].name = "Other"


def test_the_languages_ocr_can_read_are_every_offered_one_sorted_by_name():
    languages = ocr_languages()
    assert [language.name for language in languages] == sorted(OFFERED)
    assert all(language.tesseract_code for language in languages)
    assert {language.tesseract_code for language in languages} == {language.tesseract_code for language in _ENTRIES}
