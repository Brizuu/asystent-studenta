"""Asystent — serwer kont (logowanie, profil, znajomi, udostępnianie notatek).

Osobna usługa od lokalnej aplikacji: notatki i plan zostają na komputerze użytkownika,
tu trafiają tylko konta, relacje znajomych i to, co ktoś świadomie udostępni.

Lokalnie:   python -m uvicorn cloud.app:app --port 8100
Produkcja:  patrz cloud/README.md (Docker + Caddy, HTTPS).
Konfiguracja (zmienne środowiskowe):
  CLOUD_DB          ścieżka do bazy SQLite (domyślnie cloud/cloud.db)
  ALLOWED_ORIGINS   adresy aplikacji (CORS), po przecinku; domyślnie lokalne 127.0.0.1/localhost
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import zlib
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, Response
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
class _Gzip(GZipMiddleware):
    """gzip tylko dla tekstu (strona aplikacji ~1 MB, JSON synchronizacji i czatu) — PDF-y i obrazy są już skompresowane."""
    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if "/files/" in path or "/uploads/" in path or re.search(r"\.(png|jpe?g|gif|webp|pdf|zip|exe|apk|ogg|webm)$", path, re.I):
            return await self.app(scope, receive, send)
        return await super().__call__(scope, receive, send)


app.add_middleware(_Gzip, minimum_size=1024)

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
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA synchronous = NORMAL")
    return c


SCHEMA += """
CREATE TABLE IF NOT EXISTS groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    color      TEXT DEFAULT '#8b7cff',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, user_id)
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    text       TEXT NOT NULL,
    image      TEXT DEFAULT '',          -- data URL zdjęcia/zrzutu (zmniejszony w aplikacji)
    status     TEXT DEFAULT 'new',       -- new | seen | done
    reply      TEXT DEFAULT '',          -- odpowiedź administratora (widzi ją autor)
    created_at TEXT DEFAULT (datetime('now'))
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat       TEXT NOT NULL,                     -- d<min id>_<max id> (rozmowa 1:1) | g<id grupy>
    sender     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT NOT NULL DEFAULT '',
    share_id   INTEGER,                           -- wiadomość z udostępnioną notatką / zeszytem / planem
    mentions   TEXT DEFAULT '',                   -- ,id,id, oznaczonych osób
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_msg_chat ON messages(chat, id);
CREATE TABLE IF NOT EXISTS chat_state (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat      TEXT NOT NULL,
    last_read INTEGER DEFAULT 0,
    muted     INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, chat)
);
CREATE INDEX IF NOT EXISTS ix_shares_rcpt ON shares(recipient, id);
CREATE INDEX IF NOT EXISTS ix_shares_sender ON shares(sender, id);
CREATE INDEX IF NOT EXISTS ix_gm_user ON group_members(user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
"""

with db() as _c:
    _c.execute("PRAGMA journal_mode = WAL")   # wielu użytkowników naraz: czytanie (czat, synchronizacja) nie czeka na zapis
    _c.executescript(SCHEMA)
    _sh = {r[1] for r in _c.execute("PRAGMA table_info(shares)")}
    if "group_id" not in _sh:
        _c.execute("ALTER TABLE shares ADD COLUMN group_id INTEGER")
    if "hidden" not in _sh:   # odbiorca usunął z listy, ale wiadomość w czacie nadal ją pokazuje
        _c.execute("ALTER TABLE shares ADD COLUMN hidden INTEGER DEFAULT 0")
    _c.execute("CREATE INDEX IF NOT EXISTS ix_shares_group ON shares(group_id)")
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
    """Strona pobierania aplikacji (https://zenexus.pl/asystent/) — generowana przez build_site.py."""
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
    to: list[int] = []            # id znajomych
    group_id: int | None = None   # albo cała grupa (każdy członek dostaje kopię)
    kind: str                     # note | notebook
    title: str
    payload: dict
    note: str = ""                # opcjonalny komentarz — trafia do czatu razem z kartą udostępnienia


@app.post("/shares")
def share(p: Share, u=Depends(current_user)):
    if p.kind not in ("note", "notebook", "plan"):
        raise HTTPException(400, "Nieznany rodzaj udostępnienia.")
    data = json.dumps(p.payload, ensure_ascii=False)
    if len(data) > MAX_SHARE:
        raise HTTPException(413, "Za duże do udostępnienia (limit ok. 3 MB).")
    with db() as c:
        ok = _friend_ids(c, u["id"])
        targets = {t for t in set(p.to) if t in ok}
        if p.group_id:
            if not _is_member(c, p.group_id, u["id"]):
                raise HTTPException(403, "Nie należysz do tej grupy.")
            targets |= {r[0] for r in c.execute("SELECT user_id FROM group_members WHERE group_id=?", (p.group_id,))}
            targets.discard(u["id"])
        if not targets:
            raise HTTPException(400, "Wybierz co najmniej jednego znajomego." if not p.group_id else "W tej grupie nie ma jeszcze nikogo poza Tobą.")
        first = None
        note = (p.note or "").strip()[:2000]
        for t in sorted(targets):
            sid = c.execute("INSERT INTO shares(sender,recipient,kind,title,payload,group_id) VALUES(?,?,?,?,?,?)",
                            (u["id"], t, p.kind, p.title.strip()[:120] or "Bez tytułu", data, p.group_id)).lastrowid
            first = first or sid
            if not p.group_id:   # rozmowa 1:1 — karta udostępnienia w czacie, nie ginie w liście
                _post(c, _dm_key(u["id"], t), u["id"], note, sid)
        if p.group_id:
            _post(c, f"g{p.group_id}", u["id"], note, first)
    return {"sent": len(targets)}


@app.get("/shares")
def shares(from_user: int | None = None, u=Depends(current_user)):
    """Udostępnione MNIE (opcjonalnie tylko od jednego znajomego) — bez treści, sama lista."""
    q = ("SELECT s.id, s.kind, s.title, s.created_at, s.sender, s.group_id, length(s.payload) size FROM shares s "
         "WHERE s.recipient=? AND COALESCE(s.hidden,0)=0" + (" AND s.sender=?" if from_user else "") + " ORDER BY s.id DESC")
    with db() as c:
        return [dict(r) for r in c.execute(q, (u["id"], from_user) if from_user else (u["id"],))]


def _share_row(c, sid: int, uid: int):
    """Udostępnienie widzi odbiorca, nadawca i — gdy poszło do grupy — każdy jej członek (karta w czacie grupy)."""
    r = c.execute("SELECT * FROM shares WHERE id=?", (sid,)).fetchone()
    if r and (uid in (r["recipient"], r["sender"]) or (r["group_id"] and _is_member(c, r["group_id"], uid))):
        return r
    return None


@app.get("/shares/{sid}")
def share_get(sid: int, u=Depends(current_user)):
    with db() as c:
        r = _share_row(c, sid, u["id"])
    if not r:
        raise HTTPException(404, "Nie znaleziono.")
    return {"id": r["id"], "kind": r["kind"], "title": r["title"], "created_at": r["created_at"],
            "sender": r["sender"], "payload": json.loads(r["payload"])}


@app.delete("/shares/{sid}")
def share_delete(sid: int, u=Depends(current_user)):
    with db() as c:
        if c.execute("SELECT 1 FROM messages WHERE share_id=? LIMIT 1", (sid,)).fetchone():   # jest w czacie: tylko znika z mojej listy
            c.execute("UPDATE shares SET hidden=1 WHERE id=? AND recipient=?", (sid, u["id"]))
        else:
            c.execute("DELETE FROM shares WHERE id=? AND (recipient=? OR sender=?)", (sid, u["id"], u["id"]))
    return {"ok": True}


# ---------- administrator: lista kont (bez haseł) i reset hasła ----------
@app.get("/admin/users")
def admin_users(response: Response, a=Depends(admin_user)):
    response.headers["Cache-Control"] = "no-store"   # panel odświeża listę na żywo — bez kopii z cache
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


# ---------- sugestie: użytkownicy zgłaszają pomysły / błędy (opcjonalnie ze zdjęciem), administrator je przegląda ----------
MAX_SUGG_IMAGE = 4_000_000   # data URL (ok. 3 MB obrazka) — aplikacja i tak zmniejsza zdjęcie przed wysłaniem


class Suggestion(BaseModel):
    text: str
    image: str = ""


@app.post("/suggestions")
def suggestion_add(p: Suggestion, request: Request, u=Depends(current_user)):
    rate_limit(request, limit=20, window=3600)
    text = p.text.strip()[:5000]
    if len(text) < 3:
        raise HTTPException(400, "Opisz sugestię choć jednym zdaniem.")
    img = p.image or ""
    if img and (not re.match(r"^data:image/(png|jpe?g|webp|gif);base64,", img) or len(img) > MAX_SUGG_IMAGE):
        raise HTTPException(400, "Zdjęcie musi być obrazkiem (PNG/JPG/WebP) do ok. 3 MB.")
    with db() as c:
        sid = c.execute("INSERT INTO suggestions(user_id, text, image) VALUES(?,?,?)", (u["id"], text, img)).lastrowid
    return {"id": sid}


def _sugg(r, with_image=True) -> dict:
    d = {k: r[k] for k in r.keys() if k != "image"}
    d["has_image"] = bool(r["image"])
    if with_image:
        d["image"] = r["image"]
    return d


@app.get("/suggestions/mine")
def suggestion_mine(u=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM suggestions WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],)).fetchall()
    return [_sugg(r) for r in rows]


