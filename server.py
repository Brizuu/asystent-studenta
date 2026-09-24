"""Asystent — backend. FastAPI serwuje SPA (/) i REST API (/api/*).
Uruchom:  uvicorn server:app --reload
"""
import os
import re
import json
import time
import base64
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any
from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import init_db, get_conn, rows

app = FastAPI(title="Asystent")
init_db()

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/logo.png")
def logo():
    return FileResponse("logo.png")


# ---------- modele wejściowe ----------
class Task(BaseModel):
    title: str
    date: str
    start_time: str | None = None
    end_time: str | None = None
    priority: str = "normal"
    notes: str = ""
    remind_at: str | None = None
    kind: str = ""
    note_id: int | None = None


class TaskPatch(BaseModel):
    title: str | None = None
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    priority: str | None = None
    notes: str | None = None
    remind_at: str | None = None
    done: bool | None = None
    kind: str | None = None
    note_id: int | None = None


class Source(BaseModel):
    name: str
    kind: str = "other"
    expected: float = 0
    note: str = ""


class Transaction(BaseModel):
    type: str            # income | expense
    amount: float
    category: str = ""
    source_id: int | None = None
    date: str
    note: str = ""


# ---------- TASKS ----------
@app.get("/api/tasks")
def list_tasks(day: str | None = None, month: str | None = None):
    # dołącz tytuł powiązanej notatki (np. kolokwium → notatka do powtórki)
    q = ("SELECT t.*, n.title note_title, n.notebook_id note_nb FROM tasks t "
         "LEFT JOIN notes n ON n.id=t.note_id ")
    order = " ORDER BY t.date, t.start_time IS NULL, t.start_time"
    with get_conn() as c:
        if day:
            cur = c.execute(q + "WHERE t.date=?" + order, (day,))
        elif month:  # YYYY-MM — widok kalendarza
            cur = c.execute(q + "WHERE t.date LIKE ?" + order, (month + "%",))
        else:
            cur = c.execute(q + order)
        return rows(cur)


@app.post("/api/tasks")
def add_task(t: Task):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO tasks(title,date,start_time,end_time,priority,notes,remind_at,kind,note_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (t.title, t.date, t.start_time, t.end_time, t.priority, t.notes, t.remind_at, t.kind, t.note_id))
        return {"id": cur.lastrowid}


@app.patch("/api/tasks/{tid}")
def patch_task(tid: int, p: TaskPatch):
    # exclude_unset: jawne null czyści pole (np. odpięcie notatki, usunięcie godziny)
    fields = p.model_dump(exclude_unset=True)
    for k in ("title", "date", "done"):
        if fields.get(k, 0) is None:
            del fields[k]
    if not fields:
        return {"updated": 0}
    if "done" in fields:
        fields["done"] = int(fields["done"])
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
    return {"updated": 1}


@app.delete("/api/tasks/{tid}")
def del_task(tid: int):
    with get_conn() as c:
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
    return {"deleted": 1}


# ---------- SOURCES ----------
@app.get("/api/sources")
def list_sources():
    with get_conn() as c:
        return rows(c.execute("SELECT * FROM sources ORDER BY name"))


@app.post("/api/sources")
def add_source(s: Source):
    with get_conn() as c:
        cur = c.execute("INSERT INTO sources(name,kind,expected,note) VALUES(?,?,?,?)",
                        (s.name, s.kind, s.expected, s.note))
        return {"id": cur.lastrowid}


@app.delete("/api/sources/{sid}")
def del_source(sid: int):
    with get_conn() as c:
        c.execute("DELETE FROM sources WHERE id=?", (sid,))
    return {"deleted": 1}


# ---------- TRANSACTIONS ----------
@app.get("/api/transactions")
def list_tx(month: str | None = None):
    with get_conn() as c:
        if month:  # YYYY-MM
            cur = c.execute("SELECT * FROM transactions WHERE date LIKE ? ORDER BY date DESC, id DESC", (month + "%",))
        else:
            cur = c.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC")
        return rows(cur)


@app.post("/api/transactions")
def add_tx(t: Transaction):
    if t.type not in ("income", "expense"):
        raise HTTPException(400, "type musi być income|expense")
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO transactions(type,amount,category,source_id,date,note) VALUES(?,?,?,?,?,?)",
            (t.type, abs(t.amount), t.category, t.source_id, t.date, t.note))
        return {"id": cur.lastrowid}


