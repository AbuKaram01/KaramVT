# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The saved interface state (core/ui_state.py): what is remembered, and what happens to a damaged file."""

import json
import os
import stat
import sys

import pytest

from karamvt.core import paths
from karamvt.core.ui_state import (
    MAX_TEXT_CHARS, MAX_WINDOW_SIZE, MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH, STATE_VERSION, StateStore,
    TextState, UiState, VideoState, WindowState,
)


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state" / "state.json")


def a_busy_state() -> UiState:
    return UiState(
        window=WindowState(width=1100, height=640, maximized=True),
        tab="text",
        video=VideoState(
            batch=True, input_path="/home/me/courses/python", mode="translate", source_language="English",
            target_languages=["Arabic", "French"], output_format="vtt", output_dir="/home/me/subs",
        ),
        text=TextState(
            source_language="Arabic", target_language="English", input_text="مرحبا بالعالم", output_text="Hello world",
            image_dir="/home/me/Pictures",
        ),
    )


# --- Where it lives ---

def test_the_file_lives_in_the_xdg_state_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert paths.state_dir() == tmp_path / "xdg" / "karamvt"
    assert StateStore().path == tmp_path / "xdg" / "karamvt" / "state.json"


def test_without_xdg_state_home_it_is_under_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(paths, "is_windows", lambda: False)  # The Linux rule, also when this runs on Windows.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert paths.state_dir() == tmp_path / ".local" / "state" / "karamvt"


# --- The round trip ---

def test_a_first_start_has_no_file_and_gets_the_defaults(store):
    assert not store.path.exists()
    assert store.load() == UiState()


def test_a_state_that_is_just_the_defaults_is_not_written_for_a_first_start(store):
    store.load()
    assert store.save(UiState()) is True
    assert not store.path.exists()
    assert store.save(UiState(tab="text")) is True
    assert store.path.exists()


def test_everything_survives_a_save_and_a_load(store):
    assert store.save(a_busy_state())
    assert StateStore(store.path).load() == a_busy_state()


def test_arabic_text_is_written_as_it_is_and_read_back(store):
    store.save(a_busy_state())
    assert "مرحبا بالعالم" in store.path.read_text(encoding="utf-8")


def test_the_file_records_its_version(store):
    store.save(UiState())
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == STATE_VERSION


POSIX_ONLY_PERMISSIONS = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no permission bits; the profile folder's own rights keep it private",
)


@POSIX_ONLY_PERMISSIONS
def test_saving_creates_the_folder_and_keeps_the_file_private(store):
    store.save(a_busy_state())
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700


def test_saving_leaves_no_temporary_file_behind(store):
    store.save(a_busy_state())
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["state.json"]


def test_saving_the_same_state_again_writes_nothing(store):
    store.save(a_busy_state())
    first = os.stat(store.path).st_mtime_ns
    assert store.save(a_busy_state())
    assert os.stat(store.path).st_mtime_ns == first


def test_a_state_just_loaded_is_not_written_back_unchanged(store):
    store.save(a_busy_state())
    reader = StateStore(store.path)
    state = reader.load()
    marker = 1_000_000_000  # One second after 1970: a multiple of the 100 ns Windows keeps times in.
    os.utime(store.path, ns=(marker, marker))
    reader.save(state)
    assert os.stat(store.path).st_mtime_ns == marker


# --- A damaged or edited file ---