@app.get("/admin/suggestions")
def admin_suggestions(a=Depends(admin_user)):
    with db() as c:
        rows = c.execute("""SELECT s.*, u.display_name, u.email, u.avatar FROM suggestions s
                            LEFT JOIN users u ON u.id=s.user_id ORDER BY s.status='done', s.id DESC LIMIT 500""").fetchall()
    return [_sugg(r) for r in rows]


class SuggestionPatch(BaseModel):
    status: str | None = None
    reply: str | None = None


@app.patch("/admin/suggestions/{sid}")
def admin_suggestion_patch(sid: int, p: SuggestionPatch, a=Depends(admin_user)):
    with db() as c:
        if p.status in ("new", "seen", "done"):
            c.execute("UPDATE suggestions SET status=? WHERE id=?", (p.status, sid))
        if p.reply is not None:
            c.execute("UPDATE suggestions SET reply=? WHERE id=?", (p.reply.strip()[:3000], sid))
    return {"ok": True}


@app.delete("/admin/suggestions/{sid}")
def admin_suggestion_delete(sid: int, a=Depends(admin_user)):
    with db() as c:
        c.execute("DELETE FROM suggestions WHERE id=?", (sid,))
    return {"ok": True}


# ---------- synchronizacja urządzeń (dane z aplikacji jednego konta) ----------
SYNC_TABLES = {"notebooks", "note_groups", "notes", "tasks", "todo_lists", "todo_items", "todo_checks", "costs", "cost_entries", "incomes", "quiz_cards", "transit_favs"}
MAX_SYNC_ITEM = 12_000_000      # jedna notatka (JSON bloków po rozpakowaniu; obrazy jadą osobno jako pliki)
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
            d = it.data or {}
            if not it.deleted and "_gz" in d:   # aplikacja wysyła duże notatki skompresowane
                try:
                    z = zlib.decompressobj(16 + zlib.MAX_WBITS)   # gzip, z limitem (bez „bomby” po rozpakowaniu)
                    raw = z.decompress(base64.b64decode(d["_gz"]), MAX_SYNC_ITEM + 1)
                    if len(raw) > MAX_SYNC_ITEM:
                        raise ValueError
                    d = json.loads(raw.decode("utf-8"))
                except Exception:
                    raise HTTPException(413, "Jedna z notatek jest za duża do synchronizacji.")
            data = None if it.deleted else json.dumps(d, ensure_ascii=False)
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