@app.delete("/api/transactions/{tid}")
def del_tx(tid: int):
    with get_conn() as c:
        c.execute("DELETE FROM transactions WHERE id=?", (tid,))
    return {"deleted": 1}


# ---------- NAUKA: notebooks & notes ----------
class Notebook(BaseModel):
    name: str
    color: str = "#8b7cff"
    purpose: str = ""
    description: str = ""
    teacher: str = ""
    email: str = ""
    room: str = ""
    theme: str = "midnight"


class NotebookPatch(BaseModel):
    name: str | None = None
    color: str | None = None
    purpose: str | None = None
    description: str | None = None
    teacher: str | None = None
    email: str | None = None
    room: str | None = None
    theme: str | None = None


class NoteCreate(BaseModel):
    notebook_id: int
    title: str = "Nowa notatka"
    purpose: str = ""
    description: str = ""


class NotePatch(BaseModel):
    title: str | None = None
    purpose: str | None = None
    description: str | None = None
    content: Any | None = None      # lista bloków (JSON)


@app.get("/api/notebooks")
def list_notebooks():
    with get_conn() as c:
        nbs = rows(c.execute("SELECT * FROM notebooks ORDER BY created_at"))
        counts = {r["notebook_id"]: r["n"] for r in
                  c.execute("SELECT notebook_id, COUNT(*) n FROM notes GROUP BY notebook_id")}
        last = {r["notebook_id"]: r["m"] for r in
                c.execute("SELECT notebook_id, MAX(updated_at) m FROM notes GROUP BY notebook_id")}
    for nb in nbs:
        nb["notes"] = counts.get(nb["id"], 0)
        nb["last_edit"] = last.get(nb["id"]) or nb.get("created_at")
    return nbs


@app.post("/api/notebooks")
def add_notebook(nb: Notebook):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO notebooks(name,color,purpose,description,teacher,email,room,theme) VALUES(?,?,?,?,?,?,?,?)",
            (nb.name, nb.color, nb.purpose, nb.description, nb.teacher, nb.email, nb.room, nb.theme))
        return {"id": cur.lastrowid}


@app.patch("/api/notebooks/{nid}")
def patch_notebook(nid: int, p: NotebookPatch):
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if not fields:
        return {"updated": 0}
    sets = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE notebooks SET {sets} WHERE id=?", (*fields.values(), nid))
    return {"updated": 1}


@app.delete("/api/notebooks/{nid}")
def del_notebook(nid: int):
    with get_conn() as c:
        c.execute("DELETE FROM notebooks WHERE id=?", (nid,))
    return {"deleted": 1}


@app.get("/api/notes/all")
def list_all_notes():
    with get_conn() as c:
        return rows(c.execute("SELECT id,notebook_id,title FROM notes ORDER BY updated_at DESC"))


@app.get("/api/notes")
def list_notes(notebook_id: int):
    with get_conn() as c:
        return rows(c.execute(
            "SELECT id,notebook_id,title,purpose,description,group_id,created_at,updated_at FROM notes WHERE notebook_id=? ORDER BY updated_at DESC",
            (notebook_id,)))


# ---------- NAUKA: grupy notatek ----------
class NoteGroup(BaseModel):
    notebook_id: int
    name: str


class NoteGroupPatch(BaseModel):
    name: str | None = None


class NoteAssign(BaseModel):
    group_id: int | None = None      # None = wyjmij z grupy


@app.get("/api/note-groups")
def list_note_groups(notebook_id: int):
    with get_conn() as c:
        return rows(c.execute(
            "SELECT id,notebook_id,name,position,created_at FROM note_groups WHERE notebook_id=? ORDER BY position, id",
            (notebook_id,)))


@app.post("/api/note-groups")
def add_note_group(g: NoteGroup):
    with get_conn() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 p FROM note_groups WHERE notebook_id=?",
                        (g.notebook_id,)).fetchone()["p"]
        cur = c.execute("INSERT INTO note_groups(notebook_id,name,position) VALUES(?,?,?)",
                        (g.notebook_id, g.name, pos))
        return {"id": cur.lastrowid}


