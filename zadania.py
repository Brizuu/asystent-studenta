"""Zadania: nazwane listy z priorytetem, zadania z checklistą, powiązaniem z notatką/zeszytem
i opcjonalnym przypomnieniem, które trafia do harmonogramu jako wpis typu „Zadanie”."""
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()
PRIOS = {"high", "normal", "low"}

with get_conn() as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS todo_lists (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        priority   TEXT DEFAULT 'normal',
        color      TEXT DEFAULT '#8b7cff',
        position   INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS todo_items (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        list_id      INTEGER NOT NULL REFERENCES todo_lists(id) ON DELETE CASCADE,
        title        TEXT NOT NULL,
        notes        TEXT DEFAULT '',
        priority     TEXT DEFAULT 'normal',
        done         INTEGER DEFAULT 0,
        done_at      TEXT,
        note_id      INTEGER,               -- powiązana notatka
        notebook_id  INTEGER,               -- albo cały zeszyt
        remind_date  TEXT,                  -- YYYY-MM-DD
        remind_time  TEXT,                  -- HH:MM
        anchor_id    INTEGER,               -- „przy wpisie” z harmonogramu
        task_id      INTEGER,               -- wpis w harmonogramie utworzony dla przypomnienia
        position     INTEGER DEFAULT 0,
        created_at   TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS todo_checks (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id  INTEGER NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,
        text     TEXT NOT NULL,
        done     INTEGER DEFAULT 0,
        position INTEGER DEFAULT 0
    );
    """)
    if not _c.execute("SELECT 1 FROM todo_lists LIMIT 1").fetchone():
        _c.execute("INSERT INTO todo_lists(name, priority, color) VALUES('Moje zadania', 'normal', '#8b7cff')")


def _sync_calendar(c, item_id: int):
    """Przypomnienie zadania = wpis w harmonogramie (kind „Zadanie”), trzymany w zgodzie z zadaniem."""
    it = c.execute("SELECT i.*, l.name list_name FROM todo_items i JOIN todo_lists l ON l.id=i.list_id WHERE i.id=?",
                   (item_id,)).fetchone()
    if not it:
        return
    tid = it["task_id"]
    if tid and not c.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone():
        tid = None
    if not it["remind_date"]:
        if tid:
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        c.execute("UPDATE todo_items SET task_id=NULL WHERE id=?", (item_id,))
        return
    t = it["remind_time"] or None
    remind = f"{it['remind_date']}T{t}" if t else None
    vals = (it["title"], it["remind_date"], t, it["priority"], f"Zadanie z listy „{it['list_name']}”",
            remind, int(it["done"]), "Zadanie", it["note_id"])
    if tid:
        c.execute("UPDATE tasks SET title=?, date=?, start_time=?, priority=?, notes=?, remind_at=?, done=?, kind=?, note_id=? WHERE id=?",
                  (*vals, tid))
    else:
        tid = c.execute("INSERT INTO tasks(title, date, start_time, priority, notes, remind_at, done, kind, note_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", vals).lastrowid
        c.execute("UPDATE todo_items SET task_id=? WHERE id=?", (tid, item_id))


@router.get("/api/todo")
def todo_all():
    with get_conn() as c:
        # odhaczone w planie → odhaczone w zadaniach
        c.execute("""UPDATE todo_items SET done=1, done_at=datetime('now')
                     WHERE done=0 AND task_id IN (SELECT id FROM tasks WHERE done=1)""")
        lists = [dict(r) for r in c.execute("""
            SELECT l.*, (SELECT COUNT(*) FROM todo_items i WHERE i.list_id=l.id AND i.done=0) open,
                        (SELECT COUNT(*) FROM todo_items i WHERE i.list_id=l.id) total
            FROM todo_lists l
            ORDER BY CASE l.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, l.position, l.id""")]
        items = [dict(r) for r in c.execute("""
            SELECT i.*, n.title note_title, n.notebook_id note_nb, nb.name notebook_name,
                   a.title anchor_title, a.start_time anchor_time
            FROM todo_items i
            LEFT JOIN notes n ON n.id=i.note_id
            LEFT JOIN notebooks nb ON nb.id=COALESCE(i.notebook_id, n.notebook_id)
            LEFT JOIN tasks a ON a.id=i.anchor_id
            ORDER BY i.done, CASE i.priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     i.remind_date IS NULL, i.remind_date, i.remind_time, i.position, i.id""")]
        checks: dict = {}
        for r in c.execute("SELECT * FROM todo_checks ORDER BY position, id"):
            checks.setdefault(r["item_id"], []).append({"id": r["id"], "text": r["text"], "done": bool(r["done"])})
    today = date.today().isoformat()
    for it in items:
        it["done"] = bool(it["done"])
        it["checks"] = checks.get(it["id"], [])
        it["overdue"] = bool(it["remind_date"] and not it["done"] and it["remind_date"] < today)
    return {"lists": lists, "items": items, "today": today}


class TodoList(BaseModel):
    name: str | None = None
    priority: str | None = None
    color: str | None = None
    position: int | None = None


def _clean_list(p: TodoList, partial: bool) -> dict:
    f = p.model_dump(exclude_unset=True, exclude_none=True)
    if "name" in f or not partial:
        f["name"] = (f.get("name") or "").strip()[:60]
        if not f["name"]:
            raise HTTPException(400, "Podaj nazwę listy.")
    if "priority" in f and f["priority"] not in PRIOS:
        f["priority"] = "normal"
    if "color" in f and not (f["color"].startswith("#") and len(f["color"]) == 7):
        f["color"] = "#8b7cff"
    return f


@router.post("/api/todo/lists")
def add_list(p: TodoList):
    f = _clean_list(p, False)
    with get_conn() as c:
        return {"id": c.execute(f"INSERT INTO todo_lists({','.join(f)}) VALUES({','.join('?' * len(f))})",
                                tuple(f.values())).lastrowid}


@router.patch("/api/todo/lists/{lid}")
def edit_list(lid: int, p: TodoList):
    f = _clean_list(p, True)
    if f:
        with get_conn() as c:
            c.execute(f"UPDATE todo_lists SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), lid))
            for r in c.execute("SELECT id FROM todo_items WHERE list_id=?", (lid,)).fetchall():
                _sync_calendar(c, r["id"])   # nazwa listy jest w opisie wpisu
    return {"ok": True}


@router.delete("/api/todo/lists/{lid}")
def del_list(lid: int):
    with get_conn() as c:
        if c.execute("SELECT COUNT(*) FROM todo_lists").fetchone()[0] <= 1:
            raise HTTPException(400, "Musi zostać co najmniej jedna lista.")
        for r in c.execute("SELECT task_id FROM todo_items WHERE list_id=? AND task_id IS NOT NULL", (lid,)).fetchall():
            c.execute("DELETE FROM tasks WHERE id=?", (r["task_id"],))
        c.execute("DELETE FROM todo_checks WHERE item_id IN (SELECT id FROM todo_items WHERE list_id=?)", (lid,))
        c.execute("DELETE FROM todo_items WHERE list_id=?", (lid,))
        c.execute("DELETE FROM todo_lists WHERE id=?", (lid,))
    return {"ok": True}


class Check(BaseModel):
    text: str
    done: bool = False


class TodoItem(BaseModel):
    list_id: int | None = None
    title: str | None = None
    notes: str | None = None
    priority: str | None = None
    done: bool | None = None
    note_id: int | None = None
    notebook_id: int | None = None
    remind_date: str | None = None
    remind_time: str | None = None
    anchor_id: int | None = None
    checks: list[Check] | None = None


LINK_FIELDS = ("note_id", "notebook_id", "remind_date", "remind_time", "anchor_id")


def _clean_item(p: TodoItem, partial: bool) -> tuple[dict, list | None]:
    f = p.model_dump(exclude_unset=True)
    checks = f.pop("checks", None)
    f = {k: v for k, v in f.items() if v is not None or k in LINK_FIELDS}   # pola powiązań można wyczyścić (null)
    if "title" in f or not partial:
        f["title"] = (f.get("title") or "").strip()[:200]
        if not f["title"]:
            raise HTTPException(400, "Podaj treść zadania.")
    if "notes" in f:
        f["notes"] = (f["notes"] or "")[:4000]
    if "priority" in f and f["priority"] not in PRIOS:
        f["priority"] = "normal"
    if f.get("remind_date"):
        try:
            f["remind_date"] = date.fromisoformat(f["remind_date"]).isoformat()
        except ValueError:
            raise HTTPException(400, "Zła data przypomnienia.")
    if f.get("remind_time"):
        try:
            f["remind_time"] = datetime.strptime(f["remind_time"], "%H:%M").strftime("%H:%M")
        except ValueError:
            raise HTTPException(400, "Zła godzina przypomnienia.")
    if "done" in f:
        f["done"] = int(bool(f["done"]))
        f["done_at"] = datetime.now().isoformat(timespec="seconds") if f["done"] else None
    if checks is not None:
        checks = [{"text": ch["text"].strip()[:200], "done": int(bool(ch.get("done")))} for ch in checks if (ch.get("text") or "").strip()][:100]
    return f, checks


def _save_checks(c, item_id: int, checks: list | None):
    if checks is None:
        return
    c.execute("DELETE FROM todo_checks WHERE item_id=?", (item_id,))
    for i, ch in enumerate(checks):
        c.execute("INSERT INTO todo_checks(item_id, text, done, position) VALUES(?,?,?,?)", (item_id, ch["text"], ch["done"], i))


@router.post("/api/todo/items")
def add_item(p: TodoItem):
    f, checks = _clean_item(p, False)
    with get_conn() as c:
        if not f.get("list_id"):
            f["list_id"] = c.execute("SELECT id FROM todo_lists ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id LIMIT 1").fetchone()[0]
        elif not c.execute("SELECT 1 FROM todo_lists WHERE id=?", (f["list_id"],)).fetchone():
            raise HTTPException(404, "Nie ma takiej listy.")
        iid = c.execute(f"INSERT INTO todo_items({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values())).lastrowid
        _save_checks(c, iid, checks)
        _sync_calendar(c, iid)
    return {"id": iid}


@router.patch("/api/todo/items/{iid}")
def edit_item(iid: int, p: TodoItem):
    f, checks = _clean_item(p, True)
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM todo_items WHERE id=?", (iid,)).fetchone():
            raise HTTPException(404, "Nie ma takiego zadania.")
        if f:
            c.execute(f"UPDATE todo_items SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), iid))
        _save_checks(c, iid, checks)
        _sync_calendar(c, iid)
    return {"ok": True}


@router.patch("/api/todo/checks/{cid}")
def toggle_check(cid: int, done: bool):
    with get_conn() as c:
        c.execute("UPDATE todo_checks SET done=? WHERE id=?", (int(done), cid))
    return {"ok": True}


@router.delete("/api/todo/items/{iid}")
def del_item(iid: int):
    with get_conn() as c:
        r = c.execute("SELECT task_id FROM todo_items WHERE id=?", (iid,)).fetchone()
        if r and r["task_id"]:
            c.execute("DELETE FROM tasks WHERE id=?", (r["task_id"],))
        c.execute("DELETE FROM todo_checks WHERE item_id=?", (iid,))
        c.execute("DELETE FROM todo_items WHERE id=?", (iid,))
    return {"ok": True}
