"""Dojazd: komunikacja miejska Poznania (ZTM, otwarte dane GTFS + opóźnienia GTFS-RT) i pociągi (PKP PLK, Otwarte Dane Kolejowe).

Rozkład ZTM pobieramy raz na dobę do wspólnej bazy (ROOT_DIR/cache — jedna kopia także dla wszystkich kont wersji webowej),
a odjazdy liczymy lokalnie: kursy, które jadą z przystanku A do przystanku B bez przesiadki.
Ulubione trasy są w bazie użytkownika (synchronizowane jak notatki).
PKP: rozkład z Otwartych Danych Kolejowych (raz na dobę) + opóźnienia na żywo (najwyżej raz na minutę, wspólne dla wszystkich).
Klucz PLK trzyma serwer kont (ustawia go administrator) — pociągi liczy serwer, aplikacje pytają go przez /pkp/*.

Bazy rozkładu mają w nazwie znacznik czasu (ztmdb_<ts>.db): nowa wersja dostaje nowy plik, więc na Windows nie trzeba
podmieniać pliku, który ktoś właśnie czyta (os.replace na otwartym pliku = „Odmowa dostępu”). Stare pliki sprzątamy, gdy się da.
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
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import ROOT_DIR, get_conn

router = APIRouter()

CACHE = ROOT_DIR / "cache"
LEGACY = {"ztm": "ztm_gtfs.db", "pkp": "pkp.db"}   # nazwy sprzed wersji 1.4.3
KEY_PROVIDER = None   # serwer kont podstawia tu funkcję zwracającą klucz PLK ustawiony przez administratora
ZTM_GTFS_URL = os.getenv("ZTM_GTFS_URL", "https://www.ztm.poznan.pl/pl/dla-deweloperow/getGTFSFile")
ZTM_RT_URL = os.getenv("ZTM_RT_URL", "https://www.ztm.poznan.pl/pl/dla-deweloperow/getGtfsRtFile/?file=trip_updates.pb")
GEOCODE_URL = os.getenv("GEOCODE_URL", "https://nominatim.openstreetmap.org/search")
PKP_API = os.getenv("PKP_API_BASE", "https://pdp-api.plk-sa.pl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Asystent-studenta"
REFRESH = 24 * 3600
ZTM_SCHEMA = "3"   # zmiana układu bazy rozkładu → ponowny import w tle (stara baza działa do czasu podmiany)


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


def _raw(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=30)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def _db(path):
    """Połączenie zamykane od razu po użyciu (otwarte uchwyty blokowałyby na Windows sprzątanie starych baz)."""
    c = _raw(path)
    try:
        yield c
    finally:
        c.close()


def _cur(src: str):
    """Najnowsza gotowa baza rozkładu (albo None)."""
    files = sorted(CACHE.glob(f"{src}db_*.db")) if CACHE.exists() else []
    if files:
        return files[-1]
    old = CACHE / LEGACY[src]
    return old if old.exists() else None


def _publish(src: str, tmp):
    final = CACHE / f"{src}db_{int(time.time() * 1000)}.db"
    os.replace(tmp, final)   # nowa nazwa — nigdy nie koliduje z otwartym plikiem
    for f in [*CACHE.glob(f"{src}db_*.db"), *CACHE.glob(f"{src}db_*.tmp"), CACHE / LEGACY[src]]:
        if f != final and f.exists():
            try:
                f.unlink()
            except OSError:   # ktoś jeszcze czyta starą wersję — usuniemy przy następnym imporcie
                pass


def _tmp(src: str):
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / f"{src}db_{int(time.time() * 1000)}.tmp"


# ---------- import w tle (jeden naraz na źródło) ----------
_state = {"ztm": {"importing": False, "error": ""}, "pkp": {"importing": False, "error": ""}}
_lock = threading.Lock()


def _meta(path, key):
    if path is None:
        return None
    try:
        with _db(path) as c:
            r = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return r["v"] if r else None
    except Exception:
        return None


def _fresh(src: str, path) -> bool:
    at = _meta(path, "updated")
    if src == "ztm" and _meta(path, "schema") != ZTM_SCHEMA:
        return False
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


def _ensure(src: str, fn, *args):
    """Baza gotowa → zwraca jej ścieżkę (a przestarzałą odświeża w tle). Brak → import w tle i 503 z komunikatem."""
    path = _cur(src)
    if path and _meta(path, "updated"):
        if not _fresh(src, path) and not _state[src]["error"]:
            _start(src, fn, *args)
        return path
    if not _state[src]["error"]:
        _start(src, fn, *args)
    st = _state[src]
    raise HTTPException(503, st["error"] or "Pobieram rozkład (pierwszy raz trwa około minuty)…")


def ztm_db():
    return _ensure("ztm", import_ztm)


# ---------- ZTM Poznań: GTFS → SQLite ----------
def _csv(z: zipfile.ZipFile, name: str):
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig", newline=""))


def import_ztm(raw: bytes | None = None):
    raw = raw or _get(ZTM_GTFS_URL, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(raw))
    tmp = _tmp("ztm")
    c = _raw(tmp)
    c.executescript("""
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE stops (stop_id TEXT PRIMARY KEY, name TEXT, nname TEXT, code TEXT, lat REAL, lon REAL);
    CREATE TABLE routes (route_id TEXT PRIMARY KEY, short TEXT, type INTEGER);
    CREATE TABLE trips (trip_id TEXT PRIMARY KEY, route_id TEXT, service_id TEXT, headsign TEXT);
    CREATE TABLE stop_times (trip_id TEXT, seq INTEGER, stop_id TEXT, dep INTEGER);
    CREATE TABLE calendar (service_id TEXT, days TEXT, start TEXT, end TEXT);
    CREATE TABLE calendar_dates (service_id TEXT, date TEXT, type INTEGER);
    """)
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    c.executemany("INSERT OR REPLACE INTO stops VALUES(?,?,?,?,?,?)",
                  ((r["stop_id"], r.get("stop_name", ""), norm(r.get("stop_name", "")), r.get("stop_code", ""),
                    num(r.get("stop_lat")), num(r.get("stop_lon"))) for r in _csv(z, "stops.txt")))
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
    CREATE TABLE stop_lines AS SELECT DISTINCT s.nname nname, r.short short, r.type type
        FROM stop_times st JOIN stops s ON s.stop_id=st.stop_id JOIN trips t ON t.trip_id=st.trip_id JOIN routes r ON r.route_id=t.route_id;
    CREATE INDEX sl_n ON stop_lines(nname);
    """)
    c.execute("INSERT INTO meta VALUES('schema', ?)", (ZTM_SCHEMA,))
    c.execute("INSERT INTO meta VALUES('updated', ?)", (str(time.time()),))
    c.commit()
    c.close()
    _publish("ztm", tmp)


