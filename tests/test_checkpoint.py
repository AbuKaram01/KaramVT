# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Saved progress (core/checkpoint.py): what is written, what is trusted when it is read back, and why not.

Each test works in its own folder. The files are damaged by hand the way a crash, a full disk or a half-copied folder
would damage them, to check that damage ends the usable part of a record instead of being trusted.
"""

import json
import os
import stat
import sys
import time
import types

import pytest

from karamvt.core import checkpoint
from karamvt.core.checkpoint import (
    SCHEMA,
    TRANSCRIPT_UNIT,
    JobStore,
    PartialTranscript,
    captions_digest,
    file_fingerprint,
    model_signature,
    unit_header,
)
from karamvt.core.models import Caption

WHISPER = {"path": "/models/whisper", "size": 10, "mtime_ns": 5}
LLM = {"path": "/models/llm.gguf", "size": 20, "mtime_ns": 6}
CAPTIONS = [Caption(0.0, 1.5, "Hello"), Caption(1.5, 3.0, "مرحبا\nسطر ثان"), Caption(3.0, 4.25, "Bye")]


@pytest.fixture
def video(tmp_path):
    """A small stand-in video file; return its path."""
    path = tmp_path / "talk.mp4"
    path.write_bytes(b"video bytes")
    return str(path)


@pytest.fixture
def store(tmp_path):
    """A JobStore in a temporary folder; the warnings it gives collect in ``store.warnings``."""
    result = JobStore(tmp_path / "resume")
    result.warnings = []
    result.warn = result.warnings.append
    return result


def only_file(store, suffix):
    """The one file in the store's folder whose name ends with ``suffix``."""
    found = [entry for entry in store.root.iterdir() if entry.name.endswith(suffix)]
    assert len(found) == 1, found
    return found[0]


def rewrite_lines(path, change):
    """Rewrite the file at ``path`` with its lines (split on newline bytes) replaced by ``change(lines)``."""
    lines = path.read_bytes().split(b"\n")
    path.write_bytes(b"\n".join(change(lines)))


# --- Fingerprints ---

def test_a_file_is_identified_by_its_size_and_modification_time(video):
    fingerprint = file_fingerprint(video)
    assert fingerprint == {"size": 11, "mtime_ns": os.stat(video).st_mtime_ns}
    os.utime(video, ns=(1, 2_000_000_000))
    assert file_fingerprint(video)["mtime_ns"] == 2_000_000_000


def test_a_file_that_is_not_there_has_no_fingerprint(tmp_path):
    assert file_fingerprint(str(tmp_path / "gone")) is None


def test_a_model_file_is_identified_by_its_path_size_and_time(tmp_path):
    model = tmp_path / "m.gguf"
    model.write_bytes(b"12345")
    assert model_signature(str(model)) == {
        "path": str(model), "size": 5, "mtime_ns": os.stat(model).st_mtime_ns,
    }


def test_a_model_folder_is_identified_by_the_sum_of_its_files_and_the_newest_of_them(tmp_path):
    folder = tmp_path / "whisper"
    folder.mkdir()
    (folder / "a.bin").write_bytes(b"123")
    (folder / "b.bin").write_bytes(b"45")
    (folder / "sub").mkdir()  # Folders inside are not counted.
    os.utime(folder / "a.bin", ns=(1, 100))
    os.utime(folder / "b.bin", ns=(1, 300))
    assert model_signature(str(folder)) == {"path": str(folder), "size": 5, "mtime_ns": 300}


@pytest.mark.parametrize("reverse", [False, True])
def test_a_model_folder_is_identified_the_same_way_whatever_order_it_is_listed_in(tmp_path, monkeypatch, reverse):
    folder = tmp_path / "whisper"
    folder.mkdir()
    for name, mtime in (("a.bin", 100), ("b.bin", 300), ("c.bin", 200)):
        (folder / name).write_bytes(b"1")
        os.utime(folder / name, ns=(1, mtime))
    listed = sorted(os.scandir(folder), key=lambda entry: entry.name, reverse=reverse)
    monkeypatch.setattr(checkpoint.os, "scandir", lambda path: iter(listed))
    assert model_signature(str(folder))["mtime_ns"] == 300


@pytest.mark.parametrize("path", ["", "/no/such/model"])
def test_no_model_means_no_signature(path):
    assert model_signature(path) is None


def test_the_header_of_a_unit_holds_everything_that_must_match_for_it_to_be_reused(video):
    header = unit_header(video, "en", "ar", LLM, ".srt", "/out", "/in")
    assert header == {
        "v": SCHEMA, "input": file_fingerprint(video), "src": "en", "tgt": "ar", "llm": LLM,
        "fmt": ".srt", "outdir": "/out", "root": "/in",
    }
    plain = unit_header(video, None, "ar", None)
    assert plain["src"] is None and plain["fmt"] == "" and plain["outdir"] == "" and plain["root"] == ""


