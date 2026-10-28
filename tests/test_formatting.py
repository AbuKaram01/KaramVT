# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The pure helpers that decide what the interface says (gui/formatting.py)."""

from datetime import datetime, timedelta, timezone

import pytest

from karamvt.core.checkpoint import PartialInfo, ResumeReport, UnitStatus
from karamvt.gui.formatting import duration_label, full_time, preview, relative_time, resume_summary

UTC = timezone.utc
NOON = datetime(2026, 10, 21, 12, 0, tzinfo=UTC).timestamp()


def ago(**delta) -> float:
    """The timestamp that lies ``delta`` (keyword arguments of timedelta) before NOON."""
    return NOON - timedelta(**delta).total_seconds()


@pytest.mark.parametrize("moment, expected", [
    (ago(seconds=0), "Just now"),
    (ago(seconds=59), "Just now"),
    (ago(seconds=60), "1 minute ago"),
    (ago(minutes=2, seconds=30), "2 minutes ago"),
    (ago(minutes=59), "59 minutes ago"),
    (ago(hours=1), "1 hour ago"),
    (ago(hours=5, minutes=40), "5 hours ago"),
    (ago(hours=23, minutes=59), "23 hours ago"),
    (ago(hours=30), "Yesterday"),                       # 06:00 on the day before.
    (ago(days=1, hours=11), "Yesterday"),               # 01:00 on the day before.
    (ago(days=2), "2 days ago"),
    (ago(days=6), "6 days ago"),
    (ago(days=7), "14 Oct 2026"),
    (ago(days=200), "4 Apr 2026"),
])
def test_when_something_happened_is_said_the_way_a_list_of_recent_items_says_it(moment, expected):
    assert relative_time(moment, NOON, UTC) == expected


def test_a_moment_in_the_future_is_just_now_not_a_negative_age():
    assert relative_time(NOON + 3600, NOON, UTC) == "Just now"


def test_days_are_counted_on_the_calendar_not_in_blocks_of_twenty_four_hours():
    now = datetime(2026, 10, 21, 0, 10, tzinfo=UTC).timestamp()

    def at(day, hour, minute):
        return datetime(2026, 10, day, hour, minute, tzinfo=UTC).timestamp()

    assert relative_time(at(20, 23, 50), now, UTC) == "20 minutes ago"
    assert relative_time(at(20, 0, 5), now, UTC) == "Yesterday"             # Over 24 hours, but still yesterday.
    assert relative_time(at(19, 23, 50), now, UTC) == "2 days ago"          # Barely over 24 hours, two days back.


def test_an_exact_time_is_written_in_full():
    assert full_time(datetime(2026, 10, 21, 7, 5, tzinfo=UTC).timestamp(), UTC) == "21 Oct 2026, 07:05"
    assert full_time(datetime(2027, 1, 3, 23, 59, tzinfo=UTC).timestamp(), UTC) == "3 Jan 2027, 23:59"


@pytest.mark.parametrize("seconds, expected", [
    (0.0, "0.0 s"), (0.44, "0.4 s"), (4.25, "4.2 s"), (9.96, "10.0 s"), (10.0, "10 s"), (38.4, "38 s"),
    (59.6, "1 min 0 s"), (125, "2 min 5 s"), (3600, "60 min 0 s"),
])
def test_how_long_a_translation_took(seconds, expected):
    assert duration_label(seconds) == expected


def test_a_preview_is_one_line():
    assert preview("first line\nsecond   line\t\tthird") == "first line second line third"
    assert preview("  padded  ") == "padded"
    assert preview("") == ""


def test_a_long_preview_is_cut_with_an_ellipsis_and_never_exceeds_its_limit():
    text = "word " * 100
    short = preview(text, 20)
    assert len(short) == 20 and short.endswith("…")
    assert preview("x" * 20, 20) == "x" * 20


def test_a_preview_of_arabic_keeps_whole_characters():
    assert preview("مرحبا بالعالم " * 30, 15).endswith("…")


# --- What the question about an earlier run says ---

