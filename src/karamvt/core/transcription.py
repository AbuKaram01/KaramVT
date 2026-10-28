# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Speech recognition with a local Faster-Whisper model.

Provides model loading, spoken-language detection and transcription into timed captions. The model is loaded
separately from the engine so that a batch of videos shares one copy, and a transcription interrupted earlier can be
continued from the audio position reached, instead of starting over.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from faster_whisper import WhisperModel

from .cancel import CancelToken, check as _check_cancel
from .languages import Language, language_for_code
from .models import Caption

# Voice-activity-detection settings: a pause of at least 500 ms ends a speech chunk.
_VAD_PARAMETERS = {"min_silence_duration_ms": 500}

# Whisper models work at 16 kHz, and faster-whisper decodes audio to this rate.
_SAMPLE_RATE = 16000


def _decode_audio(path: str):
    # Imported lazily: only resuming needs the decoder.
    from faster_whisper.audio import decode_audio
    return decode_audio(path, sampling_rate=_SAMPLE_RATE)


def plan_resume(saved: list[Caption], carried: int = 0) -> list[Caption]:
    """Select the saved segments to keep when continuing an interrupted transcription.

    The last saved segment is deliberately not kept: it is the one that was being finished when the job stopped, so
    it is the likeliest to be cut off or wrong. It is transcribed again, along with everything after it. If that
    leaves nothing, there is nothing worth resuming from.

    Args:
        saved: Segments saved by the interrupted run, in order.
        carried: How many of ``saved`` an earlier resume had already carried over. If nothing new was produced
            since, every saved segment has already been through this once and is kept as is, so that stopping
            again right after resuming does not shrink the progress by one segment each time.

    Returns:
        The segments to keep; the new run starts after the end of the last one.
    """
    if carried and len(saved) == carried:
        return list(saved)
    return list(saved[:-1])


def load_whisper_model(
    model_path: str,
    device: str = "cpu",
    compute_type: str = "int8",
    cpu_threads: int = 4,
    on_progress: Callable[[str], None] = print,
) -> WhisperModel:
    """Load a Faster-Whisper model from a local folder.

    Load once, for example before a batch of several videos, and pass the result into every TranscriptionEngine
    constructed afterwards (as ``model=``), instead of letting each one load its own copy.

    Args:
        model_path: Folder that holds the converted Whisper model.
        device: Processing device, for example ``"cpu"`` or ``"cuda"``.
        compute_type: CTranslate2 quantisation of the weights, for example ``"int8"``.
        cpu_threads: Number of CPU threads; values below 1 are raised to 1.
        on_progress: Receives a status message before the (slow) load starts.
    """
    on_progress(f"Loading Faster-Whisper ({device})...")
    return WhisperModel(
        model_path,
        device=device,
        compute_type=compute_type,
        cpu_threads=max(1, cpu_threads),
    )


class TranscriptionEngine:
    """Wraps a local Faster-Whisper model for language detection and transcription.

    The constructor arguments ``device``, ``compute_type`` and ``cpu_threads`` are only used when the engine loads
    the model itself.

    Args:
        whisper_model_path: Folder that holds the converted Whisper model.
        device: Processing device, for example ``"cpu"`` or ``"cuda"``.
        compute_type: CTranslate2 quantisation of the weights, for example ``"int8"``.
        cpu_threads: Number of CPU threads for loading.
        on_progress: Receives human-readable status messages.
        model: An already-loaded model (see load_whisper_model()); when given, loading is skipped entirely.
        audio_loader: Returns the decoded 16 kHz audio samples for a file path. Only called when resuming; defaults
            to the faster-whisper decoder.
    """

    def __init__(
        self,
        whisper_model_path: str,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 4,
        on_progress: Callable[[str], None] = print,
        model: Optional[WhisperModel] = None,
        audio_loader: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.on_progress = on_progress
        self._audio_loader = audio_loader if audio_loader is not None else _decode_audio
        self._model = model if model is not None else load_whisper_model(
            whisper_model_path, device=device, compute_type=compute_type,
            cpu_threads=cpu_threads, on_progress=on_progress,
        )

    def detect_language(self, audio_path: str) -> Language:
        """Detect the language spoken in an audio file and report it, with the confidence, through on_progress."""
        self.on_progress("   Auto-detecting language...")
        # The returned segment generator is lazy and is never consumed, so only the model's language-detection step
        # runs, not a full transcription.
        _, info = self._model.transcribe(
            audio_path,
            language=None,
            beam_size=1,
            without_timestamps=True,
        )
        code = info.language
        confidence = round(info.language_probability * 100, 1)
        lang = language_for_code(code)
        self.on_progress(f"   Detected: {lang.name} ({code}) — {confidence}% confidence")
        if not lang.speech:
            self.on_progress(f"   Note: speech recognition is less reliable for {lang.name}; expect more errors.")
        return lang

    def transcribe(
        self,
        audio_path: str,
        source_lang: Language,
        cancel: Optional[CancelToken] = None,
        on_segment: Optional[Callable[[Caption], None]] = None,
        keep: Optional[list[Caption]] = None,
    ) -> list[Caption]:
        """Transcribe an audio file into timed captions.

        Args:
            audio_path: Media or audio file to transcribe.
            source_lang: Language spoken in the audio; its Whisper code is passed to the model.
            cancel: Checked between segments, so that Stop takes effect while the model is working.
            on_segment: Called with every segment as soon as it is decoded (absolute timestamps), so that the
                caller can save progress.
            keep: Segments already transcribed by an earlier, interrupted run (see plan_resume). The audio before
                the end of the last one is cut away and Whisper continues from there, with exactly the same
                settings as a normal run. Timestamps of the new segments are shifted by the cut so that they line
                up with the original video.

        Returns:
            All captions of the file: the ``keep`` segments followed by the newly transcribed ones.

        Raises:
            OperationCancelled: ``cancel`` was set while transcribing.
        """
        offset = 0.0
        audio: Any = audio_path
        kept: list[Caption] = list(keep) if keep else []
        if kept:
            offset = kept[-1].end
            self.on_progress(
                f"Continuing transcription from {_format_time(offset)} "
                f"({len(kept)} saved segments kept) ..."
            )
            # Drop the samples before the resume point; the decoded audio is indexed in samples at _SAMPLE_RATE.
            audio = self._audio_loader(audio_path)[int(offset * _SAMPLE_RATE):]
        self.on_progress(f"Transcribing  (source language: {source_lang.name}) ...")
        raw_segments, _ = self._model.transcribe(
            audio,
            language=source_lang.whisper_code,
            beam_size=5,
            vad_filter=True,
            vad_parameters=_VAD_PARAMETERS,
            repetition_penalty=1.1,
            condition_on_previous_text=True,
        )
        # raw_segments is a lazy generator: the model does its work while the generator is iterated, so checking for
        # cancellation between segments is what keeps Stop responsive here.
        captions: list[Caption] = list(kept)
        for segment in raw_segments:
            _check_cancel(cancel)
            caption = Caption(start=segment.start + offset, end=segment.end + offset, text=segment.text)
            captions.append(caption)
            if on_segment is not None:
                on_segment(caption)
        return captions


def _format_time(seconds: float) -> str:
    """Format a duration in seconds as ``H:MM:SS`` for progress messages."""
    total = int(seconds)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
