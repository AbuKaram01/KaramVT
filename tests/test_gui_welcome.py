# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The first-run welcome guide, driven through the real interface (see gui_support.py)."""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import pytest
from gui_support import (
    Adw, AppConfig, Gdk, Gtk, capture_toasts, downloader, env, option_by_key, pump, use_model, walk,
)

from karamvt.gui.application import APP_ID
from karamvt.gui.model_downloads import RowState
from karamvt.gui.welcome import WelcomeDialog, mark_welcome_done, should_show_welcome


def install(key: str) -> None:
    """Put a model on disk and select it, as a finished download would."""
    option = option_by_key(key)
    folder = AppConfig.load().models_folder()
    for file in option.files:
        target = folder / option.directory / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    use_model(option)


def open_welcome(app) -> WelcomeDialog:
    app.activate_action("welcome", None)
    pump(0.2)
    assert isinstance(app.welcome_dialog, WelcomeDialog)
    return app.welcome_dialog


def labels_in(widget) -> list[str]:
    return [w.get_label() for w in walk(widget) if isinstance(w, Gtk.Label)]


# --- When it opens ---

def test_a_fresh_installation_shows_the_guide_once(env):
    assert should_show_welcome()
    mark_welcome_done()
    assert not should_show_welcome() and AppConfig.load().welcome_done


def test_an_existing_installation_with_both_models_never_sees_it_and_is_remembered_as_done(env):
    install("whisper.large-v3-turbo")
    install("llm.e2b.q4_k_xl")
    assert not should_show_welcome()
    assert AppConfig.load().welcome_done  # So deleting a model later does not bring it up.


@pytest.mark.parametrize("keys", [["whisper.large-v3-turbo"], ["llm.e4b.q6_k_xl"]])
def test_having_only_one_of_the_two_models_still_shows_it(env, keys):
    for key in keys:
        install(key)
    assert should_show_welcome() and not AppConfig.load().welcome_done


def test_models_that_were_moved_away_count_as_missing(env, tmp_path):
    AppConfig(whisper_model_path=str(tmp_path / "gone"), llm_model_path=str(tmp_path / "gone.gguf")).save()
    assert should_show_welcome()


def test_the_application_opens_the_guide_after_start_up_on_a_first_run_only(env):
    app, _window = env
    app._show_welcome_if_new()
    assert pump(1.0, until=lambda: app.welcome_dialog is not None)

    app.welcome_dialog.close()
    pump(0.3)
    app._show_welcome_if_new()  # Seen once: never again.
    pump(0.3)
    assert app.welcome_dialog is None


def test_the_main_menu_can_open_it_again_and_only_one_copy_opens(env):
    app, window = env
    menu_button = [w for w in walk(window) if isinstance(w, Gtk.MenuButton)][-1]
    model = menu_button.get_menu_model()
    entries = {model.get_item_attribute_value(i, "label", None).get_string():
               model.get_item_attribute_value(i, "action", None).get_string() for i in range(model.get_n_items())}
    assert entries["Welcome Guide"] == "app.welcome"

    first = open_welcome(app)
    app.activate_action("welcome", None)
    pump(0.1)
    assert app.welcome_dialog is first


# --- Moving through the pages ---

def test_the_guide_starts_on_the_welcome_page_with_only_a_next_button(env):
    dialog = open_welcome(env[0])
    assert not dialog._back.get_visible() and dialog._next.get_label() == "Next"
    page = labels_in(dialog._pages[0])
    assert "Welcome to KaramVT" in page and "Videos and subtitles" in page and "Private by design" in page
    assert dialog._carousel.get_n_pages() == 3


def test_every_page_is_as_wide_as_the_dialog(env):
    """A carousel gives a page only its natural width unless it expands: pages once came out half as wide
    as the dialog, with the next page showing at the side."""
    dialog = open_welcome(env[0])
    pump(0.3)
    assert dialog._carousel.get_width() > 400
    assert [page.get_width() for page in dialog._pages] == [dialog._carousel.get_width()] * 3


def test_next_goes_to_the_models_page_and_back_returns(env):
    dialog = open_welcome(env[0])
    dialog._next.emit("clicked")
    assert pump(1.0, until=lambda: dialog._carousel.get_position() == pytest.approx(1.0, abs=0.01))
    assert dialog._back.get_visible() and dialog._next.get_label() == "Next"
    assert "Download the models" in labels_in(dialog._pages[1])

    dialog._back.emit("clicked")
    assert pump(1.0, until=lambda: dialog._carousel.get_position() == pytest.approx(0.0, abs=0.01))
    assert not dialog._back.get_visible()


def test_the_last_button_says_close_while_models_are_missing_and_closing_saves_that_it_was_seen(env):
    app, _window = env
    dialog = open_welcome(app)
    dialog._go(2)
    assert dialog._next.get_label() == "Close" and dialog._finish.get_title() == "Almost there"
    assert "a speech recognition model and a translation model" in dialog._finish.get_description()
    assert "Download Models" in dialog._finish.get_description()

    dialog._next.emit("clicked")
    assert pump(1.0, until=lambda: app.welcome_dialog is None)
    assert AppConfig.load().welcome_done


