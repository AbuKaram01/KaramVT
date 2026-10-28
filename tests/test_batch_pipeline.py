# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A whole batch (core/pipelines.py): videos and subtitle files in, subtitle files out.

Also covers what a stopped job leaves behind for the next run to pick up. Speech recognition and translation are
stand-ins (model_stand_ins.py).
"""

import gc
import json
import os
import time

import pytest
from model_stand_ins import SPEECH

from karamvt.config import AppConfig
from karamvt.core import checkpoint
from karamvt.core.cancel import CancelToken, OperationCancelled
from karamvt.core.checkpoint import JobStore, TRANSCRIPT_UNIT, model_signature, unit_header
from karamvt.core.languages import LANGUAGES
from karamvt.core.llm_settings import sampling_from_config
from karamvt.core.models import Caption, OutputFormat

LATER_THAN_A_MONTH = 31 * 86400
SAVED = "Using the saved transcription (4 segments, English) — skipping speech recognition."
DONE_BEFORE = "\nEverything in this batch was already completed in an earlier run."


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "resume")


@pytest.fixture
def config(tmp_path):
    """An AppConfig whose Whisper folder and LLM file exist."""
    whisper = tmp_path / "whisper"
    whisper.mkdir()
    (whisper / "model.bin").write_bytes(b"w")
    llm = tmp_path / "llm.gguf"
    llm.write_bytes(b"l")
    return AppConfig(whisper_model_path=str(whisper), llm_model_path=str(llm))


def video(tmp_path, name="talk.mp4"):
    """The path of a video, made the first time and left alone after (a rewritten file is a changed one)."""
    path = tmp_path / name
    if not path.exists():
        path.write_bytes(b"not really a video")
    return str(path)


def build(pipelines, inputs, source, targets, config, store, **more):
    """Build a BatchPipeline; return it with the messages it said and the percentages it reported."""
    log: list[str] = []
    percents: list[float] = []
    pipeline = pipelines.BatchPipeline(
        inputs, LANGUAGES[source] if source else None, [LANGUAGES[code] for code in targets],
        more.pop("fmt", OutputFormat.SRT), config, on_progress=log.append, on_percent=percents.append,
        store=store, **more,
    )
    return pipeline, log, percents


def run(pipelines, inputs, source, targets, config, store, **more):
    """Build and run a pipeline; return what it said and the percentages it reported."""
    pipeline, log, percents = build(pipelines, inputs, source, targets, config, store, **more)
    pipeline.run()
    return log, percents


def lines_of(log, prefix):
    return [line for line in log if line.startswith(prefix)]


def read(path):
    return path.read_text(encoding="utf-8")


def finished_earlier(store, config, path, unit, output, src="en"):
    """What an earlier run leaves for an output it finished: the file, and the marker saying so."""
    output.write_text("finished earlier")
    llm_sig = None if unit == TRANSCRIPT_UNIT else model_signature(config.llm_model_path)
    store.mark_done(path, unit_header(path, src, unit, llm_sig, ".srt"), str(output))


def steps(units, lines=4, done=0):
    """The percentages of a job whose units each have `lines` lines, `done` of the units being finished before it."""
    percents = []
    for unit in range(done, done + units):
        percents += [(unit + (i + 1) / lines) / (done + units) for i in range(lines)]
        percents.append((unit + 1) / (done + units))
    return percents


# --- One video ---

def test_a_video_is_transcribed_translated_and_saved_beside_itself(pipelines, fakes, store, config, tmp_path):
    log, percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    output = read(tmp_path / "talk_ar.srt")
    assert all(f"[ar] {text}" in output for text in ("Hello there", "How are you", "Goodbye", "See you"))
    assert log[0] == "Video file detected — starting transcription..."
    assert "Translating 4 segments  (English → Arabic) ..." in log
    assert lines_of(log, "  Processed") == [f"  Processed {i}/4" for i in range(1, 5)]
    assert log[-1] == "\nProcess Completed Successfully!"
    assert percents == pytest.approx([0.0, *steps(1)])
    assert store.count_files() == 0  # The job is complete, so its saved progress has served its purpose.


def test_each_line_goes_to_the_model_without_its_spaces_and_with_the_line_before_it_translated(
    pipelines, fakes, store, config, tmp_path,
):
    run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert fakes.translator.calls == [
        ("ar", "Hello there", None), ("ar", "How are you", "[ar] Hello there"),
        ("ar", "Goodbye", "[ar] How are you"), ("ar", "See you", "[ar] Goodbye"),
    ]


def test_the_models_are_loaded_the_way_the_settings_say(pipelines, fakes, store, config, tmp_path):
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert fakes.devices == ["auto"]
    [whisper] = fakes.whisper_loads
    assert whisper == {"path": config.whisper_model_path, "device": "cpu", "compute_type": "int8",
                       "cpu_threads": 4, "on_progress": whisper["on_progress"]}
    [llm] = fakes.llm_loads
    assert (llm["path"], llm["cpu_threads"], llm["n_gpu_layers"]) == (config.llm_model_path, 4, 7)
    [call] = fakes.transcriber.calls
    assert call["model"] == "the whisper model" and call["language"] == "en" and call["keep"] is None
    [engine] = fakes.translator.created
    assert engine["model"] == config.llm_model_path and engine["sampling"] == sampling_from_config(config)
    assert (engine["source"].name, engine["target"].name) == ("English", "Arabic")


def test_the_language_of_a_video_is_detected_when_none_was_chosen(pipelines, fakes, store, config, tmp_path):
    run(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    assert fakes.transcriber.detected == ["talk.mp4"]
    assert fakes.transcriber.calls[0]["language"] == "en"
    assert (tmp_path / "talk_ar.srt").exists()


def test_a_video_can_be_transcribed_without_translating_it(pipelines, fakes, store, config, tmp_path):
    log, percents = run(pipelines, [video(tmp_path)], "en", [], config, store)
    assert "Hello there" in read(tmp_path / "talk_en.srt")
    assert fakes.translator.calls == [] and fakes.llm_loads == []
    assert percents == [1.0] and log[-1] == "\nProcess Completed Successfully!"
    assert store.count_files() == 0


def test_the_output_can_be_in_another_folder_and_format(pipelines, fakes, store, config, tmp_path):
    out = tmp_path / "out"
    run(pipelines, [video(tmp_path)], "en", ["ar"], config, store, fmt=OutputFormat.VTT, output_dir=str(out))
    assert read(out / "talk_ar.vtt").startswith("WEBVTT")


def test_the_output_folder_can_mirror_the_folders_of_a_course(pipelines, fakes, store, config, tmp_path):
    course = tmp_path / "course"
    (course / "week1").mkdir(parents=True)
    lesson = video(course / "week1", "lesson.mp4")
    run(pipelines, [lesson], "en", ["ar"], config, store, output_dir=str(tmp_path / "out"), input_root=str(course))
    assert (tmp_path / "out" / "week1" / "lesson_ar.srt").exists()


# --- Several videos and several languages ---

def test_every_model_is_loaded_once_for_the_whole_batch(pipelines, fakes, store, config, tmp_path):
    first, second = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    log, percents = run(pipelines, [first, second], "en", ["ar", "fr"], config, store)
    assert len(fakes.whisper_loads) == 1 and len(fakes.llm_loads) == 1
    assert [call["video"] for call in fakes.transcriber.calls] == ["a.mp4", "b.mp4"]
    assert len({id(engine["llm"]) for engine in fakes.translator.created}) == 1 and len(fakes.translator.created) == 4
    assert "\n=== File 1/2: a.mp4 ===" in log and "\n=== File 2/2: b.mp4 ===" in log
    assert percents == pytest.approx([0.0, *steps(4)])
    for name in ("a_ar", "a_fr", "b_ar", "b_fr"):
        assert (tmp_path / f"{name}.srt").exists()


def test_each_language_is_translated_from_the_original_lines(pipelines, fakes, store, config, tmp_path):
    run(pipelines, [video(tmp_path)], "en", ["ar", "fr"], config, store)
    assert "[fr] Hello there" in read(tmp_path / "talk_fr.srt")
    assert "[ar] [fr]" not in read(tmp_path / "talk_ar.srt") and "[fr] [ar]" not in read(tmp_path / "talk_fr.srt")


def test_a_target_that_is_the_source_language_is_skipped(pipelines, fakes, store, config, tmp_path):
    log, percents = run(pipelines, [video(tmp_path)], "en", ["en", "ar"], config, store)
    assert "Skipping English for talk.mp4: same as source language." in log
    assert not (tmp_path / "talk_en.srt").exists() and (tmp_path / "talk_ar.srt").exists()
    assert percents == pytest.approx([0.0, 0.5, 0.625, 0.75, 0.875, 1.0, 1.0])


def test_a_video_without_speech_is_skipped_and_the_others_go_on(pipelines, fakes, store, config, tmp_path):
    fakes.transcriber.script = {"a.mp4": []}
    first, second = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    log, percents = run(pipelines, [first, second], "en", ["ar"], config, store)
    assert "Skipping a.mp4: no speech detected in the audio" in log
    assert not (tmp_path / "a_ar.srt").exists() and (tmp_path / "b_ar.srt").exists()
    assert percents == pytest.approx([0.0, *steps(1)])


def test_a_batch_where_nothing_has_speech_fails_with_a_reason(pipelines, fakes, store, config, tmp_path):
    fakes.transcriber.script = {"talk.mp4": []}
    pipeline, log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    with pytest.raises(ValueError, match="No file in the batch produced any usable captions"):
        pipeline.run()
    assert "Skipping talk.mp4: no speech detected in the audio" in log


def test_two_inputs_that_would_get_the_same_output_name_do_not_overwrite_each_other(
    pipelines, fakes, store, config, tmp_path,
):
    subtitles = tmp_path / "lecture.srt"
    subtitles.write_text("1\n00:00:01,000 --> 00:00:03,000\nGood morning\n\n", encoding="utf-8")
    log, _percents = run(pipelines, [video(tmp_path, "lecture.mp4"), str(subtitles)], "en", ["ar"], config, store)
    assert "[ar] Hello there" in read(tmp_path / "lecture_ar.srt")
    assert "[ar] Good morning" in read(tmp_path / "lecture_ar (2).srt")
    assert ("Note: lecture_ar.srt is already used by another file in this batch "
            "— saving lecture.srt as lecture_ar (2).srt instead.") in log


def test_the_name_of_an_output_finished_earlier_is_not_given_to_another_input(
    pipelines, fakes, store, config, tmp_path,
):
    lecture = video(tmp_path, "lecture.mp4")
    finished_earlier(store, config, lecture, "ar", tmp_path / "lecture_ar.srt")
    subtitles = subtitle_file(tmp_path, ["Good morning"], "lecture.srt")
    run(pipelines, [lecture, subtitles], "en", ["ar"], config, store)
    assert read(tmp_path / "lecture_ar.srt") == "finished earlier"
    assert "[ar] Good morning" in read(tmp_path / "lecture_ar (2).srt")


def test_the_name_of_one_language_finished_earlier_is_kept_while_the_other_languages_are_made(
    pipelines, fakes, store, config, tmp_path,
):
    lecture = video(tmp_path, "lecture.mp4")
    finished_earlier(store, config, lecture, "ar", tmp_path / "lecture_ar.srt")
    subtitles = subtitle_file(tmp_path, ["Good morning"], "lecture.srt")
    run(pipelines, [lecture, subtitles], "en", ["ar", "fr"], config, store)
    assert read(tmp_path / "lecture_ar.srt") == "finished earlier"
    assert "[ar] Good morning" in read(tmp_path / "lecture_ar (2).srt")
    assert "[fr] Hello there" in read(tmp_path / "lecture_fr.srt") and (tmp_path / "lecture_fr (2).srt").exists()


def test_names_are_given_out_to_the_first_input_that_asks_and_kept_for_it(pipelines, tmp_path):
    names = pipelines.OutputNames()
    path = str(tmp_path / "x_ar.srt")
    assert names.claim("a.mp4", path) == path
    assert names.claim("a.mp4", path) == path  # The same input keeps its name.
    assert names.claim("b.mp4", path) == str(tmp_path / "x_ar (2).srt")
    assert names.claim("c.mp4", path) == str(tmp_path / "x_ar (3).srt")
    assert names.claim("b.mp4", path) == str(tmp_path / "x_ar (2).srt")
    names.reserve("d.mp4", str(tmp_path / "y_ar.srt"))
    assert names.claim("e.mp4", str(tmp_path / "y_ar.srt")) == str(tmp_path / "y_ar (2).srt")
    assert names.claim("d.mp4", str(tmp_path / "y_ar.srt")) == str(tmp_path / "y_ar.srt")


def test_where_an_output_goes_is_decided_by_one_rule_the_window_can_ask_too(pipelines, tmp_path):
    resolve_batch_output_path = pipelines.resolve_batch_output_path
    mp4 = str(tmp_path / "course" / "week1" / "a.mp4")
    assert resolve_batch_output_path(mp4, "ar", OutputFormat.SRT) == str(tmp_path / "course" / "week1" / "a_ar.srt")
    flat = resolve_batch_output_path(mp4, "ar", OutputFormat.VTT, output_dir=str(tmp_path / "out"))
    assert flat == str(tmp_path / "out" / "a_ar.vtt") and (tmp_path / "out").is_dir()
    mirrored = resolve_batch_output_path(
        mp4, "ar", OutputFormat.SRT, output_dir=str(tmp_path / "out2"), input_root=str(tmp_path / "course"),
    )
    assert mirrored == str(tmp_path / "out2" / "week1" / "a_ar.srt") and (tmp_path / "out2" / "week1").is_dir()


def test_asking_where_an_output_goes_creates_no_folder(pipelines, tmp_path):
    mp4 = str(tmp_path / "course" / "week1" / "a.mp4")
    flat = pipelines.batch_output_path(mp4, "ar", OutputFormat.VTT, output_dir=str(tmp_path / "out"))
    assert flat == str(tmp_path / "out" / "a_ar.vtt")
    mirrored = pipelines.batch_output_path(
        mp4, "ar", OutputFormat.SRT, output_dir=str(tmp_path / "out2"), input_root=str(tmp_path / "course"),
    )
    assert mirrored == str(tmp_path / "out2" / "week1" / "a_ar.srt")
    assert not (tmp_path / "out").exists() and not (tmp_path / "out2").exists()


def test_progress_nobody_came_back_to_is_cleared_when_a_job_starts(pipelines, fakes, store, config, tmp_path):
    forgotten = video(tmp_path, "forgotten.mp4")
    store.save_transcript(forgotten, SPEECH, "en", "English", model_signature(config.whisper_model_path))
    old = store._path(forgotten, "transcript.json")
    os.utime(old, (time.time() - LATER_THAN_A_MONTH, time.time() - LATER_THAN_A_MONTH))
    run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert not old.exists()


def test_the_speech_model_is_let_go_of_before_the_translation_model_is_loaded(
    pipelines, fakes, store, config, tmp_path,
):
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    pipeline.run()
    assert fakes.whisper_held_when_llm_loads == [False]
    fakes.translator.created.clear()
    gc.collect()
    assert fakes.llm_refs[0]() is None  # Nor is the translation model kept by a finished job.


def subtitle_file(tmp_path, cues, name="talk.srt"):
    """Write an SRT file with one short cue per text in ``cues`` and return its path."""
    path = tmp_path / name
    path.write_text("".join(f"{i}\n00:00:0{i},000 --> 00:00:0{i},500\n{text}\n\n" for i, text in enumerate(cues, 1)),
                    encoding="utf-8")
    return str(path)


def test_the_language_of_a_subtitle_file_is_told_from_its_first_sixty_lines(
    pipelines, fakes, store, config, tmp_path, monkeypatch,
):
    samples = []
    monkeypatch.setattr(pipelines, "detect_language_from_text", lambda text: samples.append(text) or LANGUAGES["en"])
    cues = [f"line {i}" for i in range(1, 10)]
    path = subtitle_file(tmp_path, cues)
    log, _percents = run(pipelines, [path], None, ["ar"], config, store)
    assert samples == [" ".join(cues)]
    assert log[:3] == ["SRT file detected \u2014 skipping audio extraction and transcription.",
                       "  Auto-detecting language from subtitle text...", "  Detected: English"]
    assert "[ar] line 1" in read(tmp_path / "talk_ar.srt")


def test_a_language_that_cannot_be_told_from_a_subtitle_file_is_reported_with_the_way_out(
    pipelines, fakes, store, config, tmp_path, monkeypatch,
):
    monkeypatch.setattr(pipelines, "detect_language_from_text", lambda text: None)
    pipeline, log, _percents = build(pipelines, [subtitle_file(tmp_path, ["12345"])], None, ["ar"], config, store)
    with pytest.raises(ValueError, match="No file in the batch"):
        pipeline.run()
    assert ("Skipping talk.srt: could not match the subtitle text to a supported language "
            "\u2014 choose the source language manually") in log


# --- Stopping, and picking up again ---

def stopped_translation(pipelines, fakes, store, config, tmp_path, after=2, name="talk.mp4"):
    """A job whose translation model crashes after `after` lines; what it leaves is what the next run starts from."""
    fakes.translator.fail_after = after
    pipeline, log, _percents = build(pipelines, [video(tmp_path, name)], "en", ["ar"], config, store)
    with pytest.raises(RuntimeError, match="crashed"):
        pipeline.run()
    fakes.translator.fail_after = None
    fakes.translator.calls.clear()
    path = video(tmp_path, name)
    assert store.load_partial_transcript(path, "en", model_signature(config.whisper_model_path)) == (None, None)
    return path


def test_a_translation_that_stopped_continues_from_its_first_untranslated_line(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_translation(pipelines, fakes, store, config, tmp_path)
    log, percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert len(fakes.transcriber.calls) == 1 and len(fakes.whisper_loads) == 1  # Nothing was recognized again.
    assert SAVED in log and "Video file detected — starting transcription..." not in log
    assert "Translating 4 segments  (English → Arabic) ..." in log
    assert "  Resuming from segment 3/4 — 2 already translated." in log
    assert fakes.translator.calls == [("ar", "Goodbye", "[ar] How are you"), ("ar", "See you", "[ar] Goodbye")]
    assert percents == pytest.approx([0.0, 0.5, 0.75, 1.0, 1.0])
    output = read(tmp_path / "talk_ar.srt")
    assert all(f"[ar] {text}" in output for text in ("Hello there", "How are you", "Goodbye", "See you"))
    assert store.count_files() == 0


def test_the_note_about_changed_settings_is_given_when_a_stopped_translation_continues(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_translation(pipelines, fakes, store, config, tmp_path)
    config.llm_temperature = 0.7
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert ("  Note: the translation settings changed since this job was stopped — "
            "the remaining segments use the new settings.") in log
    assert [call[1] for call in fakes.translator.calls] == ["Goodbye", "See you"]


def test_no_such_note_when_the_settings_are_the_same(pipelines, fakes, store, config, tmp_path):
    stopped_translation(pipelines, fakes, store, config, tmp_path)
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert not [line for line in log if "settings changed" in line]


def test_a_stopped_translation_is_not_continued_when_the_transcript_it_was_made_from_changed(
    pipelines, fakes, store, config, tmp_path,
):
    path = stopped_translation(pipelines, fakes, store, config, tmp_path)
    changed = [Caption(0.0, 1.0, "Something else"), *SPEECH[1:]]
    store.save_transcript(path, changed, "en", "English", model_signature(config.whisper_model_path))
    log, _percents = run(pipelines, [path], "en", ["ar"], config, store)
    assert "Saved translation progress not reused: the transcript changed." in log
    assert [call[1] for call in fakes.translator.calls] == ["Something else", "How are you", "Goodbye", "See you"]


def test_progress_saved_without_a_record_of_the_settings_is_continued_without_a_note(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_translation(pipelines, fakes, store, config, tmp_path)
    [journal] = [entry for entry in store.root.iterdir() if entry.name.endswith("ar.journal")]
    lines = journal.read_text(encoding="utf-8").split("\n")
    header = json.loads(lines[0])["d"]
    del header["sampling"]
    journal.write_text("\n".join([checkpoint._seal(header), *lines[1:]]), encoding="utf-8")
    config.llm_temperature = 0.7
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert "  Resuming from segment 3/4 \u2014 2 already translated." in log
    assert not [line for line in log if "settings changed" in line]


def test_a_stopped_translation_is_not_continued_with_another_translation_model(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_translation(pipelines, fakes, store, config, tmp_path)
    with open(config.llm_model_path, "ab") as handle:
        handle.write(b"a newer model")
    log, percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert "Saved translation progress not reused: the translation model changed." in log
    assert SAVED in log and not [line for line in log if "Resuming" in line]
    assert [call[1] for call in fakes.translator.calls] == ["Hello there", "How are you", "Goodbye", "See you"]
    assert percents == pytest.approx([0.0, *steps(1)])


def test_a_video_that_changed_is_recognized_again_and_translated_from_the_start(
    pipelines, fakes, store, config, tmp_path,
):
    path = stopped_translation(pipelines, fakes, store, config, tmp_path)
    os.utime(path, ns=(1, 12345))
    log, _percents = run(pipelines, [path], "en", ["ar"], config, store)
    assert "Saved transcription not reused: the input file changed." in log
    assert "Saved translation progress not reused: the input file changed." in log
    assert len(fakes.transcriber.calls) == 2 and len(fakes.translator.calls) == 4


def test_a_translation_that_finished_all_its_lines_but_was_not_saved_is_saved_without_translating_again(
    pipelines, fakes, store, config, tmp_path, monkeypatch,
):
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pipeline._subtitle_handler, "save", refuse)
    with pytest.raises(OSError):
        pipeline.run()
    fakes.translator.calls.clear()
    log, percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert fakes.translator.calls == []
    assert "  All 4 segments were already translated \u2014 saving the result." in log
    assert not [line for line in log if "Resuming from segment" in line or "settings changed" in line]
    assert "[ar] See you" in read(tmp_path / "talk_ar.srt")
    assert percents == pytest.approx([0.0, 1.0, 1.0])


def test_stop_takes_effect_after_the_line_being_translated_and_loses_none_of_them(
    pipelines, fakes, store, config, tmp_path,
):
    token = CancelToken()
    fakes.translator.after_call = lambda count: token.cancel() if count == 2 else None
    path = video(tmp_path)
    pipeline, _log, _percents = build(pipelines, [path], "en", ["ar"], config, store, cancel=token)
    with pytest.raises(OperationCancelled):
        pipeline.run()
    assert len(fakes.translator.calls) == 2 and not (tmp_path / "talk_ar.srt").exists()
    report = store.scan([path], "en", ["ar"], config.llm_model_path, config.whisper_model_path, ".srt", None, None)
    assert report.transcripts == [path]
    assert [(u.state, u.lines_done, u.lines_total) for u in report.units] == [("partial", 2, 4)]


def test_stop_between_two_languages_ends_the_job_before_the_next_one_starts(
    pipelines, fakes, store, config, tmp_path,
):
    token = CancelToken()
    fakes.translator.after_call = lambda count: token.cancel() if count == 4 else None
    pipeline, log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar", "fr"], config, store, cancel=token)
    with pytest.raises(OperationCancelled):
        pipeline.run()
    assert (tmp_path / "talk_ar.srt").exists() and not (tmp_path / "talk_fr.srt").exists()
    assert not [line for line in log if "French" in line]


def test_stop_before_anything_starts_loads_nothing(pipelines, fakes, store, config, tmp_path):
    token = CancelToken()
    token.cancel()
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store, cancel=token)
    with pytest.raises(OperationCancelled):
        pipeline.run()
    assert fakes.whisper_loads == [] and fakes.llm_loads == []


def test_what_is_kept_for_progress_is_closed_when_a_job_stops_or_crashes(pipelines, fakes, store, config, tmp_path):
    closed = []

    def closing(thing, name):
        original = thing.close
        thing.close = lambda: (closed.append(name), original())
        return thing

    start_partial, open_journal = store.start_partial_transcript, store.open_journal
    store.start_partial_transcript = lambda *args, **kwargs: closing(start_partial(*args, **kwargs), "partial")

    def journal(*args, **kwargs):
        opened, note = open_journal(*args, **kwargs)
        return closing(opened, "journal"), note

    store.open_journal = journal
    fakes.transcriber.crash_after = {"talk.mp4": 1}
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    with pytest.raises(RuntimeError, match="went to sleep"):
        pipeline.run()
    assert closed == ["partial"]
    fakes.transcriber.crash_after = {}
    fakes.translator.fail_after = 1
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    with pytest.raises(RuntimeError, match="translation model crashed"):
        pipeline.run()
    assert closed == ["partial", "partial", "journal"]


def test_the_token_reaches_the_speech_recognition(pipelines, fakes, store, config, tmp_path):
    token = CancelToken()
    run(pipelines, [video(tmp_path)], "en", [], config, store, cancel=token)
    assert fakes.transcriber.calls[0]["cancel"] is token


def test_the_files_finished_by_a_stopped_batch_are_skipped_when_it_is_run_again(
    pipelines, fakes, store, config, tmp_path,
):
    first, second = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    fakes.translator.fail_after = 4  # All of a.mp4 is translated, then the crash.
    pipeline, _log, _percents = build(pipelines, [first, second], "en", ["ar"], config, store)
    with pytest.raises(RuntimeError):
        pipeline.run()
    finished = read(tmp_path / "a_ar.srt")
    assert len([entry for entry in store.root.iterdir() if entry.name.endswith(".journal")]) == 1  # Not a.mp4's.
    fakes.translator.fail_after = None
    fakes.translator.calls.clear()
    log, percents = run(pipelines, [first, second], "en", ["ar"], config, store)
    assert "Already completed in an earlier run — skipping a.mp4." in log
    assert "\n=== File 1/2: a.mp4 ===" in log and "Using the saved transcription" in " ".join(log)
    assert {call[1] for call in fakes.translator.calls} == {"Hello there", "How are you", "Goodbye", "See you"}
    assert len(fakes.translator.calls) == 4 and len(fakes.whisper_loads) == 1
    assert read(tmp_path / "a_ar.srt") == finished
    assert percents == pytest.approx([0.5, *steps(1, done=1)])


def test_every_language_finished_earlier_counts_as_work_done_in_the_bar(pipelines, fakes, store, config, tmp_path):
    done, fresh = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    finished_earlier(store, config, done, "ar", tmp_path / "a_ar.srt")
    finished_earlier(store, config, done, "fr", tmp_path / "a_fr.srt")
    log, percents = run(pipelines, [done, fresh], "en", ["ar", "fr"], config, store)
    assert [call["video"] for call in fakes.transcriber.calls] == ["b.mp4"]
    assert percents == pytest.approx([0.5, *steps(2, done=2)])


def test_one_language_finished_and_another_not_skips_only_the_finished_one(pipelines, fakes, store, config, tmp_path):
    path = video(tmp_path)
    finished = tmp_path / "talk_ar.srt"
    finished.write_text("finished earlier")
    header = unit_header(path, "en", "ar", model_signature(config.llm_model_path), ".srt")
    store.mark_done(path, header, str(finished))
    log, percents = run(pipelines, [path], "en", ["ar", "fr"], config, store)
    assert "Already completed in an earlier run — skipping Arabic for talk.mp4." in log
    assert [call[0] for call in fakes.translator.calls] == ["fr"] * 4
    assert read(finished) == "finished earlier" and (tmp_path / "talk_fr.srt").exists()
    assert percents == pytest.approx([0.0, 0.5, 0.625, 0.75, 0.875, 1.0, 1.0])


def test_a_batch_that_was_finished_earlier_says_so_and_does_nothing(pipelines, fakes, store, config, tmp_path):
    path = video(tmp_path)
    finished = tmp_path / "talk_ar.srt"
    finished.write_text("finished earlier")
    store.mark_done(path, unit_header(path, "en", "ar", model_signature(config.llm_model_path), ".srt"), str(finished))
    log, percents = run(pipelines, [path], "en", ["ar"], config, store)
    assert log == ["Already completed in an earlier run — skipping talk.mp4.", DONE_BEFORE]
    assert percents == [1.0] and read(finished) == "finished earlier"
    assert fakes.whisper_loads == [] and fakes.llm_loads == [] and store.count_files() == 0


def test_a_finished_transcription_counts_as_work_done_in_the_bar(pipelines, fakes, store, config, tmp_path):
    done, fresh = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    finished_earlier(store, config, done, TRANSCRIPT_UNIT, tmp_path / "a_en.srt")
    log, percents = run(pipelines, [done, fresh], "en", [], config, store)
    assert percents == [1.0] and read(tmp_path / "a_en.srt") == "finished earlier"


def test_the_transcriptions_finished_by_a_stopped_batch_are_skipped_when_it_is_run_again(
    pipelines, fakes, store, config, tmp_path, monkeypatch,
):
    first, second = video(tmp_path, "a.mp4"), video(tmp_path, "b.mp4")
    token = CancelToken()
    pipeline, _log, _percents = build(pipelines, [first, second], "en", [], config, store, cancel=token)
    save = pipeline._subtitle_handler.save

    def save_then_stop(*args, **kwargs):
        save(*args, **kwargs)
        token.cancel()

    monkeypatch.setattr(pipeline._subtitle_handler, "save", save_then_stop)
    with pytest.raises(OperationCancelled):
        pipeline.run()
    assert (tmp_path / "a_en.srt").exists() and not (tmp_path / "b_en.srt").exists()
    log, _percents = run(pipelines, [first, second], "en", [], config, store)
    assert "Already completed in an earlier run \u2014 skipping a.mp4." in log and (tmp_path / "b_en.srt").exists()


def test_a_finished_transcription_is_skipped_too(pipelines, fakes, store, config, tmp_path):
    path = video(tmp_path)
    finished = tmp_path / "talk_en.srt"
    finished.write_text("finished earlier")
    store.mark_done(path, unit_header(path, "en", TRANSCRIPT_UNIT, None, ".srt"), str(finished))
    log, _percents = run(pipelines, [path], "en", [], config, store)
    assert log[-1] == DONE_BEFORE and fakes.whisper_loads == []


def test_a_finished_output_that_was_changed_since_is_made_again(pipelines, fakes, store, config, tmp_path):
    path = video(tmp_path)
    finished = tmp_path / "talk_ar.srt"
    finished.write_text("finished earlier")
    store.mark_done(path, unit_header(path, "en", "ar", model_signature(config.llm_model_path), ".srt"), str(finished))
    finished.write_text("edited by hand, and longer")
    run(pipelines, [path], "en", ["ar"], config, store)
    assert "[ar] Hello there" in read(finished)


# --- A transcription that stopped ---

def stopped_transcription(pipelines, fakes, store, config, tmp_path, source=None):
    """Run a job whose transcription crashes after three segments, with resuming on; the saved segments stay behind."""
    config.resume_transcription = True
    fakes.transcriber.language = LANGUAGES["fr"]
    fakes.transcriber.crash_after = {"talk.mp4": 3}
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], source, ["ar"], config, store)
    with pytest.raises(RuntimeError, match="went to sleep"):
        pipeline.run()
    fakes.transcriber.crash_after = {}
    fakes.transcriber.calls.clear()


def test_every_segment_is_kept_as_it_is_produced(pipelines, fakes, store, config, tmp_path):
    stopped_transcription(pipelines, fakes, store, config, tmp_path)
    partial, why = store.load_partial_transcript(video(tmp_path), None, model_signature(config.whisper_model_path))
    assert why is None and partial.captions == SPEECH[:3] and partial.lang_code == "fr"


def test_a_transcription_that_stopped_is_continued_when_asked_to(pipelines, fakes, store, config, tmp_path):
    stopped_transcription(pipelines, fakes, store, config, tmp_path)
    log, _percents = run(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    [call] = fakes.transcriber.calls
    assert call["keep"] == SPEECH[:2] and call["language"] == "fr"  # The last segment is done again.
    assert fakes.transcriber.detected == ["talk.mp4"]  # Only by the first run.
    assert "Note: the join point of a resumed transcription can occasionally cut or repeat a word." in log
    assert "[ar] See you" in read(tmp_path / "talk_ar.srt")


def test_stopping_again_straight_after_continuing_a_transcription_loses_no_segment(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_transcription(pipelines, fakes, store, config, tmp_path)
    fakes.transcriber.crash_after = {"talk.mp4": 2}  # Nothing new is produced before it stops.
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    with pytest.raises(RuntimeError, match="went to sleep"):
        pipeline.run()
    fakes.transcriber.crash_after = {}
    fakes.transcriber.calls.clear()
    run(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    assert fakes.transcriber.calls[0]["keep"] == SPEECH[:2]


def test_a_transcription_that_stopped_is_started_over_when_not_asked_to_continue(
    pipelines, fakes, store, config, tmp_path,
):
    stopped_transcription(pipelines, fakes, store, config, tmp_path)
    config.resume_transcription = False
    log, _percents = run(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    [call] = fakes.transcriber.calls
    assert call["keep"] is None and call["language"] == "fr"  # Detected again.
    assert fakes.transcriber.detected == ["talk.mp4", "talk.mp4"]
    assert not [line for line in log if "join point" in line or "partial" in line]


def test_a_transcription_made_with_another_model_is_not_continued(pipelines, fakes, store, config, tmp_path):
    stopped_transcription(pipelines, fakes, store, config, tmp_path)
    with open(os.path.join(config.whisper_model_path, "extra.bin"), "wb") as handle:
        handle.write(b"another model file")
    log, _percents = run(pipelines, [video(tmp_path)], None, ["ar"], config, store)
    assert "Saved partial transcription not reused: the speech-recognition model changed." in log
    assert fakes.transcriber.calls[0]["keep"] is None


def test_a_transcription_with_nothing_worth_keeping_starts_over_silently(pipelines, fakes, store, config, tmp_path):
    config.resume_transcription = True
    fakes.transcriber.crash_after = {"talk.mp4": 1}
    pipeline, _log, _percents = build(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    with pytest.raises(RuntimeError):
        pipeline.run()
    fakes.transcriber.crash_after = {}
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, store)
    assert fakes.transcriber.calls[-1]["keep"] is None
    assert not [line for line in log if "join point" in line]


# --- Progress that cannot be saved ---

def test_a_job_whose_progress_cannot_be_saved_still_finishes_and_says_so_once(
    pipelines, fakes, config, tmp_path,
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    log, _percents = run(pipelines, [video(tmp_path)], "en", ["ar"], config, JobStore(blocker / "resume"))
    assert len(lines_of(log, "Warning: couldn't save progress (")) == 1
    assert "[ar] See you" in read(tmp_path / "talk_ar.srt") and log[-1] == "\nProcess Completed Successfully!"