# ---------- wersja webowa aplikacji pod /app (web.py z katalogu projektu) ----------
def _web_user(token: str):
    if not token:
        return None
    with db() as c:
        r = c.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires>?", (_th(token), time.time())).fetchone()
    return r["user_id"] if r else None


try:
    os.environ.setdefault("WEB_DATA", str(DB_PATH.parent / "web"))
    import web as _web   # noqa: E402

    @app.get("/app", include_in_schema=False)
    def _app_slash():   # względne przekierowanie: za nginx (/asystent/app → /app) zachowuje prefiks
        return Response(status_code=301, headers={"Location": "app/"})

    app.mount("/app", _web.make_app(_web_user))
except ImportError:   # obraz bez plików aplikacji — sam serwer kont
    pass


# ---------- POCIĄGI PKP: wspólny klucz PLK (ustawia administrator), rozkład i opóźnienia liczone tutaj dla wszystkich ----------
with db() as _c:
    _c.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")


def _setting(k: str) -> str:
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else ""


try:
    import komunikacja as _kom   # noqa: E402  (pliki aplikacji leżą obok — jak web.py)
    _kom.KEY_PROVIDER = lambda: _setting("pkp_key")
except ImportError:
    _kom = None


def _kom_ok():
    if not _kom:
        raise HTTPException(503, "Pociągi nie są dostępne na tym serwerze.")
    return _kom


