# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Text tab's translation history: every finished translation, kept in a small SQLite database.

Nothing leaves the computer, and the file is private to the user. The database carries a schema version, so a later
release can add columns without losing anyone's history (see ``_MIGRATIONS``); a damaged file is set aside as
history.db.broken and replaced by a fresh one rather than blocking the app; and a database written by a *newer*
release is left strictly alone (history is unavailable, nothing is changed).

History is a convenience, never a reason for the app to fail: every method catches database errors, logs them, and
answers as if there were nothing to report (None, an empty list, zero, False). Use it from one thread: the GTK main
loop, where translations finish.
"""

from __future__ import annotations

import functools
import logging
import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .paths import data_dir

log = logging.getLogger(__name__)

HISTORY_FILE_NAME = "history.db"
# How many translations are kept unless the user says otherwise.
DEFAULT_LIMIT = 1000
MIN_LIMIT, MAX_LIMIT = 10, 100_000
DEFAULT_PAGE_SIZE = 50


@dataclass(frozen=True)
class HistoryEntry:
    """One remembered translation.

    Attributes:
        id: Database key; entries are ordered by it, newest last.
        created_at: When the translation finished, in seconds since the epoch (UTC).
        source_language: As the picker showed it: a language name, or "Auto-detect".
        target_language: The language translated into.
        source_text: The text that was translated.
        translated_text: The translation.
        model: The translation model's file name.
        seconds: How long the translation took.
    """

    id: int
    created_at: float
    source_language: str
    target_language: str
    source_text: str
    translated_text: str
    model: str = ""
    seconds: float = 0.0


# --- Searching ---

# Arabic letters that are routinely typed interchangeably: alef wasla (as alef), alef maqsura (as ya) and ta marbuta
# (as ha). The hamza forms of alef need no entry (they decompose into a plain alef and a mark, dropped by
# search_fold), and tatweel, the stretching character, is removed.
_ARABIC_FOLD = str.maketrans({"ٱ": "ا", "ى": "ي", "ة": "ه", "ـ": None})


def search_fold(text: str) -> str:
    """Return *text* in the form in which it is compared when searching.

    The result has no case, accents or Arabic vowel marks, and the interchangeable Arabic letters are unified, so
    "cafe" finds "Café" and "مدرسه" finds "مَدْرَسَة".
    """
    decomposed = unicodedata.normalize("NFKD", text)
    unmarked = "".join(char for char in decomposed if not unicodedata.combining(char))
    return unmarked.casefold().translate(_ARABIC_FOLD)


def _like_pattern(term: str) -> str:
    """Return *term* as a LIKE pattern that matches it anywhere, with the wildcard characters taken literally."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# SQL expression that joins everything a search looks at into one folded string (search_fold is registered on the
# connection in HistoryStore._prepare).
_HAYSTACK = "search_fold(source_text || ' ' || translated_text || ' ' || source_language || ' ' || target_language)"


def _where(query: str, before_id: int | None) -> tuple[str, list]:
    """Return the WHERE clause (and its values) for a search.

    Every word of the query must appear somewhere in an entry's texts or languages, in any order. *before_id*
    restricts the result to older entries, for paging.
    """
    conditions: list[str] = []
    values: list = []
    for term in search_fold(query).split():
        conditions.append(f"{_HAYSTACK} LIKE ? ESCAPE '\\'")
        values.append(_like_pattern(term))
    if before_id is not None:
        conditions.append("id < ?")
        values.append(before_id)
    return (" WHERE " + " AND ".join(conditions)) if conditions else "", values


# --- Schema ---

