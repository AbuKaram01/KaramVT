# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The "Download models" menu in Preferences, driven through the real interface with a fake downloader."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import pytest
from gui_support import (
    Adw, AppConfig, Gtk, LLMS, NotEnoughSpaceError, PreferencesWindow, RowState, WHISPERS, capture_toasts,
    downloader, env, open_preferences, option_by_key, pump, rows_of, size_label, use_model, walk,
)

# --- The menu ---

def test_preferences_lists_every_model_and_starts_with_nothing_downloaded(env):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    assert len(rows) == len(WHISPERS) + len(LLMS) == 8
    assert {row.state for row in rows.values()} == {RowState.DOWNLOAD}


def test_only_large_v3_turbo_is_marked_recommended(env):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    badges = {key: any(isinstance(w, Gtk.Label) and w.get_label() == "Recommended" for w in walk(row))
              for key, row in rows.items()}
    assert [key for key, shown in badges.items() if shown] == ["whisper.large-v3-turbo"]
    assert "Nearly as accurate" in rows["whisper.large-v3-turbo"].get_subtitle()


def test_the_download_folder_row_shows_the_default_folder(env, tmp_path):
    prefs = open_preferences(*env)
    assert prefs._folder_row.get_text() == str(tmp_path / "data" / "karamvt" / "models")


def test_the_download_menu_can_be_opened_from_the_app_action(env):
    app, _window = env
    app.activate_action("download-models", None)
    pump(0.2)
    assert app.preferences_window._downloads_group.expander.get_expanded()
    app.activate_action("preferences", None)  # A second request shows the same window.
    first = app.preferences_window
    app.activate_action("download-models", None)
    assert app.preferences_window is first


def test_asking_to_download_shows_a_confirmation_and_only_confirming_starts_it(env, downloader):
    app, window = env
    prefs = open_preferences(app, window)
    row = rows_of(prefs)["whisper.large-v3-turbo"]
    row._download.emit("clicked")
    pump(0.1)

    dialog = prefs.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog) and "Whisper Large v3 Turbo" in dialog.get_heading()
    assert "mobiuslabsgmbh/faster-whisper-large-v3-turbo" in dialog.get_body() and downloader.calls == []

    dialog.emit("response", "cancel")
    pump(0.1)
    assert downloader.calls == [] and row.state is RowState.DOWNLOAD

    row._download.emit("clicked")
    pump(0.1)
    prefs.get_visible_dialog().emit("response", "confirm")
    assert pump(1.0, until=lambda: row.state is RowState.DOWNLOADING)
    assert [key for key, _folder in downloader.calls] == ["whisper.large-v3-turbo"]


def test_while_one_model_downloads_the_others_cannot_start_and_progress_is_shown(env, downloader):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    prefs._downloads_group._start(option_by_key("llm.e2b.q4_k_xl"))
    assert pump(1.0, until=lambda: "of" in rows["llm.e2b.q4_k_xl"].get_subtitle())

    busy = rows["llm.e2b.q4_k_xl"]
    assert busy.state is RowState.DOWNLOADING and busy._bar.get_fraction() == pytest.approx(0.25, abs=0.01)
    assert busy.get_subtitle() == f"{size_label(LLMS[0].approx_bytes // 4)} of {size_label(LLMS[0].approx_bytes)}"
    assert not rows["whisper.large-v3"]._download.get_sensitive()
    assert not rows["llm.e4b.q8_k_xl"]._download.get_sensitive()


def test_a_finished_download_is_selected_saved_and_shown_in_the_path_field(env, downloader, tmp_path):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    prefs._downloads_group._start(option_by_key("whisper.large-v3"))
    pump(0.2)
    downloader.release.set()
    assert pump(2.0, until=lambda: rows["whisper.large-v3"].state is RowState.IN_USE)

    expected = str(tmp_path / "data" / "karamvt" / "models" / "faster-whisper-large-v3")
    assert AppConfig.load().whisper_model_path == expected
    assert prefs._whisper_row.get_text() == expected
    assert rows["whisper.large-v3-turbo"].state is RowState.DOWNLOAD
    assert rows["whisper.large-v3-turbo"]._download.get_sensitive()  # Free for the next download.