@app.patch("/api/note-groups/{gid}")
def patch_note_group(gid: int, p: NoteGroupPatch):
    if p.name is None:
        return {"updated": 0}
    with get_conn() as c:
        c.execute("UPDATE note_groups SET name=? WHERE id=?", (p.name, gid))
    return {"updated": 1}


@app.delete("/api/note-groups/{gid}")
def del_note_group(gid: int):
    with get_conn() as c:
        c.execute("UPDATE notes SET group_id=NULL WHERE group_id=?", (gid,))
        c.execute("DELETE FROM note_groups WHERE id=?", (gid,))
    return {"deleted": 1}


@app.patch("/api/notes/{nid}/group")
def assign_note_group(nid: int, a: NoteAssign):
    with get_conn() as c:
        c.execute("UPDATE notes SET group_id=? WHERE id=?", (a.group_id, nid))
    return {"updated": 1}


@app.post("/api/notes")
def add_note(n: NoteCreate):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO notes(notebook_id,title,purpose,description,created_at,updated_at) VALUES(?,?,?,?,datetime('now'),datetime('now'))",
            (n.notebook_id, n.title, n.purpose, n.description))
        return {"id": cur.lastrowid}


@app.get("/api/notes/{nid}")
def get_note(nid: int):
    with get_conn() as c:
        r = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if not r:
        raise HTTPException(404, "nie ma notatki")
    d = dict(r)
    d["content"] = json.loads(d.get("content") or "[]")
    return d


@app.patch("/api/notes/{nid}")
def patch_note(nid: int, p: NotePatch):
    sets, vals = [], []
    if p.title is not None:
        sets.append("title=?"); vals.append(p.title)
    if p.purpose is not None:
        sets.append("purpose=?"); vals.append(p.purpose)
    if p.description is not None:
        sets.append("description=?"); vals.append(p.description)
    if p.content is not None:
        sets.append("content=?"); vals.append(json.dumps(p.content, ensure_ascii=False))
    if not sets:
        return {"updated": 0}
    sets.append("updated_at=datetime('now')")
    with get_conn() as c:
        c.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id=?", (*vals, nid))
    return {"updated": 1}


@app.delete("/api/notes/{nid}")
def del_note(nid: int):
    with get_conn() as c:
        c.execute("DELETE FROM notes WHERE id=?", (nid,))
    return {"deleted": 1}


# ---------- UPLOAD plików (PDF itp.) ----------
class Upload(BaseModel):
    filename: str
    data: str            # base64 (może mieć prefiks data:...;base64,)


@app.post("/api/upload")
def upload_file(u: Upload):
    raw = u.data
    if "," in raw and raw.strip().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        blob = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "Niepoprawne dane pliku.")
    if len(blob) > 40 * 1024 * 1024:
        raise HTTPException(413, "Plik za duży (max 40 MB).")
    ext = os.path.splitext(u.filename)[1].lower()
    if ext not in (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise HTTPException(400, "Dozwolone: PDF lub obraz.")
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(u.filename))[:60] or ("plik" + ext)
    name = uuid.uuid4().hex[:10] + "_" + safe
    (UPLOAD_DIR / name).write_bytes(blob)
    return {"url": "/uploads/" + name, "name": u.filename}


# ---------- AI: notatka z Gemini 2.0 Flash ----------
class AINote(BaseModel):
    text: str
    topic: str = ""


def _norm_key(k: str) -> str:
    # usuń białe znaki i ewentualne otaczające cudzysłowy
    return k.strip().strip('"').strip("'").strip()


def _gemini_key() -> str | None:
    k = os.getenv("GEMINI_API_KEY")
    if k:
        return _norm_key(k)
    p = Path(__file__).parent / ".gemini_key"
    if p.exists():
        return _norm_key(p.read_text(encoding="utf-8"))
    return None


def _clean_ai_html(s: str) -> str:
    s = s.strip()
    # usuń ewentualne ```html ... ``` opakowanie
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # bez własnego tła/ramki: usuń atrybuty style/class i zamień blockquote na zwykły akapit
    s = re.sub(r'\s(style|class)="[^"]*"', "", s)
    s = re.sub(r"</?blockquote>", lambda m: "<p>" if "/" not in m.group(0) else "</p>", s)
    return s.strip()


