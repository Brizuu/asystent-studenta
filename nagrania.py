"""Nagrywanie wykładów (aplikacja desktop) i zamiana na tekst przez Gemini.

Nagrywanie idzie w backendzie (sounddevice), niezależnie od okna: mikrofon → OGG/Opus 16 kHz mono,
cięty na części po SEGMENT_SEC (domyślnie 15 min, ~3 MB, mieści się w jednym zapytaniu do Gemini).
Zamiana na tekst później, na żądanie: 1 część = 1 zapytanie (7 h wykładów ≈ 28 zapytań).
Tekst każdej części zapisuje się od razu do part-NNN.txt, więc po limicie/awarii wznawia się od miejsca przerwania.
"""
import base64
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn, rows, DATA_DIR

router = APIRouter()
REC_DIR = DATA_DIR / "recordings"
RATE = 16000
SEGMENT_SEC = int(os.getenv("ASYSTENT_SEGMENT_SEC", "900"))

PROMPT = (
    "Przepisz dokładnie tę część nagrania wykładu akademickiego na tekst po polsku. "
    "Zachowaj treść i kolejność wypowiedzi, dodaj interpunkcję, dziel tekst na akapity (pusta linia). "
    "Pomiń wtrącenia typu „yyy”, szumy i rozmowy w tle. Nie streszczaj, nie dodawaj komentarzy ani nagłówków. "
    "Jeśli w nagraniu nie ma mowy, odpowiedz pustym tekstem."
)


def _dir(rid: int) -> Path:
    return REC_DIR / str(rid)


def _parts(rid: int) -> list[Path]:
    return sorted(_dir(rid).glob("part-*.ogg"))


