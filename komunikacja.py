"""Dojazd: komunikacja miejska Poznania (ZTM, otwarte dane GTFS + opóźnienia GTFS-RT) i pociągi (PKP PLK, Otwarte Dane Kolejowe).

Rozkład ZTM pobieramy raz na dobę do wspólnej bazy (ROOT_DIR/cache — jedna kopia także dla wszystkich kont wersji webowej),
a odjazdy liczymy lokalnie: kursy, które jadą z przystanku A do przystanku B bez przesiadki.
Ulubione trasy są w bazie użytkownika (synchronizowane jak notatki).
PKP: szkielet — klucz API z formularza PLK (pdp-api.plk-sa.pl) w Ustawieniach, rozkład na dziś i jutro, połączenia A → B.
"""
import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import ROOT_DIR, DATA_DIR, get_conn

router = APIRouter()

CACHE = ROOT_DIR / "cache"
ZTM_DB = CACHE / "ztm_gtfs.db"
PKP_DB = CACHE / "pkp.db"
ZTM_GTFS_URL = os.getenv("ZTM_GTFS_URL", "https://www.ztm.poznan.pl/pl/dla-deweloperow/getGTFSFile")
ZTM_RT_URL = os.getenv("ZTM_RT_URL", "https://www.ztm.poznan.pl/pl/dla-deweloperow/getGtfsRtFile/?file=trip_updates.pb")
PKP_API = os.getenv("PKP_API_BASE", "https://pdp-api.plk-sa.pl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Asystent-studenta"
REFRESH = 24 * 3600


def init():
    with get_conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS transit_favs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            kind      TEXT DEFAULT 'ztm',     -- ztm | pkp
            from_name TEXT NOT NULL,
            to_name   TEXT NOT NULL,
            label     TEXT DEFAULT '',
            tram_only INTEGER DEFAULT 0,
            position  INTEGER DEFAULT 0
        );
        """)


init()


# ---------- czas w Polsce (serwer może stać w UTC; Windows bywa bez bazy stref) ----------
def now_pl() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Warsaw")).replace(tzinfo=None)
    except Exception:
        u = datetime.now(timezone.utc).replace(tzinfo=None)
        last_sun = lambda m: max(date(u.year, m, d) for d in range(25, 32) if date(u.year, m, d).weekday() == 6)
        summer = datetime.combine(last_sun(3), datetime.min.time()) + timedelta(hours=1) <= u < \
            datetime.combine(last_sun(10), datetime.min.time()) + timedelta(hours=1)
        return u + timedelta(hours=2 if summer else 1)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ł", "l"))
    return re.sub(r"\s+", " ", "".join(ch for ch in s if not unicodedata.combining(ch))).strip()


def _hms(t: str) -> int | None:
    try:
        p = [int(x) for x in t.strip().split(":")]
        return p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else 0)
    except Exception:
        return None


def _get(url: str, headers: dict | None = None, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _db(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30)
    c.row_factory = sqlite3.Row
    return c


# ---------- import w tle (jeden naraz na źródło) ----------
_state = {"ztm": {"importing": False, "error": ""}, "pkp": {"importing": False, "error": ""}}
_lock = threading.Lock()


def _meta(path, key):
    try:
        with _db(path) as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return r["v"] if r else None
    except Exception:
        return None


def _fresh(path) -> bool:
    at = _meta(path, "updated")
    return bool(at) and time.time() - float(at) < REFRESH


def _start(src: str, fn, *args):
    with _lock:
        if _state[src]["importing"]:
            return
        _state[src].update(importing=True, error="")

    def run():
        try:
            fn(*args)
        except Exception as e:
            _state[src]["error"] = str(e)[:300]
        finally:
            _state[src]["importing"] = False
    threading.Thread(target=run, daemon=True).start()


def _ensure(src: str, path, fn, *args):
    """Baza gotowa → używamy jej (a przestarzałą odświeżamy w tle). Brak → import w tle i 503 z komunikatem."""
    if path.exists() and _meta(path, "updated"):
        if not _fresh(path) and not _state[src]["error"]:
            _start(src, fn, *args)
        return
    if not _state[src]["error"]:
        _start(src, fn, *args)
    st = _state[src]
    raise HTTPException(503, st["error"] or "Pobieram rozkład (pierwszy raz trwa około minuty)…")


# ---------- ZTM Poznań: GTFS → SQLite ----------
def _csv(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))


def import_ztm(raw: bytes | None = None):
    raw = raw or _get(ZTM_GTFS_URL, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(raw))
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / "ztm_gtfs.tmp"
    tmp.unlink(missing_ok=True)
    c = _db(tmp)
    c.executescript("""
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE stops (stop_id TEXT PRIMARY KEY, name TEXT, nname TEXT, code TEXT);
    CREATE TABLE routes (route_id TEXT PRIMARY KEY, short TEXT, type INTEGER);
    CREATE TABLE trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT, headsign TEXT);
    CREATE TABLE stop_times (trip_id TEXT, seq INTEGER, stop_id TEXT, dep INTEGER);
    CREATE TABLE calendar (service_id TEXT, days TEXT, start TEXT, end TEXT);
    CREATE TABLE calendar_dates (service_id TEXT, date TEXT, type INTEGER);
    """)
    c.executemany("INSERT OR REPLACE INTO stops VALUES(?,?,?,?)",
                  ((r["stop_id"], r.get("stop_name", ""), norm(r.get("stop_name", "")), r.get("stop_code", "")) for r in _csv(z, "stops.txt")))
    c.executemany("INSERT OR REPLACE INTO routes VALUES(?,?,?)",
                  ((r["route_id"], r.get("route_short_name") or r.get("route_long_name", ""), int(r.get("route_type") or 3)) for r in _csv(z, "routes.txt")))
    c.executemany("INSERT OR REPLACE INTO trips VALUES(?,?,?,?)",
                  ((r["trip_id"], r["route_id"], r["service_id"], r.get("trip_headsign", "")) for r in _csv(z, "trips.txt")))
    c.executemany("INSERT INTO stop_times VALUES(?,?,?,?)",
                  ((r["trip_id"], int(r["stop_sequence"]), r["stop_id"], _hms(r.get("departure_time") or r.get("arrival_time") or ""))
                   for r in _csv(z, "stop_times.txt") if (r.get("departure_time") or r.get("arrival_time"))))
    wd = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    c.executemany("INSERT INTO calendar VALUES(?,?,?,?)",
                  ((r["service_id"], "".join(r.get(d, "0") for d in wd), r["start_date"], r["end_date"]) for r in _csv(z, "calendar.txt")))
    c.executemany("INSERT INTO calendar_dates VALUES(?,?,?)",
                  ((r["service_id"], r["date"], int(r["exception_type"])) for r in _csv(z, "calendar_dates.txt")))
    c.executescript("""
    CREATE INDEX st_stop ON stop_times(stop_id, dep);
    CREATE INDEX st_trip ON stop_times(trip_id, seq);
    CREATE INDEX stops_n ON stops(nname);
    """)
    c.execute("INSERT INTO meta VALUES('updated', ?)", (str(time.time()),))
    c.commit()
    c.close()
    os.replace(tmp, ZTM_DB)


def _services(c, d: date) -> list[str]:
    ds, wd = d.strftime("%Y%m%d"), d.weekday()
    on = {r[0] for r in c.execute("SELECT service_id, days FROM calendar WHERE start<=? AND end>=?", (ds, ds)) if r[1][wd:wd + 1] == "1"}
    for sid, typ in c.execute("SELECT service_id, type FROM calendar_dates WHERE date=?", (ds,)):
        (on.add if typ == 1 else on.discard)(sid)
    return list(on)


def _ids(c, name: str) -> list[str]:
    n = norm(name)
    return [r[0] for r in c.execute("SELECT stop_id FROM stops WHERE nname=?", (n,))]


def ztm_next(from_name: str, to_name: str, n: int = 5, tram_only: bool = False) -> list[dict]:
    with _db(ZTM_DB) as c:
        a, b = _ids(c, from_name), _ids(c, to_name)
        if not a:
            raise HTTPException(404, f"Nie znam przystanku „{from_name}”.")
        if not b:
            raise HTTPException(404, f"Nie znam przystanku „{to_name}”.")
        now = now_pl()
        today = now.date()
        secs = now.hour * 3600 + now.minute * 60 + now.second
        out = []
        # wczoraj (kursy po północy: czasy GTFS > 24:00), dziś, jutro — z przesunięciem względem dzisiejszej północy
        for off, d in ((-86400, today - timedelta(days=1)), (0, today), (86400, today + timedelta(days=1))):
            sv = _services(c, d)
            if not sv:
                continue
            q = f"""SELECT t.trip_id, r.short, r.type, t.headsign, sa.dep dep, sb.dep arr, sa.seq seq, sa.stop_id stop
                FROM stop_times sa
                JOIN stop_times sb ON sb.trip_id=sa.trip_id AND sb.seq>sa.seq AND sb.stop_id IN ({",".join("?" * len(b))})
                JOIN trips t ON t.trip_id=sa.trip_id JOIN routes r ON r.route_id=t.route_id
                WHERE sa.stop_id IN ({",".join("?" * len(a))}) AND sa.dep>=? AND sa.dep<=?
                  AND t.service_id IN ({",".join("?" * len(sv))}) {"AND r.type=0" if tram_only else ""}
                ORDER BY sa.dep LIMIT ?"""
            lo = secs - 120 - off   # 2 min wstecz: spóźniony tramwaj jeszcze może przyjechać
            for r in c.execute(q, (*b, *a, lo, lo + 6 * 3600, *sv, n * 3)):
                out.append({"trip_id": r["trip_id"], "line": r["short"], "type": r["type"], "headsign": r["headsign"],
                            "t": r["dep"] + off, "arr": r["arr"] + off, "seq": r["seq"]})
        # ten sam kurs wpada raz (najbliższy przystanek startowy); sortowanie po czasie
        seen, uniq = set(), []
        for x in sorted(out, key=lambda x: x["t"]):
            if (x["trip_id"], x["t"] // 86400) not in seen:
                seen.add((x["trip_id"], x["t"] // 86400))
                uniq.append(x)
    delays = rt_delays()
    res = []
    for x in uniq:
        dl = _delay_for(delays.get(x["trip_id"]), x["seq"]) if x["t"] - secs < 3 * 3600 else None
        real = x["t"] + (dl or 0)
        if real < secs - 30:
            continue
        hm = lambda s: f"{(s // 3600) % 24:02d}:{(s // 60) % 60:02d}"
        res.append({"line": x["line"], "tram": x["type"] == 0, "headsign": x["headsign"], "dep": hm(x["t"]), "arr": hm(x["arr"]),
                    "in_min": max(0, round((real - secs) / 60)), "travel_min": round((x["arr"] - x["t"]) / 60),
                    "delay_min": round(dl / 60) if dl is not None else None})
    res.sort(key=lambda x: x["in_min"])   # opóźnienie może zmienić kolejność
    return res[:n]


# ---------- GTFS-RT (opóźnienia na żywo): minimalny dekoder protobuf, bez dodatkowych bibliotek ----------
def _pb(buf: bytes):
    """Pola wiadomości protobuf: (numer, typ, wartość) — varint jako int, length-delimited jako bytes."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            return
        yield f, wt, v