@app.post("/api/ai/note")
def ai_note(a: AINote):
    key = _gemini_key()
    if not key:
        raise HTTPException(400, "Brak klucza Gemini. Zapisz go w pliku .gemini_key lub ustaw zmienną GEMINI_API_KEY.")
    if not a.text.strip():
        raise HTTPException(400, "Pusty tekst — nie ma z czego zrobić notatki.")

    topic = a.topic.strip()
    topic_line = (f"Temat przewodni podany przez studenta: „{topic}”. Trzymaj się go.\n"
                  if topic else
                  "Temat nie został podany — sam rozpoznaj główny temat na podstawie treści.\n")

    prompt = (
        "Jesteś doświadczonym korepetytorem akademickim. Zamień poniższą, podyktowaną i chaotyczną "
        "wypowiedź z wykładu w PEŁNĄ, uporządkowaną notatkę do nauki na studia.\n"
        + topic_line +
        "Zasady:\n"
        "- Pisz po polsku, rzeczowo i zrozumiale dla studenta.\n"
        "- Popraw interpunkcję, literówki i gramatykę; usuń dygresje, wtrącenia i wulgaryzmy.\n"
        "- Zachowaj WSZYSTKIE fakty, daty, nazwiska, definicje i zależności przyczynowo-skutkowe.\n"
        "- Rozwiń skróty myślowe tak, aby notatka była kompletna i samodzielna do nauki.\n"
        "- Uporządkuj: krótki tytuł (nagłówek), wprowadzenie, sekcje tematyczne, listy punktowane, "
        "pogrubione kluczowe pojęcia, a na końcu sekcję „Najważniejsze do zapamiętania”.\n"
        "- Jeśli czegoś brakuje w wypowiedzi, nie zmyślaj faktów.\n"
        "Odpowiedz WYŁĄCZNIE treścią notatki w prostym HTML, używając tylko tagów: "
        "<h3>, <h4>, <p>, <ul>, <ol>, <li>, <strong>, <em>. "
        "Nie używaj <blockquote>, nie dodawaj żadnych atrybutów style ani class. "
        "Nie dodawaj komentarzy, wstępu ani znaczników ```.\n\n"
        "Treść do przetworzenia:\n" + a.text
    )

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }).encode("utf-8")
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + model + ":generateContent?key=" + key)
    data = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (503, 429) and attempt < 2:      # przeciążenie/limit — ponów
                time.sleep(1.5 * (attempt + 1))
                continue
            if e.code in (503, 429):
                raise HTTPException(503, "Model Gemini jest chwilowo przeciążony. Spróbuj ponownie za chwilę.")
            raise HTTPException(502, f"Gemini API błąd {e.code}: {detail[:300]}")
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            raise HTTPException(502, f"Nie udało się połączyć z Gemini: {e}")

    try:
        out = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        raise HTTPException(502, f"Gemini nie zwrócił notatki{(' (' + fb + ')') if fb else ''}.")

    return {"html": _clean_ai_html(out)}


# ---------- SUMMARY (dashboard) ----------
@app.get("/api/summary")
def summary():
    today = date.today().isoformat()
    month = today[:7]
    with get_conn() as c:
        inc = c.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE type='income'").fetchone()["s"]
        exp = c.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE type='expense'").fetchone()["s"]
        m_inc = c.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE type='income' AND date LIKE ?", (month + "%",)).fetchone()["s"]
        m_exp = c.execute("SELECT COALESCE(SUM(amount),0) s FROM transactions WHERE type='expense' AND date LIKE ?", (month + "%",)).fetchone()["s"]
        today_total = c.execute("SELECT COUNT(*) n FROM tasks WHERE date=?", (today,)).fetchone()["n"]
        today_done = c.execute("SELECT COUNT(*) n FROM tasks WHERE date=? AND done=1", (today,)).fetchone()["n"]
        by_source = rows(c.execute("""
            SELECT s.name, COALESCE(SUM(t.amount),0) total
            FROM sources s LEFT JOIN transactions t ON t.source_id=s.id AND t.type='income'
            GROUP BY s.id ORDER BY total DESC"""))
    return {
        "balance": inc - exp,
        "month_income": m_inc, "month_expense": m_exp, "month_net": m_inc - m_exp,
        "today_tasks": today_total, "today_done": today_done,
        "income_by_source": by_source,
        "today": today, "month": month,
    }
