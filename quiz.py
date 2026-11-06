"""Quizy z notatek: Gemini robi z notatki pytania z odpowiedziami, a nauka odbywa się w aplikacji —
odpowiedź rozmyta do kliknięcia, „Umiem” zdejmuje pytanie z puli, „Pomiń” odkłada je na później.
Pytania i postęp są w bazie (synchronizowane między urządzeniami jak notatki)."""
import json
import re
from html import unescape

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()


def init():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS quiz_cards (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            q          TEXT NOT NULL,
            a          TEXT NOT NULL,
            learned    INTEGER DEFAULT 0,
            skips      INTEGER DEFAULT 0,
            position   INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS quiz_cards_note ON quiz_cards(note_id);
        -- test ABCD z notatki: pytania z 4 odpowiedziami (JSON), wyniki podejść
        CREATE TABLE IF NOT EXISTS abcd_tests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            questions  TEXT NOT NULL,
            difficulty TEXT DEFAULT '',
            attempts   INTEGER DEFAULT 0,
            last_score INTEGER,
            best_score INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS abcd_tests_note ON abcd_tests(note_id);
        """)


init()


# ---------- tekst notatki (bloki → zwykły tekst dla AI) ----------
def _tex_out(h: str) -> str:
    """Wzór w notatce (<span class="itex" data-tex="…">…KaTeX…</span>) → $zapis LaTeX$ — bez tekstu wyrenderowanego KaTeX."""
    out, i = [], 0
    for m in re.finditer(r'<span\b[^>]*\bclass="itex( disp)?"[^>]*\bdata-tex="([^"]*)"[^>]*>', h):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        for t in re.finditer(r"<(/?)span\b[^>]*>", h[j:]):
            depth += -1 if t.group(1) else 1
            if not depth:
                j += t.end()
                break
        else:
            j = len(h)
        d = "$$" if m.group(1) else "$"
        out += [h[i:m.start()], d + unescape(m.group(2)) + d]
        i = j
    return "".join(out) + h[i:]


def _html_text(h: str) -> str:
    h = _tex_out(h or "")
    h = re.sub(r"<img\b[^>]*>", " ", h or "", flags=re.I)
    h = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote)>|<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<li[^>]*>", "• ", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", unescape(h)).strip()


def note_text(content) -> str:
    out = []
    for b in content if isinstance(content, list) else []:
        t = b.get("type")
        if t == "heading" and b.get("text"):
            out.append("## " + b["text"])
        elif t in ("text", "callout"):
            out.append(_html_text(b.get("html", "")))
        elif t == "toggle":
            out.append((b.get("summary") or "") + "\n" + _html_text(b.get("html", "")))
        elif t == "checklist":
            out.append("\n".join("• " + (i.get("text") or "") for i in b.get("items") or []))
        elif t == "table":
            out.append("\n".join(" | ".join(str(x) for x in row) for row in b.get("data") or []))
        elif t == "code" and b.get("code"):
            out.append(b["code"])
        elif t == "latex" and b.get("tex"):
            out.append("Wzór: " + b["tex"])
    return "\n\n".join(x for x in out if x and x.strip())


def load_note(note_id: int) -> tuple[str, str]:
    with get_conn() as c:
        r = c.execute("SELECT title, content FROM notes WHERE id=?", (note_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Nie ma takiej notatki.")
    try:
        content = json.loads(r["content"] or "[]")
    except Exception:
        content = []
    return r["title"] or "", note_text(content)


def parse_json_list(out: str) -> list:
    s = out.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j < i:
        raise HTTPException(502, "AI nie zwróciło listy — spróbuj jeszcze raz.")
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        raise HTTPException(502, "AI zwróciło niepoprawny format — spróbuj jeszcze raz.")
    return data if isinstance(data, list) else []


def _quiz_prompt(title: str, text: str, count: int, instructions: str, fmt: str = "json") -> str:
    instructions = (instructions or "").strip()[:1500]
    return (
        "Jesteś doświadczonym korepetytorem akademickim. Na podstawie notatki studenta przygotuj pytania do nauki "
        f"(aktywne przypominanie). Przygotuj około {count} pytań.\n"
        + (f"WSKAZÓWKI STUDENTA (najważniejsze): „{instructions}”\n" if instructions else "") +
        "Zasady:\n"
        "- Pisz po polsku. Pytania konkretne, sprawdzające zrozumienie: definicje, zależności, przyczyny i skutki, "
        "porównania, przykłady, daty, wzory, wyliczenia („Wymień…”).\n"
        "- Odpowiedź zwięzła (1–4 zdania albo krótka lista), wyłącznie na podstawie notatki — nie zmyślaj.\n"
        "- Każde pytanie o coś innego; od najważniejszych zagadnień.\n"
        + ('Zwróć WYŁĄCZNIE tablicę JSON: [{"q": "pytanie", "a": "odpowiedź"}, …] — bez komentarzy i znaczników ```.\n'
           if fmt == "json" else
           "Odpowiedz WYŁĄCZNIE listą w formacie:\nP: pytanie\nO: odpowiedź\n(pusta linia między parami), bez wstępu.\n")
        + f"\nNotatka „{title}”:\n{text[:120_000]}"
    )


class QuizGen(BaseModel):
    note_id: int
    count: int = 20
    instructions: str = ""
    replace: bool = True


def _save(note_id: int, cards: list, replace: bool) -> int:
    clean = []
    for x in cards:
        if isinstance(x, dict):
            q, a = str(x.get("q") or x.get("pytanie") or "").strip(), str(x.get("a") or x.get("odpowiedz") or x.get("odpowiedź") or "").strip()
            if q and a:
                clean.append((q[:600], a[:3000]))
    if not clean:
        raise HTTPException(502, "Nie udało się odczytać pytań — spróbuj jeszcze raz.")
    with get_conn() as c:
        if replace:
            c.execute("DELETE FROM quiz_cards WHERE note_id=?", (note_id,))
        start = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM quiz_cards WHERE note_id=?", (note_id,)).fetchone()[0]
        c.executemany("INSERT INTO quiz_cards(note_id, q, a, position) VALUES(?,?,?,?)",
                      [(note_id, q, a, start + i) for i, (q, a) in enumerate(clean)])
    return len(clean)


@router.post("/api/quiz/generate")
def generate(p: QuizGen):
    from server import gemini, GeminiError, _gemini_key
    title, text = load_note(p.note_id)
    if len(text) < 40:
        raise HTTPException(400, "Notatka jest za krótka, żeby zrobić z niej quiz — dopisz trochę treści.")
    if not _gemini_key():
        raise HTTPException(400, "Brak klucza Gemini. Dodaj go w Ustawieniach (⚙ na dole paska menu).")
    count = max(5, min(50, p.count))
    try:
        out = gemini([{"text": _quiz_prompt(title, text, count, p.instructions)}], max_tokens=8192, temperature=0.5,
                     timeout=180, kind="quiz", json_mode=True)
    except GeminiError as e:
        if e.status in (429, 503):
            raise HTTPException(503, "Model Gemini jest chwilowo przeciążony albo wyczerpano limit. Spróbuj ponownie za chwilę.")
        raise HTTPException(400 if e.status == 400 else 502, e.msg)
    return {"added": _save(p.note_id, parse_json_list(out), p.replace)}


@router.post("/api/quiz/prompt")
def prompt(p: QuizGen):
    """Tryb bez klucza: polecenie do wklejenia w zwykły czat; odpowiedź wraca przez /api/quiz/import."""
    title, text = load_note(p.note_id)
    return {"prompt": _quiz_prompt(title, text, max(5, min(50, p.count)), p.instructions, "text")}


class QuizImport(BaseModel):
    note_id: int
    text: str
    replace: bool = True


@router.post("/api/quiz/import")
def import_text(p: QuizImport):
    t = p.text.strip()
    if t.lstrip("`json \n").startswith("["):
        cards = parse_json_list(t)
    else:   # P: … / O: …
        cards, cur = [], None
        for line in t.splitlines():
            m = re.match(r"\s*\**\s*(P|Pytanie|Q)\s*\d*[:.)]\**\s*(.*)", line, re.I)
            n = re.match(r"\s*\**\s*(O|Odpowied[zź]|A)\s*[:.)]\**\s*(.*)", line, re.I)
            if m:
                cur = {"q": m.group(2).strip(), "a": ""}
                cards.append(cur)
            elif n and cur is not None:
                cur["a"] = n.group(2).strip()
            elif cur is not None and line.strip() and cur["a"]:
                cur["a"] += "\n" + line.strip()
    return {"added": _save(p.note_id, cards, p.replace)}


@router.get("/api/quiz")
def cards(note_id: int | None = None, notebook_id: int | None = None):
    with get_conn() as c:
        if note_id:
            rows = c.execute("SELECT q.*, n.title note_title FROM quiz_cards q JOIN notes n ON n.id=q.note_id "
                             "WHERE q.note_id=? ORDER BY q.position, q.id", (note_id,))
        elif notebook_id:
            rows = c.execute("SELECT q.*, n.title note_title FROM quiz_cards q JOIN notes n ON n.id=q.note_id "
                             "WHERE n.notebook_id=? ORDER BY n.id, q.position, q.id", (notebook_id,))
        else:
            raise HTTPException(400, "Podaj notatkę albo zeszyt.")
        out = [dict(r) for r in rows]
    for r in out:
        r["learned"] = bool(r["learned"])
    return out


@router.get("/api/quiz/counts")
def counts(notebook_id: int | None = None):
    """Liczba pytań (wszystkie / nauczone) na notatkę — do plakietek na liście notatek."""
    q = ("SELECT q.note_id, COUNT(*) n, SUM(q.learned) learned FROM quiz_cards q JOIN notes n ON n.id=q.note_id "
         + ("WHERE n.notebook_id=? " if notebook_id else "") + "GROUP BY q.note_id")
    with get_conn() as c:
        return {str(r["note_id"]): {"n": r["n"], "learned": r["learned"] or 0}
                for r in c.execute(q, (notebook_id,) if notebook_id else ())}


class CardPatch(BaseModel):
    learned: bool | None = None
    skip: bool | None = None
    q: str | None = None
    a: str | None = None


@router.patch("/api/quiz/{cid}")
def patch(cid: int, p: CardPatch):
    with get_conn() as c:
        if p.learned is not None:
            c.execute("UPDATE quiz_cards SET learned=? WHERE id=?", (int(p.learned), cid))
        if p.skip:
            c.execute("UPDATE quiz_cards SET skips=skips+1 WHERE id=?", (cid,))
        if p.q is not None and p.q.strip():
            c.execute("UPDATE quiz_cards SET q=? WHERE id=?", (p.q.strip()[:600], cid))
        if p.a is not None and p.a.strip():
            c.execute("UPDATE quiz_cards SET a=? WHERE id=?", (p.a.strip()[:3000], cid))
    return {"ok": True}


@router.delete("/api/quiz/{cid}")
def delete(cid: int):
    with get_conn() as c:
        c.execute("DELETE FROM quiz_cards WHERE id=?", (cid,))
    return {"ok": True}


class QuizReset(BaseModel):
    note_id: int | None = None
    notebook_id: int | None = None
    delete: bool = False


@router.post("/api/quiz/reset")
def reset(p: QuizReset):
    """Wszystkie pytania z powrotem do nauki (albo usunięcie quizu)."""
    with get_conn() as c:
        where, arg = (("note_id=?", p.note_id) if p.note_id else
                      ("note_id IN (SELECT id FROM notes WHERE notebook_id=?)", p.notebook_id))
        if not arg:
            raise HTTPException(400, "Podaj notatkę albo zeszyt.")
        c.execute(f"DELETE FROM quiz_cards WHERE {where}" if p.delete else
                  f"UPDATE quiz_cards SET learned=0, skips=0 WHERE {where}", (arg,))
    return {"ok": True}


# ---------- TEST ABCD (jednokrotnego wyboru) z notatki ----------
ABCD_LEVEL = {"easy": "łatwe — podstawowe pojęcia i fakty, dystraktory wyraźnie różne od poprawnej",
              "medium": "średnie — zrozumienie, zastosowanie, porównania; dystraktory wiarygodne",
              "hard": "trudne jak na egzaminie — szczegóły, wyjątki, zadania i pułapki; dystraktory bardzo bliskie poprawnej"}


def _abcd_prompt(title: str, text: str, count: int, instructions: str, level: str) -> str:
    instructions = (instructions or "").strip()[:1500]
    return (
        "Jesteś doświadczonym egzaminatorem. Na podstawie notatki studenta przygotuj TEST JEDNOKROTNEGO WYBORU (ABCD) "
        f"— dokładnie {count} pytań. Poziom: {ABCD_LEVEL.get(level, ABCD_LEVEL['medium'])}.\n"
        + (f"WSKAZÓWKI STUDENTA (najważniejsze): „{instructions}”\n" if instructions else "") +
        "Zasady:\n"
        "- Pisz po polsku. Każde pytanie sprawdza coś innego, od najważniejszych zagadnień notatki; pytania wyłącznie z treści notatki.\n"
        "- Dokładnie 4 odpowiedzi, DOKŁADNIE JEDNA poprawna. Błędne odpowiedzi (dystraktory) mają być wiarygodne: typowe pomyłki, "
        "pomylone pojęcia, odwrócone zależności, sąsiednie wartości — nie absurdy. Wszystkie odpowiedzi podobnej długości i w tej samej formie.\n"
        "- Bez odpowiedzi typu „wszystkie powyższe”, „żadna z powyższych”, „A i B”. Bez podpowiedzi w treści pytania.\n"
        "- Poprawna odpowiedź na RÓŻNYCH pozycjach (mniej więcej po równo A, B, C, D).\n"
        "- Gdy notatka zawiera obliczenia, część pytań niech wymaga policzenia (wynik w odpowiedziach). Matematykę zapisuj w LaTeX między $…$.\n"
        "- Do każdego pytania krótkie wyjaśnienie (1–2 zdania), DLACZEGO poprawna odpowiedź jest poprawna — na podstawie notatki.\n"
        'Zwróć WYŁĄCZNIE tablicę JSON bez komentarzy i znaczników ```: '
        '[{"q": "treść pytania", "options": ["odp. A", "odp. B", "odp. C", "odp. D"], "correct": 0, "explain": "wyjaśnienie"}, …] '
        '— "correct" to numer poprawnej odpowiedzi 0–3.\n'
        + f"\nNotatka „{title}”:\n{text[:120_000]}"
    )


def _abcd_clean(items: list) -> list:
    out, seen = [], set()
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        q = str(x.get("q") or x.get("pytanie") or x.get("question") or "").strip()
        opts = x.get("options") or x.get("odpowiedzi") or x.get("answers") or []
        if isinstance(opts, dict):
            opts = [opts.get(k) for k in ("A", "B", "C", "D")]
        opts = [re.sub(r"^\s*[A-Da-d][.)]\s+", "", str(o or "")).strip() for o in opts][:4]
        cor = x.get("correct", x.get("poprawna", x.get("answer")))
        if isinstance(cor, str):
            cor = cor.strip().upper()
            cor = "ABCD".index(cor[0]) if cor[:1] in ("A", "B", "C", "D") else (int(cor) if cor.isdigit() else -1)
        if not (q and len(opts) == 4 and all(opts) and len({o.lower() for o in opts}) == 4 and isinstance(cor, int) and 0 <= cor <= 3):
            continue
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append({"q": q[:800], "options": [o[:400] for o in opts], "correct": cor,
                    "explain": str(x.get("explain") or x.get("wyjasnienie") or x.get("wyjaśnienie") or "").strip()[:1200]})
    return out


def _abcd_out(r) -> dict | None:
    if not r:
        return None
    d = dict(r)
    d["questions"] = json.loads(d["questions"] or "[]")
    return d


def _abcd_save(note_id: int, items: list, level: str) -> dict:
    qs = _abcd_clean(items)
    if len(qs) < 3:
        raise HTTPException(502, "Nie udało się odczytać pytań testu — spróbuj jeszcze raz.")
    with get_conn() as c:
        c.execute("DELETE FROM abcd_tests WHERE note_id=?", (note_id,))   # jeden test na notatkę: nowy zastępuje stary
        tid = c.execute("INSERT INTO abcd_tests(note_id, questions, difficulty) VALUES(?,?,?)",
                        (note_id, json.dumps(qs, ensure_ascii=False), level)).lastrowid
        return _abcd_out(c.execute("SELECT * FROM abcd_tests WHERE id=?", (tid,)).fetchone())


class AbcdGen(BaseModel):
    note_id: int
    count: int = 15
    difficulty: str = "medium"
    instructions: str = ""


@router.get("/api/abcd")
def abcd_get(note_id: int):
    with get_conn() as c:
        return {"test": _abcd_out(c.execute("SELECT * FROM abcd_tests WHERE note_id=? ORDER BY id DESC LIMIT 1", (note_id,)).fetchone())}


@router.post("/api/abcd/generate")
def abcd_generate(p: AbcdGen):
    from server import gemini, GeminiError, _gemini_key
    title, text = load_note(p.note_id)
    if len(text) < 80:
        raise HTTPException(400, "Notatka jest za krótka, żeby zrobić z niej test — dopisz trochę treści.")
    if not _gemini_key():
        raise HTTPException(400, "Brak klucza Gemini. Dodaj go w Ustawieniach (⚙ na dole paska menu).")
    count = max(5, min(40, p.count))
    try:
        out = gemini([{"text": _abcd_prompt(title, text, count, p.instructions, p.difficulty)}], max_tokens=16384, temperature=0.5,
                     timeout=240, kind="quiz", json_mode=True)
    except GeminiError as e:
        if e.status in (429, 503):
            raise HTTPException(503, "Model Gemini jest chwilowo przeciążony albo wyczerpano limit. Spróbuj ponownie za chwilę.")
        raise HTTPException(400 if e.status == 400 else 502, e.msg)
    return {"test": _abcd_save(p.note_id, parse_json_list(out), p.difficulty)}


@router.post("/api/abcd/prompt")
def abcd_prompt(p: AbcdGen):
    """Tryb bez klucza: polecenie do zwykłego czatu; odpowiedź (JSON) wraca przez /api/abcd/import."""
    title, text = load_note(p.note_id)
    return {"prompt": _abcd_prompt(title, text, max(5, min(40, p.count)), p.instructions, p.difficulty)}


class AbcdImport(BaseModel):
    note_id: int
    text: str
    difficulty: str = "medium"


@router.post("/api/abcd/import")
def abcd_import(p: AbcdImport):
    return {"test": _abcd_save(p.note_id, parse_json_list(p.text), p.difficulty)}


class AbcdResult(BaseModel):
    score: int
    total: int


@router.post("/api/abcd/{tid}/result")
def abcd_result(tid: int, p: AbcdResult):
    pct = round(100 * max(0, p.score) / max(1, p.total))
    with get_conn() as c:
        c.execute("UPDATE abcd_tests SET attempts=attempts+1, last_score=?, best_score=MAX(COALESCE(best_score,0),?), "
                  "updated_at=datetime('now') WHERE id=?", (pct, pct, tid))
        return {"test": _abcd_out(c.execute("SELECT * FROM abcd_tests WHERE id=?", (tid,)).fetchone())}


@router.delete("/api/abcd/{tid}")
def abcd_delete(tid: int):
    with get_conn() as c:
        c.execute("DELETE FROM abcd_tests WHERE id=?", (tid,))
    return {"ok": True}