def _varint(b: bytes, i: int):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if not x & 0x80:
            return r, i


def _sint(v: int) -> int:   # int32 w protobuf: ujemne jako 64-bitowe dopełnienie
    return v - (1 << 64) if v >= 1 << 63 else v


def parse_trip_updates(raw: bytes) -> dict:
    """trip_id → {"delay": opóźnienie kursu lub None, "stops": [(stop_sequence, opóźnienie), …]} (sekundy)."""
    out = {}
    for f, wt, ent in _pb(raw):
        if f != 2 or wt != 2:       # FeedMessage.entity
            continue
        for f2, wt2, tu in _pb(ent):
            if f2 != 3 or wt2 != 2:  # FeedEntity.trip_update
                continue
            trip_id, delay, stops = None, None, []
            for f3, wt3, v in _pb(tu):
                if f3 == 1 and wt3 == 2:                     # TripDescriptor
                    for f4, wt4, v4 in _pb(v):
                        if f4 == 1 and wt4 == 2:
                            trip_id = v4.decode("utf-8", "replace")
                elif f3 == 5 and wt3 == 0:                   # TripUpdate.delay
                    delay = _sint(v)
                elif f3 == 2 and wt3 == 2:                   # StopTimeUpdate
                    seq, d = None, None
                    for f4, wt4, v4 in _pb(v):
                        if f4 == 1 and wt4 == 0:
                            seq = v4
                        elif f4 in (2, 3) and wt4 == 2:      # arrival / departure → StopTimeEvent.delay
                            for f5, wt5, v5 in _pb(v4):
                                if f5 == 1 and wt5 == 0:
                                    d = _sint(v5) if f4 == 3 or d is None else d
                    if d is not None:
                        stops.append((seq if seq is not None else -1, d))
            if trip_id:
                out[trip_id] = {"delay": delay, "stops": sorted(stops)}
    return out


