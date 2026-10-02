"""Asystent — backend. FastAPI serwuje SPA (/) i REST API (/api/*).
Uruchom:  uvicorn server:app --reload
"""
import os
import io
import sys
import shutil
import sqlite3
import zipfile
import re
import json
import time
import base64
import uuid
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import init_db, get_conn, rows, DATA_DIR, DB_PATH

app = FastAPI(title="Asystent")
init_db()

# pliki aplikacji (w .exe rozpakowane do sys._MEIPASS), dane użytkownika w DATA_DIR
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.get("/")
def home():
    return FileResponse(APP_DIR / "index.html")


@app.get("/logo.png")
def logo():
    return FileResponse(APP_DIR / "logo.png")


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
    room: str = ""
    building: str = ""


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
    room: str | None = None
    building: str | None = None


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


@app.get("/api/tasks/next-day")
def next_day(after: str):
    """Najbliższy dzień po `after`, w którym coś jest w kalendarzu (do subtelnej zapowiedzi na pulpicie)."""
    with get_conn() as c:
        r = c.execute("SELECT MIN(date) d FROM tasks WHERE date>?", (after,)).fetchone()
    return {"date": r["d"], "tasks": list_tasks(day=r["d"])} if r["d"] else {}


@app.post("/api/tasks")
def add_task(t: Task):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO tasks(title,date,start_time,end_time,priority,notes,remind_at,kind,note_id,room,building) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (t.title, t.date, t.start_time, t.end_time, t.priority, t.notes, t.remind_at, t.kind, t.note_id, t.room, t.building))
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
        return rows(c.execute("SELECT id,notebook_id,title,updated_at FROM notes ORDER BY updated_at DESC"))


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
    for p in (DATA_DIR / ".gemini_key", APP_DIR / ".gemini_key"):
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


class GeminiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


# ---------- zużycie AI (liczone lokalnie: Google nie udostępnia stanu limitu przez klucz) ----------
USAGE = DATA_DIR / "ai_usage.json"
_usage_lock = threading.Lock()


