# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The supported languages and the lookups built on them.

One shared table serves every tab (video transcription and translation, text translation, OCR). Each language carries
a Whisper code and a Tesseract code; the Whisper code is only used by the video pipeline and the Tesseract code only
by OCR, so a language that lacks one is unavailable for that feature alone (without a Tesseract code, for example,
OCR is unsupported but text translation still works).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Unicode range of the Arabic script, used for the wrong-script sanity check on translation output. It is a shared
# constant because Arabic, Persian and Urdu all use it.
_ARABIC_SCRIPT = [("\u0600", "\u06FF")]


@dataclass(frozen=True)
class Language:
    """A language the app can translate, and optionally transcribe or OCR.

    Attributes:
        name: Display name, also used in the translation prompt.
        whisper_code: Whisper language code (mostly ISO 639-1).
        unicode_ranges: Inclusive (first, last) character ranges of the script, for the wrong-script check on
            translation output. Empty when the script is not checked, for example for Latin-script languages; the
            check then always passes.
        is_rtl: Whether the language is written right to left.
        tesseract_code: Tesseract OCR language code; an empty string means OCR is not supported for the language.
        speech: Whisper hears the language reliably, so the video tab offers it as the language *spoken* in a
            video. False means translation and OCR only (text, subtitle files, and as a target language everywhere).
    """

    name: str
    whisper_code: str
    unicode_ranges: list[tuple[str, str]] = field(default_factory=list)
    is_rtl: bool = False
    tesseract_code: str = ""
    speech: bool = True


def _lang(
    name: str, whisper_code: str, unicode_ranges=None, is_rtl: bool = False, tesseract_code: str = "",
    speech: bool = True,
) -> Language:
    return Language(name, whisper_code, unicode_ranges or [], is_rtl, tesseract_code, speech)


