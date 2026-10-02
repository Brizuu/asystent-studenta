"""Synchronizacja między urządzeniami na jednym koncie (przez serwer kont).

Każdy rekord synchronizowanych tabel ma stały `uid` i znacznik `sync_at` (UTC, ustawiany triggerem przy
każdej lokalnej zmianie). Usunięcia trafiają do `sync_deleted`. Wysyłka = rekordy zmienione od ostatniej
wysyłki; pobranie = rekordy z serwera nowsze niż kursor. Konflikt: wygrywa nowsza zmiana.
Klucze obce wysyłane są jako uid rekordu nadrzędnego, więc działają na każdym urządzeniu.
Pliki (załączniki, nagrania) nie są synchronizowane — tylko dane z bazy."""
import json
import os
import platform
import socket
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn

router = APIRouter()

# tabela → {kolumna FK: tabela nadrzędna}; kolejność = kolejność stosowania (rodzice przed dziećmi)
TABLES = {
    "notebooks": {},
    "note_groups": {"notebook_id": "notebooks"},
    "notes": {"notebook_id": "notebooks", "group_id": "note_groups"},
    "tasks": {"note_id": "notes"},
    "todo_lists": {},
    "todo_items": {"list_id": "todo_lists", "note_id": "notes", "notebook_id": "notebooks", "anchor_id": "tasks", "task_id": "tasks"},
    "todo_checks": {"item_id": "todo_items"},
    "costs": {},
    "cost_entries": {"cost_id": "costs"},
    "incomes": {},
}
REQUIRED_FK = {("note_groups", "notebook_id"), ("notes", "notebook_id"), ("todo_items", "list_id"), ("todo_checks", "item_id")}
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _cols(c, t) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA table_info({t})")]