def _services(c, d: date) -> list[str]:
    ds, wd = d.strftime("%Y%m%d"), d.weekday()
    on = {r[0] for r in c.execute("SELECT service_id, days FROM calendar WHERE start<=? AND end>=?", (ds, ds)) if r[1][wd:wd + 1] == "1"}
    for sid, typ in c.execute("SELECT service_id, type FROM calendar_dates WHERE date=?", (ds,)):
        (on.add if typ == 1 else on.discard)(sid)
    return list(on)


def _ids(c, name: str) -> list[str]:
    n = norm(name)
    return [r[0] for r in c.execute("SELECT stop_id FROM stops WHERE nname=?", (n,))]


def _ref(at: float | None):
    """Punkt odniesienia wyszukiwania: teraz albo wybrana godzina (czas uniksowy).
    Zwraca (data, sekundy od północy tej daty, czas uniksowy tej północy)."""
    now_ts = time.time()
    base = now_pl() if at is None else now_pl() + timedelta(seconds=at - now_ts)
    secs = base.hour * 3600 + base.minute * 60 + base.second
    return base.date(), secs, (now_ts if at is None else at) - secs


def _days(d: date):
    # wczoraj (kursy po północy: czasy GTFS > 24:00), dziś, jutro — z przesunięciem względem północy dnia odniesienia
    return ((-86400, d - timedelta(days=1)), (0, d), (86400, d + timedelta(days=1)))


def _window(secs: int, off: int, back: bool, live: bool):
    """Zakres czasów odjazdu (w sekundach dnia rozkładowego) i kierunek sortowania."""
    if back:
        hi = secs - off
        return hi - 6 * 3600, hi - 1, "DESC"
    lo = secs - off - (120 if live else 0)   # na żywo 2 min wstecz: spóźniony tramwaj jeszcze może przyjechać
    return lo, lo + 6 * 3600, "ASC"


