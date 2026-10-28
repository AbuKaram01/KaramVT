# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Saved progress for video and subtitle jobs, so that a stopped or crashed job can resume.

Four kinds of file are kept per input, all in the application's data folder (never next to the user's videos):

    <key>.transcript.json     The Whisper result for a video, written once the transcription has finished. A resumed
                              job skips speech recognition entirely.
    <key>.transcript.partial  The segments Whisper has produced so far for a video that has not finished. They are
                              only used if the user opted in to resuming transcription.
    <key>.<lang>.journal      One line per translated subtitle, appended (and fsynced) as each one is produced. The
                              worker can die at any moment and at most the line being translated is lost.
    <key>.<lang>.done.json    Written after an output file has been saved completely; lets a resumed batch skip it.

<key> is derived from the input's path. Everything stored carries a fingerprint of what it was made from (the input
file, the source and target languages, the models) and is ignored, never half-trusted, when any of it has changed.
The final subtitle file is only ever written when its job is complete, so a stopped job cannot leave a half-translated
file that looks finished.

Saving progress is strictly best-effort: if the data folder cannot be written (read-only, disk full, permissions) the
job carries on without it, with one warning, instead of failing. The saved files hold the user's own text, so the
folder and its files are private to the user (0700 / 0600).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import Caption, SUBTITLE_EXTENSIONS
from .paths import data_dir

# Version of the on-disk format. It is part of every header, so records written by another version are ignored.
# Version 2: every saved record carries a checksum (a flipped bit is detected, never trusted).
SCHEMA = 2
# Default age, in days, after which JobStore.prune() deletes saved progress.
_STALE_AFTER_DAYS = 30

# Pseudo target language used for "transcribe only" jobs, which have one output per input instead of one per target
# language.
TRANSCRIPT_UNIT = "_transcript"

# Header fields that a saved journal must match to be reused; a done marker must match the output-related ones too.
_JOURNAL_KEYS = ("v", "input", "src", "tgt", "llm")
_DONE_KEYS = _JOURNAL_KEYS + ("fmt", "outdir", "root")

# Explanations shown to the user, keyed by the header field whose mismatch made saved progress unusable.
_REASONS = {
    "v": "saved by a different version of the app",
    "input": "the input file changed",
    "src": "the source language setting changed",
    "tgt": "the target language changed",
    "llm": "the translation model changed",
    "fmt": "the output format changed",
    "outdir": "the output folder changed",
    "root": "the input folder changed",
}
# Further explanations, for the checks that are not a plain header comparison.
_DAMAGED = "the saved file is damaged"
_WHISPER_CHANGED = "the speech-recognition model changed"
_LANGUAGE_CHANGED = "a different source language was chosen"
_TRANSCRIPT_CHANGED = "the transcript changed"


def default_root() -> Path:
    """Return the folder inside the application's data folder where saved progress is kept."""
    return data_dir() / "resume"


# --- Fingerprints ---

