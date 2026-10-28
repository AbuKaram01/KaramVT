# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The language lists the user sees: the pickers on both tabs and the OCR language packs."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

from gui_support import Gtk, downloader, env, walk

from karamvt.core import tessdata_manager
from karamvt.core.languages import _ENTRIES, available_language_names, speech_language_names
from karamvt.gui.language_manager_dialog import LanguageManagerDialog
from karamvt.gui.language_picker import LanguagePickerButton
from karamvt.gui.multi_language_picker import MultiLanguagePickerButton

OFFERED = available_language_names()
SPEECH = speech_language_names()
TEXT_ONLY = [name for name in OFFERED if name not in SPEECH]


def test_the_source_picker_offers_auto_detect_and_the_target_picker_starts_on_arabic(env):
    source = LanguagePickerButton(OFFERED, include_auto=True)
    target = LanguagePickerButton(OFFERED, include_auto=False)
    assert source.get_selected() == "Auto-detect" and target.get_selected() == "Arabic"


def test_the_picker_lists_only_the_offered_languages_and_searching_narrows_them(env):
    picker = LanguagePickerButton(OFFERED, include_auto=False)
    assert picker.visible_language_names() == OFFERED and len(OFFERED) == 34

    search = next(w for w in walk(picker.get_popover()) if isinstance(w, Gtk.SearchEntry))
    search.set_text("chinese")
    assert picker.visible_language_names() == ["Chinese", "Chinese (Traditional)"]
    search.set_text("swahili")
    assert picker.visible_language_names() == []


def test_the_multi_language_picker_offers_the_same_languages(env):
    picker = MultiLanguagePickerButton(OFFERED)
    labels = [w.get_label() for w in walk(picker.get_popover()) if isinstance(w, Gtk.Label)]
    assert all(name in labels for name in OFFERED)
    assert not any(dropped in labels for dropped in ("Swahili", "Pashto", "Czech", "Hebrew", "Catalan"))


def test_the_ocr_language_manager_lists_one_pack_per_offered_language(env):
    tier = tessdata_manager.QUALITY_TIERS[0][0]
    dialog = LanguageManagerDialog(tier)
    expected = {language.tesseract_code for language in _ENTRIES}
    assert set(dialog._rows) == expected and len(expected) == 34
    assert {"eng", "ara", "chi_sim", "chi_tra", "jpn", "kor", "hin", "rus", "nld", "swe", "ell", "tgl",
            "fas", "urd", "ben", "tha", "tam", "tel", "mar", "dan"} <= set(dialog._rows)
    assert set(dialog._rows).isdisjoint({"swa", "pus", "ces", "srp", "fin", "heb", "cat"})


# --- The two lists, as the tabs really build them ---

def picker_labels(picker):
    """The labels of every language in a picker's pop-over."""
    return [w.get_label() for w in walk(picker.get_popover()) if isinstance(w, Gtk.Label)]


def test_the_video_tab_offers_only_the_languages_whisper_hears_as_the_spoken_language(env):
    _app, window = env
    video = window._video_tab
    assert video._source_picker.visible_language_names() == ["Auto-detect", *SPEECH] and len(SPEECH) == 26
    assert video._source_picker.get_selected() == "Auto-detect"
    for name in ("Persian", "Urdu", "Bengali", "Thai", "Tamil", "Telugu", "Marathi", "Danish"):
        assert name not in video._source_picker.visible_language_names()


def test_hindi_is_offered_as_the_spoken_language_of_a_video_and_as_a_target_and_in_the_text_tab(env):
    _app, window = env
    assert "Hindi" in window._video_tab._source_picker.visible_language_names()
    assert "Hindi" in picker_labels(window._video_tab._target_picker)
    assert "Hindi" in window._text_tab._source_picker.visible_language_names()
    assert "Hindi" in window._text_tab._target_picker.visible_language_names()


def test_the_video_tab_can_still_translate_into_every_language_in_the_table(env):
    _app, window = env
    labels = picker_labels(window._video_tab._target_picker)
    assert all(name in labels for name in OFFERED)
    assert all(name in labels for name in TEXT_ONLY)  # Persian or Urdu subtitles for an English video.


def test_the_text_tab_offers_the_whole_table_for_both_source_and_target(env):
    _app, window = env
    text = window._text_tab
    assert text._source_picker.visible_language_names() == ["Auto-detect", *OFFERED]
    assert text._target_picker.visible_language_names() == OFFERED
    for name in TEXT_ONLY:
        assert name in text._source_picker.visible_language_names(), name
    assert text._source_picker.get_selected() == "Auto-detect" and text._target_picker.get_selected() == "Arabic"