_rt = {"at": 0.0, "data": {}}


def rt_delays() -> dict:
    if time.time() - _rt["at"] < 30:
        return _rt["data"]
    _rt["at"] = time.time()
    try:
        _rt["data"] = parse_trip_updates(_get(ZTM_RT_URL, timeout=8))
    except Exception:
        pass   # brak danych na żywo → sam rozkład
    return _rt["data"]


def _delay_for(u: dict | None, seq: int) -> int | None:
    if not u:
        return None
    best = None
    for s, d in u["stops"]:
        if s == -1 or s <= seq:
            best = d
    if best is None and u["stops"]:
        best = u["stops"][0][1]
    return best if best is not None else u["delay"]


# ---------- PKP PLK: Otwarte Dane Kolejowe (szkielet) ----------
def _pkp_key() -> str | None:
    k = os.getenv("PKP_PLK_APIKEY")
    if k:
        return k.strip()
    p = DATA_DIR / ".plk_key"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _pkp_time(t, day) -> int | None:
    if t in (None, ""):
        return None
    s = t if isinstance(t, int) else _hms(str(t))
    return None if s is None else s + int(day or 0) * 86400


def import_pkp(key: str, raw: bytes | None = None):
    if raw is None:
        d = now_pl().date()
        q = f"?dateFrom={(d - timedelta(days=1)).isoformat()}&dateTo={(d + timedelta(days=1)).isoformat()}"
        try:
            raw = _get(PKP_API + "/api/v1/schedules/shortened" + q, {"X-Api-Key": key, "Accept": "application/json"}, timeout=180)
        except urllib.error.HTTPError as e:
            raise RuntimeError("Klucz PKP jest nieprawidłowy albo jeszcze nieaktywny (aktywacja trwa 3–5 dni roboczych)."
                               if e.code in (401, 403) else f"API PKP: błąd {e.code}")
    data = json.loads(raw)
    dc = data.get("dc") or {}
    st = dc.get("st") or {}
    stations = st.values() if isinstance(st, dict) else st
    carriers = dc.get("cr") or {}
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / "pkp.tmp"
    tmp.unlink(missing_ok=True)
    c = _db(tmp)
    c.executescript("""
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT, nname TEXT);
    CREATE TABLE trains (tid INTEGER PRIMARY KEY, carrier TEXT, cat TEXT, number TEXT, name TEXT);
    CREATE TABLE train_dates (tid INTEGER, date TEXT);
    CREATE TABLE stops (tid INTEGER, ord INTEGER, station INTEGER, arr INTEGER, dep INTEGER, platform TEXT);
    """)
    c.executemany("INSERT OR REPLACE INTO stations VALUES(?,?,?)",
                  ((s.get("id"), s.get("nm", ""), norm(s.get("nm", ""))) for s in stations if isinstance(s, dict) and s.get("id") is not None))
    routes = data.get("rt") or []
    if isinstance(routes, dict):
        routes = routes.get("item") or list(routes.values())
    for tid, r in enumerate(routes):
        cats = sorted({s.get("dcc") or s.get("acc") for s in r.get("st", []) if s.get("dcc") or s.get("acc")})
        cc = (r.get("cc") or "").strip()
        c.execute("INSERT INTO trains VALUES(?,?,?,?,?)",
                  (tid, carriers.get(r.get("cc"), cc) or cc, "/".join(cats) or r.get("ccs", ""),
                   str(r.get("nn") or r.get("idn") or r.get("ian") or ""), r.get("nm") or ""))
        c.executemany("INSERT INTO train_dates VALUES(?,?)", ((tid, str(x)[:10]) for x in r.get("od", [])))
        rows = []
        for s in sorted(r.get("st", []), key=lambda s: s.get("ord", 0)):
            a, d = _pkp_time(s.get("atm"), s.get("ady")), _pkp_time(s.get("dtm"), s.get("ddy"))
            a, d = a if a is not None else d, d if d is not None else a
            if a is not None:
                rows.append((tid, s.get("ord", 0), s.get("id"), a, d, s.get("dpl") or s.get("apl") or ""))
        c.executemany("INSERT INTO stops VALUES(?,?,?,?,?,?)", rows)
    c.executescript("CREATE INDEX s_st ON stops(station, dep); CREATE INDEX s_tid ON stops(tid, ord); CREATE INDEX td ON train_dates(tid, date); CREATE INDEX st_n ON stations(nname);")
    c.execute("INSERT INTO meta VALUES('updated', ?)", (str(time.time()),))
    c.commit()
    c.close()
    os.replace(tmp, PKP_DB)


