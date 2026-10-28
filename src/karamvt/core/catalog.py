# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The models KaramVT can download, and where each one comes from.

The list is fixed in code on purpose: the application only ever fetches these files,
from these repositories, so nothing a user types can point a download somewhere else.

* Speech recognition: Whisper ``large-v3`` and ``large-v3-turbo`` in the CTranslate2 format
  that faster-whisper loads. Both are multilingual; ``large-v3-turbo`` is the recommended one.
  The smaller sizes, the English-only ``.en`` and the distilled variants are left out.
* Translation: Gemma 4 ``E2B`` and ``E4B`` as GGUF files from Unsloth, in the dynamic
  ``Q4_K_XL``, ``Q6_K_XL`` and ``Q8_K_XL`` quantizations. The folder layout is the same as
  Istinbat's, so pointing both applications at one download folder shares the files.

Only the files needed to run a model are listed. Sizes are approximate and used only to
estimate progress and disk space before the real sizes are known.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

_MB = 1_000_000
_GB = 1_000 * _MB


class ModelKind(str, Enum):
    """The two kinds of model the app downloads: speech recognition and translation."""

    WHISPER = "whisper"
    LLM = "llm"

    @property
    def config_field(self) -> str:
        """The ``AppConfig`` field that holds the chosen model of this kind."""
        return {"whisper": "whisper_model_path", "llm": "llm_model_path"}[self.value]


@dataclass(frozen=True)
class RemoteFile:
    """One file of a model repository.

    Attributes:
        path: Relative path inside the repository, and inside the local model folder.
        approx_bytes: Approximate size, used for progress and disk-space estimates only.
    """

    path: str
    approx_bytes: int

    def __post_init__(self) -> None:
        # The path is joined onto local folders, so it must stay inside them.
        parts = PurePosixPath(self.path).parts
        if not parts or PurePosixPath(self.path).is_absolute() or ".." in parts:
            raise ValueError(f"unsafe file path: {self.path!r}")


@dataclass(frozen=True)
class ModelOption:
    """One downloadable choice: a model with its repository, local folder and files.

    Attributes:
        key: Stable identifier, for example "llm.e2b.q4_k_xl" or "whisper.large-v3".
        kind: Whether this is a speech-recognition or a translation model.
        repo: Hugging Face repository, as "owner/name".
        directory: Folder name inside the models folder.
        files: The files to download, in download order.
        entry: File inside ``directory`` to load; empty means the folder itself.
        label: Display name of a Whisper model, for example "Large v3 Turbo".
        recommended: Whether this is shown as the suggested choice of its kind.
        family: "E2B" or "E4B" for language models, otherwise None.
        quant: Quantization such as "Q4_K_XL" for language models, otherwise None.
        revision: Branch or revision of the repository to download from.
    """

    key: str
    kind: ModelKind
    repo: str
    directory: str
    files: tuple[RemoteFile, ...]
    entry: str = ""
    label: str = ""
    recommended: bool = False
    family: str | None = None
    quant: str | None = None
    revision: str = "main"

    @property
    def approx_bytes(self) -> int:
        """The approximate total download size in bytes."""
        return sum(file.approx_bytes for file in self.files)

    def local_path(self, models_dir: str | Path) -> Path:
        """Return the path the settings should point to once this option is downloaded."""
        folder = Path(models_dir) / self.directory
        return folder / self.entry if self.entry else folder

    def is_downloaded(self, models_dir: str | Path) -> bool:
        """Return True if every file of this option is present in *models_dir*.

        Files are renamed into place only when complete, so their presence means a finished download.
        """
        folder = Path(models_dir) / self.directory
        return all((folder / file.path).is_file() for file in self.files)


def _whisper(
    slug: str, label: str, repo: str, *, model_mb: int, config_bytes: int, tokenizer_bytes: int,
    recommended: bool = False,
) -> ModelOption:
    """Build a faster-whisper ``large-v3`` option.

    The files are the weights, the config, the 128-mel ``preprocessor_config.json``, the tokenizer and
    ``vocabulary.json``. The large weights come last, so a missing small file fails before gigabytes are fetched.
    """
    files = (
        RemoteFile("config.json", config_bytes),
        RemoteFile("preprocessor_config.json", 340),
        RemoteFile("tokenizer.json", tokenizer_bytes),
        RemoteFile("vocabulary.json", 1_070_000),
        RemoteFile("model.bin", model_mb * _MB),
    )
    return ModelOption(
        key=f"whisper.{slug}", kind=ModelKind.WHISPER, repo=repo, directory=repo.split("/")[1],
        files=files, label=label, recommended=recommended,
    )


WHISPERS = (
    _whisper("large-v3-turbo", "Large v3 Turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
             model_mb=1_620, config_bytes=2_260, tokenizer_bytes=2_710_000, recommended=True),
    _whisper("large-v3", "Large v3", "Systran/faster-whisper-large-v3",
             model_mb=3_090, config_bytes=2_390, tokenizer_bytes=2_480_000),
)

LLM_FAMILIES = ("E2B", "E4B")
LLM_QUANTS = ("Q4_K_XL", "Q6_K_XL", "Q8_K_XL")

# Approximate download sizes in bytes, from the repositories' file listings.
_LLM_SIZES = {
    ("E2B", "Q4_K_XL"): 3_180 * _MB,
    ("E2B", "Q6_K_XL"): 4_710 * _MB,
    ("E2B", "Q8_K_XL"): 5_280 * _MB,
    ("E4B", "Q4_K_XL"): 5_130 * _MB,
    ("E4B", "Q6_K_XL"): 7_460 * _MB,
    ("E4B", "Q8_K_XL"): 8_710 * _MB,
}


def _llm_option(family: str, quant: str) -> ModelOption:
    """Build the Unsloth GGUF option of one Gemma 4 family and quantization."""
    name = f"gemma-4-{family}-it"
    filename = f"{name}-UD-{quant}.gguf"
    return ModelOption(
        key=f"llm.{family.lower()}.{quant.lower()}",
        kind=ModelKind.LLM,
        repo=f"unsloth/{name}-GGUF",
        directory=name,
        files=(RemoteFile(filename, _LLM_SIZES[(family, quant)]),),
        entry=filename,
        family=family,
        quant=quant,
    )


LLMS = tuple(_llm_option(family, quant) for family in LLM_FAMILIES for quant in LLM_QUANTS)


def all_options() -> tuple[ModelOption, ...]:
    """Return every downloadable option: the Whisper models first, then the language models."""
    return (*WHISPERS, *LLMS)


def option_by_key(key: str) -> ModelOption:
    """Return the option with the given key.

    Raises:
        KeyError: No option has this key.
    """
    for option in all_options():
        if option.key == key:
            return option
    raise KeyError(key)


def llms_of(family: str) -> tuple[ModelOption, ...]:
    """Return the language-model options of one family ("E2B" or "E4B"), one per quantization."""
    return tuple(option for option in LLMS if option.family == family)
