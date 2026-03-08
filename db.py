"""
SQLite database layer for News Accountability Bot.
"""

import sqlite3
import threading
from typing import Optional


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT    NOT NULL,
                year        INTEGER NOT NULL,
                owed        REAL    NOT NULL DEFAULT 0.0,
                PRIMARY KEY (chat_id, user_id, year)
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                date        TEXT    NOT NULL,   -- YYYY-MM-DD
                url         TEXT    NOT NULL,
                year        INTEGER NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uix_submission_day
                ON submissions(chat_id, user_id, date);

            CREATE TABLE IF NOT EXISTS defaults (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                date        TEXT    NOT NULL,
                year        INTEGER NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uix_default_day
                ON defaults(chat_id, user_id, date);
        """)
        conn.commit()

    # ── Members ──────────────────────────────────────────────────────────────

    def register_member(self, chat_id: int, user_id: int, username: str, year: int) -> bool:
        """Register a member. Returns True if newly added, False if already exists."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO members (chat_id, user_id, username, year, owed) VALUES (?, ?, ?, ?, 0.0)",
                (chat_id, user_id, username, year),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Update username in case it changed
            conn.execute(
                "UPDATE members SET username = ? WHERE chat_id = ? AND user_id = ? AND year = ?",
                (username, chat_id, user_id, year),
            )
            conn.commit()
            return False

    def is_member(self, chat_id: int, user_id: int, year: int) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM members WHERE chat_id = ? AND user_id = ? AND year = ?",
            (chat_id, user_id, year),
        ).fetchone()
        return row is not None

    def get_members(self, chat_id: int, year: int) -> list[dict]:
        rows = self._conn().execute(
            "SELECT user_id, username, owed FROM members WHERE chat_id = ? AND year = ? ORDER BY username",
            (chat_id, year),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_member_owed(self, chat_id: int, user_id: int, year: int) -> float:
        row = self._conn().execute(
            "SELECT owed FROM members WHERE chat_id = ? AND user_id = ? AND year = ?",
            (chat_id, user_id, year),
        ).fetchone()
        return row["owed"] if row else 0.0

    def add_owed(self, chat_id: int, user_id: int, amount: float, year: int):
        self._conn().execute(
            "UPDATE members SET owed = owed + ? WHERE chat_id = ? AND user_id = ? AND year = ?",
            (amount, chat_id, user_id, year),
        )
        self._conn().commit()

    def set_owed(self, chat_id: int, user_id: int, amount: float, year: int):
        self._conn().execute(
            "UPDATE members SET owed = ? WHERE chat_id = ? AND user_id = ? AND year = ?",
            (amount, chat_id, user_id, year),
        )
        self._conn().commit()

    def get_all_chats(self) -> list[int]:
        rows = self._conn().execute(
            "SELECT DISTINCT chat_id FROM members"
        ).fetchall()
        return [r["chat_id"] for r in rows]

    # ── Submissions ───────────────────────────────────────────────────────────

    def record_submission(self, chat_id: int, user_id: int, date: str, url: str, year: int):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO submissions (chat_id, user_id, date, url, year) VALUES (?, ?, ?, ?, ?)",
                (chat_id, user_id, date, url, year),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already submitted today

    def has_submitted_today(self, chat_id: int, user_id: int, date: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM submissions WHERE chat_id = ? AND user_id = ? AND date = ?",
            (chat_id, user_id, date),
        ).fetchone()
        return row is not None

    def get_submissions_today(self, chat_id: int, date: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT user_id, url FROM submissions WHERE chat_id = ? AND date = ?",
            (chat_id, date),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_user_history(self, chat_id: int, user_id: int, year: int) -> list[dict]:
        """Return a day-by-day log for a user: submitted or defaulted."""
        submitted_rows = self._conn().execute(
            "SELECT date, 1 as submitted FROM submissions WHERE chat_id = ? AND user_id = ? AND year = ?",
            (chat_id, user_id, year),
        ).fetchall()
        default_rows = self._conn().execute(
            "SELECT date, 0 as submitted FROM defaults WHERE chat_id = ? AND user_id = ? AND year = ?",
            (chat_id, user_id, year),
        ).fetchall()

        combined = {r["date"]: {"date": r["date"], "submitted": True} for r in submitted_rows}
        for r in default_rows:
            if r["date"] not in combined:
                combined[r["date"]] = {"date": r["date"], "submitted": False}

        return sorted(combined.values(), key=lambda x: x["date"])

    # ── Defaults ──────────────────────────────────────────────────────────────

    def record_default(self, chat_id: int, user_id: int, date: str, year: int):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO defaults (chat_id, user_id, date, year) VALUES (?, ?, ?, ?)",
                (chat_id, user_id, date, year),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