def pkp_next(from_name: str, to_name: str, n: int = 5) -> list[dict]:
    with _db(PKP_DB) as c:
        ids = lambda nm: [r[0] for r in c.execute("SELECT id FROM stations WHERE nname=?", (norm(nm),))]
        a, b = ids(from_name), ids(to_name)
        if not a or not b:
            raise HTTPException(404, f"Nie znam stacji „{from_name if not a else to_name}”.")
        now = now_pl()
        secs = now.hour * 3600 + now.minute * 60
        out = []
        for off, d in ((-86400, now.date() - timedelta(days=1)), (0, now.date()), (86400, now.date() + timedelta(days=1))):
            q = f"""SELECT t.*, sa.dep dep, sb.arr arr, sa.platform platform FROM stops sa
                JOIN stops sb ON sb.tid=sa.tid AND sb.ord>sa.ord AND sb.station IN ({",".join("?" * len(b))})
                JOIN trains t ON t.tid=sa.tid JOIN train_dates td ON td.tid=sa.tid AND td.date=?
                WHERE sa.station IN ({",".join("?" * len(a))}) AND sa.dep>=? ORDER BY sa.dep LIMIT ?"""
            for r in c.execute(q, (*b, d.isoformat(), *a, secs - off, n)):
                out.append((r["dep"] + off, r))
        out.sort(key=lambda x: x[0])
    hm = lambda s: f"{(s // 3600) % 24:02d}:{(s // 60) % 60:02d}"
    return [{"carrier": r["carrier"], "cat": r["cat"], "number": r["number"], "name": r["name"], "dep": hm(t), "arr": hm(r["arr"] + t - r["dep"]),
             "platform": r["platform"], "in_min": max(0, round((t - secs) / 60)), "travel_min": round((r["arr"] - r["dep"]) / 60)}
            for t, r in out[:n]]