def ztm_next(from_name: str, to_name: str, n: int = 5, tram_only: bool = False, at: float | None = None, back: bool = False) -> list[dict]:
    with _db(ztm_db()) as c:
        a, b = _ids(c, from_name), _ids(c, to_name)
        if not a:
            raise HTTPException(404, f"Nie znam przystanku „{from_name}”.")
        if not b:
            raise HTTPException(404, f"Nie znam przystanku „{to_name}”.")
        d0, secs, epoch0 = _ref(at)
        out = []
        for off, d in _days(d0):
            sv = _services(c, d)
            if not sv:
                continue
            lo, hi, order = _window(secs, off, back, at is None)
            q = f"""SELECT t.trip_id, r.short, r.type, t.headsign, sa.dep dep, sb.dep arr, sa.seq seq, sb.seq seq_to
                FROM stop_times sa
                JOIN stop_times sb ON sb.trip_id=sa.trip_id AND sb.seq>sa.seq AND sb.stop_id IN ({",".join("?" * len(b))})
                JOIN trips t ON t.trip_id=sa.trip_id JOIN routes r ON r.route_id=t.route_id
                WHERE sa.stop_id IN ({",".join("?" * len(a))}) AND sa.dep>=? AND sa.dep<=?
                  AND t.service_id IN ({",".join("?" * len(sv))}) {"AND r.type=0" if tram_only else ""}
                ORDER BY sa.dep {order} LIMIT ?"""
            for r in c.execute(q, (*b, *a, lo, hi, *sv, n * 3)):
                out.append({"trip_id": r["trip_id"], "line": r["short"], "type": r["type"], "headsign": r["headsign"],
                            "t": r["dep"] + off, "arr": r["arr"] + off, "seq": r["seq"], "seq_to": r["seq_to"], "t0": epoch0 + off})
        # ten sam kurs wpada raz (najbliższy przystanek startowy)
        seen, uniq = set(), []
        for x in sorted(out, key=lambda x: x["t"]):
            if (x["trip_id"], x["t0"]) not in seen:
                seen.add((x["trip_id"], x["t0"]))
                uniq.append(x)
    return _shape(uniq, secs, epoch0, n, at is None, back)


def _hm(s: int) -> str:
    return f"{(s // 3600) % 24:02d}:{(s // 60) % 60:02d}"


def _shape(rows: list, secs: int, epoch0: float, n: int, live: bool = True, back: bool = False) -> list[dict]:
    """Kursy → odjazdy z opóźnieniem na żywo. ts/arr_ts = czas uniksowy (odliczanie w przeglądarce co sekundę)."""
    now = time.time()
    near = [x for x in rows if abs(epoch0 + x["t"] - now) < 3 * 3600]
    delays = rt_delays() if near else {}
    res = []
    for x in rows:
        dl = _delay_for(delays.get(x["trip_id"]), x["seq"]) if abs(epoch0 + x["t"] - now) < 3 * 3600 else None
        real = x["t"] + (dl or 0)
        if live and real < secs - 30:
            continue
        d = {"line": x["line"], "tram": x["type"] == 0, "headsign": x["headsign"], "dep": _hm(x["t"]), "dep_real": _hm(real),
             "ts": round(epoch0 + real), "in_min": max(0, round((epoch0 + real - now) / 60)), "live": dl is not None,
             "delay_min": round(dl / 60) if dl is not None else None,
             "trip_id": x["trip_id"], "t0": round(x["t0"]), "seq": x["seq"], "seq_to": x.get("seq_to")}
        if "arr" in x:
            d.update(arr=_hm(x["arr"]), arr_real=_hm(x["arr"] + (dl or 0)), arr_ts=round(epoch0 + x["arr"] + (dl or 0)),
                     travel_min=round((x["arr"] - x["t"]) / 60))
        res.append(d)
    res.sort(key=lambda x: x["ts"])   # opóźnienie może zmienić kolejność
    return res[-n:] if back else res[:n]