class PkpKeyIn(BaseModel):
    key: str


@app.get("/admin/pkp")
def admin_pkp(a=Depends(admin_user)):
    k = _setting("pkp_key")
    st = _kom_ok().pkp_status()
    return {**st, "key": bool(k), "masked": (k[:4] + "…" + k[-4:]) if len(k) > 10 else ("•••" if k else "")}


@app.post("/admin/pkp/key")
def admin_pkp_key(p: PkpKeyIn, a=Depends(admin_user)):
    k = "".join(p.key.split())
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES('pkp_key', ?)", (k,))
    if k:
        kom = _kom_ok()
        kom._state["pkp"]["error"] = ""
        kom._start("pkp", kom.import_pkp, k)
    return admin_pkp(a)


@app.post("/admin/pkp/refresh")
def admin_pkp_refresh(a=Depends(admin_user)):
    kom, k = _kom_ok(), _setting("pkp_key")
    if not k:
        raise HTTPException(400, "Najpierw wpisz klucz.")
    kom._state["pkp"]["error"] = ""
    kom._start("pkp", kom.import_pkp, k)
    return admin_pkp(a)


@app.get("/pkp/status")
def pkp_status(u=Depends(current_user)):
    return _kom_ok().pkp_status()


@app.get("/pkp/stations")
def pkp_stations(q: str = "", u=Depends(current_user)):
    return _kom_ok().pkp_search(q)


@app.get("/pkp/next")
def pkp_next(frm: str, to: str, n: int = 6, at: float | None = None, back: bool = False, number: str = "", u=Depends(current_user)):
    return _kom_ok().pkp_next(frm, to, max(1, min(15, n)), at, back, number or None)


@app.get("/pkp/details")
def pkp_details(tid: int, date: str, u=Depends(current_user)):
    return _kom_ok().pkp_details(tid, date)


# ---------- GRUPY: znajomi w grupach, udostępnienie do grupy trafia do każdego członka ----------
def _is_member(c, gid: int, uid: int) -> bool:
    return bool(c.execute("SELECT 1 FROM group_members WHERE group_id=? AND user_id=?", (gid, uid)).fetchone())


def _group_out(c, g, uid) -> dict:
    members = [public(r) for r in c.execute(
        "SELECT u.* FROM group_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=? ORDER BY u.display_name", (g["id"],))]
    n = c.execute("SELECT COUNT(*) FROM shares WHERE group_id=? AND recipient=?", (g["id"], uid)).fetchone()[0]
    return {"id": g["id"], "name": g["name"], "color": g["color"], "owner": g["owner"], "is_owner": g["owner"] == uid,
            "created_at": g["created_at"], "members": members, "received": n}


@app.get("/groups")
def groups(u=Depends(current_user)):
    with db() as c:
        rows = c.execute("SELECT g.* FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? ORDER BY g.name",
                         (u["id"],)).fetchall()
        return [_group_out(c, g, u["id"]) for g in rows]


class GroupIn(BaseModel):
    name: str | None = None
    color: str | None = None
    add: list[int] = []
    remove: list[int] = []


@app.post("/groups")
def group_create(p: GroupIn, u=Depends(current_user)):
    name = (p.name or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Podaj nazwę grupy.")
    color = p.color if p.color and re.fullmatch(r"#[0-9a-fA-F]{6}", p.color) else "#8b7cff"
    with db() as c:
        gid = c.execute("INSERT INTO groups(owner,name,color) VALUES(?,?,?)", (u["id"], name, color)).lastrowid
        ok = _friend_ids(c, u["id"]) | {u["id"]}
        for m in {u["id"], *p.add}:
            if m in ok:
                c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (gid, m))
        return _group_out(c, c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone(), u["id"])


