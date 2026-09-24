"""SQLite — trwałe przechowywanie. Plik asystent.db obok kodu, dane nie giną.
Stdlib sqlite3, bez ORM (mało tabel, nie trzeba ciężkiej zależności).
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "asystent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    date       TEXT NOT NULL,              -- YYYY-MM-DD
    start_time TEXT,                       -- HH:MM
    end_time   TEXT,
    priority   TEXT DEFAULT 'normal',      -- low | normal | high
    notes      TEXT DEFAULT '',
    remind_at  TEXT,                       -- ISO datetime lub NULL
    done       INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    kind     TEXT DEFAULT 'other',         -- job | freelance | passive | other
    expected REAL DEFAULT 0,               -- planowany miesięczny przychód
    note     TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transactions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,               -- income | expense
    amount    REAL NOT NULL,
    category  TEXT DEFAULT '',
    source_id INTEGER,                     -- powiązanie z sources (dla income)
    date      TEXT NOT NULL,               -- YYYY-MM-DD
    note      TEXT DEFAULT '',
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE SET NULL
);

-- Nauka: zeszyty (katalogi) i notatki blokowe
CREATE TABLE IF NOT EXISTS notebooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    color       TEXT DEFAULT '#8b7cff',
    purpose     TEXT DEFAULT '',
    description TEXT DEFAULT '',
    teacher     TEXT DEFAULT '',
    email       TEXT DEFAULT '',
    room        TEXT DEFAULT '',
    theme       TEXT DEFAULT 'midnight',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL,
    title       TEXT DEFAULT 'Bez tytułu',
    purpose     TEXT DEFAULT '',
    description TEXT DEFAULT '',
    content     TEXT DEFAULT '[]',         -- JSON: lista bloków
    group_id    INTEGER,                   -- przynależność do grupy (NULL = luzem)
    created_at  TEXT,
    updated_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);

-- Nauka: grupy notatek wewnątrz zeszytu (ręczne kategorie)
CREATE TABLE IF NOT EXISTS note_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    notebook_id INTEGER NOT NULL,
    name        TEXT NOT NULL,
    position    INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE
);
"""

# kolumny dokładane do istniejącej bazy (SQLite nie ma ADD COLUMN IF NOT EXISTS)
_MIGRATIONS = [
    ("notebooks", "purpose",     "ALTER TABLE notebooks ADD COLUMN purpose TEXT DEFAULT ''"),
    ("notebooks", "description", "ALTER TABLE notebooks ADD COLUMN description TEXT DEFAULT ''"),
    ("notebooks", "teacher",     "ALTER TABLE notebooks ADD COLUMN teacher TEXT DEFAULT ''"),
    ("notebooks", "email",       "ALTER TABLE notebooks ADD COLUMN email TEXT DEFAULT ''"),
    ("notebooks", "room",        "ALTER TABLE notebooks ADD COLUMN room TEXT DEFAULT ''"),
    ("notebooks", "theme",       "ALTER TABLE notebooks ADD COLUMN theme TEXT DEFAULT 'midnight'"),
    ("notes",     "purpose",     "ALTER TABLE notes ADD COLUMN purpose TEXT DEFAULT ''"),
    ("notes",     "description", "ALTER TABLE notes ADD COLUMN description TEXT DEFAULT ''"),
    ("notes",     "created_at",  "ALTER TABLE notes ADD COLUMN created_at TEXT"),
    ("notes",     "group_id",    "ALTER TABLE notes ADD COLUMN group_id INTEGER"),
    ("tasks",     "kind",        "ALTER TABLE tasks ADD COLUMN kind TEXT DEFAULT ''"),
    ("tasks",     "note_id",     "ALTER TABLE tasks ADD COLUMN note_id INTEGER"),
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in _MIGRATIONS:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(ddl)
        # uzupełnij datę utworzenia dla istniejących notatek
        conn.execute("UPDATE notes SET created_at=updated_at WHERE created_at IS NULL")


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


if __name__ == "__main__":
    init_db()
    print(f"OK — baza gotowa: {DB_PATH}")