# ---------- API ----------
def _status(src, path):
    st = _state[src]
    at = _meta(path, "updated")
    return {"ready": bool(at) and path.exists(), "importing": st["importing"], "error": st["error"],
            "updated": datetime.fromtimestamp(float(at)).isoformat(timespec="minutes") if at else None}


@router.get("/api/transit/status")
def status():
    return {"ztm": _status("ztm", ZTM_DB), "pkp": {**_status("pkp", PKP_DB), "key": bool(_pkp_key())}}


@router.post("/api/transit/{src}/refresh")
def refresh(src: str):
    if src == "ztm":
        _state["ztm"]["error"] = ""
        _start("ztm", import_ztm)
    elif src == "pkp":
        key = _pkp_key()
        if not key:
            raise HTTPException(400, "Najpierw wpisz klucz API PKP.")
        _state["pkp"]["error"] = ""
        _start("pkp", import_pkp, key)
    return status()


def _search(path, table, q, limit=12):
    qn = norm(q)
    if len(qn) < 2:
        return []
    with _db(path) as c:
        rows = c.execute(f"SELECT DISTINCT name, nname FROM {table} WHERE nname LIKE ? LIMIT 400", (f"%{qn}%",)).fetchall()
    # najpierw nazwy zaczynające się od wpisanego tekstu, potem słowa, potem reszta
    rank = lambda r: (not r["nname"].startswith(qn), f" {qn}" not in f" {r['nname']}", len(r["name"]))
    return [r["name"] for r in sorted(rows, key=rank)[:limit]]