def file_fingerprint(path: str) -> Optional[dict]:
    """Return the size and modification time of a file, or None if it cannot be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def model_signature(path: str) -> Optional[dict]:
    """Return the identity of a model on disk, or None if there is no path or it cannot be read.

    The model is either a .gguf file or a Whisper model folder. For a folder the sizes of its files are summed and the
    newest modification time is taken, so that replacing any of them changes the signature.
    """
    if not path:
        return None
    try:
        if os.path.isdir(path):
            size, newest = 0, 0
            for entry in os.scandir(path):
                if entry.is_file():
                    st = entry.stat()
                    size += st.st_size
                    newest = max(newest, st.st_mtime_ns)
            return {"path": os.path.abspath(path), "size": size, "mtime_ns": newest}
        st = os.stat(path)
        return {"path": os.path.abspath(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return None


def unit_header(
    input_path: str, src_code: Optional[str], unit_code: str, llm_sig: Optional[dict],
    fmt_ext: str = "", output_dir: Optional[str] = None, input_root: Optional[str] = None,
) -> dict:
    """Return the header that a translation journal or done marker must match to be reused.

    The pipeline and the GUI's pre-start scan both build their headers here, so that they can never disagree about
    what counts as "the same job".
    """
    return {
        "v": SCHEMA,
        "input": file_fingerprint(input_path),
        "src": src_code,
        "tgt": unit_code,
        "llm": llm_sig,
        "fmt": fmt_ext,
        "outdir": output_dir or "",
        "root": input_root or "",
    }


def captions_digest(captions: list[Caption]) -> str:
    """Return a digest of the captions' timings and text.

    A journal records the digest of the transcript it was translated from, so that translations of a different
    transcript are not reused.
    """
    h = hashlib.sha1()
    for c in captions:
        h.update(f"{c.start:.3f}|{c.end:.3f}|{c.text}\n".encode("utf-8"))
    return h.hexdigest()


def _mismatch(saved: dict, wanted: dict, keys) -> Optional[str]:
    """Return the reason for the first of *keys* on which the two headers differ, or None if they agree."""
    for key in keys:
        if saved.get(key) != wanted.get(key):
            return _REASONS[key]
    return None


# --- Atomic file helpers ---

def _atomic_write(path: Path, data: str) -> None:
    """Write a file by writing a temporary sibling and renaming it over the target.

    Readers see either the old file or the complete new one, never half of it. The folder and the file are created
    private to the user because they contain the user's text.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _crc(*parts) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF


def _seal(payload: dict) -> str:
    """Return the JSON text of *payload* together with a checksum.

    The checksum makes damage to the saved file (a disk error, a half-copied folder, hand editing) detectable, instead
    of silently producing a subtly wrong subtitle.
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return json.dumps({"d": payload, "c": _crc(body)}, ensure_ascii=False)


def _unseal(text: str) -> Optional[dict]:
    """Return the payload of text made by `_seal`, or None if it is malformed or fails its checksum."""
    try:
        obj = json.loads(text)
        payload, checksum = obj["d"], obj["c"]
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return payload if isinstance(payload, dict) and checksum == _crc(body) else None
    except (ValueError, KeyError, TypeError):
        return None


def _read_sealed(path: Path) -> Optional[dict]:
    """Return the verified payload of a sealed file, or None if it is missing, unreadable or damaged."""
    try:
        return _unseal(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):     # UnicodeDecodeError is a ValueError, so undecodable bytes are covered.
        return None


def _read_lines(path: Path) -> Optional[list[str]]:
    """Return the file's lines, or None if it cannot be read.

    Each line is decoded strictly on its own: a damaged line becomes an unparseable marker (which ends the usable part
    of a journal) rather than raising, or being "repaired" into plausible-looking wrong text.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    lines = []
    for chunk in raw.split(b"\n"):
        try:
            lines.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append("�")
    return lines


def _sane_times(start: float, end: float, previous_start: float = 0.0) -> bool:
    """Return whether the times are finite, in order (previous start <= start <= end) and plausible for a video."""
    return (
        math.isfinite(start) and math.isfinite(end)
        # The upper bound (1e7 seconds, about 115 days) exceeds the length of any real video.
        and 0.0 <= previous_start <= start <= end < 1e7
    )


def _times_in_order(captions: list[Caption]) -> bool:
    previous = 0.0
    for caption in captions:
        if not _sane_times(caption.start, caption.end, previous):
            return False
        previous = caption.start
    return True


def _caption_line(c: Caption) -> str:
    return json.dumps({"s": c.start, "e": c.end, "t": c.text, "c": _crc(c.start, c.end, c.text)}, ensure_ascii=False)


def _journal_line(index: int, text: str) -> str:
    return json.dumps({"i": index, "t": text, "c": _crc(index, text)}, ensure_ascii=False)


def _journal_entry(line: str) -> Optional[tuple[int, str]]:
    """Return (index, text) from one line of a journal, or None if the line is torn or damaged."""
    try:
        record = json.loads(line)
        index, text = record["i"], record["t"]
        if isinstance(index, int) and isinstance(text, str) and record["c"] == _crc(index, text):
            return index, text
    except (ValueError, KeyError, TypeError):
        pass
    return None