def ztm_board(stop: str, n: int = 12, tram_only: bool = False, at: float | None = None, back: bool = False) -> list[dict]:
    """Tablica odjazdów z przystanku (wszystkie linie i kierunki) — jak na wyświetlaczu na przystanku."""
    with _db(ztm_db()) as c:
        a = _ids(c, stop)
        if not a:
            raise HTTPException(404, f"Nie znam przystanku „{stop}”.")
        d0, secs, epoch0 = _ref(at)
        out = []
        for off, d in _days(d0):
            sv = _services(c, d)
            if not sv:
                continue
            lo, hi, order = _window(secs, off, back, at is None)
            q = f"""SELECT t.trip_id, r.short, r.type, t.headsign, sa.dep dep, sa.seq seq FROM stop_times sa
                JOIN trips t ON t.trip_id=sa.trip_id JOIN routes r ON r.route_id=t.route_id
                WHERE sa.stop_id IN ({",".join("?" * len(a))}) AND sa.dep>=? AND sa.dep<=?
                  AND t.service_id IN ({",".join("?" * len(sv))}) {"AND r.type=0" if tram_only else ""}
                  AND EXISTS (SELECT 1 FROM stop_times nx WHERE nx.trip_id=sa.trip_id AND nx.seq>sa.seq)
                ORDER BY sa.dep {order} LIMIT ?"""
            for r in c.execute(q, (*a, lo, hi, *sv, n * 2)):
                out.append({"trip_id": r["trip_id"], "line": r["short"], "type": r["type"], "headsign": r["headsign"],
                            "t": r["dep"] + off, "seq": r["seq"], "t0": epoch0 + off})
    return _shape(sorted(out, key=lambda x: x["t"]), secs, epoch0, n, at is None, back)


def ztm_trip(trip_id: str, t0: float) -> dict:
    """Szczegóły kursu: wszystkie przystanki z godzinami (i opóźnieniem na żywo, jeśli jest)."""
    with _db(ztm_db()) as c:
        head = c.execute("SELECT t.headsign, r.short, r.type FROM trips t JOIN routes r ON r.route_id=t.route_id WHERE t.trip_id=?", (trip_id,)).fetchone()
        if not head:
            raise HTTPException(404, "Nie znalazłem tego kursu (rozkład mógł się zmienić).")
        rows = c.execute("SELECT st.seq, st.dep, s.name FROM stop_times st JOIN stops s ON s.stop_id=st.stop_id WHERE st.trip_id=? ORDER BY st.seq",
                         (trip_id,)).fetchall()
    u = rt_delays().get(trip_id) if abs(t0 + (rows[0]["dep"] if rows else 0) - time.time()) < 4 * 3600 else None
    stops = []
    for r in rows:
        dl = _delay_for(u, r["seq"]) if u else None
        stops.append({"seq": r["seq"], "name": r["name"], "time": _hm(r["dep"]), "real": _hm(r["dep"] + (dl or 0)),
                      "ts": round(t0 + r["dep"] + (dl or 0)), "delay_min": round(dl / 60) if dl is not None else None})
    return {"line": head["short"], "tram": head["type"] == 0, "headsign": head["headsign"], "live": u is not None, "stops": stops}


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


# ---------- PKP PLK: Otwarte Dane Kolejowe ----------
def _pkp_key() -> str | None:
    if KEY_PROVIDER:
        try:
            k = KEY_PROVIDER()
            if k:
                return k.strip()
        except Exception:
            pass
    k = os.getenv("PKP_PLK_APIKEY")
    return k.strip() if k else None


def _pkp_time(t, day) -> int | None:
    if t in (None, ""):
        return None
    if isinstance(t, (int, float)):
        s = int(t)
    else:
        t = str(t)
        s = _hms(t.split("T")[-1][:8])
    return None if s is None else s + int(day or 0) * 86400


def _pkp_get(path: str, key: str, timeout: int = 60) -> bytes:
    try:
        return _get(PKP_API + path, {"X-Api-Key": key, "Accept": "application/json"}, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Klucz PKP jest nieprawidłowy albo jeszcze nieaktywny (aktywacja trwa 3–5 dni roboczych)."
                           if e.code in (401, 403) else f"API PKP: błąd {e.code}")