@router.get("/api/transit/ztm/stops")
def ztm_stops(q: str = ""):
    _ensure("ztm", ZTM_DB, import_ztm)
    return _search(ZTM_DB, "stops", q)


@router.get("/api/transit/ztm/next")
def ztm_departures(frm: str, to: str, n: int = 6, tram: bool = False):
    _ensure("ztm", ZTM_DB, import_ztm)
    return ztm_next(frm, to, max(1, min(20, n)), tram)


class PkpKey(BaseModel):
    key: str


@router.post("/api/transit/pkp/key")
def pkp_key(p: PkpKey):
    k = "".join(p.key.split())
    f = DATA_DIR / ".plk_key"
    if k:
        f.write_text(k, encoding="utf-8")
        _state["pkp"]["error"] = ""
        _start("pkp", import_pkp, k)
    else:
        f.unlink(missing_ok=True)
    return status()


def _pkp_ready():
    key = _pkp_key()
    if not key:
        raise HTTPException(400, "Brak klucza API PKP — wpisz go w sekcji Pociągi.")
    _ensure("pkp", PKP_DB, import_pkp, key)


@router.get("/api/transit/pkp/stations")
def pkp_stations(q: str = ""):
    _pkp_ready()
    return _search(PKP_DB, "stations", q)


@router.get("/api/transit/pkp/next")
def pkp_departures(frm: str, to: str, n: int = 5):
    _pkp_ready()
    return pkp_next(frm, to, max(1, min(15, n)))


# ---------- ulubione trasy ----------
class Fav(BaseModel):
    kind: str = "ztm"
    from_name: str
    to_name: str
    label: str = ""
    tram_only: bool = False


@router.get("/api/transit/favs")
def favs():
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT id, kind, from_name, to_name, label, tram_only, position FROM transit_favs ORDER BY position, id")]


@router.post("/api/transit/favs")
def fav_add(p: Fav):
    f, t = p.from_name.strip()[:120], p.to_name.strip()[:120]
    if not f or not t:
        raise HTTPException(400, "Podaj przystanek początkowy i końcowy.")
    with get_conn() as c:
        if c.execute("SELECT 1 FROM transit_favs WHERE kind=? AND from_name=? AND to_name=? AND tram_only=?",
                     (p.kind, f, t, int(p.tram_only))).fetchone():
            raise HTTPException(400, "Ta trasa jest już w ulubionych.")
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM transit_favs").fetchone()[0]
        fid = c.execute("INSERT INTO transit_favs(kind, from_name, to_name, label, tram_only, position) VALUES(?,?,?,?,?,?)",
                        (p.kind if p.kind in ("ztm", "pkp") else "ztm", f, t, p.label.strip()[:60], int(p.tram_only), pos)).lastrowid
    return {"id": fid}


@router.delete("/api/transit/favs/{fid}")
def fav_del(fid: int):
    with get_conn() as c:
        c.execute("DELETE FROM transit_favs WHERE id=?", (fid,))
    return {"ok": True}


@router.get("/api/transit/favs/next")
def favs_next(n: int = 3):
    """Ulubione trasy z najbliższymi odjazdami — kafelek na Pulpicie i lista w zakładce Dojazd."""
    out = []
    for f in favs():
        item = {**f, "deps": [], "error": ""}
        try:
            if f["kind"] == "pkp":
                _pkp_ready()
                item["deps"] = pkp_next(f["from_name"], f["to_name"], n)
            else:
                _ensure("ztm", ZTM_DB, import_ztm)
                item["deps"] = ztm_next(f["from_name"], f["to_name"], n, bool(f["tram_only"]))
        except HTTPException as e:
            item["error"] = e.detail
        except Exception as e:
            item["error"] = str(e)[:200]
        out.append(item)
    return out