def _partial_caption(line: str, previous_start: float, first: bool) -> Optional[Caption]:
    """Return the segment on one line of a partial transcript, or None if the line is torn, damaged or out of order."""
    try:
        record = json.loads(line)
        start, end, text = float(record["s"]), float(record["e"]), record["t"]
        intact = (
            isinstance(text, str)
            and record["c"] == _crc(record["s"], record["e"], text)
            and _sane_times(start, end, previous_start)
        )
    except (ValueError, KeyError, TypeError):
        return None
    # Segment starts must strictly increase. The first segment is exempt: it may legitimately start at zero.
    if not intact or (not first and start <= previous_start):
        return None
    return Caption(start, end, text)


def _partial_captions(lines: list[str]) -> list[Caption]:
    """Return the segments before the first line that is torn, damaged, repeated or out of order."""
    captions: list[Caption] = []
    previous = 0.0
    for line in lines:
        if not line.strip():
            continue
        caption = _partial_caption(line, previous, first=not captions)
        if caption is None:
            break
        captions.append(caption)
        previous = caption.start
    return captions


def _stale_reason(saved: dict, input_path: str, whisper_sig: Optional[dict]) -> Optional[str]:
    """Return why a saved transcript (finished or not) was made from something else, or None if it matches."""
    if saved.get("v") != SCHEMA:
        return _REASONS["v"]
    if saved.get("input") != file_fingerprint(input_path):
        return _REASONS["input"]
    if saved.get("whisper") != whisper_sig:
        return _WHISPER_CHANGED
    return None


# --- Data objects ---

@dataclass
class CachedTranscript:
    """A finished Whisper transcription that was saved and can be reused."""

    captions: list[Caption]
    lang_code: str
    lang_name: str


@dataclass
class PartialTranscript:
    """The segments Whisper produced for a video whose transcription did not finish.

    Attributes:
        carried: How many of the leading *captions* an earlier resume carried over (already vetted).
    """

    captions: list[Caption]
    lang_code: str
    lang_name: str
    carried: int = 0

    @property
    def usable_segments(self) -> list[Caption]:
        """The segments a resumed run keeps (see core.transcription.plan_resume).

        All but the last one are kept: the segment that was being finished when the job stopped is redone. The
        exception is when nothing new was produced since the previous resume. Everything saved has then already been
        through that step and is kept, so that stopping again right after resuming never costs progress.
        """
        if self.carried and len(self.captions) == self.carried:
            return list(self.captions)
        return list(self.captions[:-1])


class _Appender:
    """Append-only line file that is flushed and fsynced after every line.

    If the disk fails mid-job it stops writing quietly (reporting once through *on_failure*) instead of raising:
    losing resumability is better than losing the job.

    Args:
        path: The file to append to; None disables writing (the caller could not create the file).
        on_failure: Called with the OSError when the file cannot be opened or written.
    """

    def __init__(self, path: Optional[Path], on_failure) -> None:
        self._on_failure = on_failure
        self._fh = None
        if path is not None:
            try:
                self._fh = open(path, "a", encoding="utf-8")
            except OSError as exc:
                on_failure(exc)

    def _write(self, line: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())
        except OSError as exc:
            self.close()
            self._on_failure(exc)

    def close(self) -> None:
        """Close the file; later writes are ignored. Safe to call more than once."""
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


class PartialTranscriptWriter(_Appender):
    """Append-only record of the segments Whisper has produced so far (fsynced per segment)."""

    def append(self, caption: Caption) -> None:
        """Record one more segment."""
        self._write(_caption_line(caption))


class TranslationJournal(_Appender):
    """Append-only record of the subtitle lines translated so far for one (input, target language) pair.

    Attributes:
        texts: The valid, contiguous prefix of lines found when the journal was opened; what a resumed job starts
            from.
        prior_header: The header the journal was resumed from; None for a fresh start.
    """

    def __init__(self, path: Optional[Path], texts: list[str], prior_header: Optional[dict], on_failure) -> None:
        super().__init__(path, on_failure)
        self.texts = texts
        self.prior_header = prior_header

    def append(self, index: int, text: str) -> None:
        """Record the translation of the subtitle at *index*."""
        self._write(_journal_line(index, text))