def unit(name, code, done=1, total=2, state="partial"):
    """The saved state of one output (``code`` is its language) of the video ``name``."""
    return UnitStatus(f"/videos/{name}", code, state, done, total)


def test_nothing_saved_describes_nothing():
    assert resume_summary(ResumeReport()) == ""


@pytest.mark.parametrize("count, line", [
    (1, "• 1 output file already finished"),
    (2, "• 2 output files already finished"),
])
def test_finished_outputs_are_counted(count, line):
    report = ResumeReport(units=[unit(f"{n}.mp4", "es", 2, 2, "done") for n in range(count)])
    assert resume_summary(report) == line


def test_a_partly_translated_file_says_how_far_it_got():
    report = ResumeReport(units=[unit("one.mp4", "es", 5, 9), unit("two.srt", "_transcript", 3, 4)])
    assert resume_summary(report) == (
        "• one.mp4 → Spanish: 5 of 9 subtitles translated\n"
        "• two.srt: 3 of 4 subtitles translated"
    )


def test_a_language_the_list_does_not_know_is_shown_by_its_code():
    assert resume_summary(ResumeReport(units=[unit("one.mp4", "zz")])) == "• one.mp4 → zz: 1 of 2 subtitles translated"


def test_only_the_first_three_partly_translated_files_are_listed():
    report = ResumeReport(units=[unit(f"{n}.mp4", "fr") for n in range(1, 7)])
    lines = resume_summary(report).split("\n")
    assert len(lines) == 4 and lines[0].startswith("• 1.mp4") and lines[2].startswith("• 3.mp4")
    assert lines[3] == "• …and 3 more in progress"


def test_exactly_three_are_all_listed_with_no_more_line():
    report = ResumeReport(units=[unit(f"{n}.mp4", "fr") for n in range(1, 4)])
    assert "more" not in resume_summary(report) and resume_summary(report).count("\n") == 2


def test_an_interrupted_transcription_that_will_be_continued_says_where():
    interrupted = [PartialInfo("/videos/talk.mp4", 3725.9, 40)]
    report = ResumeReport(partial_transcripts=interrupted, use_partial_transcripts=True)
    assert resume_summary(report) == (
        "• talk.mp4: transcription stopped at 1:02:05 — will continue from there (the join may be slightly off)"
    )


def test_an_interrupted_transcription_that_will_not_be_continued_says_how_to_change_that():
    interrupted = [PartialInfo("/videos/talk.mp4", 65.0, 3)]
    report = ResumeReport(partial_transcripts=interrupted, use_partial_transcripts=False)
    assert resume_summary(report) == (
        "• talk.mp4: transcription was interrupted and will restart from the beginning "
        "(turn on “Resume interrupted transcription” in Preferences to continue it)"
    )


def test_only_the_first_three_interrupted_transcriptions_are_listed():
    partial = [PartialInfo(f"/videos/{n}.mp4", 5.0, 1) for n in range(1, 6)]
    lines = resume_summary(ResumeReport(partial_transcripts=partial, use_partial_transcripts=True)).split("\n")
    assert len(lines) == 4 and lines[3] == "• …and 2 more interrupted transcriptions"


@pytest.mark.parametrize("count, line", [
    (1, "• 1 video already transcribed (speech recognition will be skipped)"),
    (3, "• 3 videos already transcribed (speech recognition will be skipped)"),
])
def test_saved_transcripts_are_counted(count, line):
    assert resume_summary(ResumeReport(transcripts=[f"/videos/{n}.mp4" for n in range(count)])) == line


def test_everything_is_described_in_this_order():
    report = ResumeReport(
        units=[unit("done.mp4", "es", 2, 2, "done"), unit("half.mp4", "es", 1, 2)],
        transcripts=["/videos/a.mp4"],
        partial_transcripts=[PartialInfo("/videos/b.mp4", 59.0, 2)],
        use_partial_transcripts=True,
    )
    lines = resume_summary(report).split("\n")
    assert [line.split(" ")[1] for line in lines] == ["1", "half.mp4", "b.mp4:", "1"]
    assert "0:00:59" in lines[2]