@app.patch("/groups/{gid}")
def group_edit(gid: int, p: GroupIn, u=Depends(current_user)):
    with db() as c:
        g = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not g or not _is_member(c, gid, u["id"]):
            raise HTTPException(404, "Nie ma takiej grupy.")
        if p.name is not None or p.color is not None or p.remove:
            if g["owner"] != u["id"]:
                raise HTTPException(403, "Nazwę i skład grupy zmienia jej założyciel.")
        if p.name is not None and p.name.strip():
            c.execute("UPDATE groups SET name=? WHERE id=?", (p.name.strip()[:60], gid))
        if p.color and re.fullmatch(r"#[0-9a-fA-F]{6}", p.color):
            c.execute("UPDATE groups SET color=? WHERE id=?", (p.color, gid))
        ok = _friend_ids(c, u["id"])          # każdy członek może dodać swoich znajomych
        for m in p.add:
            if m in ok:
                c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (gid, m))
        for m in p.remove:
            if m != g["owner"]:
                c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, m))
        return _group_out(c, c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone(), u["id"])


@app.delete("/groups/{gid}")
def group_delete(gid: int, u=Depends(current_user)):
    """Założyciel usuwa grupę; pozostali członkowie z niej wychodzą."""
    with db() as c:
        g = c.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
        if not g or not _is_member(c, gid, u["id"]):
            raise HTTPException(404, "Nie ma takiej grupy.")
        if g["owner"] == u["id"]:
            c.execute("DELETE FROM groups WHERE id=?", (gid,))
            c.execute("UPDATE shares SET group_id=NULL WHERE group_id=?", (gid,))
            c.execute("DELETE FROM messages WHERE chat=?", (f"g{gid}",))
            c.execute("DELETE FROM chat_state WHERE chat=?", (f"g{gid}",))
        else:
            c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (gid, u["id"]))
    return {"ok": True}


@app.get("/groups/{gid}/shares")
def group_shares(gid: int, u=Depends(current_user)):
    """Co trafiło do grupy: otrzymane przeze mnie + wysłane przeze mnie (jedna pozycja na wysyłkę)."""
    with db() as c:
        if not _is_member(c, gid, u["id"]):
            raise HTTPException(404, "Nie ma takiej grupy.")
        rows = c.execute("""SELECT s.id, s.kind, s.title, s.created_at, s.sender, s.recipient, us.display_name sender_name
            FROM shares s JOIN users us ON us.id=s.sender
            WHERE s.group_id=? AND (s.recipient=? OR s.sender=?) ORDER BY s.id DESC""", (gid, u["id"], u["id"])).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["sender"] == u["id"]:
            k = (r["title"], r["created_at"], r["kind"])
            if k in seen:
                continue
            seen.add(k)
        out.append({k: r[k] for k in ("id", "kind", "title", "created_at", "sender", "sender_name")} | {"mine": r["sender"] == u["id"]})
    return out


# ---------- CZAT: rozmowy 1:1 ze znajomymi i czaty grup (wiadomości, udostępnienia, oznaczenia, wyciszenie) ----------
MAX_MSG = 4000
_msg_rate: dict[int, deque] = defaultdict(deque)


def _dm_key(a: int, b: int) -> str:
    a, b = sorted((a, b))
    return f"d{a}_{b}"


def _chat_access(c, key: str, uid: int):
    """('dm', id znajomego) albo ('group', id grupy); inaczej 404/403."""
    m = re.fullmatch(r"d(\d+)_(\d+)", key or "")
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a < b and uid in (a, b):
            other = b if uid == a else a
            if other not in _friend_ids(c, uid):
                raise HTTPException(403, "Pisać możecie, gdy jesteście znajomymi.")
            return "dm", other
    m = re.fullmatch(r"g(\d+)", key or "")
    if m and _is_member(c, int(m.group(1)), uid):
        return "group", int(m.group(1))
    raise HTTPException(404, "Nie ma takiej rozmowy.")


def _chat_member_ids(c, kind: str, ref: int, uid: int) -> list[int]:
    if kind == "dm":
        return [uid, ref]
    return [r[0] for r in c.execute("SELECT user_id FROM group_members WHERE group_id=?", (ref,))]


