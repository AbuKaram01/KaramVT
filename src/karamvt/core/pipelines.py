# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Batch pipeline that turns video and subtitle files into (translated) subtitle files.

``BatchPipeline`` runs in two phases, so that the large Whisper and LLM models are each loaded at most once per batch
and never at the same time. Progress is persisted through ``core.checkpoint``, which makes a stopped job resumable.
"""

from __future__ import annotations

import gc
import os
from typing import Callable, Optional

from ..config import AppConfig
from .cancel import CancelToken, check as _check_cancel
from .checkpoint import (
    JobStore, TRANSCRIPT_UNIT, captions_digest, model_signature, unit_header,
)
from .hardware import resolve_llm_gpu_layers, resolve_whisper_device
from .languages import LANGUAGES, Language, detect_language_from_text
from .llm_settings import sampling_from_config
from .models import Caption, OutputFormat, SUBTITLE_EXTENSIONS, build_output_path
from .subtitle_io import SubtitleFileHandler
from .transcription import TranscriptionEngine, load_whisper_model, plan_resume
from .translation import TranslationEngine, load_llm

# Receives a human-readable progress message.
ProgressCallback = Callable[[str], None]
# Receives the overall progress of the job as a fraction between 0.0 and 1.0.
PercentCallback = Callable[[float], None]


def _default_percent(_: float) -> None:
    """Ignore the progress fraction; used when the caller does not report one."""


def _language_of(code: str, name: str) -> Language:
    """Return the supported language with this code, or one rebuilt from the saved name if it is no longer supported."""
    return LANGUAGES.get(code) or Language(name=name, whisper_code=code)


class OutputNames:
    """Hands out output file names for one batch so that two different inputs never share one.

    The naming rule alone (``<name>_<lang>.<ext>``) would give ``lecture.mp4`` and ``lecture.srt`` in the same folder
    the same output, and the second would silently replace the first. The first input to claim a name keeps it; a
    later one gets ``name (2).ext``, ``name (3).ext`` and so on. Names are also reserved for outputs finished by an
    earlier run, so a resumed batch cannot hand the name of a finished file to another input.
    """

    def __init__(self) -> None:
        self._owner: dict[str, str] = {}

    def reserve(self, input_path: str, path: str) -> None:
        """Record that the output ``path``, finished by an earlier run, belongs to ``input_path``."""
        self._owner.setdefault(os.path.abspath(path), os.path.realpath(input_path))

    def claim(self, input_path: str, path: str) -> str:
        """Return ``path``, or the first free ``name (n).ext`` variant of it, and reserve it for ``input_path``."""
        me = os.path.realpath(input_path)
        base, ext = os.path.splitext(path)
        candidate, n = path, 1
        while self._owner.setdefault(os.path.abspath(candidate), me) != me:
            n += 1
            candidate = f"{base} ({n}){ext}"
        return candidate


class BatchPipeline:
    """Batch job that turns video and/or subtitle files into subtitle files.

    Handles every combination the app supports: one file or many (a course folder), transcribe-only or translate
    into one or several target languages, output next to each input or mirrored into a separate output folder. Runs
    in two phases so that the (large) Whisper and LLM models are each loaded at most once for the whole batch, never
    both at once:

      Phase 1 - transcribe or load every input file's captions.
      Phase 2 - for files with target languages, translate a fresh copy of that file's captions into each one and
                save it.

    An empty ``target_langs`` means transcribe-only: each file's own-language captions are saved once and no
    translation happens.

    The job is resumable (see ``core.checkpoint``): the transcription of every video is saved the moment it finishes,
    every translated subtitle line is journaled as it is produced, and every finished output is marked done. Running
    the same job again picks all of that up: finished outputs are skipped, saved transcriptions skip speech
    recognition, and a half-translated file continues from its first untranslated line. Anything that no longer
    matches (the input or a model changed) is discarded rather than trusted. The GUI's "Start over" simply clears
    the saved state first.

    Args:
        input_paths: Video and/or subtitle files to process.
        source_lang: Language of the inputs, or None to detect it.
        target_langs: Languages to translate into; empty for transcribe-only.
        output_format: Subtitle format of the files written.
        config: Application settings (model paths, device, threads, sampling).
        output_dir: Folder for the outputs, or None to write each output next to its input.
        input_root: Folder whose structure is mirrored below ``output_dir``; None for a flat output folder.
        on_progress: Receives human-readable progress messages.
        on_percent: Receives the overall progress as a fraction between 0.0 and 1.0.
        cancel: Token polled at checkpoints so that the user can stop the job.
        store: Store for the saved progress; a default ``JobStore`` is created if omitted.
    """

    def __init__(
        self,
        input_paths: list[str],
        source_lang: Optional[Language],
        target_langs: list[Language],
        output_format: OutputFormat,
        config: AppConfig,
        output_dir: Optional[str] = None,
        input_root: Optional[str] = None,
        on_progress: ProgressCallback = print,
        on_percent: PercentCallback = _default_percent,
        cancel: Optional[CancelToken] = None,
        store: Optional[JobStore] = None,
    ) -> None:
        self._cancel = cancel
        self._store = store if store is not None else JobStore()
        self._src_code = source_lang.whisper_code if source_lang is not None else None
        self._llm_sig = model_signature(config.llm_model_path) if target_langs else None
        self._whisper_sig = model_signature(config.whisper_model_path)
        # Number of outputs finished by an earlier run; they count as completed work in the progress bar and are
        # skipped.
        self._pre_done_units = 0
        self._input_paths = input_paths
        self._source_lang = source_lang
        self._target_langs = target_langs
        self._output_format = output_format
        self._config = config
        self._output_dir = output_dir
        self._input_root = input_root
        self.on_progress = on_progress
        self.on_percent = on_percent
        # A failure to save progress is reported in the job log.
        self._store.warn = on_progress
        self._subtitle_handler = SubtitleFileHandler()
        self._names = OutputNames()
        # Loaded when first needed (phase 1) and released at the end of that phase.
        self._whisper_model = None
        # Likewise for phase 2.
        self._llm = None

    def run(self) -> None:
        """Run both phases, then discard the saved progress of the finished job.

        Raises:
            ValueError: No file in the batch produced any usable captions.
            OperationCancelled: The user pressed Stop.
        """
        self._store.prune()
        per_file = self._transcribe_or_load_all()
        if not per_file:
            if self._pre_done_units:
                self.on_percent(1.0)
                self.on_progress("\nEverything in this batch was already completed in an earlier run.")
                self._store.clear_inputs(self._input_paths)
                return
            raise ValueError("No file in the batch produced any usable captions.")

        if not self._target_langs:
            self._save_transcriptions_only(per_file)
        else:
            self._translate_and_save_all(per_file)

        # Fully done: the saved progress has served its purpose.
        self._store.clear_inputs(self._input_paths)
        self.on_progress("\nProcess Completed Successfully!")

    # --- Helpers shared by the phases ---

    def _unit_codes(self) -> list[str]:
        """Return the codes of the outputs of one input: one per target language, or the transcript unit."""
        return [t.whisper_code for t in self._target_langs] or [TRANSCRIPT_UNIT]

    def _header(self, input_path: str, unit_code: str) -> dict:
        """Return the checkpoint header that identifies one output of an input and the settings it depends on."""
        # No translation model is involved (and _llm_sig is None) when only transcribing.
        return unit_header(
            input_path, self._src_code, unit_code, self._llm_sig,
            self._output_format.extension, self._output_dir, self._input_root,
        )

    def _already_done(self, input_path: str, unit_code: str) -> Optional[str]:
        """Return the output file finished by an earlier run for this unit, or None if it still has to be made."""
        return self._store.done_output(input_path, self._header(input_path, unit_code))

    def _loaded_whisper(self):
        """Return the speech-recognition model, loaded on first use and kept for the rest of the phase."""
        if self._whisper_model is None:
            device, compute_type = resolve_whisper_device(self._config.processing_device)
            self._whisper_model = load_whisper_model(
                self._config.whisper_model_path, device=device, compute_type=compute_type,
                cpu_threads=self._config.cpu_threads, on_progress=self.on_progress,
            )
        return self._whisper_model

    def _loaded_llm(self):
        """Return the translation model, loaded on first use and kept for the rest of the phase."""
        if self._llm is None:
            _check_cancel(self._cancel)
            self._llm = load_llm(
                self._config.llm_model_path, cpu_threads=self._config.cpu_threads,
                n_gpu_layers=resolve_llm_gpu_layers(self._config.processing_device), on_progress=self.on_progress,
            )
        return self._llm

    # --- Phase 1: transcribe or load every input ---

    def _transcribe_or_load_all(self) -> list[tuple[str, list[Caption], Language]]:
        """Return (input path, captions, source language) for every input that still has work to do.

        Inputs that were finished by an earlier run, or whose captions cannot be obtained (reported in the progress
        log), are left out.
        """
        results: list[tuple[str, list[Caption], Language]] = []
        total = len(self._input_paths)

        try:
            for index, input_path in enumerate(self._input_paths):
                _check_cancel(self._cancel)
                label = os.path.basename(input_path)
                if total > 1:
                    self.on_progress(f"\n=== File {index + 1}/{total}: {label} ===")
                if self._skip_if_finished(input_path, label):
                    continue
                try:
                    captions, source_lang = self._captions_of(input_path)
                except ValueError as exc:
                    self.on_progress(f"Skipping {label}: {exc}")
                    continue
                results.append((input_path, captions, source_lang))
        finally:
            # Release the Whisper model before the translation model is loaded, so that the two are never in memory
            # together.
            self._whisper_model = None
            gc.collect()

        return results

    def _skip_if_finished(self, input_path: str, label: str) -> bool:
        """Return True if every output of the file was finished earlier, so it is not even transcribed again.

        The names of the finished outputs are reserved, and the outputs count as completed work in the progress bar.
        """
        unit_codes = self._unit_codes()
        finished = [self._already_done(input_path, unit) for unit in unit_codes]
        if not all(finished):
            return False
        for done_path in finished:
            self._names.reserve(input_path, done_path)
        self.on_progress(f"Already completed in an earlier run — skipping {label}.")
        self._pre_done_units += len(unit_codes)
        return True

    def _captions_of(self, input_path: str) -> tuple[list[Caption], Language]:
        ext = os.path.splitext(input_path)[1].lower()
        if ext in SUBTITLE_EXTENSIONS:
            return self._load_subtitle_file(input_path, ext)
        return self._video_captions(input_path)

    def _video_captions(self, input_path: str) -> tuple[list[Caption], Language]:
        """Return the captions of a video from its saved transcription if that is still valid, else transcribe it."""
        cached, why_not = self._store.load_transcript(input_path, self._src_code, self._whisper_sig)
        if cached is not None:
            source_lang = _language_of(cached.lang_code, cached.lang_name)
            self.on_progress(
                f"Using the saved transcription ({len(cached.captions)} segments, "
                f"{source_lang.name}) \u2014 skipping speech recognition."
            )
            return cached.captions, source_lang

        if why_not:
            self.on_progress(f"Saved transcription not reused: {why_not}.")
        captions, source_lang = self._transcribe(input_path, self._loaded_whisper())
        self._store.save_transcript(
            input_path, captions, source_lang.whisper_code, source_lang.name, self._whisper_sig
        )
        self._store.drop_partial_transcript(input_path)
        return captions, source_lang

    def _load_subtitle_file(self, input_path: str, ext: str) -> tuple[list[Caption], Language]:
        """Parse a subtitle input; its language is detected from the text if the user did not choose one.

        Raises:
            ValueError: No cue could be parsed, or the language could not be matched to a supported one.
        """
        fmt_label = ext[1:].upper()
        self.on_progress(f"{fmt_label} file detected — skipping audio extraction and transcription.")
        captions = self._subtitle_handler.load(input_path, on_progress=self.on_progress, language_code=self._src_code)
        if not captions:
            raise ValueError(f"couldn't parse any cues from the {fmt_label} file")

        source_lang = self._source_lang
        if source_lang is None:
            self.on_progress("  Auto-detecting language from subtitle text...")
            sample = " ".join(c.text for c in captions[:60])
            source_lang = detect_language_from_text(sample)
            if source_lang is None:
                raise ValueError(
                    "could not match the subtitle text to a supported language — "
                    "choose the source language manually"
                )
            self.on_progress(f"  Detected: {source_lang.name}")

        return captions, source_lang

    def _transcribe(self, input_path, whisper_model) -> tuple[list[Caption], Language]:
        """Transcribe a video, continuing an interrupted transcription if resuming is enabled in the settings.

        Raises:
            ValueError: No speech was detected in the audio.
        """
        self.on_progress("Video file detected — starting transcription...")
        transcriber = TranscriptionEngine(
            self._config.whisper_model_path, model=whisper_model, on_progress=self.on_progress
        )
        keep: list[Caption] = []
        source_lang = self._source_lang
        # Opt-in: continue an interrupted transcription.
        if self._config.resume_transcription:
            keep, resumed_lang = self._interrupted_transcription(input_path)
            if resumed_lang is not None:
                source_lang = resumed_lang
        if source_lang is None:
            source_lang = transcriber.detect_language(input_path)

        # Every segment is recorded as it is produced, whether or not resuming is enabled (the record is only used
        # if it is), so that a stop or crash keeps everything transcribed so far.
        writer = self._store.start_partial_transcript(
            input_path, self._whisper_sig, source_lang.whisper_code, source_lang.name, keep
        )
        try:
            captions = transcriber.transcribe(
                input_path, source_lang, cancel=self._cancel,
                on_segment=writer.append, keep=keep or None,
            )
        finally:
            writer.close()
        if not captions:
            raise ValueError("no speech detected in the audio")
        return captions, source_lang

    def _interrupted_transcription(self, input_path: str) -> tuple[list[Caption], Optional[Language]]:
        """Return the segments to keep and their language from an earlier run that stopped part-way.

        The result is ([], None) if there is nothing worth continuing from.
        """
        partial, why_not = self._store.load_partial_transcript(input_path, self._src_code, self._whisper_sig)
        if why_not:
            self.on_progress(f"Saved partial transcription not reused: {why_not}.")
        if partial is None:
            return [], None
        keep = plan_resume(partial.captions, partial.carried)
        if not keep:
            return [], None
        self.on_progress(
            "Note: the join point of a resumed transcription can occasionally cut "
            "or repeat a word."
        )
        return keep, _language_of(partial.lang_code, partial.lang_name)

    # --- Phase 2a: transcribe-only ---

    def _save_transcriptions_only(self, per_file: list[tuple[str, list[Caption], Language]]) -> None:
        """Save each file's own-language captions without translating them."""
        total = len(per_file) + self._pre_done_units
        for index, (input_path, captions, source_lang) in enumerate(per_file):
            _check_cancel(self._cancel)
            output_path = self._resolve_output_path(input_path, source_lang.whisper_code)
            self._subtitle_handler.save(captions, output_path, self._output_format, on_progress=self.on_progress)
            self._store.mark_done(input_path, self._header(input_path, TRANSCRIPT_UNIT), output_path)
            self.on_percent((self._pre_done_units + index + 1) / total)

    # --- Phase 2b: translate into every target language ---

    def _translate_and_save_all(self, per_file: list[tuple[str, list[Caption], Language]]) -> None:
        """Translate every file into every target language and save the results, reporting overall progress."""
        # A unit is one file in one target language; the progress bar advances per unit and, within a unit, per line.
        self._units_total = (len(per_file) * len(self._target_langs) + self._pre_done_units) or 1
        self._units_done = self._pre_done_units
        # Start the bar where the work really is, not at zero, when resuming.
        self.on_percent(self._units_done / self._units_total)

        try:
            for input_path, captions, source_lang in per_file:
                for target_lang in self._target_langs:
                    _check_cancel(self._cancel)
                    self._translate_unit(input_path, captions, source_lang, target_lang)
                    self._finish_unit()
        finally:
            self._llm = None
            gc.collect()

    def _translate_unit(
        self, input_path: str, captions: list[Caption], source_lang: Language, target_lang: Language
    ) -> None:
        """Translate one file into one language and save it.

        The unit is skipped if the target is the source language or if it was finished earlier. Otherwise it is
        translated from a fresh copy of the captions, so the original captions stay intact for the next target
        language.
        """
        code = target_lang.whisper_code
        label = os.path.basename(input_path)
        if source_lang.whisper_code == code:
            self.on_progress(f"Skipping {target_lang.name} for {label}: same as source language.")
            return

        done_path = self._already_done(input_path, code)
        if done_path:
            self._names.reserve(input_path, done_path)
            self.on_progress(
                f"Already completed in an earlier run \u2014 skipping {target_lang.name} for {label}."
            )
            return

        llm = self._loaded_llm()
        captions_copy = [Caption(c.start, c.end, c.text) for c in captions]
        self._translate_captions(input_path, captions_copy, source_lang, target_lang, llm)

        output_path = self._resolve_output_path(input_path, code)
        self._subtitle_handler.save(captions_copy, output_path, self._output_format, on_progress=self.on_progress)
        self._store.mark_done(input_path, self._header(input_path, code), output_path)
        self._store.drop_journal(input_path, code)

    def _finish_unit(self) -> None:
        """Count one more finished unit and report the new overall progress."""
        self._units_done += 1
        self.on_percent(self._units_done / self._units_total)

    def _translate_captions(self, input_path, captions, source_lang, target_lang, llm) -> None:
        """Translate the captions in place, journaling every line so that a stopped job can continue from it."""
        total = len(captions)
        sampling = sampling_from_config(self._config)
        journal, note = self._store.open_journal(
            input_path, target_lang.whisper_code, self._header(input_path, target_lang.whisper_code),
            total=total, digest=captions_digest(captions), sampling=sampling,
        )
        try:
            if note:
                self.on_progress(f"Saved translation progress not reused: {note}.")
            start = self._restore_journaled_lines(journal, captions)
            self.on_progress(
                f"Translating {total} segments  ({source_lang.name} \u2192 {target_lang.name}) ..."
            )
            if start:
                self._announce_resume(journal, start, total, sampling)

            engine = TranslationEngine(
                source_lang, target_lang, self._config.llm_model_path,
                on_progress=self.on_progress, llm=llm, sampling=sampling,
            )
            self._translate_lines(engine, captions, start, journal)
        finally:
            journal.close()

    def _restore_journaled_lines(self, journal, captions: list[Caption]) -> int:
        """Write the lines translated before the job stopped back into the captions and return how many there were."""
        start = len(journal.texts)
        for i, text in enumerate(journal.texts):
            captions[i].text = text
        if start:
            # Show the real position straight away, before the first new line is done.
            self.on_percent((self._units_done + start / len(captions)) / self._units_total)
        return start

    def _announce_resume(self, journal, start: int, total: int, sampling: dict) -> None:
        """Report where a resumed translation continues, and warn if the sampling settings changed since the stop."""
        if start >= total:
            self.on_progress(f"  All {total} segments were already translated \u2014 saving the result.")
            return
        self.on_progress(f"  Resuming from segment {start + 1}/{total} \u2014 {start} already translated.")
        if journal.prior_header is not None and journal.prior_header.get("sampling") not in (None, sampling):
            self.on_progress(
                "  Note: the translation settings changed since this job was stopped \u2014 "
                "the remaining segments use the new settings."
            )

    def _translate_lines(self, engine, captions: list[Caption], start: int, journal) -> None:
        """Translate the captions from index ``start`` on, journaling each line as soon as it is produced."""
        total = len(captions)
        # The previous line is passed to the model as context, so that a resumed job picks up with the same context
        # an uninterrupted one would have.
        previous_translation: Optional[str] = captions[start - 1].text if start else None
        for index in range(start, total):
            # Checked per segment, so Stop takes effect within one subtitle line.
            # The line just translated is already journaled; nothing is lost.
            _check_cancel(self._cancel)
            captions[index].text = engine.translate(
                captions[index].text.strip(), previous_translation=previous_translation
            )
            previous_translation = captions[index].text
            journal.append(index, captions[index].text)
            self.on_progress(f"  Processed {index + 1}/{total}")
            self.on_percent((self._units_done + (index + 1) / total) / self._units_total)

    # --- Output path resolution ---

    def _resolve_output_path(self, input_path: str, language_code: str) -> str:
        """Return the output path for an input, made unique within the batch, and report a changed name."""
        wanted = resolve_batch_output_path(
            input_path, language_code, self._output_format,
            output_dir=self._output_dir, input_root=self._input_root,
        )
        path = self._names.claim(input_path, wanted)
        if path != wanted:
            self.on_progress(
                f"Note: {os.path.basename(wanted)} is already used by another file in this batch "
                f"\u2014 saving {os.path.basename(input_path)} as {os.path.basename(path)} instead."
            )
        return path


