# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The translation history driven through the real interface (see gui_support.py).

Covers translations being recorded, the History dialog, and its settings in Preferences. The translation model is a
stand-in.
"""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import time

import pytest
from gui_support import (
    Adw, AppConfig, Gtk, MainWindow, capture_toasts, downloader, env, fake_translation, open_preferences, pump,
    ready_models, run_text_job, walk,
)

from karamvt.core.cancel import OperationCancelled
from karamvt.core.history import DEFAULT_LIMIT
from karamvt.gui.history_dialog import PAGE_SIZE, HistoryDialog

LONG_AGO = 1_700_000_000.0


# --- Helpers ---

def fill(app, count, *, start=0, source="English", target="Spanish"):
    """``count`` saved translations, numbered, the last one the newest."""
    for number in range(start, start + count):
        app.history.add(
            source, target, f"Sentence {number}", f"Frase {number}", model="model.gguf", seconds=1.5,
            now=LONG_AGO + number,
        )


def open_history(app, window) -> HistoryDialog:
    """Open the History dialog through the application's action and return it."""
    app.activate_action("history", None)
    pump(0.2)
    assert isinstance(window._history_dialog, HistoryDialog)
    return window._history_dialog


def rows_of(dialog) -> list:
    """The rows of the dialog's list, in the order shown."""
    rows, child = [], dialog._list.get_first_child()
    while child is not None:
        rows.append(child)
        child = child.get_next_sibling()
    return rows


def titles_of(dialog) -> list[str]:
    return [row.get_title() for row in rows_of(dialog)]


def button(widget, label):
    """The first button labelled ``label`` inside ``widget``, or None."""
    return next((w for w in walk(widget) if isinstance(w, Gtk.Button) and w.get_label() == label), None)


def search(dialog, text):
    """Type ``text`` into the dialog's search box and wait until the dialog has applied it."""
    dialog._search.set_text(text)
    assert pump(2.0, until=lambda: dialog._query == text)


def shown_page(dialog) -> str:
    return dialog._pages.get_visible_child_name()


def clipboard_text(widget) -> str:
    """Read the clipboard of ``widget``'s display, running the main loop until the text arrives."""
    got = []
    clipboard = widget.get_clipboard()
    clipboard.read_text_async(None, lambda source, result: got.append(source.read_text_finish(result)))
    assert pump(2.0, until=lambda: bool(got))
    return got[0]


def translate(tab, source="English", target="Spanish", text="Hello", result="Hola", model="model.gguf", seconds=0.5):
    """What the Text tab announces when a translation finishes."""
    tab.emit("translation-done", source, target, text, result, model, seconds)


def set_config(**changes) -> None:
    """Change settings in the saved file the way Preferences does (the file as it is now, plus these)."""
    config = AppConfig.load()
    for name, value in changes.items():
        setattr(config, name, value)
    config.save()


def delete_button(row):
    """The Delete button of a history row."""
    return next(w for w in walk(row) if isinstance(w, Gtk.Button) and w.get_tooltip_text() == "Delete")


def ask_to_confirm(window):
    """The confirmation dialog that is on screen now."""
    dialog = window.get_visible_dialog()
    assert isinstance(dialog, Adw.AlertDialog)
    return dialog


# --- Recording ---

def test_a_finished_translation_is_recorded_with_everything_about_it(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch)
    run_text_job(window._text_tab, text="Hello")
    [entry] = app.history.find()
    assert (entry.source_language, entry.target_language) == ("English", "Spanish")
    assert (entry.source_text, entry.translated_text) == ("Hello", "Hola")
    assert entry.model == "model.gguf"
    assert 0 <= entry.seconds < 5
    assert abs(entry.created_at - time.time()) < 30


def test_a_translation_from_auto_detect_is_recorded_as_such(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch)
    tab = window._text_tab
    tab._input_buffer.set_text("Hello")
    tab._source_picker.set_selected("Auto-detect")
    tab._target_picker.set_selected("Spanish")
    tab._translate_button.emit("clicked")
    assert pump(3.0, until=lambda: tab._worker is not None and not tab.is_busy)
    pump(0.1)
    assert app.history.find()[0].source_language == "Auto-detect"