def _post(c, key: str, uid: int, body: str, share_id: int | None = None, mentions: list[int] | None = None) -> int:
    mid = c.execute("INSERT INTO messages(chat,sender,body,share_id,mentions) VALUES(?,?,?,?,?)",
                    (key, uid, body, share_id, ("," + ",".join(map(str, mentions)) + ",") if mentions else "")).lastrowid
    c.execute("INSERT INTO chat_state(user_id,chat,last_read) VALUES(?,?,?) "
              "ON CONFLICT(user_id,chat) DO UPDATE SET last_read=MAX(last_read,excluded.last_read)", (uid, key, mid))
    return mid


def _msg_out(c, r, share_cache: dict) -> dict:
    d = {"id": r["id"], "sender": r["sender"], "body": r["body"], "created_at": r["created_at"],
         "mentions": [int(x) for x in (r["mentions"] or "").split(",") if x]}
    if r["share_id"]:
        sid = r["share_id"]
        if sid not in share_cache:
            s = c.execute("SELECT id, kind, title FROM shares WHERE id=?", (sid,)).fetchone()
            share_cache[sid] = dict(s) if s else {"id": sid, "kind": "gone", "title": "Udostępnienie usunięte"}
        d["share"] = share_cache[sid]
    return d


def _my_chats(c, uid: int) -> list[tuple[str, str, int]]:
    """Wszystkie moje rozmowy: (klucz, rodzaj, id znajomego/grupy)."""
    out = [(_dm_key(uid, f), "dm", f) for f in _friend_ids(c, uid)]
    out += [(f"g{g}", "group", g) for (g,) in c.execute("SELECT group_id FROM group_members WHERE user_id=?", (uid,))]
    return out


def _unread(c, uid: int, keys: list[str]) -> dict:
    if not keys:
        return {}
    q = ",".join("?" * len(keys))
    rows = c.execute(f"""SELECT m.chat, COUNT(*) n, SUM(CASE WHEN m.mentions LIKE ? THEN 1 ELSE 0 END) men
        FROM messages m LEFT JOIN chat_state s ON s.user_id=? AND s.chat=m.chat
        WHERE m.chat IN ({q}) AND m.id > COALESCE(s.last_read, 0) AND m.sender != ?
        GROUP BY m.chat""", (f"%,{uid},%", uid, *keys, uid)).fetchall()
    return {r["chat"]: {"n": r["n"], "mention": bool(r["men"])} for r in rows}


@app.get("/chats/unread")
def chats_unread(u=Depends(current_user)):
    """Lekkie odpytywanie (co ~20 s): liczniki na ikonach Znajomi / Grupy. Wyciszona rozmowa liczy się tylko przy oznaczeniu."""
    with db() as c:
        chats = _my_chats(c, u["id"])
        un = _unread(c, u["id"], [k for k, _, _ in chats])
        muted = {r[0] for r in c.execute("SELECT chat FROM chat_state WHERE user_id=? AND muted=1", (u["id"],))}
        invites = c.execute("SELECT COUNT(*) FROM friendships WHERE addressee=? AND status='pending'", (u["id"],)).fetchone()[0]
        last = c.execute("SELECT MAX(id) FROM messages WHERE chat IN (%s)" % ",".join("?" * len(chats)), [k for k, _, _ in chats]).fetchone()[0] if chats else 0
    dm = grp = 0
    out = {}
    for k, kind, _ in chats:
        x = un.get(k)
        if not x:
            continue
        cnt = x["n"] if k not in muted else (1 if x["mention"] else 0)
        out[k] = {**x, "muted": k in muted}
        if kind == "dm":
            dm += cnt
        else:
            grp += cnt
    return {"dm": dm, "groups": grp, "invites": invites, "chats": out, "last": last or 0}


