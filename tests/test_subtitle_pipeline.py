# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A subtitle file given to the app, from the file on disk to the translated file beside it.

The whole job runs with the models replaced by stand-ins: nothing here loads Whisper or the translation model.
"""

import pytest

from karamvt.config import AppConfig
from karamvt.core.languages import LANGUAGES
from karamvt.core.models import OutputFormat

ARABIC_SRT = "1\n00:00:01,000 --> 00:00:03,000\nمرحبا بالعالم\n\n2\n00:00:04,000 --> 00:00:06,500\nكيف حالك اليوم؟\n\n"


def write(tmp_path, name, text, encoding="utf-8"):
    """Write ``text`` to ``name`` in ``encoding`` with Windows line endings, as Windows editors do; return the path."""
    path = tmp_path / name
    path.write_bytes(text.replace("\n", "\r\n").encode(encoding))
    return str(path)


def run_job(pipelines, path, source, targets, fmt=OutputFormat.SRT):
    """Run a BatchPipeline on the file at ``path``; ``source`` and ``targets`` are language codes. Return its log."""
    log: list[str] = []
    pipelines.BatchPipeline(
        [path], LANGUAGES[source] if source else None, [LANGUAGES[code] for code in targets], fmt, AppConfig(),
        on_progress=log.append,
    ).run()
    return log


def test_an_arabic_subtitle_file_saved_as_windows_1256_is_translated_from_start_to_finish(pipelines, tmp_path):
    """Guard a file that used to be refused: Arabic text saved in the legacy Windows-1256 code page."""
    log = run_job(pipelines, write(tmp_path, "lecture.srt", ARABIC_SRT, "cp1256"), "ar", ["en"])
    output = (tmp_path / "lecture_en.srt").read_text(encoding="utf-8")
    assert "[en] مرحبا بالعالم" in output and "[en] كيف حالك اليوم؟" in output
    assert "  The file is not UTF-8: read as Windows-1256." in log
    assert not any("Skipping" in line for line in log)
    assert log[-1] == "\nProcess Completed Successfully!"


def test_the_same_file_with_the_language_left_on_auto_detect_is_read_right_first(pipelines, tmp_path):
    """Detect the language from the text, which therefore has to be decoded correctly first."""
    seen: list[str] = []

    def detect(text):
        seen.append(text)
        return LANGUAGES["ar"]

    pipelines.detect_language_from_text = detect
    run_job(pipelines, write(tmp_path, "lecture.srt", ARABIC_SRT, "cp1256"), None, ["en"])
    assert seen and "مرحبا بالعالم" in seen[0]
    assert "[en] مرحبا بالعالم" in (tmp_path / "lecture_en.srt").read_text(encoding="utf-8")


def test_a_utf_16_vtt_file_is_translated_into_two_languages(pipelines, tmp_path):
    path = write(tmp_path, "talk.vtt", "WEBVTT\n\n00:01.000 --> 00:03.000\nمرحبا بالعالم\n", "utf-16")
    run_job(pipelines, path, "ar", ["en", "fr"], OutputFormat.VTT)
    assert "[en] مرحبا بالعالم" in (tmp_path / "talk_en.vtt").read_text(encoding="utf-8")
    assert "[fr] مرحبا بالعالم" in (tmp_path / "talk_fr.vtt").read_text(encoding="utf-8")


RUSSIAN_SRT = "1\n00:00:01,000 --> 00:00:03,000\nПривет, мир! Как у тебя дела сегодня?\n\n"


def test_a_russian_subtitle_file_saved_as_windows_1251_is_translated_from_start_to_finish(pipelines, tmp_path):
    log = run_job(pipelines, write(tmp_path, "film.srt", RUSSIAN_SRT, "cp1251"), "ru", ["en"])
    assert "[en] Привет, мир! Как у тебя дела сегодня?" in (tmp_path / "film_en.srt").read_text(encoding="utf-8")
    assert "  The file is not UTF-8: read as Windows-1251." in log


def test_a_file_without_a_mark_in_another_code_page_than_the_language_default_is_still_read(pipelines, tmp_path):
    # The chosen language ("ar") usually comes in Windows-1256, but the text is Russian in Windows-1251: the code page
    # that is usual for the language is tried first, and is accepted only when the text it gives is plausible.
    log = run_job(pipelines, write(tmp_path, "film.srt", RUSSIAN_SRT, "cp1251"), "ar", ["en"])
    assert "[en] Привет, мир! Как у тебя дела сегодня?" in (tmp_path / "film_en.srt").read_text(encoding="utf-8")
    assert "  The file is not UTF-8: read as Windows-1251." in log


def test_a_subtitle_file_can_also_be_saved_untranslated_in_utf_8(pipelines, tmp_path):
    """Transcribe-only on a subtitle file reads the cues and writes them out again, now as UTF-8."""
    run_job(pipelines, write(tmp_path, "lecture.srt", ARABIC_SRT, "cp1256"), "ar", [])
    output = (tmp_path / "lecture_ar.srt").read_bytes()
    assert "مرحبا بالعالم".encode("utf-8") in output


def test_a_file_with_nothing_readable_fails_the_job_with_a_reason(pipelines, tmp_path):
    path = tmp_path / "empty.srt"
    path.write_bytes(b"")
    log: list[str] = []
    with pytest.raises(ValueError, match="No file in the batch produced any usable captions"):
        pipelines.BatchPipeline(
            [str(path)], LANGUAGES["ar"], [LANGUAGES["en"]], OutputFormat.SRT, AppConfig(), on_progress=log.append,
        ).run()
    assert any("couldn't parse any cues from the SRT file" in line for line in log)


def test_the_source_language_chosen_for_the_job_decides_between_code_pages_that_look_alike(pipelines, tmp_path):
    """Windows-1250 and Windows-1252 differ in a few Polish letters only, so the language the user chose settles it."""
    polish = "1\n00:00:01,000 --> 00:00:03,000\nDzień dobry świecie, jak się dziś masz?\n\n"
    run_job(pipelines, write(tmp_path, "film.srt", polish, "cp1250"), "pl", ["en"])
    assert "[en] Dzień dobry świecie, jak się dziś masz?" in (tmp_path / "film_en.srt").read_text(encoding="utf-8")