def test_the_digest_of_captions_changes_with_any_part_of_them():
    base = captions_digest(CAPTIONS)
    assert base == captions_digest(list(CAPTIONS)) and len(base) == 40
    assert captions_digest([Caption(0.0, 1.5, "Hello")] + CAPTIONS[1:]) == base
    for changed in (Caption(0.001, 1.5, "Hello"), Caption(0.0, 1.501, "Hello"), Caption(0.0, 1.5, "Hello!")):
        assert captions_digest([changed] + CAPTIONS[1:]) != base
    assert captions_digest([Caption(0.0001, 1.5, "Hello")] + CAPTIONS[1:]) == base  # Rounded to a millisecond.


# --- The sealed records ---

def test_a_sealed_record_comes_back_and_any_change_to_it_is_noticed():
    sealed = checkpoint._seal({"a": 1, "b": [1, 2]})
    assert checkpoint._unseal(sealed) == {"a": 1, "b": [1, 2]}
    assert checkpoint._unseal(sealed.replace('"a": 1', '"a": 2')) is None
    assert checkpoint._unseal(sealed.replace("1", "7", 1)) is None
    for damaged in ("", "not json", "[]", "{}", '{"d": {}, "c": 1}', '{"d": 5, "c": 1}', sealed[:-3]):
        assert checkpoint._unseal(damaged) is None


def test_the_checksum_tells_apart_values_that_run_together_when_written_out():
    assert checkpoint._crc(1, 23) != checkpoint._crc(12, 3)
    assert checkpoint._crc(1.5, 2.0, "a") == checkpoint._crc(1.5, 2.0, "a")


def test_a_sealed_value_that_is_not_a_record_is_not_one():
    assert checkpoint._unseal(checkpoint._seal(["not", "a", "record"])) is None
    assert checkpoint._unseal(checkpoint._seal("text")) is None


def test_a_line_that_is_not_utf8_is_marked_instead_of_repaired(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"good\n\xff\xfe broken\nlast")
    assert checkpoint._read_lines(path) == ["good", "�", "last"]
    assert checkpoint._read_lines(tmp_path / "missing") is None


def test_a_sealed_file_that_cannot_be_read_gives_nothing(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"\xff\xfe")
    assert checkpoint._read_sealed(path) is None
    assert checkpoint._read_sealed(tmp_path / "missing") is None