def test_stopping_a_download_keeps_nothing_selected_and_lets_it_start_again(env, downloader):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    prefs._downloads_group._start(option_by_key("llm.e4b.q4_k_xl"))
    assert pump(1.0, until=lambda: rows["llm.e4b.q4_k_xl"].state is RowState.DOWNLOADING)

    rows["llm.e4b.q4_k_xl"]._stack.get_child_by_name("downloading").get_last_child().emit("clicked")  # Press Stop.
    assert pump(2.0, until=lambda: rows["llm.e4b.q4_k_xl"].state is RowState.DOWNLOAD)
    assert AppConfig.load().llm_model_path == "" and rows["llm.e4b.q4_k_xl"]._download.get_sensitive()


def test_a_failed_download_returns_the_row_to_download(env, downloader):
    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    downloader.failure = NotEnoughSpaceError(5_000_000_000, 1_000_000_000)
    downloader.release.set()
    prefs._downloads_group._start(option_by_key("whisper.large-v3"))
    assert pump(2.0, until=lambda: not env[0].downloads.active)
    assert rows["whisper.large-v3"].state is RowState.DOWNLOAD and AppConfig.load().whisper_model_path == ""


def test_closing_and_reopening_preferences_shows_the_download_that_is_still_running(env, downloader):
    app, window = env
    prefs = open_preferences(app, window)
    prefs._downloads_group._start(option_by_key("llm.e2b.q8_k_xl"))
    assert pump(1.0, until=lambda: rows_of(prefs)["llm.e2b.q8_k_xl"].state is RowState.DOWNLOADING)

    prefs.close()
    pump(0.2)
    assert app.downloads.active  # The download did not stop with the window.

    reopened = open_preferences(app, window)
    assert rows_of(reopened)["llm.e2b.q8_k_xl"].state is RowState.DOWNLOADING


def test_a_model_on_disk_but_not_selected_offers_use_and_use_selects_it(env, tmp_path):
    models = tmp_path / "data" / "karamvt" / "models"
    for option in (option_by_key("llm.e2b.q4_k_xl"), option_by_key("llm.e2b.q6_k_xl")):
        target = option.local_path(models)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    use_model(option_by_key("llm.e2b.q4_k_xl"))

    prefs = open_preferences(*env)
    rows = rows_of(prefs)
    assert rows["llm.e2b.q4_k_xl"].state is RowState.IN_USE and rows["llm.e2b.q6_k_xl"].state is RowState.READY

    prefs._downloads_group._use(option_by_key("llm.e2b.q6_k_xl"))
    assert rows["llm.e2b.q6_k_xl"].state is RowState.IN_USE and rows["llm.e2b.q4_k_xl"].state is RowState.READY
    assert AppConfig.load().llm_model_path == str(option_by_key("llm.e2b.q6_k_xl").local_path(models))
    assert prefs._llm_row.get_text() == AppConfig.load().llm_model_path


def test_refreshing_the_path_fields_never_writes_an_old_value_over_the_new_one(env, tmp_path):
    prefs = open_preferences(*env)
    use_model(option_by_key("whisper.large-v3-turbo"))
    use_model(option_by_key("llm.e2b.q4_k_xl"))  # Both changed behind the window's back.
    prefs.refresh_model_paths()
    saved = AppConfig.load()
    assert saved.whisper_model_path.endswith("faster-whisper-large-v3-turbo") and saved.llm_model_path.endswith(".gguf")
    assert (
        prefs._whisper_row.get_text() == saved.whisper_model_path
        and prefs._llm_row.get_text() == saved.llm_model_path
    )


# --- The download folder ---