def test_nothing_is_recorded_while_the_history_is_turned_off(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    set_config(history_enabled=False)
    fake_translation(monkeypatch)
    run_text_job(window._text_tab)
    assert app.history.count() == 0 and not app.history.path.exists()


def test_a_failed_translation_is_not_recorded(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch, error=RuntimeError("out of memory"))
    run_text_job(window._text_tab)
    assert app.history.count() == 0


def test_a_translation_that_was_stopped_is_not_recorded(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch, error=OperationCancelled())
    run_text_job(window._text_tab)
    assert app.history.count() == 0


def test_text_read_from_an_image_is_not_a_translation_and_is_not_recorded(env):
    app, window = env
    tab = window._text_tab
    tab._launch_job(lambda _ctx: "words from a picture", on_success=lambda text: tab._input_buffer.set_text(text))
    assert pump(2.0, until=lambda: not tab.is_busy)
    pump(0.1)
    assert app.history.count() == 0


def test_translating_the_same_thing_twice_in_a_row_keeps_one_entry(env, tmp_path, monkeypatch):
    app, window = env
    ready_models(tmp_path)
    fake_translation(monkeypatch)
    run_text_job(window._text_tab, text="Hello")
    run_text_job(window._text_tab, text="Hello")
    assert app.history.count() == 1


def test_only_the_newest_translations_are_kept_up_to_the_limit_in_preferences(env):
    app, window = env
    set_config(history_limit=10)
    for number in range(13):
        translate(window._text_tab, text=f"Sentence {number}", result=f"Frase {number}")
    texts = [entry.source_text for entry in app.history.find(limit=50)]
    assert texts == [f"Sentence {number}" for number in range(12, 2, -1)]


def test_the_limit_is_the_default_one_until_it_is_changed(env):
    app, window = env
    assert AppConfig.load().history_keep == DEFAULT_LIMIT
    for number in range(12):
        translate(window._text_tab, text=f"Sentence {number}")
    assert app.history.count() == 12


def test_a_translation_finishing_while_the_history_is_open_shows_up_at_once(env):
    app, window = env
    fill(app, 2)
    dialog = open_history(app, window)
    assert len(rows_of(dialog)) == 2
    translate(window._text_tab, text="Brand new", result="Nuevo")
    assert titles_of(dialog)[0] == "Brand new" and len(rows_of(dialog)) == 3


# --- Opening it ---

def test_the_history_opens_from_the_menu_action_and_only_once(env):
    app, window = env
    dialog = open_history(app, window)
    app.activate_action("history", None)
    pump(0.1)
    assert window._history_dialog is dialog


def test_control_h_is_the_shortcut(env):
    app, _window = env
    assert app.get_accels_for_action("app.history") == ["<Control>h"]


def test_the_history_is_in_the_main_menu(env):
    _app, window = env
    labels = {}
    for widget in walk(window):
        if isinstance(widget, Gtk.MenuButton) and widget.get_menu_model() is not None:
            model = widget.get_menu_model()
            for index in range(model.get_n_items()):
                labels[model.get_item_attribute_value(index, "label", None).get_string()] = (
                    model.get_item_attribute_value(index, "action", None).get_string()
                )
    assert labels["Translation History"] == "app.history"


def test_closing_it_lets_it_be_opened_again_fresh(env):
    app, window = env
    first = open_history(app, window)
    first.close()
    pump(0.5)
    assert window._history_dialog is None
    assert open_history(app, window) is not first


# --- What it shows ---

def test_with_nothing_saved_it_says_so(env):
    app, window = env
    dialog = open_history(app, window)
    assert shown_page(dialog) == "empty"
    assert dialog._empty.get_title() == "No Translations Yet"
    assert not dialog._clear_button.get_sensitive()
    assert dialog._title.get_subtitle() == ""


def test_the_newest_translation_is_on_top_and_the_title_counts_them(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    assert titles_of(dialog) == ["Sentence 2", "Sentence 1", "Sentence 0"]
    assert rows_of(dialog)[0].get_subtitle() == "Frase 2"
    assert dialog._title.get_subtitle() == "3 translations"
    assert shown_page(dialog) == "list" and dialog._clear_button.get_sensitive()


def test_one_translation_is_counted_in_the_singular(env):
    app, window = env
    fill(app, 1)
    assert open_history(app, window)._title.get_subtitle() == "1 translation"


def test_a_long_text_is_shortened_to_one_line_in_the_list(env):
    app, window = env
    app.history.add("English", "Spanish", "word " * 100 + "\nsecond line", "palabra " * 100, now=LONG_AGO)
    row = rows_of(open_history(app, window))[0]
    assert "\n" not in row.get_title() and row.get_title().endswith("…") and len(row.get_title()) <= 70
    assert len(row.get_subtitle()) <= 90


def test_the_languages_and_the_time_are_on_each_row(env):
    app, window = env
    app.history.add("Arabic", "English", "مرحبا", "Hello", now=LONG_AGO)
    row = rows_of(open_history(app, window))[0]
    labels = [w.get_label() for w in walk(row) if isinstance(w, Gtk.Label) and "→" in (w.get_label() or "")]
    assert labels and labels[0].startswith("Arabic → English\n")


def test_the_history_can_be_read_while_new_translations_are_not_being_kept(env):
    app, window = env
    fill(app, 2)
    set_config(history_enabled=False)
    dialog = open_history(app, window)
    assert len(rows_of(dialog)) == 2
    assert dialog._banner.get_revealed()
    assert dialog._banner.get_action_name() == "app.preferences"


def test_the_banner_is_hidden_while_the_history_is_on(env):
    app, window = env
    assert not open_history(app, window)._banner.get_revealed()


def test_the_banner_follows_the_setting_when_the_dialog_refreshes(env):
    app, window = env
    dialog = open_history(app, window)
    set_config(history_enabled=False)
    dialog.refresh()
    assert dialog._banner.get_revealed()


# --- The whole text, one entry at a time ---

def test_a_row_shows_the_full_texts_only_when_it_is_opened(env):
    app, window = env
    fill(app, 2)
    dialog = open_history(app, window)
    row = rows_of(dialog)[0]
    assert button(row, "Use Again") is None
    row.set_expanded(True)
    assert button(row, "Use Again") is not None
    row.set_expanded(False)
    row.set_expanded(True)
    assert len([w for w in walk(row) if isinstance(w, Gtk.Button) and w.get_label() == "Use Again"]) == 1
    assert button(rows_of(dialog)[1], "Use Again") is None  # The other one was never opened.


def test_the_opened_row_shows_both_texts_in_full_and_the_details(env):
    app, window = env
    long_text = "A long sentence that goes on. " * 20
    app.history.add(
        "English", "Spanish", long_text, "Una frase larga. " * 20, model="gemma.gguf", seconds=4.25, now=LONG_AGO
    )
    row = rows_of(open_history(app, window))[0]
    row.set_expanded(True)
    texts = []
    for view in (w for w in walk(row) if isinstance(w, Gtk.TextView)):
        buffer = view.get_buffer()
        texts.append(buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False))
    assert texts == [long_text, "Una frase larga. " * 20]
    assert all(not view.get_editable() for view in walk(row) if isinstance(view, Gtk.TextView))
    facts = next(w.get_label() for w in walk(row) if isinstance(w, Gtk.Label) and "gemma.gguf" in (w.get_label() or ""))
    assert "gemma.gguf" in facts and "4.2 s" in facts and "2023" in facts


def test_the_details_leave_out_what_was_not_recorded(env):
    app, window = env
    app.history.add("English", "Spanish", "Hello", "Hola", now=LONG_AGO)  # No model, no duration.
    row = rows_of(open_history(app, window))[0]
    row.set_expanded(True)
    facts = [w.get_label() for w in walk(row) if isinstance(w, Gtk.Label) and (w.get_label() or "").startswith("1")]
    assert facts and " · " not in facts[0]


def test_copy_translation_puts_the_translation_on_the_clipboard(env):
    app, window = env
    app.history.add("English", "Arabic", "Hello", "مرحبا", now=LONG_AGO)
    dialog = open_history(app, window)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    button(row, "Copy Translation").emit("clicked")
    assert clipboard_text(dialog) == "مرحبا"


def test_copy_original_puts_the_original_on_the_clipboard(env):
    app, window = env
    app.history.add("English", "Arabic", "Hello", "مرحبا", now=LONG_AGO)
    dialog = open_history(app, window)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    button(row, "Copy Original").emit("clicked")
    assert clipboard_text(dialog) == "Hello"


# --- Use again ---

def test_use_again_puts_the_translation_back_in_the_text_tab_and_closes_the_history(env):
    app, window = env
    app.history.add("Arabic", "French", "مرحبا بالعالم", "Bonjour le monde", now=LONG_AGO)
    dialog = open_history(app, window)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    tab = window._text_tab
    heard = []
    tab.connect("state-changed", lambda _tab: heard.append(True))
    window._view_stack.set_visible_child_name("video")

    button(row, "Use Again").emit("clicked")
    pump(0.5)
    assert window._view_stack.get_visible_child_name() == "text"
    assert (tab._source_picker.get_selected(), tab._target_picker.get_selected()) == ("Arabic", "French")
    assert tab._get_buffer_text(tab._input_buffer) == "مرحبا بالعالم"
    assert tab._get_buffer_text(tab._output_buffer) == "Bonjour le monde"
    assert heard  # The tab announced the change, so it is remembered for next time as well.
    assert window._history_dialog is None


def test_use_again_works_for_a_translation_made_from_auto_detect(env):
    app, window = env
    app.history.add("Auto-detect", "Spanish", "Hello", "Hola", now=LONG_AGO)
    dialog = open_history(app, window)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    button(row, "Use Again").emit("clicked")
    assert window._text_tab._source_picker.get_selected() == "Auto-detect"


def test_use_again_waits_while_a_translation_is_running(env):
    app, window = env
    app.history.add("English", "Spanish", "Hello", "Hola", now=LONG_AGO)
    dialog = open_history(app, window)
    toasts = capture_toasts(window._toast_overlay)
    window._text_tab._input_buffer.set_text("typing something else")
    window._text_tab.is_busy = True
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    button(row, "Use Again").emit("clicked")
    pump(0.2)
    assert window._text_tab._get_buffer_text(window._text_tab._input_buffer) == "typing something else"
    assert window._history_dialog is dialog  # Still open: nothing happened.
    assert [toast.get_title() for toast in toasts] == ["Wait for the current text job to finish first."]
    window._text_tab.is_busy = False


# --- Searching ---

def test_searching_narrows_the_list_and_the_title_says_how_many_match(env):
    app, window = env
    app.history.add("English", "Spanish", "Good morning", "Buenos días", now=LONG_AGO)
    app.history.add("English", "Spanish", "Good night", "Buenas noches", now=LONG_AGO + 1)
    app.history.add("English", "French", "Thank you", "Merci", now=LONG_AGO + 2)
    dialog = open_history(app, window)
    search(dialog, "good")
    assert titles_of(dialog) == ["Good night", "Good morning"]
    assert dialog._title.get_subtitle() == "2 of 3 translations"
    search(dialog, "")
    assert len(rows_of(dialog)) == 3 and dialog._title.get_subtitle() == "3 translations"


def test_the_search_looks_in_the_translation_and_the_language_names_too(env):
    app, window = env
    app.history.add("English", "Spanish", "Good morning", "Buenos días", now=LONG_AGO)
    app.history.add("English", "French", "Thank you", "Merci", now=LONG_AGO + 1)
    dialog = open_history(app, window)
    search(dialog, "merci")
    assert titles_of(dialog) == ["Thank you"]
    search(dialog, "spanish")
    assert titles_of(dialog) == ["Good morning"]


def test_the_search_ignores_accents_and_arabic_vowel_marks_and_letter_variants(env):
    app, window = env
    app.history.add("Arabic", "English", "مَرْحَبًا بِكَ", "Welcome", now=LONG_AGO)
    app.history.add("Arabic", "English", "أحمد في المدرسة", "Ahmed is at school", now=LONG_AGO + 1)
    app.history.add("English", "Spanish", "Good morning", "Buenos días", now=LONG_AGO + 2)
    dialog = open_history(app, window)
    search(dialog, "مرحبا")
    assert titles_of(dialog) == ["مَرْحَبًا بِكَ"]
    search(dialog, "احمد")
    assert titles_of(dialog) == ["أحمد في المدرسة"]
    search(dialog, "dias")
    assert titles_of(dialog) == ["Good morning"]


def test_a_search_with_no_match_says_so_and_names_what_was_searched(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    search(dialog, "zebra")
    assert shown_page(dialog) == "no-match"
    assert dialog._no_match.get_title() == "No Results" and "zebra" in dialog._no_match.get_description()
    assert dialog._title.get_subtitle() == "0 of 3 translations"
    assert dialog._clear_button.get_sensitive()  # The history itself is not empty.
    search(dialog, "")
    assert shown_page(dialog) == "list"


def test_a_search_is_not_confused_by_characters_that_mean_something_to_a_database(env):
    app, window = env
    app.history.add("English", "Spanish", "100% sure", "Seguro al 100%", now=LONG_AGO)
    app.history.add("English", "Spanish", "Just fine", "Muy bien", now=LONG_AGO + 1)
    dialog = open_history(app, window)
    search(dialog, "100%")
    assert titles_of(dialog) == ["100% sure"]
    search(dialog, "_")
    assert titles_of(dialog) == []


def test_pressing_escape_in_the_search_clears_it(env):
    app, window = env
    fill(app, 2)
    dialog = open_history(app, window)
    search(dialog, "sentence 1")
    assert len(rows_of(dialog)) == 1
    dialog._search.emit("stop-search")
    assert pump(2.0, until=lambda: len(rows_of(dialog)) == 2)


# --- Deleting ---

def collect_toasts(dialog) -> list:
    """Collect the toasts that the dialog sends instead of showing them."""
    return capture_toasts(dialog._toasts)


def test_deleting_one_translation_removes_it_from_the_list_and_the_history(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    toasts = collect_toasts(dialog)
    row = rows_of(dialog)[1]  # The row titled "Sentence 1".
    row.set_expanded(True)
    delete_button(row).emit("clicked")
    assert titles_of(dialog) == ["Sentence 2", "Sentence 0"]
    assert [entry.source_text for entry in app.history.find()] == ["Sentence 2", "Sentence 0"]
    assert dialog._title.get_subtitle() == "2 translations"
    assert [toast.get_title() for toast in toasts] == ["Translation deleted"] and toasts[0].get_button_label() == "Undo"


def test_undoing_a_deletion_puts_the_translation_back_where_it_was(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    toasts = collect_toasts(dialog)
    row = rows_of(dialog)[1]
    row.set_expanded(True)
    delete_button(row).emit("clicked")
    toasts[0].emit("button-clicked")
    assert titles_of(dialog) == ["Sentence 2", "Sentence 1", "Sentence 0"]
    assert dialog._title.get_subtitle() == "3 translations"
    assert [entry.source_text for entry in app.history.find()] == ["Sentence 2", "Sentence 1", "Sentence 0"]


def test_deleting_the_last_translation_shows_the_empty_page_and_undo_brings_it_back(env):
    app, window = env
    fill(app, 1)
    dialog = open_history(app, window)
    toasts = collect_toasts(dialog)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    delete_button(row).emit("clicked")
    assert shown_page(dialog) == "empty" and not dialog._clear_button.get_sensitive()
    toasts[0].emit("button-clicked")
    assert shown_page(dialog) == "list" and titles_of(dialog) == ["Sentence 0"]


def test_deleting_while_searching_keeps_the_search_and_the_counts_true(env):
    app, window = env
    app.history.add("English", "Spanish", "Good morning", "Buenos días", now=LONG_AGO)
    app.history.add("English", "Spanish", "Good night", "Buenas noches", now=LONG_AGO + 1)
    app.history.add("English", "French", "Thank you", "Merci", now=LONG_AGO + 2)
    dialog = open_history(app, window)
    search(dialog, "good")
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    delete_button(row).emit("clicked")
    assert titles_of(dialog) == ["Good morning"]
    assert dialog._title.get_subtitle() == "1 of 2 translations"


def test_a_deletion_that_somebody_else_already_made_does_nothing_more(env):
    app, window = env
    fill(app, 2)
    dialog = open_history(app, window)
    toasts = collect_toasts(dialog)
    row = rows_of(dialog)[0]
    row.set_expanded(True)
    app.history.delete(app.history.find()[0].id)
    delete_button(row).emit("clicked")
    assert toasts == [] and len(rows_of(dialog)) == 2  # Nothing was deleted by this click.


# --- Clearing ---

def test_clearing_asks_first_and_cancelling_keeps_everything(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    dialog._clear_button.emit("clicked")
    pump(0.2)
    alert = ask_to_confirm(window)
    assert alert.get_heading() == "Clear the whole history?" and "All 3 saved translations" in alert.get_body()
    alert.emit("response", "cancel")
    pump(0.2)
    assert app.history.count() == 3 and len(rows_of(dialog)) == 3


def test_clearing_deletes_everything_and_shows_the_empty_page(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    toasts = collect_toasts(dialog)
    dialog._clear_button.emit("clicked")
    pump(0.2)
    ask_to_confirm(window).emit("response", "confirm")
    pump(0.2)
    assert app.history.count() == 0 and rows_of(dialog) == []
    assert shown_page(dialog) == "empty" and not dialog._clear_button.get_sensitive()
    assert [toast.get_title() for toast in toasts] == ["History cleared"]


def test_clearing_while_a_search_is_on_clears_the_search_too(env):
    app, window = env
    fill(app, 3)
    dialog = open_history(app, window)
    search(dialog, "sentence 1")
    dialog._clear_button.emit("clicked")
    pump(0.2)
    ask_to_confirm(window).emit("response", "confirm")
    pump(0.5)
    assert dialog._search.get_text() == "" and shown_page(dialog) == "empty"


# --- Long histories ---

def test_a_long_history_is_shown_a_page_at_a_time(env):
    app, window = env
    total = PAGE_SIZE * 2 + 15
    fill(app, total)
    dialog = open_history(app, window)
    assert len(rows_of(dialog)) == PAGE_SIZE and dialog._more.get_visible()
    assert dialog._title.get_subtitle() == f"{total} translations"
    dialog._more.emit("clicked")
    assert len(rows_of(dialog)) == PAGE_SIZE * 2 and dialog._more.get_visible()
    dialog._more.emit("clicked")
    assert len(rows_of(dialog)) == total and not dialog._more.get_visible()
    titles = titles_of(dialog)
    assert titles == [f"Sentence {number}" for number in range(total - 1, -1, -1)]  # Newest first, none twice.


def test_a_short_history_has_no_show_more_button(env):
    app, window = env
    fill(app, 3)
    assert not open_history(app, window)._more.get_visible()


def test_pages_follow_a_search_too(env):
    app, window = env
    fill(app, PAGE_SIZE + 10)
    dialog = open_history(app, window)
    search(dialog, "sentence")
    assert len(rows_of(dialog)) == PAGE_SIZE and dialog._more.get_visible()
    dialog._more.emit("clicked")
    assert len(rows_of(dialog)) == PAGE_SIZE + 10 and not dialog._more.get_visible()


def test_deleting_the_last_row_of_a_page_loads_the_rest_of_the_matches(env):
    app, window = env
    fill(app, PAGE_SIZE + 3)
    dialog = open_history(app, window)
    for _ in range(PAGE_SIZE):
        row = rows_of(dialog)[0]
        row.set_expanded(True)
        delete_button(row).emit("clicked")
    assert titles_of(dialog) == ["Sentence 2", "Sentence 1", "Sentence 0"]
    assert not dialog._more.get_visible()


# --- Preferences ---

def test_preferences_has_the_history_settings_with_the_defaults(env):
    app, window = env
    prefs = open_preferences(app, window)
    assert prefs._history_row.get_title() == "Save translation history" and prefs._history_row.get_active()
    assert prefs._history_limit_row.get_title() == "Keep up to"
    assert prefs._history_limit_row.get_value() == DEFAULT_LIMIT and prefs._history_limit_row.get_sensitive()
    assert prefs._saved_row.get_subtitle() == "Nothing saved yet" and not prefs._clear_history_button.get_sensitive()


def test_the_history_switch_is_saved_and_dims_the_limit(env):
    app, window = env
    prefs = open_preferences(app, window)
    prefs._history_row.set_active(False)
    assert AppConfig.load().history_enabled is False and not prefs._history_limit_row.get_sensitive()
    prefs._history_row.set_active(True)
    assert AppConfig.load().history_enabled is True and prefs._history_limit_row.get_sensitive()


def test_the_limit_is_saved_and_applies_to_the_next_translation(env):
    app, window = env
    fill(app, 30)
    prefs = open_preferences(app, window)
    prefs._history_limit_row.set_value(20)
    assert AppConfig.load().history_limit == 20
    translate(window._text_tab, text="One more", result="Una más")
    assert app.history.count() == 20 and app.history.find(limit=1)[0].source_text == "One more"


def test_changing_the_history_settings_keeps_the_rest_of_the_saved_settings(env):
    app, window = env
    AppConfig(cpu_threads=7, welcome_done=True).save()
    prefs = open_preferences(app, window)
    AppConfig(cpu_threads=9, welcome_done=True).save()  # Changed meanwhile (e.g. from OCR Settings).
    prefs._history_row.set_active(False)
    prefs._history_limit_row.set_value(50)
    saved = AppConfig.load()
    assert (
        saved.history_enabled is False
        and saved.history_limit == 50
        and saved.cpu_threads == 9
        and saved.welcome_done
    )


def test_preferences_counts_the_saved_translations(env):
    app, window = env
    fill(app, 4)
    prefs = open_preferences(app, window)
    assert prefs._saved_row.get_subtitle() == "4 saved on this computer" and prefs._clear_history_button.get_sensitive()
    fill(app, 1, start=10)  # One more arrives while it is open.
    prefs._update_saved_row()
    assert prefs._saved_row.get_subtitle() == "5 saved on this computer"


def test_clearing_from_preferences_asks_first_and_then_deletes_everything(env):
    app, window = env
    fill(app, 4)
    prefs = open_preferences(app, window)
    toasts = capture_toasts(prefs)
    prefs._clear_history_button.emit("clicked")
    pump(0.2)
    alert = prefs.get_visible_dialog()
    assert isinstance(alert, Adw.AlertDialog) and "All 4 saved translations" in alert.get_body()
    alert.emit("response", "cancel")
    pump(0.2)
    assert app.history.count() == 4

    prefs._clear_history_button.emit("clicked")
    pump(0.2)
    prefs.get_visible_dialog().emit("response", "confirm")
    pump(0.2)
    assert app.history.count() == 0
    assert prefs._saved_row.get_subtitle() == "Nothing saved yet" and not prefs._clear_history_button.get_sensitive()
    assert [toast.get_title() for toast in toasts] == ["History cleared"]


def test_preferences_opened_without_a_history_has_the_settings_but_no_saved_row(env):
    app, window = env
    from karamvt.gui.preferences import PreferencesWindow

    prefs = PreferencesWindow(transient_for=window)
    assert prefs._history_row.get_active() and not hasattr(prefs, "_saved_row")
    prefs.destroy()