def _create_translations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE translations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at       REAL    NOT NULL,
            source_language  TEXT    NOT NULL,
            target_language  TEXT    NOT NULL,
            source_text      TEXT    NOT NULL,
            translated_text  TEXT    NOT NULL,
            model            TEXT    NOT NULL DEFAULT '',
            seconds          REAL    NOT NULL DEFAULT 0
        )
        """
    )


# Step n takes a database from version n to version n + 1. Append a step to change the schema; never edit one that
# has shipped. The database's "user_version" says how many steps it has been through.
_MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_create_translations]
SCHEMA_VERSION = len(_MIGRATIONS)


class HistoryUnavailable(Exception):
    """The database cannot be used (it comes from a newer release); history does nothing rather than risk it."""


def _safe(empty: Callable[[], object]):
    """Decorate a store method so that it returns ``empty()`` (and logs why) instead of raising a database error.

    A database that SQLite reports as damaged is set aside and the method is tried once more on a fresh one, so a
    translation that finishes just after the damage is found is not lost.
    """
    def decorate(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            for attempt in (1, 2):
                try:
                    return method(self, *args, **kwargs)
                except (sqlite3.OperationalError, HistoryUnavailable, OSError) as error:
                    # Locked, no space, no permission, or a newer release's file: not damage, so left as it is.
                    log.warning("Translation history: %s failed: %s", method.__name__, error)
                    return empty()
                except sqlite3.DatabaseError as error:
                    # Only the base class means "damaged"; its subclasses (integrity errors and the like) describe
                    # ordinary failures.
                    if type(error) is not sqlite3.DatabaseError:
                        log.warning("Translation history: %s failed: %s", method.__name__, error)
                        return empty()
                    log.warning("Translation history: the database is damaged (%s)", error)
                    if attempt == 2 or not self._set_aside():
                        return empty()
                except sqlite3.Error as error:
                    log.warning("Translation history: %s failed: %s", method.__name__, error)
                    return empty()
        return wrapper
    return decorate


class HistoryStore:
    """The translations kept so far.

    The location is settled when the store is made (the XDG data folder as it is then, or the ``path`` given), so one
    store always means one database. Nothing is created on disk until something is added.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else data_dir() / HISTORY_FILE_NAME
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        """The location of the database file."""
        return self._path

    # --- Writing ---

    @_safe(lambda: None)
    def add(
        self, source_language: str, target_language: str, source_text: str, translated_text: str,
        *, model: str = "", seconds: float = 0.0, keep: int | None = None, now: float | None = None,
    ) -> HistoryEntry | None:
        """Remember a finished translation and return it (None if there was nothing worth keeping).

        Translating the very same text into the very same result twice in a row is one entry whose time is
        refreshed, not two. With ``keep`` set, the oldest entries beyond that many are dropped. ``now`` replaces the
        current time as the entry's timestamp.
        """
        if not source_text.strip() or not translated_text.strip():
            return None
        moment = time.time() if now is None else now
        with self._transaction(self._open()) as connection:
            latest = connection.execute("SELECT * FROM translations ORDER BY id DESC LIMIT 1").fetchone()
            if latest is not None and (
                latest["source_language"], latest["target_language"], latest["source_text"], latest["translated_text"],
            ) == (source_language, target_language, source_text, translated_text):
                connection.execute(
                    "UPDATE translations SET created_at = ?, model = ?, seconds = ? WHERE id = ?",
                    (moment, model, seconds, latest["id"]),
                )
                entry_id = latest["id"]
            else:
                entry_id = connection.execute(
                    "INSERT INTO translations (created_at, source_language, target_language, source_text,"
                    " translated_text, model, seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (moment, source_language, target_language, source_text, translated_text, model, seconds),
                ).lastrowid
            if keep is not None:
                self._trim(connection, keep)
            row = connection.execute("SELECT * FROM translations WHERE id = ?", (entry_id,)).fetchone()
        return _entry(row) if row is not None else None

    @_safe(lambda: False)
    def restore(self, entry: HistoryEntry) -> bool:
        """Put back an entry that was just deleted, in its old place (it keeps its id and time).

        Returns False if an entry with that id exists again.
        """
        with self._transaction(self._open()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO translations (id, created_at, source_language, target_language, source_text,"
                " translated_text, model, seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, entry.created_at, entry.source_language, entry.target_language, entry.source_text,
                 entry.translated_text, entry.model, entry.seconds),
            )
            return cursor.rowcount == 1

    @_safe(lambda: False)
    def delete(self, entry_id: int) -> bool:
        """Delete one entry and return whether it existed."""
        connection = self._existing()
        if connection is None:
            return False
        with self._transaction(connection):
            return connection.execute("DELETE FROM translations WHERE id = ?", (entry_id,)).rowcount == 1

    @_safe(lambda: 0)
    def clear(self) -> int:
        """Forget everything and return how many entries there were.

        The file is compacted afterwards, and the deleted text is overwritten rather than left in the file's free
        space.
        """
        connection = self._existing()
        if connection is None:
            return 0
        with self._transaction(connection):
            removed = connection.execute("DELETE FROM translations").rowcount
        connection.execute("VACUUM")
        return removed

    @_safe(lambda: 0)
    def trim(self, keep: int) -> int:
        """Drop the oldest entries beyond the newest ``keep`` and return how many went."""
        connection = self._existing()
        if connection is None:
            return 0
        with self._transaction(connection):
            return self._trim(connection, keep)

    @staticmethod
    def _trim(connection: sqlite3.Connection, keep: int) -> int:
        """Delete all but the newest *keep* entries; must be called inside a transaction."""
        return connection.execute(
            "DELETE FROM translations WHERE id NOT IN (SELECT id FROM translations ORDER BY id DESC LIMIT ?)",
            (max(keep, 0),),
        ).rowcount

    # --- Reading ---

    @_safe(list)
    def find(
        self, query: str = "", *, limit: int = DEFAULT_PAGE_SIZE, before_id: int | None = None,
    ) -> list[HistoryEntry]:
        """Return the entries, newest first, that contain every word of ``query`` (all of them when it is blank).

        To get the next page, pass the id of the last entry of the previous one as ``before_id``.
        """
        connection = self._existing()
        if connection is None:
            return []
        where, values = _where(query, before_id)
        rows = connection.execute(
            f"SELECT * FROM translations{where} ORDER BY id DESC LIMIT ?", [*values, max(limit, 0)],
        ).fetchall()
        return [_entry(row) for row in rows]

    @_safe(lambda: 0)
    def count(self, query: str = "") -> int:
        """Return how many entries match *query* (all of them when it is blank)."""
        connection = self._existing()
        if connection is None:
            return 0
        where, values = _where(query, None)
        return connection.execute(f"SELECT COUNT(*) FROM translations{where}", values).fetchone()[0]

    @_safe(lambda: None)
    def get(self, entry_id: int) -> HistoryEntry | None:
        """Return the entry with this id, or None if there is none."""
        connection = self._existing()
        row = None if connection is None else connection.execute(
            "SELECT * FROM translations WHERE id = ?", (entry_id,)).fetchone()
        return _entry(row) if row is not None else None

    def close(self) -> None:
        """Close the database connection, if one is open; a later call reopens it as needed."""
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

    # --- Database ---

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """Run the block in one write transaction: commit on success, roll back on any exception."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _open(self) -> sqlite3.Connection:
        """Return the database connection, creating the database the first time it is needed."""
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _existing(self) -> sqlite3.Connection | None:
        """Return the database connection if the database exists, else None.

        Reading, deleting and clearing use this, so they never create the database.
        """
        if self._connection is None and not self.path.exists():
            return None
        return self._open()

    def _connect(self) -> sqlite3.Connection:
        """Open the database, first creating its folder (private to the user) if needed."""
        path = self.path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._prepare(path)

    def _set_aside(self) -> bool:
        """Move a damaged database and its journal files out of the way, as history.db.broken.

        Returns False if the database could not be moved.
        """
        self.close()
        path = self.path
        try:
            os.replace(path, path.with_name(path.name + ".broken"))
        except OSError as error:
            log.warning("Translation history: could not set %s aside: %s", path, error)
            return False
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.unlink(path.with_name(path.name + suffix))
            except OSError:
                pass
        return True

    def _prepare(self, path: Path) -> sqlite3.Connection:
        """Open the database file, creating it if needed, and bring its schema up to date."""
        if not path.exists():
            # Created with mode 0600, so that the file is private from the first byte.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        # Autocommit: transactions are begun by hand (see _transaction), which also makes the schema changes in
        # _migrate part of one.
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("search_fold", 1, search_fold, deterministic=True)
            # Deleted text is wiped, not left behind in the file's free space.
            connection.execute("PRAGMA secure_delete = ON")
            self._migrate(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Run the migration steps this database has not been through yet, all in one transaction.

        Raises:
            HistoryUnavailable: The database was written by a newer release.
        """
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise HistoryUnavailable(f"the database is version {version}, this release understands {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for step in _MIGRATIONS[version:]:
                step(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")


def _entry(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"], created_at=row["created_at"], source_language=row["source_language"],
        target_language=row["target_language"], source_text=row["source_text"],
        translated_text=row["translated_text"], model=row["model"], seconds=row["seconds"],
    )