@dataclass
class UnitStatus:
    """Saved progress found for one unit of work: an input and one target language (or its transcription).

    Attributes:
        unit_code: The target language code, or TRANSCRIPT_UNIT for a transcribe-only job.
        state: "done" (the output is finished), "partial" (a usable journal exists) or "fresh" (nothing to resume).
        lines_done: How many translated lines the journal holds.
        lines_total: How many lines the whole subtitle has, as recorded in the journal; 0 unless state is "partial".
    """

    input_path: str
    unit_code: str
    state: str
    lines_done: int = 0
    lines_total: int = 0


@dataclass
class PartialInfo:
    """An interrupted transcription whose saved segments a resume could continue from.

    Attributes:
        seconds_done: How far into the video the saved segments reach.
        segments: How many segments would be kept.
    """

    input_path: str
    seconds_done: float
    segments: int


@dataclass
class ResumeReport:
    """What a scan found saved for a set of inputs, for the GUI to show before a job starts.

    Attributes:
        units: The status of every (input, target language) unit that was scanned, saved or not.
        transcripts: Videos whose finished transcription is saved.
        completed_outputs: Output files that are already finished.
        partial_transcripts: Videos whose transcription was interrupted and can be continued.
        use_partial_transcripts: Whether the user opted in to resuming those.
    """

    units: list[UnitStatus] = field(default_factory=list)
    transcripts: list[str] = field(default_factory=list)
    completed_outputs: set[str] = field(default_factory=set)
    partial_transcripts: list[PartialInfo] = field(default_factory=list)
    use_partial_transcripts: bool = False

    @property
    def done(self) -> list[UnitStatus]:
        """The units whose output is already finished."""
        return [u for u in self.units if u.state == "done"]

    @property
    def partial(self) -> list[UnitStatus]:
        """The units with a usable partial journal."""
        return [u for u in self.units if u.state == "partial"]

    @property
    def has_progress(self) -> bool:
        """Whether anything saved would be reused (partial transcripts count only if the user opted in)."""
        return bool(
            self.done or self.partial or self.transcripts
            or (self.use_partial_transcripts and self.partial_transcripts)
        )


def _transcript_from(saved: dict, requested_code: Optional[str]) -> tuple[Optional[CachedTranscript], Optional[str]]:
    """Return (transcript, None) for a sound saved transcript, else (None, reason).

    The reason is None when the saved transcript simply holds no segments.
    """
    try:
        code, name = saved["lang"]["code"], saved["lang"]["name"]
        if requested_code is not None and requested_code != code:
            return None, _LANGUAGE_CHANGED
        captions = [Caption(float(s), float(e), str(t)) for s, e, t in saved["captions"]]
    except (KeyError, TypeError, ValueError):
        return None, _DAMAGED
    if not _times_in_order(captions):
        return None, _DAMAGED
    if not captions:
        return None, None
    return CachedTranscript(captions, code, name), None


# --- The store ---

