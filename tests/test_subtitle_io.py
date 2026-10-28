# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reading subtitle files whatever they were saved as, and the layouts they come in (core/subtitle_io.py)."""

import codecs

import pytest

from karamvt.core.models import Caption, OutputFormat
from karamvt.core.subtitle_io import SubtitleFileHandler, decode_subtitle

ARABIC = "مرحبا بالعالم، كيف حالك اليوم؟ أنا بخير والحمد لله. هل تريد أن نذهب إلى السوق؟"
SRT_TEXT = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا بالعالم\n\n2\n00:00:04,000 --> 00:00:06,500\nكيف حالك اليوم؟\n\n"


def write(tmp_path, name, text, encoding="utf-8", newline=None):
    path = tmp_path / name
    path.write_bytes((text.replace("\n", newline) if newline else text).encode(encoding))
    return str(path)


def load(path, language_code=None):
    messages: list[str] = []
    captions = SubtitleFileHandler().load(path, on_progress=messages.append, language_code=language_code)
    return captions, messages


# --- The encodings ---

# (language, text, the encoding it was saved in)
SAMPLES = [
    ("ar", ARABIC, "cp1256"),
    ("ar", "مَرْحَبًا بِكَ فِي بَيْتِنَا، كَيْفَ حَالُكَ الْيَوْمَ؟", "cp1256"),
    ("ar", "مرحبا Hello بالعالم World كيف حالك How are you اليوم today", "cp1256"),
    ("ar", "نعم.\nلا؟\nحسنا، تمام!\nأين أنت؟\nأنا هنا.\nمن؟\nلماذا؟", "cp1256"),
    ("fa", "سلام دنيا، حال شما چطور است؟ من خوبم، ممنون از شما.", "cp1256"),
    ("ru", "Привет, мир! Как у тебя дела сегодня? Я хочу пойти в магазин, а ты? Это моя семья.", "cp1251"),
    ("uk", "Привіт, світе! Як у тебе справи сьогодні? Я хочу піти до магазину, я знаю.", "cp1251"),
    ("el", "Γεια σου κόσμε, τι κάνεις σήμερα; Θέλω να πάω στην αγορά.", "cp1253"),
    ("he", "שלום עולם, מה שלומך היום? אני רוצה ללכת לשוק עכשיו.", "cp1255"),
    ("fr", "Bonjour le monde, ça va très bien aujourd'hui. J'ai mangé du pâté près de l'église, déjà.", "cp1252"),
    ("es", "¿Cómo estás? ¡Mañana voy a la montaña con mi niño, será una gran aventura!", "cp1252"),
    ("de", "Schöne Grüße aus München! Wir müssen früh aufstehen, weil der Zug um sechs fährt.", "cp1252"),
    ("pt", "Não sei se você vai à praça amanhã, mas eu irei de manhã com a minha avó.", "cp1252"),
    ("en", "It’s a “quote” — and it’s fine… really.", "cp1252"),
    ("en", "“It’s fine,” she said — “really…” ‘Yes’ – “no”… ’Tis “ok”.", "cp1252"),
    ("en", "The café’s naïve résumé — fiancée’s coöperation", "cp1252"),
    ("ja", "こんにちは世界、今日は元気ですか？市場に行きたいです。", "cp932"),
    ("ja", "はい。\nいいえ。\nどこ？\nここです。\n誰？", "cp932"),
    ("zh", "你好世界，你今天好吗？我想去市场买东西。", "gb18030"),
    ("zh", "好的。\n不是。\n在哪里？\n在这里。", "gb18030"),
    ("ko", "안녕하세요 세계, 오늘 기분이 어떠세요? 시장에 가고 싶어요.", "cp949"),
]


@pytest.mark.parametrize("language, text, encoding", SAMPLES, ids=[f"{s[0]}-{s[2]}-{i}" for i, s in enumerate(SAMPLES)])
def test_a_legacy_file_is_read_correctly_when_its_language_is_known(language, text, encoding):
    decoded, used = decode_subtitle((text + "\n").encode(encoding), language)
    assert decoded.strip() == text and not used.endswith("?")