def batch_output_path(
    input_path: str,
    language_code: str,
    output_format: OutputFormat,
    output_dir: Optional[str] = None,
    input_root: Optional[str] = None,
) -> str:
    """Return the output path that ``BatchPipeline`` derives for an input from the naming rule alone.

    Exposed so that the GUI can predict output paths ahead of time (for example for an overwrite warning) without
    duplicating the logic. The result does not include the numeric suffix that the pipeline adds when two inputs of a
    batch would otherwise share an output name. Nothing is created on disk.

    Args:
        input_path: The video or subtitle file being processed.
        language_code: Language code of the output (the code of the target language, or of the source language
            when only transcribing).
        output_format: Subtitle format of the output.
        output_dir: Folder for the output, or None to place it next to the input.
        input_root: Folder whose structure is mirrored below ``output_dir``; None for a flat output folder.
    """
    if output_dir is None:
        return build_output_path(input_path, language_code, output_format)

    if input_root:
        rel = os.path.relpath(input_path, input_root)
    else:
        rel = os.path.basename(input_path)
    rel_base, _ = os.path.splitext(rel)
    return os.path.join(output_dir, f"{rel_base}_{language_code}{output_format.extension}")


def resolve_batch_output_path(
    input_path: str,
    language_code: str,
    output_format: OutputFormat,
    output_dir: Optional[str] = None,
    input_root: Optional[str] = None,
) -> str:
    """Return the output path of ``batch_output_path`` and create the folder that will hold the file.

    This is for a job that is about to write; a caller that only wants to know the path (the overwrite warning, which
    the user may still cancel) uses ``batch_output_path``, so that a cancelled job leaves no folder behind. A path
    next to its input needs no new folder, so only an output folder is created.

    Args:
        input_path: The video or subtitle file being processed.
        language_code: Language code of the output.
        output_format: Subtitle format of the output.
        output_dir: Folder for the output, or None to place it next to the input.
        input_root: Folder whose structure is mirrored below ``output_dir``; None for a flat output folder.
    """
    out_path = batch_output_path(input_path, language_code, output_format, output_dir, input_root)
    if output_dir is not None:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    return out_path