def test_closing_with_the_window_button_or_escape_counts_as_seen_too(env):
    app, _window = env
    dialog = open_welcome(app)
    dialog.close()
    assert pump(1.0, until=lambda: app.welcome_dialog is None)
    assert AppConfig.load().welcome_done


# --- The models page ---

def test_the_models_page_lists_the_same_models_as_preferences_with_only_turbo_recommended(env):
    dialog = open_welcome(env[0])
    assert len(dialog._rows) == 8 and {row.state for row in dialog._rows.values()} == {RowState.DOWNLOAD}
    badges = [key for key, row in dialog._rows.items()
              if any(isinstance(w, Gtk.Label) and w.get_label() == "Recommended" for w in walk(row))]
    assert badges == ["whisper.large-v3-turbo"]
    assert any(text.startswith("Saved to ") for text in labels_in(dialog._pages[1]))


def test_a_download_started_from_the_guide_asks_first_then_shows_progress(env, downloader):
    app, window = env
    dialog = open_welcome(app)
    dialog._go(1)
    row = dialog._rows["whisper.large-v3-turbo"]

    row._download.emit("clicked")
    pump(0.2)
    confirmation = window.get_visible_dialog()
    assert isinstance(confirmation, Adw.AlertDialog) and "Whisper Large v3 Turbo" in confirmation.get_heading()
    assert downloader.calls == []

    confirmation.emit("response", "confirm")
    assert pump(1.0, until=lambda: row.state is RowState.DOWNLOADING)
    assert not dialog._rows["whisper.large-v3"]._download.get_sensitive()  # One at a time.


def test_finished_downloads_select_the_models_and_turn_the_last_page_into_all_set(env, downloader):
    app, _window = env
    dialog = open_welcome(app)
    downloader.release.set()

    app.downloads.start(option_by_key("whisper.large-v3-turbo"))
    assert pump(2.0, until=lambda: dialog._rows["whisper.large-v3-turbo"].state is RowState.IN_USE)
    dialog._go(2)
    assert dialog._finish.get_title() == "Almost there"
    assert "a translation model" in dialog._finish.get_description()
    assert "speech recognition" not in dialog._finish.get_description()

    app.downloads.start(option_by_key("llm.e2b.q4_k_xl"))
    assert pump(2.0, until=lambda: dialog._finish.get_title() == "You're all set")
    assert dialog._next.get_label() == "Start Translating"
    assert AppConfig.load().whisper_ready and AppConfig.load().llm_ready


def test_the_last_page_mentions_a_download_that_is_still_running(env, downloader):
    app, _window = env
    dialog = open_welcome(app)
    app.downloads.start(option_by_key("llm.e4b.q4_k_xl"))
    pump(0.3)
    dialog._go(2)
    assert "download is in progress" in dialog._finish.get_description()


def test_closing_the_guide_does_not_stop_a_download_and_stops_listening_to_it(env, downloader):
    app, _window = env
    listeners_before = len(app.downloads._listeners)
    dialog = open_welcome(app)
    assert len(app.downloads._listeners) == listeners_before + 1

    app.downloads.start(option_by_key("llm.e2b.q8_k_xl"))
    pump(0.3)
    dialog.close()
    assert pump(1.0, until=lambda: app.welcome_dialog is None)
    assert app.downloads.active and len(app.downloads._listeners) == listeners_before


def test_how_a_download_ended_is_shown_inside_the_guide_while_it_is_open(env, downloader):
    app, window = env
    dialog = open_welcome(app)
    in_guide, in_window = capture_toasts(dialog), capture_toasts(window._toast_overlay)
    downloader.release.set()
    app.downloads.start(option_by_key("whisper.large-v3"))
    assert pump(2.0, until=lambda: bool(in_guide))
    assert in_guide[0].get_title() == "Whisper Large v3 downloaded and selected." and in_window == []


# --- Details ---

def _first_status_page(dialog):
    return next(w for w in walk(dialog._pages[0]) if isinstance(w, Adw.StatusPage))


def test_the_welcome_page_shows_the_app_icon_when_it_is_installed_and_a_fallback_when_it_is_not(env):
    app, _window = env
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    assert theme.has_icon("video-x-generic-symbolic")
    shown = WelcomeDialog(app.downloads, app_icon="video-x-generic-symbolic")
    missing = WelcomeDialog(app.downloads, app_icon="no-such-icon-karamvt")
    try:
        assert _first_status_page(shown).get_icon_name() == "video-x-generic-symbolic"
        assert _first_status_page(missing).get_icon_name() == "video-x-generic-symbolic"  # The fallback.
        assert APP_ID == "io.github.AbuKaram01.KaramVT"
    finally:
        shown.detach()
        missing.detach()


def test_every_icon_the_guide_uses_exists_in_the_standard_icon_theme(env):
    from karamvt.gui.welcome import _FEATURES

    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not theme.has_icon("window-close-symbolic"):
        pytest.skip("no standard icon theme is installed")
    icons = [icon for icon, _title, _text in _FEATURES] + ["object-select-symbolic", "folder-download-symbolic"]
    assert [icon for icon in icons if not theme.has_icon(icon)] == []