@pytest.mark.parametrize("language, text, encoding", SAMPLES, ids=[f"{s[0]}-{s[2]}-{i}" for i, s in enumerate(SAMPLES)])
def test_a_legacy_file_is_read_correctly_even_when_its_language_is_not_known(language, text, encoding):
    decoded, _used = decode_subtitle((text + "\n").encode(encoding), None)
    assert decoded.strip() == text


def test_arabic_in_windows_1256_is_not_taken_for_western_text_and_says_so():
    """The reason the file was refused: strict UTF-8 cannot read it, and Windows-1252 turns it into nonsense."""
    raw = ARABIC.encode("cp1256")
    assert decode_subtitle(raw, None) == (ARABIC, "cp1256")
    assert raw.decode("cp1252", errors="replace") != ARABIC


@pytest.mark.parametrize("language", ["tr", "pl", "cs"])
def test_a_language_with_its_own_code_page_is_read_right_once_the_language_is_given(language):
    """Windows-1250 and -1254 differ from Windows-1252 in a few letters only; the language settles which."""
    text = {
        "tr": "Merhaba dünya, bugün nasılsın? Çok güzel bir gün, şimdi çay içelim.",
        "pl": "Dzień dobry świecie, jak się dziś masz? Chciałbym pójść do sklepu, żeby kupić chleb.",
        "cs": "Dobrý den světe, jak se dnes máš? Chtěl bych jít do obchodu koupit chléb.",
    }[language]
    encoding = {"tr": "cp1254", "pl": "cp1250", "cs": "cp1250"}[language]
    assert decode_subtitle(text.encode(encoding), language) == (text, encoding)


def test_a_language_code_with_a_region_works():
    assert decode_subtitle(ARABIC.encode("cp1256"), "ar-EG")[0] == ARABIC
    assert decode_subtitle(ARABIC.encode("cp1256"), "AR")[0] == ARABIC


def test_a_wrong_language_hint_does_not_force_the_wrong_code_page():
    french = "Bonjour le monde, ça va très bien aujourd'hui. J'ai mangé près de l'église, déjà."
    assert decode_subtitle(french.encode("cp1252"), "ar")[0] == french
    assert decode_subtitle(ARABIC.encode("cp1256"), "fr")[0] == ARABIC


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_utf_encodings_are_read_with_or_without_a_byte_order_mark(encoding):
    text = ARABIC + "\n"
    raw = text.encode(encoding)
    if encoding in ("utf-16-le", "utf-16-be"):
        raw = codecs.BOM_UTF16_LE + raw if encoding.endswith("le") else codecs.BOM_UTF16_BE + raw
    assert decode_subtitle(raw, None)[0] == text


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("text", [
    # Only ASCII: valid UTF-8 as well, zeros and all.
    "1\n00:00:01,000 --> 00:00:03,000\nHello there\n",
    # Mostly timing lines around a little Arabic.
    SRT_TEXT,
    # Mostly Arabic: few zeros.
    "1\n00:00:01,000 --> 00:00:03,000\n" + ARABIC * 3 + "\n",
    # Chinese, Japanese: no zeros in the text itself.
    "1\n00:00:01,000 --> 00:00:03,000\n" + "漢字" * 40 + "\n",
])
def test_utf_16_without_a_byte_order_mark_is_recognised(text, encoding):
    assert decode_subtitle(text.encode(encoding), None) == (text, "utf-16")


def test_a_stray_zero_byte_does_not_turn_utf_8_into_utf_16():
    text = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا\x00 بالعالم\n" * 5
    assert decode_subtitle(text.encode("utf-8"), None) == (text, "utf-8")


def test_zeros_spread_over_both_halves_of_each_pair_are_not_utf_16():
    """In UTF-16 the zeros of the ASCII characters all sit on the same side of their pair."""
    assert decode_subtitle(b"ab\x00" * 30, None)[1] == "utf-8"


def test_binary_with_many_zeros_is_not_taken_for_utf_16():
    assert decode_subtitle(b"\x00" * 50, None)[1] != "utf-16"
    assert decode_subtitle(bytes(range(256)) * 4, None)[1] != "utf-16"


def test_utf_8_is_what_is_used_for_anything_that_is_valid_utf_8():
    assert decode_subtitle("café".encode("utf-8"), "ar") == ("café", "utf-8")  # Even against a hint.
    assert decode_subtitle(b"plain ascii", None) == ("plain ascii", "utf-8")


