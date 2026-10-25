import sqlite3
import time


class Store:
    """Notes and reminders in SQLite. Each row belongs to one chat ("telegram:123")."""

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY,
                chat_key TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY,
                chat_key TEXT NOT NULL,
                text TEXT NOT NULL,
                due_at REAL NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS reminders_due ON reminders (sent, due_at);
            CREATE TABLE IF NOT EXISTS inbox (
                id INTEGER PRIMARY KEY,
                chat_key TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )

    def add_note(self, chat_key: str, text: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO notes (chat_key, text, created_at) VALUES (?, ?, ?)",
                (chat_key, text, time.time()),
            )

    def list_notes(self, chat_key: str) -> list[tuple[float, str]]:
        return self.db.execute(
            "SELECT created_at, text FROM notes WHERE chat_key = ? ORDER BY id DESC LIMIT 50",
            (chat_key,),
        ).fetchall()

    def add_reminder(self, chat_key: str, text: str, due_at: float) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO reminders (chat_key, text, due_at) VALUES (?, ?, ?)",
                (chat_key, text, due_at),
            )

    def due_reminders(self, now: float) -> list[tuple[int, str, str]]:
        return self.db.execute(
            "SELECT id, chat_key, text FROM reminders WHERE sent = 0 AND due_at <= ?",
            (now,),
        ).fetchall()

    def mark_sent(self, reminder_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))

    def add_inbox(self, chat_key: str, text: str) -> None:
        """Queue a message for the app, which polls for it."""
        with self.db:
            self.db.execute(
                "INSERT INTO inbox (chat_key, text, created_at) VALUES (?, ?, ?)",
                (chat_key, text, time.time()),
            )

    def inbox_after(self, chat_key: str, after_id: int) -> list[tuple[int, str, float]]:
        return self.db.execute(
            "SELECT id, text, created_at FROM inbox WHERE chat_key = ? AND id > ? ORDER BY id LIMIT 100",
            (chat_key, after_id),
        ).fetchall()
