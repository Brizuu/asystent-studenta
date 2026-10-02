"""Asystent — serwer kont (logowanie, profil, znajomi, udostępnianie notatek).

Osobna usługa od lokalnej aplikacji: notatki i plan zostają na komputerze użytkownika,
tu trafiają tylko konta, relacje znajomych i to, co ktoś świadomie udostępni.

Lokalnie:   python -m uvicorn cloud.app:app --port 8100
Produkcja:  patrz cloud/README.md (Docker + Caddy, HTTPS).
Konfiguracja (zmienne środowiskowe):
  CLOUD_DB          ścieżka do bazy SQLite (domyślnie cloud/cloud.db)
  ALLOWED_ORIGINS   adresy aplikacji (CORS), po przecinku; domyślnie lokalne 127.0.0.1/localhost
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DB_PATH = Path(os.getenv("CLOUD_DB") or Path(__file__).parent / "cloud.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
SESSION_DAYS = 60
MAX_AVATAR = 300_000          # data URL avatara (ok. 200 KB obrazka)
MAX_SHARE = 3_000_000         # treść udostępnienia (JSON)
# administratorzy: e-maile z ADMIN_EMAILS (po przecinku); domyślnie właściciel projektu
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "fabian26012006@gmail.com").split(",") if e.strip()}

app = FastAPI(title="Asystent — konta")
origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    # aplikacja zawsze działa lokalnie (przeglądarka albo okno desktop, czasem na losowym porcie);
    # token idzie w nagłówku, nie w ciasteczku, więc inne strony i tak go nie mają
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_methods=["*"], allow_headers=["*"],
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    pw_hash      TEXT NOT NULL,
    bio          TEXT DEFAULT '',
    avatar       TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS friendships (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    requester  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    addressee  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending | accepted
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(requester, addressee)
);
CREATE TABLE IF NOT EXISTS sync_items (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tbl        TEXT NOT NULL,
    uid        TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted    INTEGER DEFAULT 0,
    data       TEXT,
    seq        INTEGER NOT NULL,
    device     TEXT DEFAULT '',
    PRIMARY KEY (user_id, tbl, uid)
);
CREATE INDEX IF NOT EXISTS ix_sync_seq ON sync_items(user_id, seq);
CREATE TABLE IF NOT EXISTS devices (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_uid TEXT NOT NULL,
    name       TEXT DEFAULT '',
    platform   TEXT DEFAULT '',
    summary    TEXT DEFAULT '{}',
    last_seen  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, device_uid)
);
CREATE TABLE IF NOT EXISTS shares (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sender     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                     -- note | notebook
    title      TEXT NOT NULL,
    payload    TEXT NOT NULL,                     -- JSON (migawka notatki / zeszytu)
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


with db() as _c:
    _c.executescript(SCHEMA)
    # migracje: kolumny dodane później (istniejące konta zostają)
    _have = {r[1] for r in _c.execute("PRAGMA table_info(users)")}
    for _col in ("university", "field", "study_year", "last_login"):
        if _col not in _have:
            _c.execute(f"ALTER TABLE users ADD COLUMN {_col} TEXT DEFAULT ''")


# ---------- hasła i sesje (stdlib: scrypt + losowe tokeny, w bazie tylko ich hash) ----------
def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + h.hex()


def check_pw(pw: str, stored: str) -> bool:
    salt, h = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), h)


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)", (_th(token), uid, time.time() + SESSION_DAYS * 86400))
    return token


def current_user(authorization: str = Header(default="")) -> sqlite3.Row:
    token = authorization.removeprefix("Bearer ").strip()
    with db() as c:
        u = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                      (_th(token), time.time())).fetchone() if token else None
    if not u:
        raise HTTPException(401, "Zaloguj się ponownie.")
    return u


# prosty limit prób logowania (na IP), żeby utrudnić zgadywanie haseł
_attempts: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, limit: int = 10, window: int = 300, record: bool = True):
    """Sprawdza limit dla IP; record=False — tylko sprawdź (przy logowaniu liczymy same nieudane próby)."""
    ip = request.client.host if request.client else "?"
    q = _attempts[ip]
    now = time.time()
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "Za dużo prób. Spróbuj za kilka minut.")
    if record:
        q.append(now)


def public(u, full=False) -> dict:
    # e-mail widoczny dla znajomych — po nim się wyszukują
    d = {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "avatar": u["avatar"], "email": u["email"],
         "university": u["university"] or "", "field": u["field"] or "", "study_year": u["study_year"] or ""}
    if full:
        d.update(bio=u["bio"], created_at=u["created_at"], is_admin=is_admin(u))
    return d


def is_admin(u) -> bool:
    return (u["email"] or "").lower() in ADMIN_EMAILS


def admin_user(u=Depends(current_user)):
    if not is_admin(u):
        raise HTTPException(403, "Tylko dla administratora.")
    return u


# ---------- konto ----------
class Register(BaseModel):
    display_name: str       # imię i nazwisko — widoczne u znajomych
    email: str
    password: str


class Login(BaseModel):
    login: str      # e-mail
    password: str


@app.get("/health")
def health():
    return {"ok": True}


SITE = Path(__file__).parent / "site" / "index.html"


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def site():
    """Strona pobierania aplikacji (https://bte-poland.pl/asystent/) — generowana przez build_site.py."""
    if not SITE.exists():
        return HTMLResponse("<h1>Asystent — serwer kont</h1><p>Strona pobierania nie jest zbudowana.</p>")
    return HTMLResponse(SITE.read_text(encoding="utf-8"), headers={"Cache-Control": "public, max-age=300"})


@app.post("/auth/register")
def register(p: Register, request: Request):
    rate_limit(request)
    email, name = p.email.strip().lower(), " ".join(p.display_name.split())[:60]
    if len(name) < 2:
        raise HTTPException(400, "Podaj imię i nazwisko.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Podaj poprawny adres e-mail.")
    if len(p.password) < 8:
        raise HTTPException(400, "Hasło musi mieć co najmniej 8 znaków.")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "Konto z tym adresem e-mail już istnieje. Zaloguj się.")
        # wewnętrzny identyfikator (niewidoczny): z e-maila, unikalny
        base = re.sub(r"[^a-z0-9_.]", "", email.split("@")[0])[:20] or "user"
        username, n = base, 1
        while c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            n += 1
            username = f"{base}{n}"
        uid = c.execute("INSERT INTO users(email,username,display_name,pw_hash) VALUES(?,?,?,?)",
                        (email, username, name, hash_pw(p.password))).lastrowid
    return {"token": new_session(uid)}