# Language table. Translating and hearing are different skills, so each entry says whether Whisper can be trusted to
# hear the language (speech=True, the default). Two lists come from the one table:
#   * the video tab's source-language list shows only the speech languages;
#   * every other picker shows the whole table: the video tab's target languages, the text tab, OCR and its language
#     packs, and the language a subtitle file is detected as.
# A video in a language that is not in the speech list still works with Auto-detect. Languages that Whisper hears but
# that are not in this table also still work as an auto-detected *source* (see language_for_code()); they just cannot be
# picked.
#
# Which languages are in the table: the translation model must handle the language well. Population does not matter; a
# small language is in if the model is strong on it.
#   * Gemma 3 4B baseline MetricX on WMT24++, English into the language, of about 6.5 or lower (lower is better;
#     TranslateGemma technical report, Table 4): German 2.72, Indonesian 3.27, Chinese 3.27, Spanish 3.35, Italian 3.60,
#     Vietnamese 3.62, French 3.76, Portuguese 3.77, Bengali 3.86, Dutch 3.87, Swedish 4.14, Thai 4.14, Norwegian 4.23,
#     Croatian 4.26, Danish 4.40, Arabic 4.5, Russian 4.54, Korean 4.72, Persian 4.77, Hindi 5.03, Tamil 5.04, Japanese
#     5.09, Tagalog 5.22, Ukrainian 5.40, Marathi 5.64, Romanian 5.70, Telugu 5.76, Bulgarian 5.81, Greek 6.31. Gemma 4
#     publishes no per-language numbers, only "35+ languages out of the box", so the small Gemma 3 model is the closest
#     published stand-in for E2B/E4B.
#   * Polish (7.07), Turkish (8.03) and Urdu (7.80) are above that bar. They are kept as judgment calls because they are
#     far too large to leave out.
#
# Which are speech languages: Whisper large-v3 word error rate (WER) on FLEURS of about 11.5% or lower (Dolphin paper,
# Table 8, and a public 59-language benchmark): Korean 2.6, Italian 1.5, Portuguese 3.0, German 3.1, French 3.4, Turkish
# 4.1, Spanish 4.3, Polish 4.3, Japanese 4.8, Russian 5.4, Dutch 5.8, Greek 5.8, Indonesian 6.5, Chinese 7.2, Romanian
# 8.1, Norwegian 8.1, Vietnamese 8.9, Swedish 9.3, Bulgarian 9.6, Croatian 10.1, Arabic 10.5, Ukrainian 10.5, Tagalog
# 11.4. Hindi (16.5) is above that bar and kept as a judgment call, because it is far too large to leave out.
# Translation only (speech=False), with their Whisper error rates: Danish 12.7, Thai 19.1 (and the recommended Turbo
# model is documented to be weaker on it), Urdu 21.5, Tamil 29.7, Persian 30.7, Marathi 35.3, Telugu 39.2, Bengali 47.9.
#
# Not in the table, so that nobody is offered a language the models handle badly:
#   * Translation too weak (MetricX above the bar): Hebrew 6.70, Serbian 6.94, Catalan 6.97, Czech 7.47, Finnish 7.54,
#     Gujarati 7.67, Slovak 9.17, Slovenian 9.39, Kannada 10.48, Hungarian 10.75, Punjabi 11.2, Malayalam 11.89,
#     Lithuanian 13.39, Swahili 14.05, Estonian 14.78, Latvian 15.75, Icelandic 19.22, Zulu 21.52, and the other
#     low-resource languages.
#   * Pashto: Whisper writes it in the wrong script (89.8% WER; under 1% of the output is in Pashto script) and no
#     translation figure was found. Malay: no translation figure found.
#
# To change the lists, edit the table below (the 81-language list is in git history).
_ENTRIES: list[Language] = [
    _lang("Arabic", "ar", _ARABIC_SCRIPT, is_rtl=True, tesseract_code="ara"),
    _lang("Bengali", "bn", [("\u0980", "\u09FF")], tesseract_code="ben", speech=False),
    _lang("Bulgarian", "bg", [("\u0400", "\u04FF")], tesseract_code="bul"),
    _lang("Chinese", "zh", [("\u4E00", "\u9FFF"), ("\u3400", "\u4DBF")], tesseract_code="chi_sim"),
    _lang("Chinese (Traditional)", "zh", [("\u4E00", "\u9FFF"), ("\u3400", "\u4DBF")], tesseract_code="chi_tra"),
    _lang("Croatian", "hr", tesseract_code="hrv"),
    _lang("Danish", "da", tesseract_code="dan", speech=False),
    _lang("Dutch", "nl", tesseract_code="nld"),
    _lang("English", "en", tesseract_code="eng"),
    _lang("French", "fr", tesseract_code="fra"),
    _lang("German", "de", tesseract_code="deu"),
    _lang("Greek", "el", [("\u0370", "\u03FF"), ("\u1F00", "\u1FFF")], tesseract_code="ell"),
    _lang("Hindi", "hi", [("\u0900", "\u097F")], tesseract_code="hin"),
    _lang("Indonesian", "id", tesseract_code="ind"),
    _lang("Italian", "it", tesseract_code="ita"),
    _lang("Japanese", "ja", [("\u3040", "\u30FF"), ("\u4E00", "\u9FFF")], tesseract_code="jpn"),
    _lang("Korean", "ko", [("\uAC00", "\uD7A3"), ("\u1100", "\u11FF")], tesseract_code="kor"),
    _lang("Marathi", "mr", [("\u0900", "\u097F")], tesseract_code="mar", speech=False),
    _lang("Norwegian", "no", tesseract_code="nor"),
    _lang("Persian", "fa", _ARABIC_SCRIPT, is_rtl=True, tesseract_code="fas", speech=False),
    _lang("Polish", "pl", tesseract_code="pol"),
    _lang("Portuguese", "pt", tesseract_code="por"),
    _lang("Romanian", "ro", tesseract_code="ron"),
    _lang("Russian", "ru", [("\u0400", "\u04FF")], tesseract_code="rus"),
    _lang("Spanish", "es", tesseract_code="spa"),
    _lang("Swedish", "sv", tesseract_code="swe"),
    _lang("Tagalog", "tl", tesseract_code="tgl"),
    _lang("Tamil", "ta", [("\u0B80", "\u0BFF")], tesseract_code="tam", speech=False),
    _lang("Telugu", "te", [("\u0C00", "\u0C7F")], tesseract_code="tel", speech=False),
    _lang("Thai", "th", [("\u0E00", "\u0E7F")], tesseract_code="tha", speech=False),
    _lang("Turkish", "tr", tesseract_code="tur"),
    _lang("Ukrainian", "uk", [("\u0400", "\u04FF")], tesseract_code="ukr"),
    _lang("Urdu", "ur", _ARABIC_SCRIPT, is_rtl=True, tesseract_code="urd", speech=False),
    _lang("Vietnamese", "vi", tesseract_code="vie"),
]