def test_a_new_download_folder_is_saved_applies_to_the_next_download_and_refreshes_the_rows(env, downloader, tmp_path):
    prefs = open_preferences(*env)
    elsewhere = tmp_path / "big-disk"
    prefs._folder_row.set_text(str(elsewhere))
    prefs._folder_row.emit("apply")
    assert AppConfig.load().models_dir == str(elsewhere)

    prefs._downloads_group._start(option_by_key("whisper.large-v3-turbo"))
    downloader.release.set()
    assert pump(2.0, until=lambda: rows_of(prefs)["whisper.large-v3-turbo"].state is RowState.IN_USE)
    assert downloader.calls == [("whisper.large-v3-turbo", str(elsewhere))]
    assert (elsewhere / "faster-whisper-large-v3-turbo" / "model.bin").is_file()


def test_typing_the_default_folder_back_saves_it_as_the_default(env, tmp_path):
    prefs = open_preferences(*env)
    prefs._folder_row.set_text(str(tmp_path / "somewhere"))
    prefs._folder_row.emit("apply")
    prefs._folder_row.set_text(str(tmp_path / "data" / "karamvt" / "models"))
    prefs._folder_row.emit("apply")
    assert AppConfig.load().models_dir == ""


def test_models_already_in_the_chosen_folder_show_as_downloaded(env, tmp_path):
    shared = tmp_path / "istinbat-models"
    option = option_by_key("llm.e4b.q4_k_xl")
    option.local_path(shared).parent.mkdir(parents=True)
    option.local_path(shared).write_bytes(b"x")  # As if Istinbat had downloaded it.

    prefs = open_preferences(*env)
    prefs._folder_row.set_text(str(shared))
    prefs._folder_row.emit("apply")
    assert rows_of(prefs)["llm.e4b.q4_k_xl"].state is RowState.READY


# --- The main window ---

def test_a_missing_model_message_carries_a_download_models_button_and_other_messages_do_not(env):
    _app, window = env
    toasts = capture_toasts(window._toast_overlay)
    for message in AppConfig().validation_errors():
        window._on_toast_requested(None, message)
    window._on_toast_requested(None, "Type or paste some text first.")

    *model_toasts, other = toasts
    assert len(model_toasts) == 2
    assert all(t.get_button_label() == "Download Models" and t.get_action_name() == "app.download-models"
               for t in model_toasts)
    assert other.get_button_label() is None and other.get_action_name() is None


def test_how_a_download_ended_is_shown_in_the_main_window_when_preferences_is_closed(env, downloader):
    app, window = env
    toasts = capture_toasts(window._toast_overlay)
    downloader.release.set()
    app.downloads.start(option_by_key("whisper.large-v3-turbo"))
    assert pump(2.0, until=lambda: any("downloaded and selected" in t.get_title() for t in toasts))
    assert [t.get_title() for t in toasts] == ["Whisper Large v3 Turbo downloaded and selected."]


def test_how_a_download_ended_is_shown_in_preferences_when_it_is_open(env, downloader):
    app, window = env
    prefs = open_preferences(app, window)
    in_prefs, in_main = capture_toasts(prefs), capture_toasts(window._toast_overlay)
    downloader.failure = NotEnoughSpaceError(5_000_000_000, 1_000_000_000)
    downloader.release.set()
    app.downloads.start(option_by_key("llm.e4b.q8_k_xl"))
    assert pump(2.0, until=lambda: bool(in_prefs))
    assert in_prefs[0].get_title() == "Not enough free disk space: 5.0 GB needed, 1.0 GB available."
    assert in_main == []


def test_a_stopped_download_says_so(env, downloader):
    app, window = env
    toasts = capture_toasts(window._toast_overlay)
    app.downloads.start(option_by_key("whisper.large-v3"))
    pump(0.2)
    app.downloads.cancel()
    assert pump(2.0, until=lambda: bool(toasts))
    assert toasts[0].get_title() == "Download stopped. Press Download to continue later."


def test_the_main_menu_offers_download_models(env):
    _app, window = env
    menu_button = [w for w in walk(window) if isinstance(w, Gtk.MenuButton)][-1]
    model = menu_button.get_menu_model()
    entries = [(model.get_item_attribute_value(i, "label", None).get_string(),
                model.get_item_attribute_value(i, "action", None).get_string()) for i in range(model.get_n_items())]
    assert ("Download Models…", "app.download-models") in entries
