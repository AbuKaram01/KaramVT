# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The translation history store (core/history.py): what it keeps, finds and forgets, and how it copes with
a damaged or foreign database."""

import os
import sqlite3
import stat
import sys

import pytest

from karamvt.core import history as history_module
from karamvt.core import paths
from karamvt.core.history import HistoryEntry, HistoryStore, SCHEMA_VERSION, search_fold


@pytest.fixture
def store(tmp_path):
    opened = HistoryStore(tmp_path / "data" / "history.db")
    yield opened
    opened.close()


def add(store, source="Hello", translated="Hola", *, to="Spanish", frm="English", **kwargs):
    return store.add(frm, to, source, translated, **kwargs)


# --- Where it lives and when it exists ---

def test_the_database_lives_in_the_xdg_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "karamvt"
    assert HistoryStore().path == tmp_path / "xdg" / "karamvt" / "history.db"


def test_nothing_is_created_until_something_is_added(store):
    assert store.find() == [] and store.count() == 0 and store.get(1) is None
    assert store.delete(1) is False and store.clear() == 0 and store.trim(5) == 0
    assert not store.path.exists() and not store.path.parent.exists()
    add(store)
    assert store.path.exists()


POSIX_ONLY_PERMISSIONS = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no permission bits; the profile folder's own rights keep it private",
)


@POSIX_ONLY_PERMISSIONS
def test_the_database_is_private_to_the_user(store):
    add(store)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700


# --- Adding ---

def test_an_added_translation_comes_back_whole(store):
    entry = add(store, "Good morning", "Buenos días", model="gemma.gguf", seconds=2.5, now=1000.0)
    assert entry == HistoryEntry(
        id=entry.id, created_at=1000.0, source_language="English", target_language="Spanish",
        source_text="Good morning", translated_text="Buenos días", model="gemma.gguf", seconds=2.5,
    )
    assert store.get(entry.id) == entry


def test_the_newest_entry_comes_first(store):
    first, second, third = (add(store, f"text {n}", f"texto {n}") for n in range(3))
    assert [entry.id for entry in store.find()] == [third.id, second.id, first.id]


def test_nothing_is_kept_for_blank_text(store):
    assert add(store, "   ", "Hola") is None
    assert add(store, "Hello", "") is None
    assert add(store, "Hello", " \n ") is None
    assert store.count() == 0


def test_the_same_translation_twice_in_a_row_is_one_entry_with_a_fresh_time(store):
    first = add(store, now=1000.0, seconds=3.0)
    again = add(store, now=2000.0, seconds=1.0, model="other.gguf")
    assert again.id == first.id and store.count() == 1
    assert (again.created_at, again.seconds, again.model) == (2000.0, 1.0, "other.gguf")


def test_the_same_text_with_another_result_or_language_is_a_new_entry(store):
    add(store)
    add(store, translated="Buenas")
    add(store, to="French")
    assert store.count() == 3


def test_an_old_duplicate_is_not_merged_with_a_later_one(store):
    add(store, "Hello", "Hola")
    add(store, "Bye", "Adiós")
    add(store, "Hello", "Hola")
    assert store.count() == 3


def test_arabic_and_mixed_direction_text_survive_exactly(store):
    source = "مرحبا بالعالم\nthe second line, with English and 123"
    translated = "Hello world\r\nالسطر الثاني"
    entry = add(store, source, translated, frm="Arabic", to="English")
    assert store.get(entry.id).source_text == source and store.get(entry.id).translated_text == translated


def test_a_large_text_is_kept(store):
    text = "كلمة " * 4000
    assert store.get(add(store, text, text).id).source_text == text


# --- The limit ---

def test_the_oldest_entries_beyond_the_limit_are_dropped_as_new_ones_arrive(store):
    for n in range(8):
        add(store, f"text {n}", f"texto {n}", keep=5)
    assert [entry.source_text for entry in store.find()] == [f"text {n}" for n in range(7, 2, -1)]


def test_trimming_keeps_the_newest_and_says_how_many_went(store):
    for n in range(10):
        add(store, f"text {n}", f"texto {n}")
    assert store.trim(4) == 6
    assert [entry.source_text for entry in store.find()] == ["text 9", "text 8", "text 7", "text 6"]
    assert store.trim(4) == 0


def test_a_limit_of_zero_keeps_nothing(store):
    for n in range(3):
        add(store, f"t{n}", f"u{n}")
    assert store.trim(0) == 3 and store.count() == 0


# --- Reading in pages ---

def test_pages_follow_one_another_without_gaps_or_repeats(store):
    ids = [add(store, f"text {n}", f"texto {n}").id for n in range(23)]
    seen, before = [], None
    while page := store.find(limit=10, before_id=before):
        seen += [entry.id for entry in page]
        before = page[-1].id
    assert seen == ids[::-1]


def test_a_page_stays_put_when_something_new_is_added_meanwhile(store):
    for n in range(5):
        add(store, f"text {n}", f"texto {n}")
    first_page = store.find(limit=3)
    add(store, "newest", "el más nuevo")
    assert [entry.source_text for entry in store.find(limit=3, before_id=first_page[-1].id)] == ["text 1", "text 0"]


def test_count_matches_what_find_would_return(store):
    for n in range(7):
        add(store, f"apple {n}" if n % 2 else f"pear {n}", f"translated {n}")
    assert store.count() == 7 and store.count("apple") == len(store.find("apple", limit=100)) == 3


# --- Searching ---

def test_search_looks_in_both_texts_and_both_languages(store):
    add(store, "The cat sleeps", "El gato duerme")
    add(store, "A dog barks", "Un perro ladra", to="Spanish")
    add(store, "A bird sings", "Un oiseau chante", to="French")
    assert [e.source_text for e in store.find("gato")] == ["The cat sleeps"]
    assert [e.source_text for e in store.find("dog")] == ["A dog barks"]
    assert [e.source_text for e in store.find("french")] == ["A bird sings"]
    assert store.count("english") == 3


def test_every_word_must_match_but_in_any_order_and_place(store):
    add(store, "The red car is fast", "El coche rojo es rápido")
    add(store, "The red house is big", "La casa roja es grande")
    assert [e.source_text for e in store.find("car red")] == ["The red car is fast"]
    assert [e.source_text for e in store.find("rojo rápido")] == ["The red car is fast"]
    assert store.find("red blue") == []


def test_search_ignores_case_and_accents(store):
    add(store, "A little café", "Un petit café")
    assert store.count("CAFE") == 1 and store.count("Café") == 1 and store.count("cafe petit") == 1


def test_arabic_search_ignores_vowel_marks_and_interchangeable_letters(store):
    add(store, "school", "مَدْرَسَة جميلة", to="Arabic")
    add(store, "to Cairo", "إلى القاهرة", to="Arabic")
    add(store, "mosque", "مسجد كبير", to="Arabic")
    assert store.count("مدرسه") == 1  # Typed with ha for ta marbuta, without the marks.
    assert store.count("مدرسة") == 1
    assert store.count("الي") == 1  # Alef with hamza below, typed as a plain alef.
    assert store.count("ألى") == 1 and store.count("الى") == 1
    assert store.count("جميله") == 1
    assert store.count("مسجد") == 1


def test_wildcard_characters_in_a_search_are_taken_literally(store):
    add(store, "100% sure", "seguro al 100%")
    add(store, "snake_case name", "nombre")
    add(store, "plain words", "palabras")
    assert [e.source_text for e in store.find("100%")] == ["100% sure"]
    assert [e.source_text for e in store.find("snake_case")] == ["snake_case name"]
    assert store.find("%") != store.find("") and store.count("%") == 1
    assert store.count("_") == 1
    assert store.find("a\\b") == []


def test_a_blank_search_finds_everything(store):
    add(store)
    add(store, "Bye", "Adiós")
    assert store.count("") == store.count("   ") == 2


def test_quotes_in_a_search_do_no_harm(store):
    add(store, "It's \"quoted\"", "Está «citado»")
    assert store.count("it's") == 1 and store.count("'; DROP TABLE translations; --") == 0
    assert store.count() == 1


def test_search_fold_unifies_what_people_type_differently():
    assert search_fold("Café") == search_fold("cafe") == "cafe"
    assert search_fold("مَدْرَسَة") == search_fold("مدرسه")
    assert search_fold("ـــمـــدرسة") == search_fold("مدرسة")
    assert search_fold("ٱلله") == search_fold("الله")
    assert search_fold("Straße") == "strasse"


# --- Deleting ---

def test_deleting_removes_one_entry_and_says_whether_it_was_there(store):
    keep, drop = add(store, "keep", "guardar"), add(store, "drop", "soltar")
    assert store.delete(drop.id) is True
    assert store.delete(drop.id) is False
    assert [entry.id for entry in store.find()] == [keep.id]


def test_a_deleted_entry_can_be_put_back_in_its_old_place(store):
    first, second, third = (add(store, f"text {n}", f"texto {n}") for n in range(3))
    store.delete(second.id)
    assert store.restore(second) is True
    assert [entry.id for entry in store.find()] == [third.id, second.id, first.id]
    assert store.get(second.id) == second


def test_restoring_an_entry_that_is_still_there_changes_nothing(store):
    entry = add(store)
    assert store.restore(entry) is False and store.count() == 1


def test_clearing_forgets_everything_and_new_entries_start_again(store):
    for n in range(4):
        add(store, f"text {n}", f"texto {n}")
    assert store.clear() == 4 and store.count() == 0
    assert add(store) is not None and store.count() == 1


def test_cleared_text_does_not_stay_in_the_file(store):
    secret = "the-launch-code-is-swordfish"
    add(store, secret, "el código de lanzamiento es pez espada")
    store.close()
    assert secret.encode() in store.path.read_bytes()

    store.clear()
    store.close()
    assert secret.encode() not in store.path.read_bytes()


def test_a_deleted_entry_is_wiped_from_the_file_too(store):
    secret = "a-single-secret-sentence"
    entry = add(store, secret, "una frase secreta")
    add(store, "something else", "otra cosa")
    store.delete(entry.id)
    store.close()
    assert secret.encode() not in store.path.read_bytes()


# --- The database across sessions ---

def test_entries_survive_closing_and_opening_again(store):
    entry = add(store, "Hello", "مرحبا", to="Arabic", model="m.gguf", seconds=1.5, now=1234.5)
    store.close()
    assert HistoryStore(store.path).get(entry.id) == entry


def test_a_new_database_records_its_schema_version(store):
    add(store)
    store.close()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_later_migration_upgrades_an_existing_database_without_losing_entries(store, monkeypatch):
    entry = add(store, "Hello", "Hola", now=50.0)
    store.close()

    def add_a_favourite_column(connection):
        connection.execute("ALTER TABLE translations ADD COLUMN favourite INTEGER NOT NULL DEFAULT 0")

    monkeypatch.setattr(history_module, "_MIGRATIONS", [*history_module._MIGRATIONS, add_a_favourite_column])
    monkeypatch.setattr(history_module, "SCHEMA_VERSION", len(history_module._MIGRATIONS))
    upgraded = HistoryStore(store.path)
    assert upgraded.get(entry.id) == entry
    upgraded.close()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "favourite" in [row[1] for row in connection.execute("PRAGMA table_info(translations)")]


def test_a_failed_migration_changes_nothing(store, monkeypatch):
    add(store)
    store.close()

    def broken(connection):
        connection.execute("ALTER TABLE translations ADD COLUMN extra INTEGER")
        raise sqlite3.OperationalError("the second step failed")

    monkeypatch.setattr(history_module, "_MIGRATIONS", [*history_module._MIGRATIONS, broken])
    monkeypatch.setattr(history_module, "SCHEMA_VERSION", len(history_module._MIGRATIONS))
    assert HistoryStore(store.path).find() == []  # Unavailable, reported as empty.
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "extra" not in [row[1] for row in connection.execute("PRAGMA table_info(translations)")]


def test_a_database_from_a_newer_release_is_left_strictly_alone(store):
    add(store)
    store.close()
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 3}")
    before = store.path.read_bytes()

    newer = HistoryStore(store.path)
    assert newer.find() == [] and newer.add("English", "Spanish", "Hi", "Hola") is None
    assert newer.delete(1) is False and newer.clear() == 0
    assert store.path.read_bytes() == before
    assert not store.path.with_name("history.db.broken").exists()


# --- Damage ---

@pytest.mark.parametrize("content", [b"this is not a database at all, just some text " * 50, b"\x00" * 4096])
def test_a_file_that_is_not_a_database_is_set_aside_and_replaced(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(content)
    entry = add(store)
    assert entry is not None and store.count() == 1
    assert store.path.with_name("history.db.broken").read_bytes() == content


def test_a_damaged_database_is_set_aside_and_replaced(store):
    for n in range(40):
        add(store, f"text {n} " * 50, f"texto {n} " * 50)
    store.close()
    data = bytearray(store.path.read_bytes())
    data[100:140] = b"\xff" * 40  # Inside the header: not a database any more.
    store.path.write_bytes(bytes(data))

    fresh = HistoryStore(store.path)
    assert add(fresh) is not None
    assert fresh.count() == 1 and store.path.with_name("history.db.broken").exists()
    fresh.close()


def test_damage_found_while_reading_is_reported_as_empty_and_the_next_add_starts_clean(store):
    for n in range(40):
        add(store, f"text {n} " * 50, f"texto {n} " * 50)
    store.close()
    data = bytearray(store.path.read_bytes())
    data[100:140] = b"\xff" * 40
    store.path.write_bytes(bytes(data))

    reader = HistoryStore(store.path)
    assert reader.find() == []
    assert store.path.with_name("history.db.broken").exists() and not store.path.exists()
    assert add(reader, "after", "después") is not None and reader.count() == 1
    reader.close()


def test_the_journal_of_a_damaged_database_does_not_follow_it_into_the_new_one(store):
    add(store)
    store.close()
    store.path.write_bytes(b"not a database " * 100)
    journal = store.path.with_name("history.db-journal")
    journal.write_bytes(b"left behind by the old file")
    assert add(store, "fresh", "nuevo") is not None
    assert not journal.exists() and store.count() == 1


def test_a_database_that_cannot_be_opened_is_not_blamed_and_not_moved(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the folder should be")
    broken = HistoryStore(blocker / "history.db")
    assert broken.add("English", "Spanish", "Hi", "Hola") is None
    assert broken.find() == [] and broken.count() == 0
    assert blocker.read_text() == "a file where the folder should be"


def test_a_write_that_fails_halfway_leaves_the_history_as_it_was(store, monkeypatch):
    add(store, "keep", "guardar")

    real = store._open()
    original = real.execute

    class Proxy:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO translations"):
                raise sqlite3.OperationalError("database or disk is full")
            return original(sql, *args)

    store._connection = Proxy()
    assert add(store, "lose", "perder") is None
    store._connection = real
    assert [entry.source_text for entry in store.find()] == ["keep"]
    assert add(store, "later", "luego") is not None  # And it carries on afterwards.