def import_pkp(key: str, raw: bytes | None = None):
    if raw is None:
        d = now_pl().date()
        raw = _pkp_get(f"/api/v1/schedules/shortened?dateFrom={(d - timedelta(days=1)).isoformat()}&dateTo={(d + timedelta(days=2)).isoformat()}",
                       key, timeout=300)
    data = json.loads(raw)
    dc = data.get("dc") or {}
    st = dc.get("st") or {}
    stations = st.values() if isinstance(st, dict) else st
    carriers = dc.get("cr") or {}
    tmp = _tmp("pkp")
    c = _raw(tmp)
    c.executescript("""
    PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
    CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE stations (id INTEGER PRIMARY KEY, name TEXT, nname TEXT);
    CREATE TABLE trains (tid INTEGER PRIMARY KEY, cc TEXT, carrier TEXT, cat TEXT, number TEXT, name TEXT, sid INTEGER, oid INTEGER);
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
        c.execute("INSERT INTO trains VALUES(?,?,?,?,?,?,?,?)",
                  (tid, cc, carriers.get(r.get("cc"), cc) or cc, "/".join(cats) or r.get("ccs", ""),
                   str(r.get("nn") or r.get("idn") or r.get("ian") or ""), r.get("nm") or "", r.get("sid"), r.get("oid")))
        c.executemany("INSERT INTO train_dates VALUES(?,?)", ((tid, str(x)[:10]) for x in r.get("od", [])))
        rows = []
        for s in sorted(r.get("st", []), key=lambda s: s.get("ord", 0)):
            a, d = _pkp_time(s.get("atm"), s.get("ady")), _pkp_time(s.get("dtm"), s.get("ddy"))
            a, d = a if a is not None else d, d if d is not None else a
            if a is not None:
                rows.append((tid, s.get("ord", 0), s.get("id"), a, d, s.get("dpl") or s.get("apl") or ""))
        c.executemany("INSERT INTO stops VALUES(?,?,?,?,?,?)", rows)
    c.executescript("""CREATE INDEX s_st ON stops(station, dep); CREATE INDEX s_tid ON stops(tid, ord); CREATE INDEX td ON train_dates(tid, date);
                       CREATE INDEX st_n ON stations(nname); CREATE INDEX tr_no ON trains(number); CREATE INDEX tr_sid ON trains(sid, oid);""")
    c.execute("INSERT INTO meta VALUES('updated', ?)", (str(time.time()),))
    c.commit()
    c.close()
    _publish("pkp", tmp)


def pkp_db():
    key = _pkp_key()
    if not key:
        raise HTTPException(400, "Pociągi nie są jeszcze skonfigurowane — administrator musi dodać klucz PKP.")
    return _ensure("pkp", import_pkp, key)


# opóźnienia pociągów na żywo: jedno zapytanie o wszystkie pociągi w Polsce, najwyżej raz na minutę (oszczędzamy klucz)
_ops = {"at": 0.0, "data": {}, "lock": threading.Lock()}
OPS_TTL = 60


def pkp_live() -> dict:
    """(sid, oid, data kursu) → {stacja: (przyjazd, odjazd)} w sekundach dnia (rzeczywiste czasy)."""
    with _ops["lock"]:
        if time.time() - _ops["at"] < OPS_TTL:
            return _ops["data"]
        _ops["at"] = time.time()
        key = _pkp_key()
        if not key:
            return _ops["data"]
        out = {}
        try:
            for page in range(1, 6):
                raw = json.loads(_pkp_get(f"/api/v1/operations/shortened?page={page}&pageSize=10000&fullRoutes=true", key, timeout=40))
                for tr in raw.get("tr") or []:
                    stops = {}
                    for s in tr.get("st") or []:
                        a, d = _pkp_time(s.get("aa"), 0), _pkp_time(s.get("ad"), 0)
                        if s.get("id") is not None and (a is not None or d is not None):
                            stops[s["id"]] = (a, d, bool(s.get("cn")))
                    out[(tr.get("sid"), tr.get("oid"), str(tr.get("od") or "")[:10])] = stops
                if not (raw.get("pg") or {}).get("hn"):
                    break
            _ops["data"] = out
        except Exception:
            pass   # brak danych na żywo → sam rozkład (spróbujemy za minutę)
        return _ops["data"]


def _pkp_delay(live: dict, r, station: int, planned: int, which: int = 1):
    """Opóźnienie (s) na stacji: rzeczywisty czas minus planowy; pilnujemy przejścia przez północ."""
    st = live.get((r["sid"], r["oid"], r["date"]))
    if not st or station not in st:
        return None, False
    a, d, cn = st[station]
    t = d if which == 1 and d is not None else a if a is not None else d
    if t is None:
        return None, cn
    diff = (t - planned % 86400 + 43200) % 86400 - 43200
    return diff, cn


def pkp_next(from_name: str, to_name: str, n: int = 5, at: float | None = None, back: bool = False, number: str | None = None) -> list[dict]:
    with _db(pkp_db()) as c:
        ids = lambda nm: [r[0] for r in c.execute("SELECT id FROM stations WHERE nname=?", (norm(nm),))]
        a, b = ids(from_name), ids(to_name)
        if not a or not b:
            raise HTTPException(404, f"Nie znam stacji „{from_name if not a else to_name}”.")
        d0, secs, epoch0 = _ref(at)
        out = []
        for off, d in _days(d0):
            lo, hi, order = _window(secs, off, back, at is None)
            hi = hi + 12 * 3600 if not back else hi   # pociągi jeżdżą rzadziej — szersze okno
            lo = lo - 12 * 3600 if back else lo
            q = f"""SELECT t.*, td.date date, sa.station st_from, sb.station st_to, sa.dep dep, sb.arr arr, sa.platform platform FROM stops sa
                JOIN stops sb ON sb.tid=sa.tid AND sb.ord>sa.ord AND sb.station IN ({",".join("?" * len(b))})
                JOIN trains t ON t.tid=sa.tid JOIN train_dates td ON td.tid=sa.tid AND td.date=?
                WHERE sa.station IN ({",".join("?" * len(a))}) AND sa.dep>=? AND sa.dep<=? {"AND t.number=?" if number else ""}
                ORDER BY sa.dep {order} LIMIT ?"""
            args = (*b, d.isoformat(), *a, lo, hi, *((number,) if number else ()), n * 2)
            for r in c.execute(q, args):
                out.append((r["dep"] + off, epoch0 + off, r))
        out.sort(key=lambda x: x[0])
    now = time.time()
    live = pkp_live() if any(abs(t0 + t - now) < 6 * 3600 for t, t0, _ in out) else {}
    res = []
    for t, t0, r in out:
        dl, cn = _pkp_delay(live, r, r["st_from"], r["dep"]) if abs(t0 + t - now) < 6 * 3600 else (None, False)
        da, _ = _pkp_delay(live, r, r["st_to"], r["arr"], 0) if dl is not None else (None, False)
        real = t + (dl or 0)
        if at is None and not back and real < secs - 60:
            continue
        arr = r["arr"] - r["dep"] + t
        res.append({"carrier": r["carrier"], "cc": r["cc"], "cat": r["cat"], "number": r["number"], "name": r["name"],
                    "dep": _hm(t), "dep_real": _hm(real), "arr": _hm(arr), "arr_real": _hm(arr + (da if da is not None else dl or 0)),
                    "platform": r["platform"], "travel_min": round((r["arr"] - r["dep"]) / 60), "live": dl is not None, "cancelled": cn,
                    "delay_min": round(dl / 60) if dl is not None else None, "ts": round(t0 + real),
                    "arr_ts": round(t0 + arr + (da if da is not None else dl or 0)), "in_min": max(0, round((t0 + real - now) / 60)),
                    "tid": r["tid"], "date": r["date"], "from_st": r["st_from"], "to_st": r["st_to"]})
    res.sort(key=lambda x: x["ts"])
    return res[-n:] if back else res[:n]


def pkp_details(tid: int, day: str) -> dict:
    with _db(pkp_db()) as c:
        t = c.execute("SELECT * FROM trains WHERE tid=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "Nie znalazłem tego pociągu (rozkład mógł się zmienić).")
        rows = c.execute("SELECT s.*, st.name FROM stops s JOIN stations st ON st.id=s.station WHERE s.tid=? ORDER BY s.ord", (tid,)).fetchall()
    d0 = date.fromisoformat(day)
    _, secs, epoch0 = _ref(None)
    t0 = epoch0 + (d0 - now_pl().date()).days * 86400
    live = pkp_live() if abs(t0 + (rows[0]["dep"] if rows else 0) - time.time()) < 12 * 3600 else {}
    r = {"sid": t["sid"], "oid": t["oid"], "date": day}
    stops = []
    for s in rows:
        dl, cn = _pkp_delay(live, r, s["station"], s["dep"])
        stops.append({"name": s["name"], "time": _hm(s["dep"]), "arr": _hm(s["arr"]), "real": _hm(s["dep"] + (dl or 0)),
                      "ts": round(t0 + s["dep"] + (dl or 0)), "platform": s["platform"], "station": s["station"],
                      "delay_min": round(dl / 60) if dl is not None else None, "cancelled": cn})
    return {"carrier": t["carrier"], "cc": t["cc"], "cat": t["cat"], "number": t["number"], "name": t["name"],
            "live": any(x["delay_min"] is not None for x in stops), "stops": stops}


def pkp_search(q: str) -> list[str]:
    return _search(pkp_db(), "stations", q)


def pkp_status() -> dict:
    return {**_status("pkp"), "key": bool(_pkp_key())}


# ---------- API (aplikacja lokalna / wersja webowa; pociągi liczy serwer kont — patrz cloud/app.py) ----------
def _status(src):
    st = _state[src]
    path = _cur(src)
    at = _meta(path, "updated")
    return {"ready": bool(at), "importing": st["importing"], "error": st["error"],
            "updated": datetime.fromtimestamp(float(at), timezone.utc).isoformat(timespec="minutes") if at else None}


@router.get("/api/transit/status")
def status():
    return {"ztm": _status("ztm")}


@router.post("/api/transit/{src}/refresh")
def refresh(src: str):
    if src == "ztm":
        _state["ztm"]["error"] = ""
        _start("ztm", import_ztm)
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
    """Podpowiedzi przystanków z liniami, które się na nich zatrzymują (tramwaje najpierw)."""
    path = ztm_db()
    names = _search(path, "stops", q, 8)
    with _db(path) as c:
        return [{"name": nm, "lines": _lines(c, norm(nm))} for nm in names]


# ---------- adres → najbliższe przystanki (geokodowanie OpenStreetMap / Nominatim) ----------
_geo_cache: dict = {}
_geo_lock = threading.Lock()
_geo_last = [0.0]
POZNAN_BOX = "16.55,52.62,17.30,52.22"   # obszar ZTM (Poznań i gminy aglomeracji) — wyniki z okolicy najpierw


@router.get("/api/transit/geocode")
def geocode(q: str):
    """Adres → miejsca (maks. 5). Nominatim: limit 1 zapytanie/s i własny User-Agent — pilnujemy tego tutaj, wyniki trzymamy w pamięci."""
    qn = norm(q)
    if len(qn) < 4:
        return []
    if qn in _geo_cache:
        return _geo_cache[qn]
    params = urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "pl", "viewbox": POZNAN_BOX,
                                     "bounded": 1, "addressdetails": 1, "accept-language": "pl"})
    with _geo_lock:
        wait = 1.05 - (time.time() - _geo_last[0])
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()
        try:
            data = json.loads(_get(GEOCODE_URL + "?" + params, {"User-Agent": UA + " (zenfix.pl/asystent)"}, timeout=10))
        except Exception:
            raise HTTPException(502, "Wyszukiwarka adresów chwilowo nie odpowiada.")
    out = []
    for r in data if isinstance(data, list) else []:
        a = r.get("address") or {}
        street = " ".join(x for x in (a.get("road") or a.get("pedestrian") or r.get("name") or "", a.get("house_number") or "") if x).strip()
        place = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or ""
        label = ", ".join(x for x in (street or r.get("name") or "", a.get("suburb") if place != a.get("suburb") else "", place) if x)
        try:
            out.append({"label": label or r.get("display_name", "")[:80], "lat": float(r["lat"]), "lon": float(r["lon"])})
        except (KeyError, ValueError):
            pass
    if len(_geo_cache) > 500:
        _geo_cache.clear()
    _geo_cache[qn] = out
    return out


def _lines(c, nname: str) -> list[dict]:
    try:
        L = c.execute("SELECT short, type FROM stop_lines WHERE nname=?", (nname,)).fetchall()
    except sqlite3.OperationalError:   # baza sprzed wersji z liniami (zaraz podmieni ją import w tle)
        return []
    return [{"l": r["short"], "tram": r["type"] == 0} for r in sorted(L, key=lambda r: (r["type"] != 0, len(r["short"]), r["short"]))]


@router.get("/api/transit/ztm/near")
def ztm_near(lat: float, lon: float, n: int = 5):
    """Najbliższe przystanki (po nazwie — słupki jednego przystanku liczą się razem) z odległością i czasem dojścia."""
    import math
    path = ztm_db()
    k = math.cos(math.radians(lat))
    d = 0.02   # ok. 2 km — najpierw zawężamy prostokątem, potem liczymy odległość
    with _db(path) as c:
        try:
            rows = c.execute("SELECT name, nname, lat, lon FROM stops WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                             (lat - d, lat + d, lon - d / k, lon + d / k)).fetchall()
            if not rows:
                rows = c.execute("SELECT name, nname, lat, lon FROM stops WHERE lat IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            raise HTTPException(503, "Aktualizuję rozkład (dochodzą współrzędne przystanków) — spróbuj za minutę.")
        best: dict = {}
        for r in rows:
            if r["lat"] is None:
                continue
            m = 6371000 * math.hypot(math.radians(r["lat"] - lat), math.radians(r["lon"] - lon) * k)
            if r["nname"] not in best or m < best[r["nname"]][0]:
                best[r["nname"]] = (m, r["name"])
        top = sorted(best.items(), key=lambda x: x[1][0])[:max(1, min(10, n))]
        return [{"name": nm, "dist_m": round(m), "walk_min": max(1, round(m * 1.25 / 80)), "lines": _lines(c, nn)}
                for nn, (m, nm) in top]


@router.get("/api/transit/ztm/board")
def ztm_board_api(stop: str, n: int = 12, tram: bool = False, at: float | None = None, back: bool = False):
    return ztm_board(stop, max(1, min(30, n)), tram, at, back)


@router.get("/api/transit/ztm/next")
def ztm_departures(frm: str, to: str, n: int = 6, tram: bool = False, at: float | None = None, back: bool = False):
    return ztm_next(frm, to, max(1, min(20, n)), tram, at, back)


@router.get("/api/transit/ztm/trip")
def ztm_trip_api(trip_id: str, t0: float):
    return ztm_trip(trip_id, t0)


# ---------- ulubione trasy ----------
class Fav(BaseModel):
    kind: str = "ztm"            # ztm (trasa) | pkp (trasa pociągiem) | pkp_train (konkretny pociąg — numer w label)
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
        if c.execute("SELECT 1 FROM transit_favs WHERE kind=? AND from_name=? AND to_name=? AND tram_only=? AND label=?",
                     (p.kind, f, t, int(p.tram_only), p.label.strip()[:60])).fetchone():
            raise HTTPException(400, "To jest już w ulubionych.")
        pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM transit_favs").fetchone()[0]
        fid = c.execute("INSERT INTO transit_favs(kind, from_name, to_name, label, tram_only, position) VALUES(?,?,?,?,?,?)",
                        (p.kind if p.kind in ("ztm", "pkp", "pkp_train") else "ztm", f, t, p.label.strip()[:60], int(p.tram_only), pos)).lastrowid
    return {"id": fid}


@router.delete("/api/transit/favs/{fid}")
def fav_del(fid: int):
    with get_conn() as c:
        c.execute("DELETE FROM transit_favs WHERE id=?", (fid,))
    return {"ok": True}


@router.get("/api/transit/favs/next")
def favs_next(n: int = 3):
    """Ulubione trasy z najbliższymi odjazdami — kafelek na Pulpicie i lista w zakładce Dojazd.
    Pociągi (pkp, pkp_train) dociąga aplikacja z serwera kont — tu wracają bez odjazdów."""
    out = []
    for f in favs():
        item = {**f, "deps": [], "error": ""}
        try:
            if f["kind"] == "ztm":
                item["deps"] = ztm_next(f["from_name"], f["to_name"], n, bool(f["tram_only"]))
        except HTTPException as e:
            item["error"] = e.detail
        except Exception as e:
            item["error"] = str(e)[:200]
        out.append(item)
    return out