@pytest.mark.parametrize("start, end, previous, sane", [
    (0, 1, 0, True), (1, 1, 0, True), (2, 1, 0, False), (-1, 1, 0, False), (1, 2, 1.5, False),
    (0, float("inf"), 0, False), (float("nan"), 1, 0, False), (0, 1e7, 0, False), (0, 9_999_999, 0, True),
])
def test_times_that_cannot_be_a_real_video_are_not_sane(start, end, previous, sane):
    assert checkpoint._sane_times(start, end, previous) is sane


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_what_is_saved_is_private_to_the_user(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(only_file(store, "transcript.json").stat().st_mode) == 0o600


# --- Finished transcripts ---

def test_a_saved_transcript_is_reused(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    cached, why = store.load_transcript(video, "en", WHISPER)
    assert why is None and cached == checkpoint.CachedTranscript(CAPTIONS, "en", "English")
    assert store.load_transcript(video, None, WHISPER)[0] is not None  # "Auto-detect" accepts any language.


def test_without_a_saved_transcript_there_is_nothing_and_nothing_to_explain(store, video):
    assert store.load_transcript(video, "en", WHISPER) == (None, None)


@pytest.mark.parametrize("change, why", [
    ("model", "the speech-recognition model changed"),
    ("language", "a different source language was chosen"),
    ("input", "the input file changed"),
])
def test_a_transcript_made_from_something_else_is_ignored_and_the_reason_is_given(store, video, change, why):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    requested, signature = "en", WHISPER
    if change == "model":
        signature = dict(WHISPER, size=11)
    elif change == "language":
        requested = "fr"
    else:
        os.utime(video, ns=(1, 12345))
    assert store.load_transcript(video, requested, signature) == (None, why)


def test_a_transcript_saved_by_another_version_is_ignored(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = only_file(store, "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))["d"]
    path.write_text(checkpoint._seal(dict(payload, v=SCHEMA - 1)), encoding="utf-8")
    assert store.load_transcript(video, "en", WHISPER) == (None, "saved by a different version of the app")


def test_a_transcript_of_another_version_is_reported_as_that_even_if_the_input_changed_too(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = only_file(store, "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))["d"]
    path.write_text(checkpoint._seal(dict(payload, v=SCHEMA - 1)), encoding="utf-8")
    os.utime(video, ns=(1, 12345))
    other_whisper = dict(WHISPER, size=1)
    assert store.load_transcript(video, "fr", other_whisper) == (None, "saved by a different version of the app")


def test_a_damaged_transcript_is_ignored(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = only_file(store, "transcript.json")
    path.write_text(path.read_text(encoding="utf-8").replace("Hello", "Hellp"), encoding="utf-8")
    assert store.load_transcript(video, "en", WHISPER) == (None, "the saved file is damaged")


@pytest.mark.parametrize("captions", [
    [[1.0, 0.5, "backwards"]], [[1, 2, "a"], [0.5, 2, "b"]], [[0, 1e8, "huge"]], [["x", 1, "t"]], [[0, 1]], 7,
])
def test_a_transcript_with_impossible_captions_is_ignored(store, video, captions):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = only_file(store, "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))["d"]
    path.write_text(checkpoint._seal(dict(payload, captions=captions)), encoding="utf-8")
    assert store.load_transcript(video, "en", WHISPER) == (None, "the saved file is damaged")


def test_captions_may_overlap_but_not_start_before_the_one_before(store, video):
    overlapping = [Caption(0.0, 5.0, "a"), Caption(1.0, 6.0, "b"), Caption(1.0, 2.0, "c")]
    store.save_transcript(video, overlapping, "en", "English", WHISPER)
    assert store.load_transcript(video, "en", WHISPER)[0].captions == overlapping


def test_a_transcript_without_language_is_ignored(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = only_file(store, "transcript.json")
    payload = json.loads(path.read_text(encoding="utf-8"))["d"]
    path.write_text(checkpoint._seal(dict(payload, lang={})), encoding="utf-8")
    assert store.load_transcript(video, "en", WHISPER) == (None, "the saved file is damaged")


def test_a_transcript_without_captions_is_as_good_as_none(store, video):
    store.save_transcript(video, [], "en", "English", WHISPER)
    assert store.load_transcript(video, "en", WHISPER) == (None, None)


def test_the_input_is_compared_before_the_model_and_the_model_before_the_language(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    other_model = dict(WHISPER, size=1)
    assert store.load_transcript(video, "fr", other_model) == (None, "the speech-recognition model changed")
    os.utime(video, ns=(1, 12345))
    assert store.load_transcript(video, "fr", other_model) == (None, "the input file changed")


def test_the_same_path_always_gets_the_same_files_and_other_paths_get_others(store, tmp_path):
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    assert store._key(str(first)) == store._key(str(tmp_path / "x" / ".." / "a.mp4"))
    assert store._key(str(first)) != store._key(str(second))
    assert len(store._key(str(first))) == 20


def test_a_link_to_a_file_is_the_same_input_as_the_file(store, tmp_path, video):
    link = tmp_path / "link.mp4"
    try:
        link.symlink_to(video)
    except (OSError, NotImplementedError):
        pytest.skip("links need a privilege on this system")
    assert store._key(str(link)) == store._key(video)


def test_a_folder_that_cannot_be_written_costs_a_warning_not_the_job(tmp_path, video):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the folder should be")
    store = JobStore(blocked / "resume")
    warnings = []
    store.warn = warnings.append
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    assert len(warnings) == 1 and warnings[0].startswith("Warning: couldn't save progress (")
    assert "if it stops it can't be resumed" in warnings[0]


# --- A transcription that did not finish ---

def record_partial(store, video, captions, keep=(), signature=WHISPER, code="en"):
    """Save a partial transcript holding ``captions``, as a transcription that stopped leaves it."""
    writer = store.start_partial_transcript(video, signature, code, "English", list(keep))
    for caption in captions:
        writer.append(caption)
    writer.close()


def test_the_segments_written_so_far_come_back(store, video):
    record_partial(store, video, CAPTIONS)
    partial, why = store.load_partial_transcript(video, "en", WHISPER)
    assert why is None and partial == PartialTranscript(CAPTIONS, "en", "English", 0)
    assert partial.usable_segments == CAPTIONS[:-1]  # The one being finished when it stopped is redone.


def test_after_a_resume_that_added_nothing_everything_saved_is_kept(store, video):
    record_partial(store, video, [], keep=CAPTIONS)
    partial, _why = store.load_partial_transcript(video, "en", WHISPER)
    assert partial.carried == 3 and partial.usable_segments == CAPTIONS
    record_partial(store, video, [Caption(4.25, 5.0, "new")], keep=CAPTIONS)
    partial, _why = store.load_partial_transcript(video, "en", WHISPER)
    assert partial.carried == 3 and len(partial.captions) == 4 and partial.usable_segments == CAPTIONS


def test_starting_again_without_segments_to_keep_discards_the_older_record(store, video):
    record_partial(store, video, CAPTIONS)
    record_partial(store, video, [CAPTIONS[0]])
    partial, _why = store.load_partial_transcript(video, "en", WHISPER)
    assert partial.captions == [CAPTIONS[0]]


def test_a_record_with_no_segments_is_as_good_as_none(store, video):
    record_partial(store, video, [])
    assert store.load_partial_transcript(video, "en", WHISPER) == (None, None)
    assert JobStore(store.root / "other").load_partial_transcript(video, "en", WHISPER) == (None, None)


def test_a_torn_last_line_ends_the_usable_part(store, video):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")
    path.write_bytes(path.read_bytes().rstrip(b"\n")[:-9])
    partial, _why = store.load_partial_transcript(video, "en", WHISPER)
    assert partial.captions == CAPTIONS[:2]


@pytest.mark.parametrize("damage", [
    "checksum", "backwards", "repeated", "impossible times", "text type", "missing field", "garbage",
])
def test_a_damaged_line_ends_the_usable_part_before_it(store, video, damage):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")

    def change(lines):
        second = json.loads(lines[2])
        if damage == "checksum":
            second["t"] = "changed"
        elif damage == "backwards":
            second.update(s=-1.0, e=0.5, c=checkpoint._crc(-1.0, 0.5, second["t"]))
        elif damage == "repeated":
            second.update(s=0.0, e=1.0, c=checkpoint._crc(0.0, 1.0, second["t"]))
        elif damage == "impossible times":
            second.update(s=5.0, e=1.0, c=checkpoint._crc(5.0, 1.0, second["t"]))
        elif damage == "text type":
            second.update(t=5, c=checkpoint._crc(second["s"], second["e"], 5))
        elif damage == "missing field":
            del second["c"]
        else:
            lines[2] = b"{{{"
            return lines
        lines[2] = json.dumps(second, ensure_ascii=False).encode("utf-8")
        return lines

    rewrite_lines(path, change)
    partial, _why = store.load_partial_transcript(video, "en", WHISPER)
    assert partial.captions == CAPTIONS[:1]


def test_a_segment_after_a_damaged_one_is_not_used_even_if_it_would_fit(store, video):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")
    rewrite_lines(path, lambda lines: [lines[0], lines[1], b"{{{", lines[2], lines[3]])
    assert store.load_partial_transcript(video, "en", WHISPER)[0].captions == CAPTIONS[:1]


def test_blank_lines_between_segments_are_skipped(store, video):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")
    rewrite_lines(path, lambda lines: [lines[0], b"", lines[1], b"  ", *lines[2:]])
    assert store.load_partial_transcript(video, "en", WHISPER)[0].captions == CAPTIONS


@pytest.mark.parametrize("change, requested, signature, why", [
    ("input", "en", WHISPER, "the input file changed"),
    ("model", "en", dict(WHISPER, size=1), "the speech-recognition model changed"),
    ("language", "fr", WHISPER, "a different source language was chosen"),
])
def test_a_partial_transcript_made_from_something_else_is_ignored_with_the_reason(
    store, video, change, requested, signature, why,
):
    record_partial(store, video, CAPTIONS)
    if change == "input":
        os.utime(video, ns=(1, 12345))
    assert store.load_partial_transcript(video, requested, signature) == (None, why)


def test_a_partial_transcript_is_continued_in_whatever_language_it_was_made_when_none_is_chosen(store, video):
    record_partial(store, video, CAPTIONS)
    partial, why = store.load_partial_transcript(video, None, WHISPER)
    assert why is None and (partial.lang_code, partial.lang_name) == ("en", "English")


def test_a_partial_transcript_with_a_damaged_or_foreign_header_is_ignored(store, video):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")
    original = path.read_bytes()
    path.write_bytes(original.replace(b'"d"', b'"x"', 1))
    assert store.load_partial_transcript(video, "en", WHISPER) == (None, "the saved file is damaged")
    header = json.loads(original.split(b"\n")[0])["d"]
    rest = original.split(b"\n", 1)[1]
    path.write_bytes(checkpoint._seal(dict(header, v=SCHEMA + 1)).encode("utf-8") + b"\n" + rest)
    assert store.load_partial_transcript(video, "en", WHISPER) == (None, "saved by a different version of the app")
    path.write_bytes(checkpoint._seal({"v": SCHEMA}).encode("utf-8") + b"\n" + rest)
    assert store.load_partial_transcript(video, "en", WHISPER) == (None, "the saved file is damaged")


def test_the_number_carried_over_is_taken_only_when_it_is_a_positive_number(store, video):
    record_partial(store, video, CAPTIONS)
    path = only_file(store, "transcript.partial")
    header = json.loads(path.read_bytes().split(b"\n")[0])["d"]
    rest = path.read_bytes().split(b"\n", 1)[1]
    for carried, expected in ((2, 2), (0, 0), (-1, 0), ("2", 0), (None, 0)):
        path.write_bytes(checkpoint._seal(dict(header, keep=carried)).encode("utf-8") + b"\n" + rest)
        assert store.load_partial_transcript(video, "en", WHISPER)[0].carried == expected


def test_a_partial_transcript_can_be_dropped(store, video):
    record_partial(store, video, CAPTIONS)
    store.drop_partial_transcript(video)
    store.drop_partial_transcript(video)  # Already gone: not an error.
    assert store.load_partial_transcript(video, "en", WHISPER) == (None, None)


def test_a_partial_transcript_that_cannot_be_started_still_gives_a_writer_that_does_nothing(tmp_path, video):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    store = JobStore(blocked / "resume")
    warnings = []
    store.warn = warnings.append
    writer = store.start_partial_transcript(video, WHISPER, "en", "English", [])
    writer.append(CAPTIONS[0])
    writer.close()
    assert len(warnings) == 1


def test_a_writer_that_loses_its_file_stops_quietly_and_says_so_once(tmp_path):
    failures = []
    writer = checkpoint.PartialTranscriptWriter(tmp_path / "f.partial", failures.append)
    writer.append(CAPTIONS[0])
    writer._fh.close()  # What a disk failure looks like to the next write.
    writer._fh = type("Broken", (), {
        "write": lambda self, text: (_ for _ in ()).throw(OSError(28, "No space left")),
        "close": lambda self: None,
    })()
    writer.append(CAPTIONS[1])
    writer.append(CAPTIONS[2])
    assert len(failures) == 1 and failures[0].errno == 28
    assert (tmp_path / "f.partial").read_text(encoding="utf-8").count("\n") == 1


# --- Translation journals ---

def header_for(video, unit="ar", src="en", llm=LLM):
    """The header that identifies one output of ``video`` in the saved progress."""
    return unit_header(video, src, unit, llm, ".srt")


def open_journal(store, video, total=3, digest="d1", unit="ar", resume=True, **header):
    """Open the translation journal of ``video``; keyword arguments override fields of the expected header."""
    wanted = dict(header_for(video, unit), **header)
    return store.open_journal(video, unit, wanted, total, digest, {"temperature": 0.2}, resume)


def test_a_new_journal_starts_empty_and_records_each_line(store, video):
    journal, note = open_journal(store, video)
    assert note is None and journal.texts == [] and journal.prior_header is None
    journal.append(0, "one")
    journal.append(1, "اثنان")
    journal.close()
    again, note = open_journal(store, video)
    assert note is None and again.texts == ["one", "اثنان"]
    assert again.prior_header["n"] == 3 and again.prior_header["digest"] == "d1"
    again.close()


def test_the_settings_are_kept_in_the_journal_header(store, video):
    journal, _note = open_journal(store, video)
    journal.close()
    path = only_file(store, "ar.journal")
    header = json.loads(path.read_text(encoding="utf-8").split("\n")[0])["d"]
    assert header["sampling"] == {"temperature": 0.2} and header["n"] == 3 and header["digest"] == "d1"


@pytest.mark.parametrize("change, why", [
    ({"src": "fr"}, "the source language setting changed"),
    ({"llm": dict(LLM, size=1)}, "the translation model changed"),
    ({"tgt": "de"}, "the target language changed"),
])
def test_a_journal_made_from_something_else_is_dropped_with_the_reason(store, video, change, why):
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.close()
    again, note = open_journal(store, video, **change)
    assert note == why and again.texts == [] and again.prior_header is None
    again.close()
    assert open_journal(store, video)[0].texts == []  # The old lines were rewritten away.


@pytest.mark.parametrize("total, digest", [(4, "d1"), (3, "d2")])
def test_a_journal_of_a_transcript_that_changed_is_dropped(store, video, total, digest):
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.close()
    again, note = open_journal(store, video, total=total, digest=digest)
    assert note == "the transcript changed" and again.texts == []


def test_a_journal_of_a_changed_input_is_dropped(store, video):
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.close()
    os.utime(video, ns=(1, 12345))
    assert open_journal(store, video)[1] == "the input file changed"


def test_a_journal_is_not_resumed_when_not_asked_to(store, video):
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.close()
    again, note = open_journal(store, video, resume=False)
    assert note is None and again.texts == []


def test_a_damaged_journal_header_is_reported(store, video):
    journal, _note = open_journal(store, video)
    journal.close()
    path = only_file(store, "ar.journal")
    path.write_bytes(b"garbage\n")
    again, note = open_journal(store, video)
    assert note == "the saved file is damaged" and again.texts == []


def test_more_lines_than_the_transcript_has_are_cut(store, video):
    journal, _note = open_journal(store, video, total=5)
    for index in range(5):
        journal.append(index, f"line {index}")
    journal.close()
    again, _note = open_journal(store, video, total=5)
    assert len(again.texts) == 5
    path = only_file(store, "ar.journal")
    lines = path.read_text(encoding="utf-8").split("\n")
    header = json.loads(lines[0])["d"]
    path.write_text(checkpoint._seal(dict(header, n=3)) + "\n" + "\n".join(lines[1:]), encoding="utf-8")
    cut, _note = open_journal(store, video, total=3)
    assert cut.texts == ["line 0", "line 1", "line 2"]


def read_journal(store):
    """Read the journal file back the way a later run does."""
    return checkpoint.JobStore._read_journal(only_file(store, "ar.journal"))


@pytest.mark.parametrize("damage, kept", [
    ("torn", 2), ("checksum", 1), ("duplicate", 4), ("gap", 1), ("not a number", 1), ("text type", 1), ("garbage", 1),
])
def test_a_damaged_journal_line_ends_the_valid_part_before_it(store, video, damage, kept):
    journal, _note = open_journal(store, video, total=4)
    for index in range(4):
        journal.append(index, f"line {index}")
    journal.close()
    path = only_file(store, "ar.journal")

    def change(lines):
        record = json.loads(lines[2])
        if damage == "torn":
            return lines[:3] + [lines[3][:-6]] + lines[4:]
        if damage == "checksum":
            record["t"] = "changed"
        elif damage == "duplicate":
            return lines[:3] + [lines[2]] + lines[3:]
        elif damage == "gap":
            record.update(i=5, c=checkpoint._crc(5, record["t"]))
        elif damage == "not a number":
            record.update(i="1", c=checkpoint._crc("1", record["t"]))
        elif damage == "text type":
            record.update(t=7, c=checkpoint._crc(1, 7))
        else:
            lines[2] = b"{{{"
            return lines
        lines[2] = json.dumps(record, ensure_ascii=False).encode("utf-8")
        return lines

    rewrite_lines(path, change)
    header, texts = checkpoint.JobStore._read_journal(path)
    assert header is not None and len(texts) == kept


def test_a_line_that_comes_late_does_not_fill_a_gap_left_before_it(store, video):
    journal, _note = open_journal(store, video, total=4)
    for index in range(3):
        journal.append(index, f"line {index}")
    journal.close()
    path = only_file(store, "ar.journal")
    rewrite_lines(path, lambda lines: [lines[0], lines[1], lines[3], lines[2], b""])
    assert len(checkpoint.JobStore._read_journal(path)[1]) == 1


def test_a_valid_line_after_a_damaged_one_is_not_used_even_if_it_is_the_one_expected(store, video):
    journal, _note = open_journal(store, video, total=4)
    for index in range(3):
        journal.append(index, f"line {index}")
    journal.close()
    path = only_file(store, "ar.journal")
    rewrite_lines(path, lambda lines: [lines[0], lines[1], b"{{{", lines[2], lines[3], b""])
    assert len(checkpoint.JobStore._read_journal(path)[1]) == 1


def test_a_journal_that_is_missing_or_has_no_header_has_nothing_to_read(store, tmp_path):
    assert checkpoint.JobStore._read_journal(tmp_path / "missing") == (None, [])
    path = tmp_path / "empty.journal"
    path.write_bytes(b"")
    assert checkpoint.JobStore._read_journal(path) == (None, [])


def test_a_torn_journal_is_rewritten_clean_before_more_is_appended(store, video):
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.append(1, "two")
    journal.close()
    path = only_file(store, "ar.journal")
    path.write_bytes(path.read_bytes().rstrip(b"\n")[:-4])
    again, _note = open_journal(store, video)
    assert again.texts == ["one"]
    again.append(1, "two again")
    again.close()
    assert open_journal(store, video)[0].texts == ["one", "two again"]


def test_a_journal_that_cannot_be_written_still_resumes_from_what_was_read(tmp_path, video):
    store = JobStore(tmp_path / "resume")
    warnings = []
    store.warn = warnings.append
    journal, _note = open_journal(store, video)
    journal.append(0, "one")
    journal.close()
    original = checkpoint._atomic_write

    def refuse(path, data):
        raise OSError(30, "Read-only file system")

    checkpoint._atomic_write = refuse
    try:
        again, note = open_journal(store, video)
    finally:
        checkpoint._atomic_write = original
    assert again.texts == ["one"] and note is None and again._fh is None
    again.append(1, "lost")  # Nothing happens, nothing raises.
    assert len(warnings) == 1 and "Read-only file system" in warnings[0]


def test_a_journal_can_be_dropped(store, video):
    journal, _note = open_journal(store, video)
    journal.close()
    store.drop_journal(video, "ar")
    assert not [entry for entry in store.root.iterdir() if entry.name.endswith(".journal")]


# --- Finished outputs ---

@pytest.fixture
def output(tmp_path):
    path = tmp_path / "talk_ar.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,000\nمرحبا\n", encoding="utf-8")
    return str(path)


def test_a_finished_output_is_remembered_while_it_is_as_it_was_left(store, video, output):
    header = header_for(video)
    assert store.done_output(video, header) is None
    store.mark_done(video, header, output)
    assert store.done_output(video, header) == output


@pytest.mark.parametrize("change", ["touched", "rewritten", "removed"])
def test_an_output_that_changed_since_is_not_trusted(store, video, output, change):
    header = header_for(video)
    store.mark_done(video, header, output)
    left_at = os.stat(output).st_mtime_ns
    if change == "touched":
        os.utime(output, ns=(1, 99_000_000_000))
    elif change == "rewritten":  # The file grows, and its modification time is put back as it was.
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("more")
        os.utime(output, ns=(1, left_at))
    else:
        os.remove(output)
    assert store.done_output(video, header) is None


@pytest.mark.parametrize("change", [{"src": "fr"}, {"llm": None}, {"fmt": ".vtt"}, {"outdir": "/else"}, {"root": "/r"}])
def test_an_output_made_with_other_settings_is_not_reused(store, video, output, change):
    header = header_for(video)
    store.mark_done(video, header, output)
    assert store.done_output(video, dict(header, **change)) is None


def test_the_markers_of_other_languages_are_kept_apart(store, video, output):
    store.mark_done(video, header_for(video, "ar"), output)
    assert store.done_output(video, header_for(video, "de")) is None


def test_a_damaged_marker_is_not_trusted(store, video, output):
    header = header_for(video)
    store.mark_done(video, header, output)
    path = only_file(store, "ar.done.json")
    path.write_text(path.read_text(encoding="utf-8").replace("size", "sizz"), encoding="utf-8")
    assert store.done_output(video, header) is None


# --- What the window shows before a job starts ---

@pytest.fixture
def models(tmp_path):
    """(whisper folder, translation model file) as scan() takes them, with their signatures."""
    whisper = tmp_path / "w"
    whisper.mkdir()
    (whisper / "model.bin").write_bytes(b"x")
    llm = tmp_path / "llm.gguf"
    llm.write_bytes(b"y")
    return types.SimpleNamespace(
        whisper=str(whisper), whisper_sig=model_signature(str(whisper)),
        llm=str(llm), llm_sig=model_signature(str(llm)),
    )


def scan(store, models, inputs, targets=("ar",), src="en", partial=False):
    """Ask the store what is saved for ``inputs`` and ``targets``, as the window does before a job starts."""
    return store.scan(inputs, src, list(targets), models.llm, models.whisper, ".srt", None, None, partial)


def save_journal(store, video, models, lines, *, unit="ar", src="en", total=4):
    """Save a translation journal that holds ``lines`` out of ``total``, as a job that stopped leaves it."""
    header = dict(unit_header(video, src, unit, models.llm_sig, ".srt"), n=total, digest="x")
    journal, _note = store.open_journal(video, unit, header, total, "x", {})
    for index, text in enumerate(lines):
        journal.append(index, text)
    journal.close()


def test_scanning_nothing_saved_finds_fresh_units(store, models, video):
    report = scan(store, models, [video], targets=("ar", "de"))
    assert [(u.unit_code, u.state) for u in report.units] == [("ar", "fresh"), ("de", "fresh")]
    assert not report.has_progress and report.transcripts == [] and report.partial_transcripts == []


def test_scanning_finds_each_kind_of_saved_progress(store, models, video, output):
    store.save_transcript(video, CAPTIONS, "en", "English", models.whisper_sig)
    store.mark_done(video, unit_header(video, "en", "ar", models.llm_sig, ".srt"), output)
    save_journal(store, video, models, ["one", "two"], unit="de")
    report = scan(store, models, [video], targets=("ar", "de", "fr"))
    assert report.transcripts == [video]
    assert [(u.unit_code, u.state, u.lines_done, u.lines_total) for u in report.units] == [
        ("ar", "done", 0, 0), ("de", "partial", 2, 4), ("fr", "fresh", 0, 0),
    ]
    assert report.completed_outputs == {output} and report.has_progress
    assert [u.unit_code for u in report.done] == ["ar"] and [u.unit_code for u in report.partial] == ["de"]


def test_every_input_is_scanned(store, models, tmp_path, video):
    other = tmp_path / "other.mp4"
    other.write_bytes(b"o")
    save_journal(store, str(other), models, ["one"])
    report = scan(store, models, [video, str(other)])
    assert [(u.input_path, u.state) for u in report.units] == [(video, "fresh"), (str(other), "partial")]


def test_a_journal_without_lines_counts_as_fresh(store, models, video):
    save_journal(store, video, models, [])
    assert scan(store, models, [video]).units[0].state == "fresh"


def test_a_journal_of_other_settings_counts_as_fresh(store, models, video):
    save_journal(store, video, models, ["one"], src="fr")
    assert scan(store, models, [video]).units[0].state == "fresh"


def test_a_damaged_journal_counts_as_fresh(store, models, video):
    save_journal(store, video, models, ["one"])
    only_file(store, "ar.journal").write_bytes(b"garbage\n")
    assert scan(store, models, [video]).units[0].state == "fresh"


def test_transcribing_only_has_one_unit_and_needs_no_translation_model(store, models, video):
    report = scan(store, models, [video], targets=())
    assert [(u.unit_code, u.state) for u in report.units] == [(TRANSCRIPT_UNIT, "fresh")]


def test_a_finished_transcription_is_found_whatever_translation_model_is_set(store, models, video, output):
    store.mark_done(video, unit_header(video, "en", TRANSCRIPT_UNIT, None, ".srt"), output)
    report = scan(store, models, [video], targets=())
    assert [(u.unit_code, u.state) for u in report.units] == [(TRANSCRIPT_UNIT, "done")]


def test_a_partial_transcript_is_reported_with_how_far_it_got(store, models, video):
    record_partial(store, video, CAPTIONS, signature=models.whisper_sig)
    report = scan(store, models, [video], partial=True)
    assert report.partial_transcripts == [checkpoint.PartialInfo(video, 3.0, 2)]
    assert report.has_progress and report.use_partial_transcripts
    assert not scan(store, models, [video]).has_progress  # Not asked to resume those: not progress.


def test_a_finished_transcript_hides_the_partial_one(store, models, video):
    record_partial(store, video, CAPTIONS, signature=models.whisper_sig)
    store.save_transcript(video, CAPTIONS, "en", "English", models.whisper_sig)
    report = scan(store, models, [video])
    assert report.transcripts == [video] and report.partial_transcripts == []


def test_a_transcript_of_another_language_is_not_counted(store, models, video):
    store.save_transcript(video, CAPTIONS, "en", "English", models.whisper_sig)
    assert scan(store, models, [video], src="fr").transcripts == []
    assert scan(store, models, [video], src=None).transcripts == [video]


def test_a_subtitle_file_has_no_transcript(store, models, tmp_path):
    subtitles = tmp_path / "talk.SRT"
    subtitles.write_text("x")
    store.save_transcript(str(subtitles), CAPTIONS, "en", "English", models.whisper_sig)
    report = scan(store, models, [str(subtitles)])
    assert report.transcripts == [] and [u.state for u in report.units] == ["fresh"]


def test_a_partial_transcript_with_nothing_usable_is_not_reported(store, models, video):
    record_partial(store, video, [CAPTIONS[0]], signature=models.whisper_sig)  # Its only segment is redone.
    assert scan(store, models, [video]).partial_transcripts == []


# --- Clearing up ---

def test_a_folder_inside_the_store_is_not_progress(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    (store.root / "nested").mkdir()
    assert store.count_files() == 1
    assert store.clear_all() == 1
    assert (store.root / "nested").is_dir()


def test_the_progress_of_chosen_inputs_can_be_forgotten_and_the_rest_stays(store, tmp_path, video):
    other = tmp_path / "other.mp4"
    other.write_bytes(b"o")
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    store.save_transcript(str(other), CAPTIONS, "en", "English", WHISPER)
    assert store.count_files() == 2
    store.clear_inputs([video])
    assert store.count_files() == 1 and store.load_transcript(str(other), "en", WHISPER)[0] is not None


def test_everything_can_be_cleared_and_the_files_are_counted(store, tmp_path, video):
    other = tmp_path / "other.mp4"
    other.write_bytes(b"o")
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    store.save_transcript(str(other), CAPTIONS, "en", "English", WHISPER)
    assert store.clear_all() == 2 and store.count_files() == 0


def test_a_store_whose_folder_is_missing_has_nothing_to_clear_or_count(tmp_path):
    store = JobStore(tmp_path / "never-made")
    assert store.count_files() == 0 and store.clear_all() == 0
    store.clear_inputs(["/x"])
    store.prune()


def test_progress_nobody_came_back_to_is_pruned_and_recent_progress_is_kept(store, tmp_path, video):
    other = tmp_path / "other.mp4"
    other.write_bytes(b"o")
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    store.save_transcript(str(other), CAPTIONS, "en", "English", WHISPER)
    old = store._path(video, "transcript.json")
    long_ago = time.time() - 31 * 86400
    os.utime(old, (long_ago, long_ago))
    store.prune()
    assert not old.exists() and store.count_files() == 1
    store.prune(max_age_days=0)
    assert store.count_files() == 0


def test_progress_of_the_last_month_survives_a_default_prune(store, video):
    store.save_transcript(video, CAPTIONS, "en", "English", WHISPER)
    path = store._path(video, "transcript.json")
    almost = time.time() - 29 * 86400
    os.utime(path, (almost, almost))
    store.prune()
    assert path.exists()
    store.prune(max_age_days=28)
    assert not path.exists()
