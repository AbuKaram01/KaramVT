# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure helpers that decide what to display.

The functions here turn sizes, times, job results and resume reports into user-facing text. They import no GTK,
so they can be tested without a display.
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo

from ..core.checkpoint import TRANSCRIPT_UNIT, ResumeReport, UnitStatus
from ..core.languages import LANGUAGES

_MB = 1_000_000
_GB = 1_000 * _MB
_NOTICE_ERROR_CHARS = 120
# How many files of one kind the resume question names before it says "…and N more".
_LISTED_PER_KIND = 3


def size_label(size: int) -> str:
    """Return a download size as Hugging Face writes it: decimal units, e.g. "3.2 GB" or "17 MB"."""
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{max(1, round(size / _MB))} MB"


def video_done_notice(selected_path: str | None, batch: bool, files: int) -> tuple[str, str]:
    """Return the title and text of the notification for a finished video job.

    The text names the file for a single video, and the folder with its file count for a batch.
    """
    name = os.path.basename(os.path.normpath(selected_path)) if selected_path else ""
    if not batch:
        return "Subtitles are ready", name
    count = f"{files} file{'s' if files != 1 else ''}"
    return "Batch finished", f"{name} — {count}" if name else count


def short_error(message: str, limit: int = _NOTICE_ERROR_CHARS) -> str:
    """Return the first line of an error message, shortened with an ellipsis, for use in a notification."""
    line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    if not line:
        return "Check the log for details."
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def relative_time(moment: float, now: float, tz: tzinfo | None = None) -> str:
    """Return when something happened, as a list of recent items words it.

    The result is "Just now", "5 minutes ago", "Yesterday" and so on, and from a week on the date. The text is
    English, like the rest of the interface. ``tz`` is the local time zone unless a test passes another one.
    """
    seconds = max(0.0, now - moment)
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{_plural(int(seconds // 60), 'minute')} ago"
    if seconds < 86400:
        return f"{_plural(int(seconds // 3600), 'hour')} ago"
    then, today = datetime.fromtimestamp(moment, tz), datetime.fromtimestamp(now, tz)
    days = (today.date() - then.date()).days
    if days <= 1:
        return "Yesterday"
    if days < 7:
        return f"{days} days ago"
    return f"{then.day} {_MONTHS[then.month - 1]} {then.year}"


def full_time(moment: float, tz: tzinfo | None = None) -> str:
    """Return an exact date and time, e.g. "21 Oct 2026, 07:41"."""
    then = datetime.fromtimestamp(moment, tz)
    return f"{then.day} {_MONTHS[then.month - 1]} {then.year}, {then:%H:%M}"


def duration_label(seconds: float) -> str:
    """Return how long something took, e.g. "0.4 s", "4.2 s", "38 s" or "2 min 5 s"."""
    if seconds < 10:
        return f"{seconds:.1f} s"
    whole = round(seconds)
    if whole < 60:
        return f"{whole} s"
    return f"{whole // 60} min {whole % 60} s"


def preview(text: str, limit: int = 80) -> str:
    """Return ``text`` as a single line for a list, cut with an ellipsis when it is longer than ``limit``."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "\u2026"


# --- What an earlier run of a job left behind ---

def _language_name(code: str) -> str:
    language = LANGUAGES.get(code)
    return language.name if language else code


def _clock(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _partial_translation_lines(partial: list[UnitStatus]) -> list[str]:
    lines = []
    for unit in partial[:_LISTED_PER_KIND]:
        target = "" if unit.unit_code == TRANSCRIPT_UNIT else f" \u2192 {_language_name(unit.unit_code)}"
        name = os.path.basename(unit.input_path)
        lines.append(f"\u2022 {name}{target}: {unit.lines_done} of {unit.lines_total} subtitles translated")
    if len(partial) > _LISTED_PER_KIND:
        lines.append(f"\u2022 \u2026and {len(partial) - _LISTED_PER_KIND} more in progress")
    return lines


def _interrupted_transcription_lines(report: ResumeReport) -> list[str]:
    interrupted = report.partial_transcripts
    lines = []
    for info in interrupted[:_LISTED_PER_KIND]:
        name = os.path.basename(info.input_path)
        if report.use_partial_transcripts:
            lines.append(
                f"\u2022 {name}: transcription stopped at {_clock(info.seconds_done)} "
                "\u2014 will continue from there (the join may be slightly off)"
            )
        else:
            lines.append(
                f"\u2022 {name}: transcription was interrupted and will restart from the beginning "
                "(turn on \u201cResume interrupted transcription\u201d in Preferences to continue it)"
            )
    if len(interrupted) > _LISTED_PER_KIND:
        lines.append(f"\u2022 \u2026and {len(interrupted) - _LISTED_PER_KIND} more interrupted transcriptions")
    return lines


def resume_summary(report: ResumeReport) -> str:
    """Return the text of the question whether to continue an earlier, unfinished run of the same job.

    The text lists what that run left on disk, one bullet per kind of thing.
    """
    lines = []
    if report.done:
        lines.append(f"\u2022 {_plural(len(report.done), 'output file')} already finished")
    lines += _partial_translation_lines(report.partial)
    lines += _interrupted_transcription_lines(report)
    if report.transcripts:
        lines.append(
            f"\u2022 {_plural(len(report.transcripts), 'video')} already transcribed "
            "(speech recognition will be skipped)"
        )
    return "\n".join(lines)
