<h1 align="center">KaramVT</h1>
<p align="center"><em>Karam Video Translator</em></p>

**Video tab:** give it a video (or a whole course folder) and get a
subtitle file back — in the video's own language, or translated into
one or several other languages at once. You can also feed it an existing
`.srt` / `.vtt` file and get a translated version.

**Text tab:** translate a block of text directly, or pull text out of an
image first (OCR) and translate that.

Everything runs locally: speech transcription via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper), translation
via a local GGUF chat model through
[llama-cpp-python](https://github.com/abetlen/llama-cpp-python), and OCR
via [Tesseract](https://github.com/tesseract-ocr/tesseract).

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).