class JobStore:
    """Saved progress for jobs, kept as files in one private folder (see the module docstring for the layout).

    Every write is best-effort: the first failure is reported once through ``warn`` and later writes carry on without
    raising.

    Args:
        root: The folder to keep the files in; defaults to :func:`default_root`.

    Attributes:
        warn: Called once, with a sentence ready to show, the first time saving progress fails. Replace it to route
            the warning to the job log.
    """

    def __init__(self, root: Optional[os.PathLike] = None) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.warn = lambda message: None
        self._failed = False

    def _failure(self, exc: OSError) -> None:
        """Report the first failure to save progress through ``warn``; later failures are ignored."""
        if self._failed:
            return
        self._failed = True
        self.warn(
            f"Warning: couldn't save progress ({exc.strerror or exc}). The job continues normally, "
            "but if it stops it can't be resumed."
        )

    # --- Paths ---

    @staticmethod
    def _key(input_path: str) -> str:
        """Return a short, stable identifier for an input, derived from its resolved path."""
        return hashlib.sha1(os.path.realpath(input_path).encode("utf-8")).hexdigest()[:20]

    def _path(self, input_path: str, suffix: str) -> Path:
        return self.root / f"{self._key(input_path)}.{suffix}"

    # --- Transcripts (video inputs only) ---

    def save_transcript(
        self, input_path: str, captions: list[Caption], lang_code: str, lang_name: str,
        whisper_sig: Optional[dict],
    ) -> None:
        """Save the finished transcription of a video, so that a resumed job can skip speech recognition."""
        payload = {
            "v": SCHEMA, "input": file_fingerprint(input_path), "whisper": whisper_sig,
            "lang": {"code": lang_code, "name": lang_name},
            "captions": [[c.start, c.end, c.text] for c in captions],
        }
        try:
            _atomic_write(self._path(input_path, "transcript.json"), _seal(payload))
        except OSError as exc:
            self._failure(exc)

    def load_transcript(
        self, input_path: str, requested_code: Optional[str], whisper_sig: Optional[dict]
    ) -> tuple[Optional[CachedTranscript], Optional[str]]:
        """Return a saved transcript that can be reused, as (transcript, None).

        Otherwise the result is (None, reason), where reason says why saved data was not reused, or is None when
        nothing usable was saved. A transcript in a language other than *requested_code* is not reused; None accepts
        any language.
        """
        path = self._path(input_path, "transcript.json")
        if not path.exists():
            return None, None
        saved = _read_sealed(path)
        if saved is None:
            return None, _DAMAGED
        stale = _stale_reason(saved, input_path, whisper_sig)
        if stale is not None:
            return None, stale
        return _transcript_from(saved, requested_code)

    # --- Partial transcripts (a video Whisper has not finished) ---

    def start_partial_transcript(
        self, input_path: str, whisper_sig: Optional[dict], lang_code: str, lang_name: str,
        keep: list[Caption],
    ) -> PartialTranscriptWriter:
        """Begin recording the segments Whisper produces for a video.

        The record starts with a header and *keep*, the segments a resumed run carries over. For a fresh run *keep* is
        empty, which also discards any older record. If the file cannot be written, the returned writer is inert.
        """
        path = self._path(input_path, "transcript.partial")
        header = {
            "v": SCHEMA, "input": file_fingerprint(input_path), "whisper": whisper_sig,
            "lang": {"code": lang_code, "name": lang_name}, "keep": len(keep),
        }
        lines = [_seal(header)] + [_caption_line(c) for c in keep]
        try:
            _atomic_write(path, "\n".join(lines) + "\n")
        except OSError as exc:
            self._failure(exc)
            return PartialTranscriptWriter(None, self._failure)
        return PartialTranscriptWriter(path, self._failure)

    def load_partial_transcript(
        self, input_path: str, requested_code: Optional[str], whisper_sig: Optional[dict]
    ) -> tuple[Optional[PartialTranscript], Optional[str]]:
        """Return an interrupted transcription that can be continued, as (partial, None).

        Otherwise the result is (None, reason), where reason says why saved data was not reused, or is None when
        nothing usable was saved.
        """
        lines = _read_lines(self._path(input_path, "transcript.partial"))
        if lines is None:
            return None, None
        header = _unseal(lines[0])
        try:
            # The header is None when it is damaged; subscripting None raises TypeError.
            code, name = header["lang"]["code"], header["lang"]["name"]
        except (KeyError, TypeError):
            return None, _DAMAGED
        stale = _stale_reason(header, input_path, whisper_sig)
        if stale is not None:
            return None, stale
        if requested_code is not None and requested_code != code:
            return None, _LANGUAGE_CHANGED
        captions = _partial_captions(lines[1:])
        if not captions:
            return None, None
        carried = header.get("keep")
        return PartialTranscript(captions, code, name, carried if isinstance(carried, int) and carried > 0 else 0), None

    def drop_partial_transcript(self, input_path: str) -> None:
        """Delete the saved partial transcript of a video."""
        self._remove(self._path(input_path, "transcript.partial"))

    # --- Translation journals ---

    def open_journal(
        self, input_path: str, unit_code: str, header: dict, total: int, digest: str,
        sampling: dict, resume: bool = True,
    ) -> tuple[TranslationJournal, Optional[str]]:
        """Open the journal for one translation unit, resuming a saved one or creating a new one.

        A saved journal is reused only if its header matches *header* and it was made from the same transcript
        (*total* lines, *digest*). With ``resume=False`` any saved journal is discarded. *sampling* is recorded in
        the header for reference and does not decide whether the journal is reused.

        Returns:
            (journal, note). ``journal.texts`` holds the lines to resume from. *note* explains why saved progress was
            not reused, if some existed and was discarded; otherwise it is None.
        """
        path = self._path(input_path, f"{unit_code}.journal")
        wanted = dict(header, n=total, digest=digest)
        texts: list[str] = []
        prior: Optional[dict] = None
        note: Optional[str] = None
        if resume and path.exists():
            texts, prior, note = self._resume_point(path, wanted, total, digest)

        # Rewrite the file compactly before appending to it. This drops a torn last line and anything after the
        # valid prefix.
        lines = [_seal(dict(wanted, sampling=sampling))]
        lines += [_journal_line(i, t) for i, t in enumerate(texts)]
        try:
            _atomic_write(path, "\n".join(lines) + "\n")
        except OSError as exc:
            self._failure(exc)
            # The saved lines were read successfully, so the job can still resume from them without a journal file.
            return TranslationJournal(None, texts, prior, self._failure), note
        return TranslationJournal(path, texts, prior, self._failure), note

    @classmethod
    def _resume_point(
        cls, path: Path, wanted: dict, total: int, digest: str
    ) -> tuple[list[str], Optional[dict], Optional[str]]:
        """Return (lines to resume from, the header they were saved with, why the journal was not reused).

        The last item is None when the saved journal is reused.
        """
        saved_header, saved_texts = cls._read_journal(path)
        if saved_header is None:
            return [], None, _DAMAGED
        note = _mismatch(saved_header, wanted, _JOURNAL_KEYS)
        if note is None and (saved_header.get("n") != total or saved_header.get("digest") != digest):
            note = _TRANSCRIPT_CHANGED
        if note is not None:
            return [], None, note
        return saved_texts[:total], saved_header, None

    @staticmethod
    def _read_journal(path: Path) -> tuple[Optional[dict], list[str]]:
        """Return (header, valid lines) of a journal file; a torn, garbled or checksum-failing line ends the lines."""
        lines = _read_lines(path)
        header = _unseal(lines[0]) if lines is not None else None
        if header is None:
            return None, []
        texts: list[str] = []
        for line in lines[1:]:
            if not line.strip():
                continue
            entry = _journal_entry(line)
            if entry is None:
                break
            index, text = entry
            if index == len(texts):
                texts.append(text)
            elif index > len(texts):
                break     # A gap: nothing after it can be trusted.
            # Otherwise the index repeats a line that is already held, and the repeat is skipped.
        return header, texts

    def drop_journal(self, input_path: str, unit_code: str) -> None:
        """Delete the saved journal of one translation unit."""
        self._remove(self._path(input_path, f"{unit_code}.journal"))

    # --- Done markers ---

    def mark_done(self, input_path: str, header: dict, output_path: str) -> None:
        """Record that an output file was saved completely, so that a resumed batch can skip it.

        The marker holds the unit's header and a fingerprint of the output file.
        """
        payload = dict(header, output=dict(file_fingerprint(output_path) or {}, path=output_path))
        try:
            _atomic_write(self._path(input_path, f"{header['tgt']}.done.json"), _seal(payload))
        except OSError as exc:
            self._failure(exc)

    def done_output(self, input_path: str, header: dict) -> Optional[str]:
        """Return the path of the finished output file of a unit, or None.

        A path is returned only if the unit was completed earlier with the same inputs and settings and the output
        file is still exactly as it was left (same size and modification time).
        """
        saved = _read_sealed(self._path(input_path, f"{header['tgt']}.done.json"))
        if not saved or _mismatch(saved, header, _DONE_KEYS) is not None:
            return None
        out = saved.get("output") or {}
        current = file_fingerprint(out.get("path", ""))
        if current is None or current["size"] != out.get("size") or current["mtime_ns"] != out.get("mtime_ns"):
            return None
        return out["path"]

    # --- Scanning (used by the GUI before a job starts) ---

    def scan(
        self, input_paths: list[str], src_code: Optional[str], target_codes: list[str],
        llm_path: str, whisper_path: str, fmt_ext: str,
        output_dir: Optional[str], input_root: Optional[str],
        use_partial_transcripts: bool = False,
    ) -> ResumeReport:
        """Report the saved progress that a job on these inputs could reuse.

        For each input that is not a subtitle file, a saved or interrupted transcription is looked for. For each
        target language, a finished output or a journal is looked for. With no target languages the job only
        transcribes, and its single unit per input is TRANSCRIPT_UNIT.
        """
        report = ResumeReport(use_partial_transcripts=use_partial_transcripts)
        llm_sig = model_signature(llm_path) if target_codes else None
        whisper_sig = model_signature(whisper_path)
        unit_codes = target_codes or [TRANSCRIPT_UNIT]

        for input_path in input_paths:
            if os.path.splitext(input_path)[1].lower() not in SUBTITLE_EXTENSIONS:
                self._scan_transcript(report, input_path, src_code, whisper_sig)
            for unit in unit_codes:
                header = unit_header(input_path, src_code, unit, llm_sig, fmt_ext, output_dir, input_root)
                self._scan_unit(report, input_path, unit, header)
        return report

    def _scan_transcript(
        self, report: ResumeReport, input_path: str, src_code: Optional[str], whisper_sig: Optional[dict]
    ) -> None:
        """Add to *report* a saved transcript, or else an interrupted transcription that has segments to keep."""
        cached, _why = self.load_transcript(input_path, src_code, whisper_sig)
        if cached is not None:
            report.transcripts.append(input_path)
            return
        partial, _why = self.load_partial_transcript(input_path, src_code, whisper_sig)
        usable = partial.usable_segments if partial is not None else []
        if usable:
            report.partial_transcripts.append(PartialInfo(input_path, usable[-1].end, len(usable)))

    def _scan_unit(self, report: ResumeReport, input_path: str, unit: str, header: dict) -> None:
        """Add to *report* the status of one unit: finished, partially translated or fresh."""
        finished = self.done_output(input_path, header)
        if finished is not None:
            report.units.append(UnitStatus(input_path, unit, "done"))
            report.completed_outputs.add(finished)
            return
        path = self._path(input_path, f"{unit}.journal")
        saved_header, texts = self._read_journal(path) if path.exists() else (None, [])
        if saved_header is not None and texts and _mismatch(saved_header, header, _JOURNAL_KEYS) is None:
            report.units.append(UnitStatus(input_path, unit, "partial", len(texts), int(saved_header.get("n") or 0)))
        else:
            report.units.append(UnitStatus(input_path, unit, "fresh"))

    # --- Cleanup ---

    @staticmethod
    def _remove(path: Path) -> None:
        """Delete a file, ignoring errors: saved progress is best-effort."""
        try:
            path.unlink()
        except OSError:
            pass

    def clear_inputs(self, input_paths: list[str]) -> None:
        """Delete everything saved for these inputs, after success or when the user starts over."""
        keys = {self._key(p) for p in input_paths}
        for entry in self._entries():
            if entry.name.split(".", 1)[0] in keys:
                self._remove(entry)

    def clear_all(self) -> int:
        """Delete all saved progress and return the number of files removed."""
        removed = 0
        for entry in self._entries():
            if entry.is_file():
                self._remove(entry)
                removed += 1
        return removed

    def prune(self, max_age_days: int = _STALE_AFTER_DAYS) -> None:
        """Delete saved progress that nobody came back to (stopped jobs that were never resumed).

        A file is removed when its modification time is older than *max_age_days*.
        """
        cutoff = time.time() - max_age_days * 86400
        for entry in self._entries():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                pass

    def count_files(self) -> int:
        """Return the number of saved files."""
        return sum(1 for e in self._entries() if e.is_file())

    def _entries(self) -> list[Path]:
        try:
            return list(self.root.iterdir())
        except OSError:      # The folder is missing or unreadable: there is nothing to manage.
            return []