def test_nothing_makes_decoding_fail():
    assert decode_subtitle(b"", None)[0] == ""
    for raw in (b"\xff\xfe", b"\xff\xfe\x00\x00\x00", bytes(range(256)) * 4, b"\x00" * 50, b"\x80\x81\x82\x83"):
        text, used = decode_subtitle(raw, None)
        assert isinstance(text, str) and used


def test_bytes_that_fit_nothing_are_read_as_western_text_and_flagged():
    text, used = decode_subtitle(bytes(range(128, 256)) * 3, None)
    assert used == "cp1252?" and "�" in text or used == "cp1252?"


# --- Through the file reader ---

@pytest.mark.parametrize("name, encoding", [
    ("a.srt", "utf-8"), ("b.srt", "utf-8-sig"), ("c.srt", "utf-16"), ("d.srt", "cp1256"), ("e.SRT", "utf-8"),
])
def test_an_srt_file_in_any_of_these_encodings_gives_its_cues(tmp_path, name, encoding):
    captions, _messages = load(write(tmp_path, name, SRT_TEXT, encoding, newline="\r\n"))
    assert [(c.start, c.end, c.text) for c in captions] == [
        (1.0, 3.0, "مرحبا بالعالم"), (4.0, 6.5, "كيف حالك اليوم؟"),
    ]


@pytest.mark.parametrize(
    "name, encoding", [("a.vtt", "utf-8"), ("b.vtt", "utf-8-sig"), ("c.vtt", "cp1256"), ("d.vtt", "utf-16")]
)
def test_a_vtt_file_in_any_of_these_encodings_gives_its_cues(tmp_path, name, encoding):
    vtt = "WEBVTT\n\n00:01.000 --> 00:03.000\nمرحبا بالعالم\n\n00:04.000 --> 00:06.500\nكيف حالك اليوم؟\n"
    captions, messages = load(write(tmp_path, name, vtt, encoding))
    assert [(c.start, c.end, c.text) for c in captions] == [(1.0, 3.0, "مرحبا بالعالم"), (4.0, 6.5, "كيف حالك اليوم؟")]
    # A mark before WEBVTT is no malformed cue.
    assert not any("Skipped" in message for message in messages)


def test_the_log_says_what_a_file_that_is_not_utf_8_was_read_as(tmp_path):
    _captions, messages = load(write(tmp_path, "a.srt", SRT_TEXT, "cp1256"))
    assert messages == ["  The file is not UTF-8: read as Windows-1256."]
    _captions, messages = load(write(tmp_path, "b.srt", SRT_TEXT, "utf-16"))
    assert messages == ["  The file is not UTF-8: read as UTF-16."]


def test_a_utf_8_file_is_read_silently(tmp_path):
    assert load(write(tmp_path, "a.srt", SRT_TEXT))[1] == []
    assert load(write(tmp_path, "b.srt", SRT_TEXT, "utf-8-sig"))[1] == []


def test_a_file_whose_encoding_cannot_be_told_still_loads_and_warns(tmp_path):
    raw = b"1\n00:00:01,000 --> 00:00:03,000\n" + bytes(range(128, 200)) + b"\n"
    path = tmp_path / "a.srt"
    path.write_bytes(raw)
    captions, messages = load(str(path))
    assert len(captions) == 1 and any("could not tell this file's encoding" in message for message in messages)


def test_the_language_given_to_the_reader_picks_the_code_page(tmp_path):
    text = "1\n00:00:01,000 --> 00:00:03,000\nDzień dobry świecie, jak się dziś masz?\n\n"
    path = write(tmp_path, "a.srt", text, "cp1250")
    assert load(path, "pl")[0][0].text == "Dzień dobry świecie, jak się dziś masz?"


def test_a_file_that_is_not_subtitles_at_all_gives_no_cues_rather_than_an_error(tmp_path):
    path = tmp_path / "a.srt"
    path.write_bytes(bytes(range(256)) * 20)
    assert load(str(path))[0] == []


# --- The layouts ---