def setup():
    """Kolumny uid/sync_at, triggery i stan synchronizacji (idempotentne — przy każdym starcie)."""
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sync_deleted (tbl TEXT NOT NULL, uid TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (tbl, uid));
        CREATE TABLE IF NOT EXISTS sync_state (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS sync_seen (uid TEXT PRIMARY KEY);   -- uid znane serwerowi (wysłane albo pobrane)
        """)
        for t in TABLES:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
                continue
            have = _cols(c, t)
            if "uid" not in have:
                c.execute(f"ALTER TABLE {t} ADD COLUMN uid TEXT")
            if "sync_at" not in have:
                c.execute(f"ALTER TABLE {t} ADD COLUMN sync_at TEXT")
            c.execute(f"UPDATE {t} SET uid=lower(hex(randomblob(16))) WHERE uid IS NULL")
            c.execute(f"UPDATE {t} SET sync_at={NOW} WHERE sync_at IS NULL")
            c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{t}_uid ON {t}(uid)")
            c.executescript(f"""
            CREATE TRIGGER IF NOT EXISTS sy_{t}_ins AFTER INSERT ON {t} WHEN NEW.uid IS NULL OR NEW.sync_at IS NULL BEGIN
              UPDATE {t} SET uid=COALESCE(NEW.uid, lower(hex(randomblob(16)))), sync_at=COALESCE(NEW.sync_at, {NOW}) WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS sy_{t}_upd AFTER UPDATE ON {t} WHEN NEW.sync_at IS OLD.sync_at BEGIN
              UPDATE {t} SET sync_at={NOW} WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS sy_{t}_del AFTER DELETE ON {t} WHEN OLD.uid IS NOT NULL BEGIN
              INSERT OR REPLACE INTO sync_deleted(tbl, uid, at) VALUES('{t}', OLD.uid, {NOW});
            END;
            """)
        if not c.execute("SELECT 1 FROM sync_state WHERE k='device_uid'").fetchone():
            c.execute("INSERT INTO sync_state VALUES('device_uid', ?)", (uuid.uuid4().hex,))
            name = socket.gethostname() or "Komputer"
            if not os.getenv("ASYSTENT_DESKTOP"):
                name += " (przeglądarka)"
            c.execute("INSERT INTO sync_state VALUES('device_name', ?)", (name[:60],))


def _state(c) -> dict:
    return {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM sync_state")}


def _set(c, **kv):
    for k, v in kv.items():
        c.execute("INSERT OR REPLACE INTO sync_state(k, v) VALUES(?, ?)", (k, None if v is None else str(v)))


def summary(c) -> dict:
    out = {}
    for t in TABLES:
        try:
            out[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            out[t] = 0
    last = [c.execute(f"SELECT MAX(sync_at) FROM {t}").fetchone()[0] for t in TABLES if out.get(t) is not None]
    out["last_change"] = max([x for x in last if x] or [None]) if any(last) else None
    return out


def _uid_map(c, t) -> dict:
    return {r[0]: r[1] for r in c.execute(f"SELECT id, uid FROM {t}")}


def collect(c, since: str) -> list[dict]:
    """Zmiany od `since` (rekordy + usunięcia) w formacie serwera."""
    items = []
    maps = {t: _uid_map(c, t) for t in TABLES}
    for t, fks in TABLES.items():
        cols = _cols(c, t)
        for r in c.execute(f"SELECT * FROM {t} WHERE sync_at > ?", (since,)):
            d = {k: r[k] for k in cols if k not in ("id", "uid", "sync_at")}
            for col, parent in fks.items():
                if col in d:
                    d[col] = maps[parent].get(d[col]) if d[col] is not None else None
            items.append({"tbl": t, "uid": r["uid"], "updated_at": r["sync_at"], "deleted": False, "data": d})
    for r in c.execute("SELECT tbl, uid, at FROM sync_deleted WHERE at > ?", (since,)):
        if r["tbl"] in TABLES:
            items.append({"tbl": r["tbl"], "uid": r["uid"], "updated_at": r["at"], "deleted": True, "data": None})
    return items


def apply(c, items: list[dict], applied: set | None = None) -> dict:
    """Wpisuje zmiany z serwera (nowsza wersja wygrywa). Zwraca liczniki."""
    order = list(TABLES)
    items = sorted((i for i in items if i.get("tbl") in TABLES), key=lambda i: (order.index(i["tbl"]), i["updated_at"]))
    applied = set() if applied is None else applied
    stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0}
    cols_cache = {t: _cols(c, t) for t in TABLES}
    for it in items:
        t, uid, at = it["tbl"], it["uid"], it["updated_at"]
        row = c.execute(f"SELECT id, sync_at FROM {t} WHERE uid=?", (uid,)).fetchone()
        if it.get("deleted"):
            if row and (row["sync_at"] or "") <= at:
                c.execute(f"DELETE FROM {t} WHERE id=?", (row["id"],))
                stats["deleted"] += 1
            continue
        if row and (row["sync_at"] or "") >= at:
            stats["skipped"] += 1
            continue
        data = dict(it.get("data") or {})
        ok = True
        for col, parent in TABLES[t].items():
            if col not in data:
                continue
            ref = data[col]
            pid = c.execute(f"SELECT id FROM {parent} WHERE uid=?", (ref,)).fetchone() if ref else None
            data[col] = pid[0] if pid else None
            if data[col] is None and (t, col) in REQUIRED_FK:
                ok = False
        if not ok:
            stats["skipped"] += 1
            continue
        data = {k: v for k, v in data.items() if k in cols_cache[t] and k not in ("id", "uid", "sync_at")}
        if not row and t == "tasks" and data.get("ext_uid"):
            # te same zajęcia z USOS zaimportowane na dwóch urządzeniach — scal zamiast dublować
            dup = c.execute("SELECT id, sync_at FROM tasks WHERE ext_uid=?", (data["ext_uid"],)).fetchone()
            if dup:
                c.execute("UPDATE tasks SET uid=?, sync_at=? WHERE id=?", (uid, at, dup["id"]))
                row = c.execute("SELECT id, sync_at FROM tasks WHERE id=?", (dup["id"],)).fetchone()
        if not row and t == "todo_lists" and data.get("name"):
            # domyślna lista („Moje zadania”) tworzona na każdym urządzeniu — przejmij zamiast dublować
            dup = c.execute("SELECT id FROM todo_lists WHERE name=? AND uid NOT IN (SELECT uid FROM sync_seen)", (data["name"],)).fetchone()
            if dup:
                c.execute("UPDATE todo_lists SET uid=?, sync_at=? WHERE id=?", (uid, at, dup["id"]))
                row = c.execute("SELECT id, sync_at FROM todo_lists WHERE id=?", (dup["id"],)).fetchone()
        if row:
            sets = ",".join(f"{k}=?" for k in data) + ("," if data else "") + "sync_at=?"
            c.execute(f"UPDATE {t} SET {sets} WHERE id=?", (*data.values(), at, row["id"]))
            stats["updated"] += 1
        else:
            keys = [*data, "uid", "sync_at"]
            c.execute(f"INSERT INTO {t}({','.join(keys)}) VALUES({','.join('?' * len(keys))})", (*data.values(), uid, at))
            stats["added"] += 1
        c.execute("DELETE FROM sync_deleted WHERE tbl=? AND uid=?", (t, uid))
        c.execute("INSERT OR IGNORE INTO sync_seen(uid) VALUES(?)", (uid,))
        applied.add((t, uid))
    return stats


def _cloud(base: str, token: str, method: str, path: str, body=None) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=None if body is None else json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:
            msg = None
        raise HTTPException(e.code if e.code in (400, 401, 403, 413) else 502, msg or f"Serwer kont: błąd {e.code}")
    except Exception as e:
        raise HTTPException(502, f"Nie udało się połączyć z serwerem kont: {e}")


def _device(c) -> dict:
    st = _state(c)
    return {"uid": st["device_uid"], "name": st.get("device_name") or "Urządzenie",
            "platform": ("Windows" if os.name == "nt" else platform.system()) + (" · aplikacja" if os.getenv("ASYSTENT_DESKTOP") else " · przeglądarka"),
            "summary": summary(c)}


class SyncRun(BaseModel):
    cloud: str
    token: str


@router.post("/api/sync/run")
def run(p: SyncRun):
    """Najpierw pobranie (nowe urządzenie przejmuje dane konta), potem wysyłka własnych zmian."""
    started = _now()
    with get_conn() as c:
        st = _state(c)
        dev_uid = st["device_uid"]
    cursor = int(st.get("cursor") or 0)
    stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0}
    applied: set = set()
    pulled = 0
    while True:
        r = _cloud(p.cloud, p.token, "GET", f"/sync/pull?since={cursor}&device={dev_uid}")
        if r["items"]:
            with get_conn() as c:
                s_ = apply(c, r["items"], applied)
            for k in stats:
                stats[k] += s_[k]
            pulled += len(r["items"])
        cursor = r["cursor"]
        if not r.get("more"):
            break
    with get_conn() as c:
        items = [i for i in collect(c, st.get("last_push") or "") if (i["tbl"], i["uid"]) not in applied]
        dev = _device(c)
    pushed = 0
    for i in range(0, max(1, len(items)), 500):   # paczki; zawsze co najmniej jedno wywołanie — aktualizuje liczniki urządzenia
        chunk = items[i:i + 500]
        _cloud(p.cloud, p.token, "POST", "/sync/push", {"device": dev, "items": chunk})
        pushed += len(chunk)
    with get_conn() as c:
        c.executemany("INSERT OR IGNORE INTO sync_seen(uid) VALUES(?)", [(i["uid"],) for i in items])
        _set(c, last_push=started, cursor=cursor, last_sync=_now())
    return {"pushed": pushed, "pulled": pulled, **stats, "last_sync": _state_get("last_sync")}


def _state_get(k: str):
    with get_conn() as c:
        r = c.execute("SELECT v FROM sync_state WHERE k=?", (k,)).fetchone()
        return r[0] if r else None


@router.get("/api/sync/state")
def state():
    with get_conn() as c:
        st = _state(c)
        return {"device": _device(c), "last_sync": st.get("last_sync"), "auto": st.get("auto", "1") == "1",
                "pending": len(collect(c, st.get("last_push") or ""))}


class SyncSettings(BaseModel):
    name: str | None = None
    auto: bool | None = None


@router.post("/api/sync/settings")
def settings(p: SyncSettings):
    with get_conn() as c:
        if p.name is not None and p.name.strip():
            _set(c, device_name=p.name.strip()[:60])
        if p.auto is not None:
            _set(c, auto="1" if p.auto else "0")
    return state()


@router.post("/api/sync/reset")
def reset():
    """Inne konto na tym urządzeniu: przy następnej synchronizacji wyślij wszystko od nowa."""
    with get_conn() as c:
        _set(c, last_push="", cursor=0, last_sync=None)
        c.execute("DELETE FROM sync_seen")
    return {"ok": True}