@pytest.mark.parametrize("content", ["", "not json at all", "[1, 2, 3]", '"just a string"', "42", "{"])
def test_a_file_that_is_not_a_json_object_gives_the_defaults_and_is_set_aside(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")
    assert store.load() == UiState()
    assert not store.path.exists()
    assert store.path.with_name("state.json.broken").read_text(encoding="utf-8") == content


def test_after_a_broken_file_the_next_save_starts_clean(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{", encoding="utf-8")
    store.load()
    assert store.save(a_busy_state())
    assert StateStore(store.path).load() == a_busy_state()


def test_a_file_that_cannot_be_decoded_gives_the_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert store.load() == UiState()


def test_one_wrong_value_costs_that_setting_only(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({
        "window": {"width": "wide", "height": 500, "maximized": 1},
        "tab": 7,
        "video": {"batch": "yes", "mode": "translate", "target_languages": ["Arabic", 3], "output_dir": "/x"},
        "text": {"source_language": "English", "input_text": ["not", "text"]},
    }), encoding="utf-8")
    state = store.load()
    assert state.window == WindowState(height=500)  # The wrong width and flag fall back alone.
    assert state.tab == "video"
    assert state.video.batch is False and state.video.mode == "translate" and state.video.output_dir == "/x"
    assert state.video.target_languages == []
    assert state.text.source_language == "English" and state.text.input_text == ""


def test_sections_of_the_wrong_kind_give_their_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"window": [1], "video": "x", "text": None, "tab": "text"}), encoding="utf-8")
    state = store.load()
    assert (state.window, state.video, state.text) == (WindowState(), VideoState(), TextState())
    assert state.tab == "text"


def test_unknown_keys_and_a_newer_version_are_ignored_not_fatal(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({
        "version": STATE_VERSION + 5, "tab": "text", "dashboard": {"x": 1}, "window": {"width": 900, "dpi": 2},
    }), encoding="utf-8")
    state = store.load()
    assert state.tab == "text" and state.window.width == 900


def test_a_file_from_before_a_setting_existed_still_loads(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"tab": "text", "video": {"batch": True}}), encoding="utf-8")
    state = store.load()
    assert state.tab == "text" and state.video.batch is True and state.video.output_format == "srt"


# --- Values brought inside what the interface can use ---

@pytest.mark.parametrize("width, height, expected", [
    (10, 10, (MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)),
    (-5, 0, (MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)),
    (10**9, 10**9, (MAX_WINDOW_SIZE, MAX_WINDOW_SIZE)),
    (900, 700, (900, 700)),
])
def test_a_window_size_outside_sensible_bounds_is_pulled_inside(store, width, height, expected):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"window": {"width": width, "height": height}}), encoding="utf-8")
    window = store.load().window
    assert (window.width, window.height) == expected


def test_names_that_are_not_choices_fall_back_to_the_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({
        "tab": "settings", "video": {"mode": "dub", "output_format": "ass"},
    }), encoding="utf-8")
    state = store.load()
    assert state.tab == "video" and state.video.mode == "transcribe" and state.video.output_format == "srt"


def test_text_and_lists_are_kept_to_a_sane_length(store):
    state = UiState(
        text=TextState(input_text="x" * (MAX_TEXT_CHARS + 500), output_text="y" * (MAX_TEXT_CHARS * 3)),
        video=VideoState(target_languages=["Arabic", "French", "Arabic"]),
    )
    store.save(state)
    loaded = StateStore(store.path).load()
    assert len(loaded.text.input_text) == MAX_TEXT_CHARS and len(loaded.text.output_text) == MAX_TEXT_CHARS
    assert loaded.video.target_languages == ["Arabic", "French"]  # No duplicates.


# --- Saving never raises ---

def test_a_folder_that_cannot_be_written_is_reported_as_false_not_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    store = StateStore(blocker / "state.json")
    assert store.save(a_busy_state()) is False


def test_a_failed_replace_leaves_the_old_file_and_no_temporary_one(store, monkeypatch):
    store.save(UiState(tab="video"))

    def refuse(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", refuse)
    assert store.save(UiState(tab="text")) is False
    assert StateStore(store.path).load().tab == "video"
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["state.json"]


def test_text_that_cannot_be_encoded_is_a_failed_save_not_a_crash(store):
    assert store.save(UiState(text=TextState(input_text="broken \ud800 surrogate"))) is False
    assert not store.path.exists()


def test_an_unreadable_file_gives_the_defaults(store):
    store.path.parent.mkdir(parents=True)
    store.path.mkdir()  # A folder where the file should be.
    assert store.load() == UiState()
