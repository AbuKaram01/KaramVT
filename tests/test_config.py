# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The saved settings (config.py): the models folder, which models count as ready, and updating one setting."""

from pathlib import Path

import pytest

from karamvt.config import AppConfig, is_model_problem, update_saved_config
from karamvt.core.paths import default_models_dir


def test_the_models_folder_defaults_to_the_data_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert AppConfig().models_folder() == tmp_path / "karamvt" / "models" == default_models_dir()


def test_a_chosen_models_folder_wins_and_a_blank_one_means_the_default():
    assert AppConfig(models_dir="/data/models").models_folder() == Path("/data/models")
    assert AppConfig(models_dir="   ").models_folder() == default_models_dir()


def test_the_models_folder_survives_a_save_and_a_load(config_file):
    AppConfig(models_dir="/data/models").save()
    assert AppConfig.load().models_dir == "/data/models"


def test_a_config_file_from_before_downloads_existed_still_loads(config_file):
    config_file.parent.mkdir(parents=True)
    config_file.write_text('{"whisper_model_path": "/w", "llm_model_path": "/m.gguf", "cpu_threads": 8}')
    loaded = AppConfig.load()
    assert (loaded.whisper_model_path, loaded.llm_model_path, loaded.cpu_threads) == ("/w", "/m.gguf", 8)
    assert loaded.models_dir == ""


def test_every_model_problem_message_is_recognised_so_the_window_can_offer_the_download(tmp_path):
    for config in (
        AppConfig(),
        AppConfig(whisper_model_path=str(tmp_path / "nope"), llm_model_path=str(tmp_path / "nope.gguf")),
    ):
        errors = config.validation_errors()
        assert len(errors) == 2 and all(is_model_problem(error) for error in errors)


def test_other_messages_are_not_model_problems():
    assert not is_model_problem("Please choose a file first.")
    assert not is_model_problem("Type or paste some text first.")


def test_the_welcome_guide_flag_starts_off_and_is_saved(config_file):
    assert AppConfig().welcome_done is False
    AppConfig(welcome_done=True).save()
    assert AppConfig.load().welcome_done is True


def test_updating_a_setting_keeps_what_another_window_saved_meanwhile(config_file):
    AppConfig(cpu_threads=8).save()
    stale = AppConfig.load()
    AppConfig(cpu_threads=8, notify_when_done=False).save()
    update_saved_config(history_limit=7, ocr_psm=3)
    saved = AppConfig.load()
    assert (saved.history_limit, saved.ocr_psm) == (7, 3)
    assert saved.notify_when_done is False and stale.notify_when_done is True


def test_updating_a_name_that_is_not_a_setting_is_refused_and_saves_nothing(config_file):
    AppConfig(cpu_threads=8).save()
    with pytest.raises(AttributeError, match="not_a_setting"):
        update_saved_config(cpu_threads=2, not_a_setting=1)
    assert AppConfig.load().cpu_threads == 8


def test_a_model_is_ready_only_when_its_path_exists_on_disk(tmp_path):
    folder, gguf = tmp_path / "faster-whisper-x", tmp_path / "model.gguf"
    assert not AppConfig().whisper_ready and not AppConfig().llm_ready
    assert not AppConfig(whisper_model_path=str(folder), llm_model_path=str(gguf)).whisper_ready
    folder.mkdir()
    gguf.write_bytes(b"x")
    ready = AppConfig(whisper_model_path=str(folder), llm_model_path=str(gguf))
    assert ready.whisper_ready and ready.llm_ready
    assert not AppConfig(whisper_model_path=str(gguf)).whisper_ready      # A file is not a model folder.
    assert not AppConfig(llm_model_path=str(folder)).llm_ready            # A folder is not a .gguf file.


def test_both_models_are_needed_for_the_models_to_be_ready(tmp_path):
    folder, gguf = tmp_path / "faster-whisper-x", tmp_path / "model.gguf"
    folder.mkdir()
    gguf.write_bytes(b"x")
    assert not AppConfig().models_ready
    assert not AppConfig(whisper_model_path=str(folder)).models_ready
    assert not AppConfig(llm_model_path=str(gguf)).models_ready
    assert AppConfig(whisper_model_path=str(folder), llm_model_path=str(gguf)).models_ready
