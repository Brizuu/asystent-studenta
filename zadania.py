"""Zadania: nazwane listy z priorytetem, zadania z checklistą, powiązaniem z notatką/zeszytem
i opcjonalnym przypomnieniem, które trafia do harmonogramu jako wpis typu „Zadanie”."""
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()
PRIOS = {"high", "normal", "low"}

def init():
    """Tabele modułu (idempotentne) — przy starcie i dla każdej nowej bazy (wersja webowa)."""
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
        # Kanban: Do zrobienia / W trakcie / Zrobione (zrobione = done)
        if "status" not in [r[1] for r in _c.execute("PRAGMA table_info(todo_items)")]:
            _c.execute("ALTER TABLE todo_items ADD COLUMN status TEXT DEFAULT 'todo'")
            _c.execute("UPDATE todo_items SET status='done' WHERE done=1")
        if not _c.execute("SELECT 1 FROM todo_lists LIMIT 1").fetchone():
            _c.execute("INSERT INTO todo_lists(name, priority, color) VALUES('Moje zadania', 'normal', '#8b7cff')")


init()


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
    status: str | None = None
    position: int | None = None


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
    if "status" in f:   # kolumna Kanbana; „Zrobione” = odhaczone
        if f["status"] not in ("todo", "doing", "done"):
            f["status"] = "todo"
        f["done"] = f["status"] == "done"
    elif "done" in f:
        f["status"] = "done" if f["done"] else "todo"
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


# ---------- tworzenie kart (Kanban) z notatki: checklisty bez AI albo plan zadań od Gemini ----------
class FromNote(BaseModel):
    note_id: int
    list_id: int | None = None
    instructions: str = ""
    ai: bool = False


def _target_list(c, lid):
    if lid and c.execute("SELECT 1 FROM todo_lists WHERE id=?", (lid,)).fetchone():
        return lid
    return c.execute("SELECT id FROM todo_lists ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id LIMIT 1").fetchone()[0]


def _insert_items(list_id: int, note_id: int, items: list) -> int:
    n = 0
    with get_conn() as c:
        lid = _target_list(c, list_id)
        have = {r[0].strip().lower() for r in c.execute("SELECT title FROM todo_items WHERE list_id=? AND done=0", (lid,))}
        for it in items:
            p = TodoItem(list_id=lid, title=str(it.get("title") or "")[:200], notes=str(it.get("notes") or "")[:4000],
                         priority=it.get("priority") if it.get("priority") in PRIOS else "normal", note_id=note_id,
                         status=it.get("status") if it.get("status") in ("todo", "doing", "done") else "todo",
                         remind_date=it.get("remind_date") or None,
                         checks=[Check(text=str(x)[:200]) for x in (it.get("checks") or []) if str(x).strip()][:30])
            if not (p.title or "").strip() or p.title.strip().lower() in have:
                continue
            try:
                f, checks = _clean_item(p, False)
            except HTTPException:
                f, checks = _clean_item(p.model_copy(update={"remind_date": None}), False)   # zła data od AI — bez terminu
            iid = c.execute(f"INSERT INTO todo_items({','.join(f)}) VALUES({','.join('?' * len(f))})", tuple(f.values())).lastrowid
            _save_checks(c, iid, checks)
            _sync_calendar(c, iid)
            have.add(p.title.strip().lower())
            n += 1
    return n


@router.post("/api/todo/from-note")
def from_note(p: FromNote):
    import json as _json
    import re as _re
    from quiz import load_note, parse_json_list, _html_text
    title, text = load_note(p.note_id)
    if not p.ai:
        # bez AI: punkty z list zadań (checklist) i list punktowanych w notatce
        with get_conn() as c:
            raw = c.execute("SELECT content FROM notes WHERE id=?", (p.note_id,)).fetchone()["content"] or "[]"
        items = []
        for b in _json.loads(raw):
            if b.get("type") == "checklist":
                items += [{"title": i.get("text"), "status": "done" if i.get("done") else "todo"} for i in b.get("items") or [] if (i.get("text") or "").strip()]
            elif b.get("type") in ("text", "callout", "toggle"):
                items += [{"title": _html_text(li)} for li in _re.findall(r"<li[^>]*>(.*?)</li>", b.get("html") or "", _re.S) if _html_text(li)]
        if not items:
            raise HTTPException(400, "W tej notatce nie ma list ani checklist — użyj opcji z AI.")
        return {"added": _insert_items(p.list_id, p.note_id, items)}
    from server import gemini, GeminiError, _gemini_key
    if not _gemini_key():
        raise HTTPException(400, "Brak klucza Gemini. Dodaj go w Ustawieniach (⚙ na dole paska menu).")
    if len(text) < 20:
        raise HTTPException(400, "Notatka jest pusta — nie ma z czego zrobić zadań.")
    ins = p.instructions.strip()[:1500]
    prompt = ("Na podstawie notatki studenta (np. opis projektu, sylabus, wymagania na zaliczenie, ustalenia z zajęć) przygotuj "
              "konkretny plan pracy jako karty zadań na tablicę Kanban.\n"
              + (f"WSKAZÓWKI STUDENTA (najważniejsze): „{ins}”\n" if ins else "") +
              f"Dzisiaj jest {date.today().isoformat()}. Zasady:\n"
              "- 4–15 zadań, każde wykonalne i zaczynające się od czasownika (np. „Przygotować…”, „Powtórzyć…”).\n"
              "- Jeśli w notatce są terminy, ustaw remind_date (YYYY-MM-DD); inaczej pomiń to pole.\n"
              "- priority: high | normal | low. Opcjonalnie checks: lista 2–6 kroków.\n"
              'Zwróć WYŁĄCZNIE tablicę JSON: [{"title": "...", "notes": "...", "priority": "normal", "remind_date": "2026-11-20", "checks": ["..."]}]\n\n'
              f"Notatka „{title}”:\n{text[:100_000]}")
    try:
        out = gemini([{"text": prompt}], max_tokens=6000, temperature=0.4, timeout=150, kind="tasks", json_mode=True)
    except GeminiError as e:
        if e.status in (429, 503):
            raise HTTPException(503, "Model Gemini jest chwilowo przeciążony albo wyczerpano limit. Spróbuj ponownie za chwilę.")
        raise HTTPException(400 if e.status == 400 else 502, e.msg)
    items = [x for x in parse_json_list(out) if isinstance(x, dict)]
    return {"added": _insert_items(p.list_id, p.note_id, items)}