def _pacific_now() -> datetime:
    """Darmowe limity Gemini odnawiają się o północy czasu pacyficznego."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:   # brak bazy stref (np. Windows bez tzdata) — przybliżenie DST USA
        u = datetime.now(timezone.utc)
        off = -7 if 3 <= u.month <= 10 else -8
        return u.astimezone(timezone(timedelta(hours=off)))


def _usage_load() -> dict:
    try:
        return json.loads(USAGE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _usage_add(kind: str, meta: dict | None = None, ok: bool = True, daily_limit: bool = False):
    with _usage_lock:
        u = _usage_load()
        days = u.setdefault("days", {})
        d = days.setdefault(_pacific_now().date().isoformat(), {"req": 0, "in": 0, "out": 0, "err": 0, "kinds": {}})
        d["req"] += 1
        d["kinds"][kind] = d["kinds"].get(kind, 0) + 1
        if not ok:
            d["err"] += 1
        if daily_limit:
            d["limit_hit"] = True
        if meta:
            d["in"] += int(meta.get("promptTokenCount") or 0)
            d["out"] += int(meta.get("candidatesTokenCount") or 0) + int(meta.get("thoughtsTokenCount") or 0)
        for k in sorted(days)[:-60]:          # trzymamy ostatnie 60 dni
            days.pop(k)
        USAGE.write_text(json.dumps(u), encoding="utf-8")


def gemini(parts: list, max_tokens: int = 4096, temperature: float = 0.4, timeout: int = 90, tries: int = 3,
           kind: str = "other") -> str:
    """Jedno zapytanie generateContent (tekst/audio). Ponawia przy 503/429/zerwanym połączeniu."""
    key = _gemini_key()
    if not key:
        raise GeminiError(400, "Brak klucza Gemini. Dodaj go w Ustawieniach (⚙ na dole paska menu).")
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }).encode("utf-8")
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    base = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com")
    url = base + "/v1beta/models/" + model + ":generateContent?key=" + key
    data = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (503, 429) and attempt < tries - 1 and "PerDay" not in detail:
                time.sleep(1.5 * (attempt + 1))
                continue
            if e.code == 429:   # odrzucone przez limit — liczy się do dziennego wykorzystania
                _usage_add(kind, ok=False, daily_limit="PerDay" in detail or "per day" in detail.lower())
            raise GeminiError(e.code, f"Gemini API błąd {e.code}: {detail[:300]}")
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(1)
                continue
            raise GeminiError(502, f"Nie udało się połączyć z Gemini: {e}")
    _usage_add(kind, (data or {}).get("usageMetadata"))
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        fb = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(502, f"Gemini nie zwrócił odpowiedzi{(' (' + fb + ')') if fb else ''}.")


def _note_prompt(text: str, topic: str, fmt: str = "html") -> str:
    fmt_rule = ("Odpowiedz WYŁĄCZNIE treścią notatki w prostym HTML, używając tylko tagów: "
                "<h3>, <h4>, <p>, <ul>, <ol>, <li>, <strong>, <em>. "
                "Nie używaj <blockquote>, nie dodawaj żadnych atrybutów style ani class. "
                "Nie dodawaj komentarzy, wstępu ani znaczników ```.\n\n") if fmt == "html" else (
               "Odpowiedz WYŁĄCZNIE treścią notatki w Markdown (nagłówki ###, listy -, **pogrubienia**). "
               "Bez wstępu i komentarzy.\n\n")
    topic = topic.strip()
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
        + fmt_rule +
        "Treść do przetworzenia:\n" + text
    )

    return prompt


class NotePrompt(BaseModel):
    text: str
    topic: str = ""


@app.post("/api/ai/note/prompt")
def ai_note_prompt(a: NotePrompt):
    """Tryb bez klucza: gotowe polecenie do wklejenia w zwykły czat (Gemini/ChatGPT)."""
    if not a.text.strip():
        raise HTTPException(400, "Pusty tekst — nie ma z czego zrobić notatki.")
    return {"prompt": _note_prompt(a.text, a.topic, "markdown")}


# ---------- USTAWIENIA: połączenie z AI ----------
SETTINGS = DATA_DIR / "settings.json"


def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


class AiSettings(BaseModel):
    key: str | None = None        # nowy klucz Gemini ("" = usuń)
    mode: str | None = None       # key | chat
    chat: str | None = None       # gemini | chatgpt


@app.get("/api/settings/ai")
def get_ai_settings():
    k = _gemini_key()
    st = _settings()
    return {"has_key": bool(k), "masked": (k[:4] + "…" + k[-4:]) if k and len(k) > 10 else ("•••" if k else ""),
            "from_env": bool(os.getenv("GEMINI_API_KEY")), "mode": st.get("mode", "key"), "chat": st.get("chat", "gemini")}


@app.post("/api/settings/ai")
def set_ai_settings(p: AiSettings):
    st = _settings()
    if p.mode in ("key", "chat"):
        st["mode"] = p.mode
    if p.chat in ("gemini", "chatgpt"):
        st["chat"] = p.chat
    SETTINGS.write_text(json.dumps(st), encoding="utf-8")
    kf = DATA_DIR / ".gemini_key"
    if p.key is not None:
        k = _norm_key(p.key)
        if not k:
            kf.unlink(missing_ok=True)
        else:
            if not re.fullmatch(r"[A-Za-z0-9_\-]{20,}", k):
                raise HTTPException(400, "To nie wygląda na klucz Gemini (zwykle zaczyna się od „AIza…”).")
            kf.write_text(k, encoding="utf-8")
    return get_ai_settings()


@app.post("/api/settings/ai/test")
def test_ai():
    try:
        out = gemini([{"text": "Odpowiedz jednym słowem: OK"}], max_tokens=10, temperature=0, timeout=30, tries=1, kind="test")
        return {"ok": True, "reply": out.strip()[:40]}
    except GeminiError as e:
        msg = e.msg
        if e.status in (400, 403) and "API key" in msg:
            msg = "Klucz jest nieprawidłowy albo wyłączony. Skopiuj go jeszcze raz z Google AI Studio."
        elif e.status == 429:
            msg = "Klucz działa, ale chwilowo przekroczono limit. Spróbuj za minutę."
        return {"ok": e.status == 429, "error": msg}


class UsageLimit(BaseModel):
    daily: int


@app.get("/api/settings/ai/usage")
def ai_usage():
    u = _usage_load()
    now = _pacific_now()
    today = now.date().isoformat()
    days = u.get("days", {})
    reset = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    last = [(now.date() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    empty = {"req": 0, "in": 0, "out": 0, "err": 0, "kinds": {}}
    return {"today": {**empty, **days.get(today, {})}, "daily_limit": int(u.get("daily_limit") or 250),
            "reset_at": reset.isoformat().replace("+00:00", "Z"),
            "history": [{"date": d, **{**empty, **days.get(d, {})}} for d in last],
            "model": os.getenv("GEMINI_MODEL", "gemini-3.6-flash")}


@app.post("/api/settings/ai/usage")
def set_ai_usage(p: UsageLimit):
    with _usage_lock:
        u = _usage_load()
        u["daily_limit"] = max(1, min(100000, p.daily))
        USAGE.write_text(json.dumps(u), encoding="utf-8")
    return ai_usage()


# ---------- logo uczelni (pobierane raz, trzymane lokalnie) ----------
LOGOS = DATA_DIR / "logos"


@app.get("/api/unilogo/{domain}")
def uni_logo(domain: str):
    domain = domain.lower()
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain) or len(domain) > 80:
        raise HTTPException(400, "Zła domena")
    LOGOS.mkdir(parents=True, exist_ok=True)
    f, miss = LOGOS / (domain + ".img"), LOGOS / (domain + ".miss")
    if not f.exists():
        if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
            raise HTTPException(404, "Brak logo")
        for src in (f"https://www.google.com/s2/favicons?domain={domain}&sz=128",
                    f"https://icons.duckduckgo.com/ip3/{domain}.ico",
                    f"https://{domain}/apple-touch-icon.png"):
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 Asystent"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    b = r.read(512_000)
                if len(b) > 200 and (b[:8] == b"\x89PNG\r\n\x1a\n" or b[:4] == b"\x00\x00\x01\x00"
                                     or b[:3] == b"\xff\xd8\xff" or b[:4] == b"GIF8" or b[8:12] == b"WEBP"):
                    f.write_bytes(b)
                    break
            except Exception:
                continue
        else:
            miss.touch()
            raise HTTPException(404, "Brak logo")
    b = f.read_bytes()
    mt = ("image/png" if b[:4] == b"\x89PNG" else "image/x-icon" if b[:4] == b"\x00\x00\x01\x00"
          else "image/jpeg" if b[:3] == b"\xff\xd8\xff" else "image/gif" if b[:4] == b"GIF8" else "image/webp")
    return Response(b, media_type=mt, headers={"Cache-Control": "public, max-age=604800"})


@app.post("/api/ai/note")
def ai_note(a: AINote):
    key = _gemini_key()
    if not key:
        raise HTTPException(400, "Brak klucza Gemini. Dodaj go w Ustawieniach (⚙ na dole paska menu).")
    if not a.text.strip():
        raise HTTPException(400, "Pusty tekst — nie ma z czego zrobić notatki.")

    prompt = _note_prompt(a.text, a.topic)

    try:
        out = gemini([{"text": prompt}], max_tokens=4096, temperature=0.4, kind="note")
    except GeminiError as e:
        if e.status in (429, 503):
            raise HTTPException(503, "Model Gemini jest chwilowo przeciążony. Spróbuj ponownie za chwilę.")
        raise HTTPException(400 if e.status == 400 else 502, e.msg)

    return {"html": _clean_ai_html(out)}


# ---------- EKSPORT / IMPORT danych (ZIP: baza + załączniki) ----------
@app.post("/api/export")
def export_data():
    # spójna kopia bazy przez sqlite backup API, zapis do folderu Pobrane
    out_dir = Path.home() / "Downloads"
    if not out_dir.is_dir():
        out_dir = Path.home()
    out = out_dir / f"asystent-kopia-{date.today().isoformat()}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        tmp = DATA_DIR / "export.tmp.db"
        src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        z.write(tmp, "asystent.db"); tmp.unlink()
        for f in UPLOAD_DIR.iterdir():
            if f.is_file():
                z.write(f, "uploads/" + f.name)
        for f in (DATA_DIR / "recordings").glob("*/*"):
            z.write(f, f"recordings/{f.parent.name}/{f.name}")
    return {"path": str(out)}


class ImportData(BaseModel):
    filename: str
    data: str            # base64: .zip z eksportu albo sam plik asystent.db


@app.post("/api/import")
def import_data(u: ImportData):
    raw = u.data.split(",", 1)[1] if u.data.startswith("data:") else u.data
    try:
        blob = base64.b64decode(raw)
    except Exception:
        raise HTTPException(400, "Niepoprawne dane pliku.")
    files: dict[str, bytes] = {}
    if blob[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if n == "asystent.db" or (n.startswith(("uploads/", "recordings/")) and not n.endswith("/")):
                    files[n] = z.read(n)
    else:
        files["asystent.db"] = blob
    db = files.get("asystent.db", b"")
    if not db.startswith(b"SQLite format 3\x00"):
        raise HTTPException(400, "To nie jest kopia Asystenta (brak bazy asystent.db).")
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, DB_PATH.with_suffix(".db.bak"))   # stara baza na wszelki wypadek
    DB_PATH.write_bytes(db)
    for n, b in files.items():
        if n.startswith("uploads/"):
            (UPLOAD_DIR / os.path.basename(n)).write_bytes(b)
        elif n.startswith("recordings/") and n.count("/") == 2:
            rid, name = n.split("/")[1:]
            if rid.isdigit():
                d = DATA_DIR / "recordings" / rid
                d.mkdir(parents=True, exist_ok=True)
                (d / os.path.basename(name)).write_bytes(b)
    init_db()   # migracje, jeśli kopia jest ze starszej wersji
    return {"ok": True, "uploads": sum(n.startswith("uploads/") for n in files)}


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


# ---------- NAGRANIA WYKŁADÓW (tylko aplikacja desktop) ----------
@app.get("/api/config")
def config():
    cloud = _settings().get("cloud_url") or os.getenv("ASYSTENT_CLOUD_URL", "http://127.0.0.1:8100")
    return {"desktop": bool(os.getenv("ASYSTENT_DESKTOP")), "gemini": bool(_gemini_key()), "cloud_url": cloud.rstrip("/")}


class CloudUrl(BaseModel):
    url: str


@app.post("/api/settings/cloud")
def set_cloud(p: CloudUrl):
    url = p.url.strip().rstrip("/")
    if url and not re.match(r"^https?://[^\s/]+", url):
        raise HTTPException(400, "Adres musi zaczynać się od http:// albo https://")
    st = _settings()
    st["cloud_url"] = url
    SETTINGS.write_text(json.dumps(st), encoding="utf-8")
    return config()


import nagrania   # noqa: E402  (po definicji gemini — moduł z niego korzysta)
import usos   # noqa: E402
app.include_router(nagrania.router)
app.include_router(usos.router)