@app.post("/auth/login")
def login(p: Login, request: Request):
    rate_limit(request, record=False)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (p.login.strip().lower(),)).fetchone()
    if not u or not check_pw(p.password, u["pw_hash"]):
        rate_limit(request)   # nieudana próba się liczy
        raise HTTPException(401, "Nieprawidłowy e-mail lub hasło.")
    with db() as c:
        c.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (u["id"],))
    return {"token": new_session(u["id"])}


@app.post("/auth/logout")
def logout(authorization: str = Header(default="")):
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (_th(authorization.removeprefix("Bearer ").strip()),))
    return {"ok": True}


@app.get("/me")
def me(u=Depends(current_user)):
    return public(u, full=True)


class Profile(BaseModel):
    display_name: str | None = None
    bio: str | None = None
    university: str | None = None
    field: str | None = None
    study_year: str | None = None
    avatar: str | None = None       # data:image/...;base64,… albo "" (usuń)


@app.patch("/me")
def update_me(p: Profile, u=Depends(current_user)):
    f = {}
    if p.display_name is not None:
        f["display_name"] = " ".join(p.display_name.split())[:60] or u["display_name"]
    if p.bio is not None:
        f["bio"] = p.bio.strip()[:300]
    for k in ("university", "field", "study_year"):
        v = getattr(p, k)
        if v is not None:
            f[k] = " ".join(v.split())[:80]
    if p.avatar is not None:
        if p.avatar and (not re.match(r"^data:image/(png|jpeg|webp);base64,", p.avatar) or len(p.avatar) > MAX_AVATAR):
            raise HTTPException(400, "Avatar: obraz PNG/JPG/WebP, do ok. 200 KB.")
        f["avatar"] = p.avatar
    if f:
        with db() as c:
            c.execute(f"UPDATE users SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), u["id"]))
    with db() as c:
        return public(c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone(), full=True)


class Password(BaseModel):
    old: str
    new: str


@app.post("/me/password")
def change_password(p: Password, u=Depends(current_user)):
    if not check_pw(p.old, u["pw_hash"]):
        raise HTTPException(400, "Obecne hasło jest nieprawidłowe.")
    if len(p.new) < 8:
        raise HTTPException(400, "Nowe hasło musi mieć co najmniej 8 znaków.")
    with db() as c:
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(p.new), u["id"]))
    return {"ok": True}