def _set(rid: int, **f):
    with get_conn() as c:
        c.execute(f"UPDATE recordings SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), rid))


# ---------- nagrywanie ----------
class Recorder:
    def __init__(self, rid: int, note_id: int | None):
        self.rid, self.note_id = rid, note_id
        self.q: queue.Queue = queue.Queue()
        self.frames = 0
        self.level = 0.0
        self.error = ""

    def start(self):
        import sounddevice as sd   # lazy: tylko desktop ma tę zależność
        _dir(self.rid).mkdir(parents=True, exist_ok=True)
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", callback=self._cb)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()
        self.stream.start()

    def _cb(self, data, frames, t, status):
        self.q.put(data.copy())
        self.level = min(1.0, float(abs(data).mean()) / 3000)

    def _write(self):
        import soundfile as sf
        f, part, in_part = None, 0, 0
        try:
            while (data := self.q.get()) is not None:
                if f is None or in_part >= SEGMENT_SEC * RATE:
                    if f:
                        f.close()
                    part += 1
                    in_part = 0
                    f = sf.SoundFile(_dir(self.rid) / f"part-{part:03d}.ogg", "w", RATE, 1, format="OGG", subtype="OPUS")
                    _set(self.rid, parts=part, seconds=self.frames / RATE)   # po awarii zostaje prawie pełny czas
                f.write(data)
                in_part += len(data)
                self.frames += len(data)
        except Exception as e:   # np. pełny dysk — nagranie do tego miejsca zostaje
            self.error = str(e)
        finally:
            if f:
                f.close()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        finally:
            self.q.put(None)
            self.writer.join(timeout=30)


current: Recorder | None = None
lock = threading.Lock()


class StartRec(BaseModel):
    note_id: int | None = None
    title: str = ""


@router.post("/api/rec/start")
def rec_start(p: StartRec):
    global current
    with lock:
        if current:
            raise HTTPException(409, "Nagrywanie już trwa.")
        with get_conn() as c:
            rid = c.execute("INSERT INTO recordings(note_id,title) VALUES(?,?)",
                            (p.note_id, p.title or time.strftime("Wykład %d.%m.%Y %H:%M"))).lastrowid
        rec = Recorder(rid, p.note_id)
        try:
            rec.start()
        except Exception as e:
            with get_conn() as c:
                c.execute("DELETE FROM recordings WHERE id=?", (rid,))
            shutil.rmtree(_dir(rid), ignore_errors=True)
            raise HTTPException(500, f"Nie udało się włączyć mikrofonu: {e}")
        current = rec
    return {"id": rid}


@router.post("/api/rec/stop")
def rec_stop():
    global current
    with lock:
        rec, current = current, None
    if not rec:
        return {"stopped": False}
    rec.stop()
    _set(rec.rid, seconds=rec.frames / RATE, parts=len(_parts(rec.rid)), status="pending",
         error=rec.error and f"Nagrywanie przerwane: {rec.error}")
    return {"stopped": True, "id": rec.rid}


@router.get("/api/rec/status")
def rec_status():
    rec = current
    if not rec:
        return {}
    return {"id": rec.rid, "note_id": rec.note_id, "seconds": rec.frames / RATE, "level": rec.level, "error": rec.error}


@router.get("/api/rec")
def rec_list(note_id: int | None = None):
    with get_conn() as c:
        if note_id is None:
            return rows(c.execute("SELECT * FROM recordings ORDER BY id DESC"))
        return rows(c.execute("SELECT * FROM recordings WHERE note_id=? ORDER BY id DESC", (note_id,)))


@router.get("/api/rec/{rid}/text")
def rec_text(rid: int):
    txt = [p.with_suffix(".txt") for p in _parts(rid)]
    return {"text": "\n\n".join(t.read_text(encoding="utf-8").strip() for t in txt if t.exists()).strip()}


@router.post("/api/rec/{rid}/inserted")
def rec_inserted(rid: int):
    _set(rid, inserted=1)
    return {"ok": True}


@router.delete("/api/rec/{rid}")
def rec_delete(rid: int):
    if current and current.rid == rid:
        raise HTTPException(409, "Najpierw zatrzymaj nagrywanie.")
    with get_conn() as c:
        c.execute("DELETE FROM recordings WHERE id=?", (rid,))
    shutil.rmtree(_dir(rid), ignore_errors=True)
    return {"deleted": 1}


# ---------- zamiana na tekst (kolejka, 1 wątek → zapytania po kolei) ----------
jobs: queue.Queue = queue.Queue()


def _transcribe(rid: int):
    from server import gemini, GeminiError
    parts = _parts(rid)
    _set(rid, status="transcribing", error="", parts=len(parts))
    for i, p in enumerate(parts, 1):
        out = p.with_suffix(".txt")
        if out.exists():
            continue
        if p.stat().st_size < 4096:   # strzęp < ~1 s (np. urwany przy awarii) — nic do zamiany
            out.write_text("", encoding="utf-8")
            _set(rid, done_parts=i)
            continue
        audio = base64.b64encode(p.read_bytes()).decode()
        for attempt in range(6):
            try:
                text = gemini([{"inline_data": {"mime_type": "audio/ogg", "data": audio}}, {"text": PROMPT}],
                              max_tokens=16384, temperature=0, timeout=300, kind="transcribe")
                break
            except GeminiError as e:
                daily = "PerDay" in e.msg or "per day" in e.msg.lower()
                if e.status in (429, 503) and not daily and attempt < 5:
                    time.sleep(30)    # limit na minutę → chwila przerwy i dalej
                    continue
                st = "limit" if e.status == 429 else "error"
                _set(rid, status=st, error=("Dzienny limit Gemini wyczerpany — dokończ jutro (zrobione części zostają)."
                                            if st == "limit" else e.msg))
                return
        out.write_text(text.strip(), encoding="utf-8")
        _set(rid, done_parts=i)
    _set(rid, status="done", done_parts=len(parts))


def _worker():
    while True:
        rid = jobs.get()
        try:
            _transcribe(rid)
        except Exception as e:
            _set(rid, status="error", error=str(e))


threading.Thread(target=_worker, daemon=True).start()


@router.post("/api/rec/{rid}/transcribe")
def rec_transcribe(rid: int):
    if current and current.rid == rid:
        raise HTTPException(409, "Najpierw zatrzymaj nagrywanie.")
    if not _parts(rid):
        raise HTTPException(400, "To nagranie jest puste.")
    _set(rid, status="queued", error="")
    jobs.put(rid)
    return {"queued": True}


# po awarii/zamknięciu aplikacji w trakcie: nagranie do tego miejsca zostaje, czeka na zamianę
with get_conn() as _c:
    _c.execute("UPDATE recordings SET status='pending' WHERE status IN ('recording','queued','transcribing')")
