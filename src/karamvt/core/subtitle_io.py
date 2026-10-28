# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Reading and writing of SRT and WebVTT subtitle files.

Reading is tolerant: the text encoding of a file is detected (``decode_subtitle``) rather than assumed to be UTF-8,
and malformed cues are skipped and reported instead of failing the whole file. Writing is atomic.
"""

from __future__ import annotations

import codecs
import os
import re
import unicodedata
from typing import Callable

from .models import Caption, OutputFormat

# --- Reading a subtitle file whatever it was saved as ---
#
# Subtitle files found on the web are not all UTF-8: Arabic ones are very often Windows-1256, Russian ones
# Windows-1251, Western European ones Windows-1252, and some tools write a byte-order mark or UTF-16. Reading all of
# them as strict UTF-8 made every one of those files fail, so a file that "looked fine" in any player was refused.
# UTF-8 (with or without a mark) is still tried first, because nearly every file is UTF-8; the other encodings are
# only for files that would otherwise have been refused.

# Byte-order marks and the codec that reads each. The UTF-32 marks come before the UTF-16 ones because the
# little-endian UTF-32 mark starts with the same bytes as the little-endian UTF-16 mark.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# The legacy code page that files in a language (Whisper language code) usually come in; tried first when the source
# language is known.
_LEGACY_FOR_LANGUAGE = {
    "ar": "cp1256", "fa": "cp1256", "ur": "cp1256", "ps": "cp1256", "ug": "cp1256", "sd": "cp1256",
    "he": "cp1255", "yi": "cp1255",
    "el": "cp1253",
    "ru": "cp1251", "uk": "cp1251", "bg": "cp1251", "sr": "cp1251", "mk": "cp1251", "be": "cp1251", "kk": "cp1251",
    "mn": "cp1251", "tg": "cp1251", "ky": "cp1251",
    "tr": "cp1254", "az": "cp1254",
    "pl": "cp1250", "cs": "cp1250", "sk": "cp1250", "hu": "cp1250", "ro": "cp1250", "hr": "cp1250",
    "sl": "cp1250", "bs": "cp1250", "sq": "cp1250",
    "vi": "cp1258", "th": "cp874",
    "ja": "cp932", "ko": "cp949", "zh": "gb18030",
}
# Candidate code pages when there is no language hint, in this order. Western European comes first: it is the most
# common, and a file that really is one has few accented letters. The order of the others settles which of two that
# both decode wins (a code page with undefined bytes fails on text meant for another one, which is what tells them
# apart).
_COMMON_LEGACY = ("cp1252", "cp1256", "cp1255", "cp1253", "cp1251", "cp1254", "cp1250")
_EAST_ASIAN_LEGACY = ("cp932", "cp949", "gb18030", "big5")
# Symbols that occur in ordinary text and therefore do not suggest a wrongly decoded file: Spanish question and
# exclamation marks, units and currencies.
_EVERYDAY_SYMBOLS = frozenset("¿¡°º€£¥©®·")
# The punctuation a Western code page keeps in 0x80-0x9F (curly quotes, dashes, the ellipsis). A little of it is
# normal; a lot means the bytes were East Asian text, whose first bytes fall in that range.
_TYPOGRAPHIC = frozenset(bytes(range(0x80, 0xA0)).decode("cp1252", errors="ignore")) - frozenset("ŠŒŽšœžŸƒˆ˜")

# Scripts that count as one when judging the non-ASCII letters, because East Asian text normally mixes them.
_SCRIPT_GROUPS = {"CJK": "CJK", "HIRAGANA": "CJK", "KATAKANA": "CJK", "HANGUL": "CJK", "BOPOMOFO": "CJK"}
# How many leading characters of a decoded file are inspected when judging whether it reads like text.
_SAMPLE_CHARS = 20_000
# A word: two or more letters, without digits or underscores.
_WORD = re.compile(r"[^\W\d_]{2,}")


def _script(char: str) -> str:
    """Return the script of a character (the first word of its Unicode name), with the CJK scripts merged."""
    first = unicodedata.name(char, "").split(" ", 1)[0]
    return _SCRIPT_GROUPS.get(first, first)


def _symbols_among_accented(sample: str, accented: list[str]) -> float:
    """Return the share of the non-ASCII characters that are symbols rather than letters.

    In real text the non-ASCII characters are letters (and their marks); text decoded with the wrong code page leaves
    a third of them as symbols such as ¡ ¢ £ § ¶ ¼ ¿.
    """
    others = [
        char for char in sample
        if ord(char) > 127 and not char.isspace() and char not in _TYPOGRAPHIC and char not in _EVERYDAY_SYMBOLS
        and unicodedata.category(char)[0] not in "LM" and unicodedata.category(char) not in ("Pi", "Pf", "Pd")
    ]
    return len(others) / (len(accented) + len(others))


def _main_script_share(accented: list[str]) -> tuple[str, float]:
    """Return the script that most of the non-ASCII letters are in, and the share of them that it has."""
    scripts: dict[str, int] = {}
    for char in accented:
        name = _script(char)
        scripts[name] = scripts.get(name, 0) + 1
    main, count = max(scripts.items(), key=lambda item: item[1])
    return main, count / len(accented)


def _plausible_non_ascii(sample: str, letters: list[str], accented: list[str]) -> bool:
    """Return whether the non-ASCII letters of ``sample`` look like those of one language."""
    if sum(1 for char in sample if char in _TYPOGRAPHIC) / len(sample) > 0.08:
        return False
    if _symbols_among_accented(sample, accented) > 0.20:
        return False
    main, share = _main_script_share(accented)
    if share < 0.85:
        return False
    # In Latin text, accents are a small part of the letters.
    return not (main == "LATIN" and len(accented) / len(letters) > 0.30)


def _switches_case_inside_words(sample: str) -> bool:
    """Return whether words change between lower and upper case in the middle, as wrongly decoded text does."""
    words = _WORD.findall(sample)
    if not words:
        return False
    switching = sum(
        1 for word in words if any(a.islower() and b.isupper() for a, b in zip(word, word[1:], strict=False))
    )
    return switching / len(words) > 0.03


def _plausible_text(text: str) -> bool:
    """Return whether ``text`` reads like text in one language, not like bytes read with the wrong code page.

    Read wrongly, Arabic comes out as accented Latin letters in random case, Russian as Latin letters that are nearly
    all accented, one code page's letters as another script's mixed with Latin. So the non-ASCII letters must be of
    one script; in Latin text they must be a small part of the letters (as accents are); and a word must not switch
    between lower and upper case in the middle. A replacement character or a C1 control character rejects the text
    outright.
    """
    sample = text[:_SAMPLE_CHARS]
    letters = [char for char in sample if char.isalpha()]
    if not letters:
        return True
    if "\ufffd" in sample or any("\x80" <= char <= "\x9f" for char in sample):
        return False
    accented = [char for char in letters if ord(char) > 127]
    if accented and not _plausible_non_ascii(sample, letters, accented):
        return False
    return not _switches_case_inside_words(sample)


def _plausible_east_asian(encoding: str, text: str) -> bool:
    """Return whether ``text`` is plausible as text in the East Asian code page ``encoding``.

    East Asian code pages decode each other's text into valid but meaningless ideographs, so what the text must
    contain differs: Japanese has kana, Korean is Hangul, Chinese is ideographs alone.
    """
    kana = hangul = han = 0
    for char in text[:_SAMPLE_CHARS]:
        name = unicodedata.name(char, "")
        if name.startswith(("HIRAGANA", "KATAKANA")):
            kana += 1
        elif name.startswith("HANGUL"):
            hangul += 1
        elif name.startswith("CJK UNIFIED"):
            han += 1
    total = kana + hangul + han
    if not total:
        return False
    if encoding == "cp932":
        return kana / total >= 0.15 and hangul / total < 0.05
    if encoding == "cp949":
        return hangul / total >= 0.8
    return han / total >= 0.95


def _encoding_label(encoding: str) -> str:
    """Return the name of a Python codec as shown to the user, for example "Windows-1256"."""
    return {
        "utf-8": "UTF-8", "utf-8-sig": "UTF-8", "utf-16": "UTF-16", "utf-32": "UTF-32",
        "gb18030": "GB18030", "big5": "Big5",
    }.get(encoding, encoding.replace("cp", "Windows-") if encoding.startswith("cp1") else encoding.upper())


def _decode_with_mark(raw: bytes) -> tuple[str, str] | None:
    """Return the text of a file that starts with a byte-order mark, and its encoding.

    The result is None when there is no mark, or when the text after it cannot be read as the mark says.
    """
    for mark, encoding in _BOMS:
        if raw.startswith(mark):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                return None
    return None


def _decode_utf16_without_mark(raw: bytes) -> str | None:
    """Return the text of UTF-16 without a byte-order mark, recognised by its zero bytes.

    UTF-16 shows as zeros beside the ASCII letters, digits and punctuation, all on the same side. Text that is only
    ASCII is valid UTF-8 as well, zeros included, so this check runs before UTF-8 is accepted. The result is None
    when the bytes do not look like that, or decode to many control characters.
    """
    zeros = raw.count(0)
    if len(raw) < 4 or zeros < max(2, len(raw) // 50):
        return None
    even, odd = raw[0::2].count(0), raw[1::2].count(0)
    if max(even, odd) < 0.9 * zeros:
        return None
    try:
        text = raw.decode("utf-16-le" if odd >= even else "utf-16-be")
    except UnicodeDecodeError:
        return None
    control_characters = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    return text if control_characters <= len(text) // 20 else None


def _decode_legacy(raw: bytes, language_code: str | None) -> tuple[str, str] | None:
    """Return the text of a file in one of the legacy code pages, and which one.

    The code page usual for ``language_code`` is tried first, then the others; the first that decodes to text that
    reads like text wins. The result is None when none does.
    """
    hinted = _LEGACY_FOR_LANGUAGE.get((language_code or "").split("-")[0].lower())
    candidates = [hinted] if hinted else []
    candidates += [name for name in (*_COMMON_LEGACY, *_EAST_ASIAN_LEGACY) if name != hinted]
    for encoding in candidates:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        east_asian = encoding in _EAST_ASIAN_LEGACY
        if _plausible_east_asian(encoding, text) if east_asian else _plausible_text(text):
            return text, encoding
    return None


def decode_subtitle(raw: bytes, language_code: str | None = None) -> tuple[str, str]:
    """Return the text of a subtitle file and the encoding it was read as.

    A byte-order mark decides; else UTF-16 without a mark (told by its zeros); else UTF-8; else the legacy code pages,
    the one usual for ``language_code`` (a Whisper code such as "ar", when known) first. When nothing fits, the file
    is read as Windows-1252 with the unreadable bytes replaced, so that the job still goes on and the log says what
    happened; the encoding is then reported as "cp1252?", with a question mark. Never raises.
    """
    marked = _decode_with_mark(raw)
    if marked is not None:
        return marked
    utf16 = _decode_utf16_without_mark(raw)
    if utf16 is not None:
        return utf16, "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    legacy = _decode_legacy(raw, language_code)
    return legacy if legacy is not None else (raw.decode("cp1252", errors="replace"), "cp1252?")


def _normalize_line_endings(content: str) -> str:
    r"""Convert ``\r\n`` and bare ``\r`` line endings to ``\n``.

    Files sourced from Windows tools (or downloaded off the web) commonly use ``\r\n`` or bare ``\r`` line endings.
    Left alone, splitting into blocks at blank lines silently fails to find the blank lines at all, and trailing
    ``\r`` characters end up glued onto timestamps, which breaks float parsing.
    """
    return content.replace("\r\n", "\n").replace("\r", "\n")


# Matches an SRT timing line such as "00:00:01,000 --> 00:00:03,500", with "." accepted for the comma and any spacing
# around the arrow; whatever follows (screen coordinates) is ignored. Groups 1-4 are the start time, 5-8 the end time.
_TIMING =re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


# A block that starts like this is the header or metadata of a WebVTT file, not a cue.
_VTT_DIRECTIVES = ("WEBVTT", "NOTE", "STYLE", "REGION")


def _report_skipped(skipped: int, on_progress: Callable[[str], None]) -> None:
    """Tell the user how many malformed subtitle entries were skipped, if any."""
    if skipped:
        on_progress(f"  Skipped {skipped} malformed subtitle entr{'y' if skipped == 1 else 'ies'}.")


class SubtitleFileHandler:
    """Reader and writer for SRT and WebVTT subtitle files.

    The class keeps no state. The format of a file that is read follows its extension; the format of a file that is
    written is chosen by the caller.
    """

    # --- Public interface ---

    def load(
        self, path: str, on_progress: Callable[[str], None] = print, language_code: str | None = None,
    ) -> list[Caption]:
        """Parse a subtitle file, detecting the format from its extension.

        A ``.vtt`` file is read as WebVTT, any other file as SRT. Malformed cues are skipped and counted in a message
        to ``on_progress``.

        Args:
            path: The subtitle file to read.
            on_progress: Receives human-readable progress messages.
            language_code: Whisper code of the subtitle language, if known; it helps when the file is not UTF-8
                (see ``decode_subtitle``).
        """
        ext = os.path.splitext(path)[1].lower()
        content = self._read(path, on_progress, language_code)
        if ext == ".vtt":
            return self._parse_vtt(content, on_progress)
        return self._parse_srt(content, on_progress)

    @staticmethod
    def _read(path: str, on_progress: Callable[[str], None], language_code: str | None) -> str:
        """Read a file as text whatever its encoding, with normalized line endings, and note it if it is not UTF-8."""
        with open(path, "rb") as fh:
            raw = fh.read()
        text, encoding = decode_subtitle(raw, language_code)
        if encoding.endswith("?"):
            on_progress("  Warning: could not tell this file's encoding; read as Windows-1252, "
                        "so some characters may be wrong. Saving it as UTF-8 first may help.")
        elif encoding not in ("utf-8", "utf-8-sig"):
            on_progress(f"  The file is not UTF-8: read as {_encoding_label(encoding)}.")
        return _normalize_line_endings(text)

    def save(
        self,
        captions: list[Caption],
        output_path: str,
        fmt: OutputFormat = OutputFormat.SRT,
        on_progress: Callable[[str], None] = print,
    ) -> None:
        """Write the captions to ``output_path`` as an SRT or WebVTT file (UTF-8).

        If writing fails or is interrupted, the temporary file is removed and the exception propagates.
        """
        on_progress(f"\nSaving {fmt.label} subtitles to: {output_path}")
        # Written to a temporary file and renamed into place, so that a crash or power cut mid-save can never leave a
        # truncated subtitle file (or replace a good existing one with half of a new one).
        tmp_path = output_path + ".part"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                if fmt is OutputFormat.VTT:
                    self._write_vtt(fh, captions)
                else:
                    self._write_srt(fh, captions)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, output_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    # --- SRT ---

    def _parse_srt(self, content: str, on_progress: Callable[[str], None]) -> list[Caption]:
        """Parse the text of an SRT file; blocks without a usable cue are skipped and counted."""
        # A regex split on two or more consecutive blank lines (rather than a literal "\n\n") tolerates the
        # occasional extra blank line that some generators leave between cues, instead of desyncing every block
        # after it.
        blocks = [b for b in re.split(r"\n{2,}", content.strip()) if b.strip()]
        cues = [self._srt_cue(block) for block in blocks]
        _report_skipped(cues.count(None), on_progress)
        return [cue for cue in cues if cue is not None]

    def _srt_cue(self, block: str) -> Caption | None:
        """Return the caption in one block of an SRT file, or None when the block has no usable one."""
        lines = block.strip().split("\n")
        # The timing line is normally the second (after the number), but files without numbers, with an extra line
        # before it, or with screen coordinates after the end time are common.
        index = next((i for i, line in enumerate(lines) if _TIMING.search(line)), None)
        if index is None:
            return None
        match = _TIMING.search(lines[index])
        text = "\n".join(lines[index + 1:]).strip()
        if not text:
            return None
        try:
            return Caption(
                start=self._seconds(*match.group(1, 2, 3, 4)),
                end=self._seconds(*match.group(5, 6, 7, 8)),
                text=text,
            )
        except ValueError:
            # One malformed cue must not take down the whole file. It is skipped, and the caller counts and reports it,
            # so that it is not a silent loss.
            return None

    def _write_srt(self, fh, captions: list[Caption]) -> None:
        for index, caption in enumerate(captions, start=1):
            start = self._format_srt_time(caption.start)
            end = self._format_srt_time(caption.end)
            fh.write(f"{index}\n{start} --> {end}\n{caption.text.strip()}\n\n")

    # --- VTT ---

    def _parse_vtt(self, content: str, on_progress: Callable[[str], None]) -> list[Caption]:
        """Parse the text of a WebVTT file into Caption objects.

        Handles:
          - WEBVTT header (with or without optional description on the same line)
          - NOTE / STYLE / REGION metadata blocks (skipped)
          - Optional cue identifiers before the timestamp line
          - Trailing cue settings on the timestamp line (position, align, etc.)
          - Both HH:MM:SS.mmm and MM:SS.mmm timestamp formats
        """
        captions: list[Caption] = []
        skipped = 0
        for block in re.split(r"\n{2,}", content.strip()):
            # Whitespace-only lines inside a block are dropped.
            lines = [ln for ln in block.strip().split("\n") if ln.strip()]
            if not lines or lines[0].startswith(_VTT_DIRECTIVES):
                continue
            cue = self._vtt_cue(lines)
            if cue is None:
                skipped += 1
            else:
                captions.append(cue)
        _report_skipped(skipped, on_progress)
        return captions

    def _vtt_cue(self, lines: list[str]) -> Caption | None:
        """Return the caption in one block (its non-empty lines) of a WebVTT file, or None if it has no usable one."""
        # The timestamp line is the first one that contains " --> "; an optional cue identifier may precede it.
        ts_idx = next((i for i, ln in enumerate(lines) if " --> " in ln), None)
        if ts_idx is None:
            return None
        # The timestamp line may have trailing cue settings after the end time, so only the first three tokens
        # are used: start, "-->", end.
        ts_tokens = lines[ts_idx].split()
        if len(ts_tokens) < 3:
            return None
        # Everything after the timestamp line is the cue payload.
        text = "\n".join(lines[ts_idx + 1:]).strip()
        if not text:
            return None
        try:
            return Caption(start=self._parse_vtt_time(ts_tokens[0]), end=self._parse_vtt_time(ts_tokens[2]), text=text)
        except ValueError:
            return None

    def _write_vtt(self, fh, captions: list[Caption]) -> None:
        fh.write("WEBVTT\n\n")
        for index, caption in enumerate(captions, start=1):
            start = self._format_vtt_time(caption.start)
            end = self._format_vtt_time(caption.end)
            fh.write(f"{index}\n{start} --> {end}\n{caption.text.strip()}\n\n")

    # --- Time helpers ---

    @staticmethod
    def _format_srt_time(total_seconds: float) -> str:
        """Format a time in seconds as ``HH:MM:SS,mmm``."""
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02},{ms:03}"

    @staticmethod
    def _format_vtt_time(total_seconds: float) -> str:
        """Format a time in seconds as ``HH:MM:SS.mmm``; WebVTT uses a dot, not a comma, before the milliseconds."""
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}.{ms:03}"

    @staticmethod
    def _seconds(hours: str, minutes: str, seconds: str, fraction: str) -> float:
        """Convert the four groups of one time captured by ``_TIMING`` to seconds."""
        return int(hours) * 3600 + int(minutes) * 60 + float(f"{int(seconds)}.{fraction}")

    @staticmethod
    def _parse_vtt_time(time_str: str) -> float:
        """Convert a WebVTT timestamp, either ``HH:MM:SS.mmm`` or ``MM:SS.mmm``, to seconds.

        Raises:
            ValueError: The timestamp is malformed.
        """
        parts = time_str.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        return float(time_str)