def test_cues_without_a_number_are_read(tmp_path):
    text = "00:00:01,000 --> 00:00:03,000\nFirst\n\n00:00:04,000 --> 00:00:06,000\nSecond\n"
    assert [c.text for c in load(write(tmp_path, "a.srt", text))[0]] == ["First", "Second"]


def test_a_dot_instead_of_the_comma_and_a_plain_arrow_are_read(tmp_path):
    text = "1\n00:00:01.500-->00:00:03.250\nFirst\n\n2\n0:00:04,5 --> 0:00:06,25\nSecond\n"
    captions = load(write(tmp_path, "a.srt", text))[0]
    assert [(c.start, c.end, c.text) for c in captions] == [(1.5, 3.25, "First"), (4.5, 6.25, "Second")]


def test_screen_coordinates_after_the_end_time_are_ignored(tmp_path):
    text = "1\n00:00:01,000 --> 00:00:03,000  X1:100 X2:600 Y1:50 Y2:80\nFirst\n"
    captions = load(write(tmp_path, "a.srt", text))[0]
    assert [(c.start, c.end, c.text) for c in captions] == [(1.0, 3.0, "First")]


def test_a_line_before_the_timing_line_and_several_text_lines_are_read(tmp_path):
    text = "Part 1\n1\n00:00:01,000 --> 00:00:03,000\nLine one\nLine two\n"
    assert load(write(tmp_path, "a.srt", text))[0][0].text == "Line one\nLine two"


def test_hours_over_ninety_nine_and_long_times_are_read(tmp_path):
    text = "1\n100:00:01,000 --> 100:00:03,000\nLate\n"
    assert load(write(tmp_path, "a.srt", text))[0][0].start == 360001.0


def test_extra_blank_lines_between_cues_do_not_lose_any(tmp_path):
    text = "1\n00:00:01,000 --> 00:00:03,000\nA\n\n\n\n2\n00:00:04,000 --> 00:00:06,000\nB\n"
    assert [c.text for c in load(write(tmp_path, "a.srt", text))[0]] == ["A", "B"]


def test_old_mac_line_endings_are_read(tmp_path):
    text = "1\r00:00:01,000 --> 00:00:03,000\rA\r\r2\r00:00:04,000 --> 00:00:06,000\rB\r"
    assert [c.text for c in load(write(tmp_path, "a.srt", text))[0]] == ["A", "B"]


def test_a_broken_cue_costs_that_cue_only_and_is_counted(tmp_path):
    text = ("1\n00:00:01,000 --> 00:00:03,000\nA\n\n2\nnot a time --> nonsense\nB\n\n"
            "3\n00:00:07,000 --> 00:00:08,000\nC\n\n4\n00:00:09,000 --> 00:00:10,000\n")
    captions, messages = load(write(tmp_path, "a.srt", text))
    assert [c.text for c in captions] == ["A", "C"]
    assert messages == ["  Skipped 2 malformed subtitle entries."]


def test_a_cue_with_no_text_is_skipped(tmp_path):
    captions, messages = load(write(tmp_path, "a.srt", "1\n00:00:01,000 --> 00:00:03,000\n"))
    assert captions == [] and messages == ["  Skipped 1 malformed subtitle entry."]


def test_a_vtt_with_notes_styles_identifiers_and_settings_is_read(tmp_path):
    vtt = ("WEBVTT - the title\n\nNOTE a comment\nover two lines\n\nSTYLE\n::cue { color: red }\n\n"
           "intro\n00:01.000 --> 00:03.000 align:start position:10%\nFirst\n\n01:00:04.500 --> 01:00:06.000\nSecond\n")
    captions = load(write(tmp_path, "a.vtt", vtt))[0]
    assert [(c.start, c.end, c.text) for c in captions] == [(1.0, 3.0, "First"), (3604.5, 3606.0, "Second")]


def test_what_is_saved_is_read_back_the_same(tmp_path):
    handler = SubtitleFileHandler()
    captions = [Caption(1.0, 3.5, "مرحبا"), Caption(4.0, 6.25, "Two\nlines")]
    for fmt, name in ((OutputFormat.SRT, "out.srt"), (OutputFormat.VTT, "out.vtt")):
        path = str(tmp_path / name)
        handler.save(captions, path, fmt, on_progress=lambda _message: None)
        assert handler.load(path, on_progress=lambda _message: None) == captions