@app.get("/chats")
def chats_list(u=Depends(current_user)):
    """Lista rozmów z ostatnią wiadomością i liczbą nieprzeczytanych (od najświeższej)."""
    with db() as c:
        chats = _my_chats(c, u["id"])
        un = _unread(c, u["id"], [k for k, _, _ in chats])
        state = {r["chat"]: r for r in c.execute("SELECT * FROM chat_state WHERE user_id=?", (u["id"],))}
        out, cache = [], {}
        for k, kind, ref in chats:
            last = c.execute("SELECT * FROM messages WHERE chat=? ORDER BY id DESC LIMIT 1", (k,)).fetchone()
            item = {"key": k, "kind": kind, "ref": ref, "unread": (un.get(k) or {}).get("n", 0),
                    "mention": (un.get(k) or {}).get("mention", False), "muted": bool(state.get(k) and state[k]["muted"]),
                    "last": _msg_out(c, last, cache) if last else None}
            if last:
                item["last"]["sender_name"] = c.execute("SELECT display_name FROM users WHERE id=?", (last["sender"],)).fetchone()[0]
            out.append(item)
    out.sort(key=lambda x: x["last"]["id"] if x["last"] else 0, reverse=True)
    return out


@app.get("/chats/{key}/messages")
def chat_messages(key: str, after: int = 0, before: int = 0, limit: int = 50, u=Depends(current_user)):
    limit = max(1, min(limit, 100))
    with db() as c:
        kind, ref = _chat_access(c, key, u["id"])
        if after:
            rows = c.execute("SELECT * FROM messages WHERE chat=? AND id>? ORDER BY id LIMIT ?", (key, after, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages WHERE chat=?" + (" AND id<?" if before else "") + " ORDER BY id DESC LIMIT ?",
                             (key, before, limit) if before else (key, limit)).fetchall()[::-1]
        cache = {}
        ids = _chat_member_ids(c, kind, ref, u["id"])
        members = [public(r) for r in c.execute(f"SELECT * FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)]
        st = c.execute("SELECT * FROM chat_state WHERE user_id=? AND chat=?", (u["id"], key)).fetchone()
        # do kiedy przeczytał rozmówca (w rozmowie 1:1 — „Przeczytane” pod ostatnią wiadomością)
        seen = c.execute("SELECT last_read FROM chat_state WHERE user_id=? AND chat=?", (ref, key)).fetchone() if kind == "dm" else None
        return {"messages": [_msg_out(c, r, cache) for r in rows], "members": members, "muted": bool(st and st["muted"]),
                "last_read": st["last_read"] if st else 0, "seen": seen[0] if seen else 0, "more": len(rows) == limit and not after}


class MsgIn(BaseModel):
    body: str
    mentions: list[int] = []


@app.post("/chats/{key}/messages")
def chat_send(key: str, p: MsgIn, u=Depends(current_user)):
    body = (p.body or "").strip()
    if not body:
        raise HTTPException(400, "Pusta wiadomość.")
    if len(body) > MAX_MSG:
        raise HTTPException(413, f"Za długa wiadomość (limit {MAX_MSG} znaków).")
    q, now = _msg_rate[u["id"]], time.time()
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= 40:
        raise HTTPException(429, "Za dużo wiadomości naraz — chwilę poczekaj.")
    q.append(now)
    with db() as c:
        kind, ref = _chat_access(c, key, u["id"])
        members = set(_chat_member_ids(c, kind, ref, u["id"])) - {u["id"]}
        mentions = sorted({m for m in p.mentions if m in members})
        mid = _post(c, key, u["id"], body, None, mentions)
        return _msg_out(c, c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone(), {})


class ReadIn(BaseModel):
    last_id: int


@app.post("/chats/{key}/read")
def chat_read(key: str, p: ReadIn, u=Depends(current_user)):
    with db() as c:
        _chat_access(c, key, u["id"])
        top = c.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE chat=?", (key,)).fetchone()[0]
        c.execute("INSERT INTO chat_state(user_id,chat,last_read) VALUES(?,?,?) "
                  "ON CONFLICT(user_id,chat) DO UPDATE SET last_read=MAX(last_read,excluded.last_read)", (u["id"], key, min(p.last_id, top)))
    return {"ok": True}


class MuteIn(BaseModel):
    muted: bool


@app.post("/chats/{key}/mute")
def chat_mute(key: str, p: MuteIn, u=Depends(current_user)):
    with db() as c:
        _chat_access(c, key, u["id"])
        c.execute("INSERT INTO chat_state(user_id,chat,muted) VALUES(?,?,?) "
                  "ON CONFLICT(user_id,chat) DO UPDATE SET muted=excluded.muted", (u["id"], key, int(p.muted)))
    return {"muted": p.muted}


@app.get("/chats/{key}/shares")
def chat_shares(key: str, u=Depends(current_user)):
    """Zakładka „Udostępnione”: wszystko, co wymieniliście w tej rozmowie (także wysłane przeze mnie — z podglądem)."""
    with db() as c:
        kind, ref = _chat_access(c, key, u["id"])
        if kind == "dm":
            rows = c.execute("""SELECT s.id, s.kind, s.title, s.created_at, s.sender, us.display_name sender_name FROM shares s
                JOIN users us ON us.id=s.sender
                WHERE s.group_id IS NULL AND ((s.sender=? AND s.recipient=?) OR (s.sender=? AND s.recipient=? AND COALESCE(s.hidden,0)=0))
                ORDER BY s.id DESC""", (u["id"], ref, ref, u["id"])).fetchall()
        else:
            rows = c.execute("""SELECT s.id, s.kind, s.title, s.created_at, s.sender, us.display_name sender_name FROM shares s
                JOIN users us ON us.id=s.sender
                WHERE s.id IN (SELECT share_id FROM messages WHERE chat=? AND share_id IS NOT NULL)
                   OR (s.group_id=? AND s.recipient=? AND COALESCE(s.hidden,0)=0)
                ORDER BY s.id DESC""", (key, ref, u["id"])).fetchall()
    out, seen = [], set()
    for r in rows:
        k = (r["sender"], r["title"], r["created_at"], r["kind"])
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(r) | {"mine": r["sender"] == u["id"]})
    return out


# ---------- PLIKI (PDF i inne załączniki): kopia na koncie, dostępna na każdym urządzeniu i w udostępnieniach ----------
FILES_DIR = Path(os.getenv("CLOUD_FILES") or DB_PATH.parent / "files")
MAX_FILE = 60 * 1024 * 1024
QUOTA = int(os.getenv("FILES_QUOTA_MB", "2048")) * 1024 * 1024
_FNAME = re.compile(r"^[^/\\\x00]{1,200}$")


def _udir(uid: int) -> Path:
    d = FILES_DIR / f"u{uid}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fname(name: str) -> str:
    name = os.path.basename(name or "")
    if not _FNAME.match(name) or name in (".", ".."):
        raise HTTPException(400, "Zła nazwa pliku.")
    return name


@app.get("/files")
def files_list(u=Depends(current_user)):
    d = _udir(u["id"])
    return [{"name": f.name, "size": f.stat().st_size} for f in d.iterdir() if f.is_file()]


@app.put("/files/{name}")
async def files_put(name: str, request: Request, u=Depends(current_user)):
    name = _fname(name)
    body = await request.body()
    if not body:
        raise HTTPException(400, "Pusty plik.")
    if len(body) > MAX_FILE:
        raise HTTPException(413, "Plik za duży (limit 60 MB).")
    d = _udir(u["id"])
    used = sum(f.stat().st_size for f in d.iterdir() if f.is_file() and f.name != name)
    if used + len(body) > QUOTA:
        raise HTTPException(413, "Brak miejsca na koncie na kolejne pliki.")
    (d / name).write_bytes(body)
    return {"ok": True, "size": len(body)}


def _send_file(f: Path):
    from fastapi.responses import FileResponse
    if not f.is_file():
        raise HTTPException(404, "Nie ma takiego pliku na serwerze.")
    return FileResponse(f, filename=f.name.split("_", 1)[-1] if re.match(r"^[0-9a-f]{10}_", f.name) else f.name)


@app.get("/files/{name}")
def files_get(name: str, u=Depends(current_user)):
    return _send_file(_udir(u["id"]) / _fname(name))


@app.get("/shares/{sid}/files/{name}")
def share_file(sid: int, name: str, u=Depends(current_user)):
    """Plik z udostępnionej notatki — tylko gdy jest w jej treści; pochodzi z konta nadawcy."""
    name = _fname(name)
    with db() as c:
        r = _share_row(c, sid, u["id"])
    if not r or ("uploads/" + name) not in r["payload"]:
        raise HTTPException(404, "Nie znaleziono.")
    return _send_file(_udir(r["sender"]) / name)