# ---------- znajomi ----------
def _friend_ids(c, uid) -> set[int]:
    return {r[0] for r in c.execute(
        "SELECT CASE WHEN requester=? THEN addressee ELSE requester END FROM friendships "
        "WHERE status='accepted' AND (requester=? OR addressee=?)", (uid, uid, uid))}


@app.get("/friends")
def friends(u=Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT f.id fid, f.status, f.requester, u.* FROM friendships f
            JOIN users u ON u.id = CASE WHEN f.requester=? THEN f.addressee ELSE f.requester END
            WHERE f.requester=? OR f.addressee=? ORDER BY u.display_name""", (u["id"], u["id"], u["id"])).fetchall()
        unread = {r["sender"]: r["n"] for r in c.execute(
            "SELECT sender, COUNT(*) n FROM shares WHERE recipient=? GROUP BY sender", (u["id"],))}
    out = {"friends": [], "incoming": [], "outgoing": []}
    for r in rows:
        item = {"fid": r["fid"], **public(r)}
        if r["status"] == "accepted":
            out["friends"].append({**item, "shared_with_me": unread.get(r["id"], 0)})
        else:
            out["incoming" if r["requester"] != u["id"] else "outgoing"].append(item)
    return out


class FriendRequest(BaseModel):
    email: str


@app.post("/friends/request")
def friend_request(p: FriendRequest, u=Depends(current_user)):
    with db() as c:
        t = c.execute("SELECT * FROM users WHERE email=?", (p.email.strip().lower(),)).fetchone()
        if not t:
            raise HTTPException(404, "Nie ma konta z tym adresem e-mail.")
        if t["id"] == u["id"]:
            raise HTTPException(400, "To Twoje konto.")
        ex = c.execute("SELECT * FROM friendships WHERE (requester=? AND addressee=?) OR (requester=? AND addressee=?)",
                       (u["id"], t["id"], t["id"], u["id"])).fetchone()
        if ex:
            if ex["status"] == "pending" and ex["requester"] == t["id"]:   # on zaprosił nas wcześniej → od razu znajomi
                c.execute("UPDATE friendships SET status='accepted' WHERE id=?", (ex["id"],))
                return {"status": "accepted"}
            raise HTTPException(409, "Zaproszenie już wysłane albo jesteście znajomymi.")
        c.execute("INSERT INTO friendships(requester,addressee) VALUES(?,?)", (u["id"], t["id"]))
    return {"status": "pending"}


@app.post("/friends/{fid}/accept")
def friend_accept(fid: int, u=Depends(current_user)):
    with db() as c:
        n = c.execute("UPDATE friendships SET status='accepted' WHERE id=? AND addressee=? AND status='pending'",
                      (fid, u["id"])).rowcount
    if not n:
        raise HTTPException(404, "Nie znaleziono zaproszenia.")
    return {"ok": True}


@app.delete("/friends/{fid}")
def friend_remove(fid: int, u=Depends(current_user)):
    """Odrzuć / anuluj zaproszenie albo usuń ze znajomych."""
    with db() as c:
        c.execute("DELETE FROM friendships WHERE id=? AND (requester=? OR addressee=?)", (fid, u["id"], u["id"]))
    return {"ok": True}


# ---------- udostępnianie ----------
class Share(BaseModel):
    to: list[int]                 # id znajomych
    kind: str                     # note | notebook
    title: str
    payload: dict


@app.post("/shares")
def share(p: Share, u=Depends(current_user)):
    if p.kind not in ("note", "notebook"):
        raise HTTPException(400, "Nieznany rodzaj udostępnienia.")
    data = json.dumps(p.payload, ensure_ascii=False)
    if len(data) > MAX_SHARE:
        raise HTTPException(413, "Za duże do udostępnienia (limit ok. 3 MB).")
    with db() as c:
        ok = _friend_ids(c, u["id"])
        targets = [t for t in set(p.to) if t in ok]
        if not targets:
            raise HTTPException(400, "Wybierz co najmniej jednego znajomego.")
        for t in targets:
            c.execute("INSERT INTO shares(sender,recipient,kind,title,payload) VALUES(?,?,?,?,?)",
                      (u["id"], t, p.kind, p.title.strip()[:120] or "Bez tytułu", data))
    return {"sent": len(targets)}


@app.get("/shares")
def shares(from_user: int | None = None, u=Depends(current_user)):
    """Udostępnione MNIE (opcjonalnie tylko od jednego znajomego) — bez treści, sama lista."""
    q = ("SELECT s.id, s.kind, s.title, s.created_at, s.sender, length(s.payload) size FROM shares s "
         "WHERE s.recipient=?" + (" AND s.sender=?" if from_user else "") + " ORDER BY s.id DESC")
    with db() as c:
        return [dict(r) for r in c.execute(q, (u["id"], from_user) if from_user else (u["id"],))]


@app.get("/shares/{sid}")
def share_get(sid: int, u=Depends(current_user)):
    with db() as c:
        r = c.execute("SELECT * FROM shares WHERE id=? AND (recipient=? OR sender=?)", (sid, u["id"], u["id"])).fetchone()
    if not r:
        raise HTTPException(404, "Nie znaleziono.")
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "created_at": r["created_at"],
            "sender": r["sender"], "payload": json.loads(r["payload"])}


@app.delete("/shares/{sid}")
def share_delete(sid: int, u=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM shares WHERE id=? AND (recipient=? OR sender=?)", (sid, u["id"], u["id"]))
    return {"ok": True}


# ---------- administrator: lista kont (bez haseł) i reset hasła ----------
@app.get("/admin/users")
def admin_users(a=Depends(admin_user)):
    now = time.time()
    with db() as c:
        rows = c.execute("""
            SELECT u.*,
              (SELECT COUNT(*) FROM friendships f WHERE f.status='accepted' AND (f.requester=u.id OR f.addressee=u.id)) friends,
              (SELECT COUNT(*) FROM friendships f WHERE f.status='pending' AND f.addressee=u.id) pending,
              (SELECT COUNT(*) FROM shares s WHERE s.sender=u.id) shares_sent,
              (SELECT COUNT(*) FROM shares s WHERE s.recipient=u.id) shares_received,
              (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.expires>?) sessions
            FROM users u ORDER BY u.created_at DESC""", (now,)).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys() if k != "pw_hash"}   # hasła (hash) nigdy nie wychodzą
        d["is_admin"] = is_admin(r)
        d["has_avatar"] = bool(r["avatar"])
        out.append(d)
    return out


@app.post("/admin/users/{uid}/reset-password")
def admin_reset_password(uid: int, a=Depends(admin_user)):
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # bez mylących znaków (l, 1, O, 0)
    pw = "".join(secrets.choice(alphabet) for _ in range(12))
    with db() as c:
        u = c.execute("SELECT id, email, display_name FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "Nie ma takiego konta.")
        c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), uid))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))   # stare logowania tracą ważność
    return {"password": pw, "email": u["email"], "display_name": u["display_name"]}


# ---------- synchronizacja urządzeń (dane z aplikacji jednego konta) ----------
SYNC_TABLES = {"notebooks", "note_groups", "notes", "tasks", "todo_lists", "todo_items", "todo_checks", "costs", "cost_entries", "incomes"}
MAX_SYNC_ITEM = 2_000_000       # jedna notatka (JSON bloków)
MAX_SYNC_USER = 200_000_000     # łączny rozmiar danych konta


class SyncDevice(BaseModel):
    uid: str
    name: str = ""
    platform: str = ""
    summary: dict = {}


class SyncItem(BaseModel):
    tbl: str
    uid: str
    updated_at: str
    deleted: bool = False
    data: dict | None = None


class SyncPush(BaseModel):
    device: SyncDevice
    items: list[SyncItem] = []


@app.post("/sync/push")
def sync_push(p: SyncPush, u=Depends(current_user)):
    if len(p.items) > 2000:
        raise HTTPException(413, "Za dużo zmian w jednej paczce.")
    accepted = 0
    with db() as c:
        c.execute("""INSERT INTO devices(user_id, device_uid, name, platform, summary, last_seen) VALUES(?,?,?,?,?,datetime('now'))
                     ON CONFLICT(user_id, device_uid) DO UPDATE SET name=excluded.name, platform=excluded.platform,
                     summary=excluded.summary, last_seen=excluded.last_seen""",
                  (u["id"], p.device.uid[:64], p.device.name[:60], p.device.platform[:60], json.dumps(p.device.summary)[:4000]))
        used = c.execute("SELECT COALESCE(SUM(LENGTH(data)),0) FROM sync_items WHERE user_id=?", (u["id"],)).fetchone()[0]
        seq = c.execute("SELECT COALESCE(MAX(seq),0) FROM sync_items WHERE user_id=?", (u["id"],)).fetchone()[0]
        for it in p.items:
            if it.tbl not in SYNC_TABLES or not it.uid or len(it.uid) > 64:
                continue
            data = None if it.deleted else json.dumps(it.data or {}, ensure_ascii=False)
            if data and len(data) > MAX_SYNC_ITEM:
                raise HTTPException(413, "Jedna z notatek jest za duża do synchronizacji.")
            old = c.execute("SELECT updated_at FROM sync_items WHERE user_id=? AND tbl=? AND uid=?", (u["id"], it.tbl, it.uid)).fetchone()
            if old and old["updated_at"] >= it.updated_at:
                continue   # serwer ma tę samą albo nowszą wersję
            used += len(data or "")
            if used > MAX_SYNC_USER:
                raise HTTPException(413, "Przekroczono limit danych synchronizacji na koncie.")
            seq += 1
            c.execute("""INSERT INTO sync_items(user_id, tbl, uid, updated_at, deleted, data, seq, device) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(user_id, tbl, uid) DO UPDATE SET updated_at=excluded.updated_at, deleted=excluded.deleted,
                         data=excluded.data, seq=excluded.seq, device=excluded.device""",
                      (u["id"], it.tbl, it.uid, it.updated_at, int(it.deleted), data, seq, p.device.uid[:64]))
            accepted += 1
    return {"accepted": accepted, "seq": seq}


@app.get("/sync/pull")
def sync_pull(since: int = 0, device: str = "", u=Depends(current_user)):
    limit = 1000
    with db() as c:
        rows = c.execute("SELECT * FROM sync_items WHERE user_id=? AND seq>? ORDER BY seq LIMIT ?", (u["id"], since, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [{"tbl": r["tbl"], "uid": r["uid"], "updated_at": r["updated_at"], "deleted": bool(r["deleted"]),
              "data": json.loads(r["data"]) if r["data"] else None} for r in rows if r["device"] != device]
    return {"items": items, "cursor": rows[-1]["seq"] if rows else since, "more": more}


@app.get("/sync/devices")
def sync_devices(u=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM devices WHERE user_id=? ORDER BY last_seen DESC", (u["id"],)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM sync_items WHERE user_id=? AND deleted=0", (u["id"],)).fetchone()[0]
    return {"devices": [{"uid": r["device_uid"], "name": r["name"], "platform": r["platform"], "last_seen": r["last_seen"],
                         "summary": json.loads(r["summary"] or "{}")} for r in rows], "items": total}


@app.delete("/sync/devices/{uid}")
def sync_forget_device(uid: str, u=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM devices WHERE user_id=? AND device_uid=?", (u["id"], uid))
    return {"ok": True}
