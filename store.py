import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


@contextmanager
def single_instance(directory):
    with (directory / "bot.lock").open("a+b") as file:
        file.seek(0, 2)
        if not file.tell():
            file.write(b"\x00")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("Этот бот уже запущен из данной папки.") from None
        yield


class Store:
    def __init__(self, directory=DATA):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "bot.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS routes (
                    chat INTEGER NOT NULL, forum INTEGER NOT NULL,
                    since INTEGER NOT NULL, checked INTEGER NOT NULL,
                    sweep INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (chat, forum)
                );
                CREATE TABLE IF NOT EXISTS topics (
                    chat INTEGER NOT NULL, name TEXT NOT NULL, topic INTEGER NOT NULL,
                    PRIMARY KEY (chat, name)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, chat INTEGER NOT NULL, forum INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL, kind TEXT NOT NULL, event_time INTEGER NOT NULL,
                    created INTEGER NOT NULL, bumped INTEGER NOT NULL,
                    parts TEXT, sent INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_try INTEGER NOT NULL DEFAULT 0, error TEXT,
                    UNIQUE (chat, thread_id, kind, event_time)
                );
                CREATE INDEX IF NOT EXISTS delivery ON outbox (chat, state, next_try, event_time);
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def settings(self):
        with self.db() as db:
            return {
                row["name"]: json.loads(row["value"])
                for row in db.execute("SELECT * FROM settings")
            }

    def save(self, **settings):
        with self.db() as db:
            db.executemany(
                "INSERT OR REPLACE INTO settings VALUES (?, ?)",
                (
                    (name, json.dumps(value, ensure_ascii=False))
                    for name, value in settings.items()
                ),
            )

    def delete_settings(self, *names):
        with self.db() as db:
            db.executemany(
                "DELETE FROM settings WHERE name=?", ((name,) for name in names)
            )

    def route(self, chat, forum, now):
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO routes (chat, forum, since, checked) VALUES (?, ?, ?, ?)",
                (chat, forum, now, now),
            )
            return db.execute(
                "SELECT * FROM routes WHERE chat=? AND forum=?", (chat, forum)
            ).fetchone()

    def advance(self, chat, forum, *, checked, sweep):
        with self.db() as db:
            db.execute(
                "UPDATE routes SET checked=?, sweep=? WHERE chat=? AND forum=?",
                (checked, sweep, chat, forum),
            )

    def topic(self, chat, forum, *, legacy_name=""):
        key = f"forum:{forum}"
        with self.db() as db:
            if legacy_name:
                db.execute(
                    "UPDATE OR IGNORE topics SET name=? WHERE chat=? AND name=?",
                    (key, chat, legacy_name),
                )
            row = db.execute(
                "SELECT topic FROM topics WHERE chat=? AND name=?", (chat, key)
            ).fetchone()
            return row["topic"] if row else None

    def save_topic(self, chat, forum, topic):
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO topics VALUES (?, ?, ?)",
                (chat, f"forum:{forum}", topic),
            )

    def forget_topic(self, chat, forum):
        with self.db() as db:
            db.execute(
                "DELETE FROM topics WHERE chat=? AND name=?", (chat, f"forum:{forum}")
            )

    def enqueue(self, chat, forum, events):
        with self.db() as db:
            db.executemany(
                """
                INSERT INTO outbox (chat, forum, thread_id, kind, event_time, created, bumped)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (chat, thread_id, kind, event_time) DO NOTHING
            """,
                ((chat, forum, *event) for event in events),
            )

    def pending(self, chat, now):
        with self.db() as db:
            return db.execute(
                """
                SELECT * FROM outbox WHERE chat=? AND state='pending' AND next_try<=?
                ORDER BY event_time, id LIMIT 16
            """,
                (chat, now),
            ).fetchall()

    def reroute(self, event_id, forum):
        with self.db() as db:
            db.execute(
                """
                UPDATE outbox SET forum=?, parts=NULL, sent=0, attempts=0, error=NULL WHERE id=?
            """,
                (forum, event_id),
            )

    def save_parts(self, event_id, parts):
        with self.db() as db:
            db.execute(
                "UPDATE outbox SET parts=? WHERE id=?",
                (json.dumps(parts, ensure_ascii=False), event_id),
            )

    def sent(self, event_id, count, done):
        with self.db() as db:
            db.execute(
                """
                UPDATE outbox SET sent=?, state=?, parts=CASE WHEN ? THEN NULL ELSE parts END,
                attempts=0, next_try=0, error=NULL WHERE id=?
            """,
                (count, "sent" if done else "pending", done, event_id),
            )

    def fail(self, event_id, message, next_try, *, skipped=False):
        with self.db() as db:
            db.execute(
                """
                UPDATE outbox SET attempts=attempts+1, error=?, next_try=?, state=?,
                parts=CASE WHEN ? THEN NULL ELSE parts END WHERE id=?
            """,
                (
                    message,
                    next_try,
                    "skipped" if skipped else "pending",
                    skipped,
                    event_id,
                ),
            )

    def stats(self, chat):
        with self.db() as db:
            counts = {
                row["state"]: row["n"]
                for row in db.execute(
                    "SELECT state, count(*) AS n FROM outbox WHERE chat=? GROUP BY state",
                    (chat,),
                )
            }
            error = db.execute(
                """
                SELECT error FROM outbox WHERE chat=? AND error IS NOT NULL ORDER BY next_try DESC LIMIT 1
            """,
                (chat,),
            ).fetchone()
        return counts, error["error"] if error else ""