# Whisper can recognise these too, but they are not offered in the language pickers (see the comment above _ENTRIES).
# The names exist only so that a language Whisper auto-detects in a video appears with its name (for example
# "Catalan") in the translation prompt and the log, instead of its code ("CA").
_DETECTED_ONLY: dict[str, str] = {
    "af": "Afrikaans", "sq": "Albanian", "am": "Amharic", "hy": "Armenian", "bs": "Bosnian", "my": "Burmese",
    "ca": "Catalan", "cs": "Czech", "et": "Estonian", "fi": "Finnish", "gl": "Galician", "ka": "Georgian",
    "gu": "Gujarati", "ha": "Hausa", "he": "Hebrew", "hu": "Hungarian", "is": "Icelandic", "ga": "Irish",
    "jw": "Javanese", "kn": "Kannada", "kk": "Kazakh", "km": "Khmer", "ku": "Kurdish (Sorani)",
    "ky": "Kyrgyz", "lo": "Lao", "lv": "Latvian", "lt": "Lithuanian", "mk": "Macedonian", "ms": "Malay",
    "ml": "Malayalam", "mt": "Maltese", "mn": "Mongolian", "ne": "Nepali", "ps": "Pashto", "pa": "Punjabi",
    "sr": "Serbian", "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "so": "Somali", "su": "Sundanese",
    "sw": "Swahili", "tg": "Tajik", "uz": "Uzbek", "cy": "Welsh", "yo": "Yoruba", "zu": "Zulu",
}

LANGUAGES: dict[str, Language] = {}
for _entry in _ENTRIES:
    LANGUAGES[_entry.name.lower()] = _entry
    LANGUAGES.setdefault(_entry.whisper_code.lower(), _entry)


def resolve_language(raw: str) -> Optional[Language]:
    """Look up a Language by full name or ISO code, case-insensitively."""
    return LANGUAGES.get(raw.strip().lower())


def language_for_code(code: str) -> Language:
    """Return the language for a Whisper code: the offered one, else a name-only stand-in.

    Whisper detects the language of a video by itself, and it knows far more languages than the pickers offer. Such a
    language still works as the *source* of a translation; it just cannot be chosen.
    """
    code = code.strip().lower()
    # A stand-in is not vouched for, so speech=False: the transcription log says Whisper is less reliable on it.
    return LANGUAGES.get(code) or Language(
        name=_DETECTED_ONLY.get(code, code.upper()), whisper_code=code, speech=False,
    )


def speech_language_names() -> list[str]:
    """Names of the languages Whisper hears reliably, sorted: the video tab's source language list."""
    return sorted(language.name for language in _ENTRIES if language.speech)


def ocr_languages() -> list[Language]:
    """Return the languages Tesseract can read, sorted by name.

    This is the list of language packs that the OCR settings let the user download.
    """
    return sorted((language for language in _ENTRIES if language.tesseract_code), key=lambda language: language.name)


def available_language_names() -> list[str]:
    """Return the sorted display names, one per language."""
    return sorted({lang.name for lang in LANGUAGES.values()})


def detect_language_from_text(text: str) -> Optional[Language]:
    """Detect the language of a chunk of plain text, or return None if it cannot be determined.

    Used when translating an existing subtitle file whose source language was not specified. The detected code is
    matched against the table by its full form first, then by the part before any hyphen.

    Raises:
        ImportError: The optional ``langdetect`` dependency is not installed.
    """
    try:
        from langdetect import detect
    except ImportError as exc:
        raise ImportError(
            "langdetect is required for subtitle language detection.\n"
            "Install it with:  pip install langdetect"
        ) from exc

    try:
        code = detect(text)
    except Exception:
        return None

    short_code = code.split("-")[0]
    return LANGUAGES.get(code) or LANGUAGES.get(short_code)
