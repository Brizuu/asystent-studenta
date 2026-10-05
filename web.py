"""Wersja webowa Asystenta (https://zenfix.pl/asystent/app/) — ta sama aplikacja co na komputerze,
podpięta pod serwer kont w tym samym kontenerze.

- Bez ważnej sesji serwer zwraca tylko stronę logowania (aplikacja i dane są niedostępne).
- Sesja = token z serwera kont w ciasteczku `asys_web` (albo nagłówku Authorization).
- Każde konto ma osobny katalog danych: WEB_DATA/u<id>/ (baza, załączniki, ustawienia, klucz AI).
- Synchronizacja z aplikacją na Windows idzie przez serwer kont jak z każdego innego urządzenia.
"""
import os
from pathlib import Path

os.environ["ASYSTENT_WEB"] = "1"
os.environ.setdefault("ASYSTENT_SYNC_URL", "http://127.0.0.1:8100")
WEB_DATA = Path(os.getenv("WEB_DATA") or "/data/web")
os.environ.setdefault("ASYSTENT_DATA", str(WEB_DATA / "_wspolne"))   # katalog domyślny (nieużywany przez konta)

import db       # noqa: E402
import server   # noqa: E402

PUBLIC = {"/logo.png", "/icon-192.png", "/icon-512.png", "/manifest.webmanifest"}
_ready: set = set()


def _token(scope) -> str:
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "asys_web":
            return v.strip()
    return ""


async def _send(send, status: int, body: bytes, ctype: str, extra=()):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype.encode()), (b"cache-control", b"no-store"), *extra]})
    await send({"type": "http.response.body", "body": body})


def make_app(resolve_user):
    """resolve_user(token) -> id konta albo None (sprawdza sesję w bazie serwera kont)."""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await server.app(scope, receive, send)
        root = scope.get("root_path", "")
        path = scope["path"]
        rel = path[len(root):] if root and path.startswith(root) else path
        if rel == "" and scope["method"] == "GET":   # /asystent/app → /asystent/app/ (względne ścieżki)
            return await _send(send, 301, b"", "text/plain", [(b"location", (root + "/").encode())])
        if rel in PUBLIC:
            return await server.app(scope, receive, send)
        uid = resolve_user(_token(scope))
        if not uid:
            if rel.startswith("/api/") or rel.startswith("/uploads/"):
                return await _send(send, 401, b'{"detail":"Zaloguj si\\u0119."}', "application/json")
            return await _send(send, 200, LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        tok = db.use_data_dir(WEB_DATA / f"u{uid}")
        try:
            if uid not in _ready:
                db.DATA_DIR.mkdir(parents=True, exist_ok=True)
                server.init_all()
                _ready.add(uid)
            return await server.app(scope, receive, send)
        finally:
            db.reset_data_dir(tok)

    return app


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="pl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asystent — zaloguj się</title><meta name="theme-color" content="#0b0b14"><meta name="color-scheme" content="dark">
<link rel="icon" href="icon-192.png"><link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-192.png">
<style>
:root{--ink:#f5f5f7;--dim:#b9b4d6}
*{box-sizing:border-box;margin:0}
body{min-height:100vh;min-height:100dvh;display:grid;place-items:center;padding:24px 16px;color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Inter,Roboto,sans-serif;
  background:radial-gradient(55% 45% at 18% 12%,rgba(205,192,255,.55),transparent 70%),radial-gradient(48% 42% at 88% 18%,rgba(255,192,150,.42),transparent 70%),
    radial-gradient(55% 50% at 72% 92%,rgba(150,205,255,.4),transparent 70%),radial-gradient(42% 40% at 8% 88%,rgba(128,220,194,.36),transparent 70%),
    linear-gradient(160deg,#36325a,#2b2c4b 55%,#372d47) fixed}
.wrap{width:min(420px,100%);text-align:center}
.logo{width:76px;height:76px;margin:0 auto 18px;border-radius:24%;box-shadow:0 18px 40px -10px rgba(110,96,255,.6)}
h1{font-size:30px;letter-spacing:-.025em;font-weight:700}
.sub{color:var(--dim);margin:6px 0 22px}
.card{padding:22px;border-radius:24px;text-align:left;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);
  backdrop-filter:blur(22px) saturate(160%);-webkit-backdrop-filter:blur(22px) saturate(160%);box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 30px 60px -30px rgba(10,8,30,.8)}
.tabs{display:flex;gap:4px;padding:4px;border-radius:14px;background:rgba(0,0,0,.18);margin-bottom:18px}
.tabs button{flex:1;padding:9px;border:0;border-radius:11px;background:transparent;color:var(--dim);font:inherit;font-weight:600;cursor:pointer}
.tabs button.on{background:rgba(255,255,255,.16);color:var(--ink)}
label{display:block;margin-bottom:12px} label span{display:block;font-size:12.5px;color:var(--dim);margin-bottom:6px}
input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.18);background:rgba(10,10,25,.35);color:var(--ink);font:inherit;outline:none}
input:focus{border-color:rgba(195,181,255,.8);box-shadow:0 0 0 3px rgba(159,171,255,.25)}
input:-webkit-autofill{-webkit-box-shadow:0 0 0 40px #262641 inset;-webkit-text-fill-color:var(--ink)}
.btn{width:100%;margin-top:6px;padding:13px;border:0;border-radius:12px;font:inherit;font-weight:600;color:#1d1b33;cursor:pointer;
  background:linear-gradient(135deg,#c3b5ff,#9fd0ff);transition:transform .15s}
.btn:active{transform:scale(.98)} .btn[disabled]{opacity:.6}
.err{min-height:20px;margin:4px 0 2px;color:#ffb0a8;font-size:13.5px}
.foot{margin-top:16px;font-size:12.5px;color:var(--dim)} .foot a{color:#d9d2ff}
[hidden]{display:none!important}
</style></head>
<body><div class="wrap">
  <img class="logo" src="icon-192.png" alt="">
  <h1>Asystent studenta</h1>
  <p class="sub">Zaloguj się, żeby otworzyć wersję webową.</p>
  <form class="card" id="f">
    <div class="tabs"><button type="button" class="on" data-t="login">Logowanie</button><button type="button" data-t="register">Załóż konto</button></div>
    <label id="l-name" hidden><span>Imię i nazwisko</span><input id="name" autocomplete="name"></label>
    <label><span>E-mail</span><input id="mail" type="email" autocomplete="email" required></label>
    <label><span>Hasło</span><input id="pass" type="password" autocomplete="current-password" required minlength="8"></label>
    <div class="err" id="err"></div>
    <button class="btn" id="go">Zaloguj się</button>
  </form>
  <p class="foot">To samo konto co w aplikacji na Windows — dane synchronizują się między urządzeniami.<br><a href="../">Pobierz aplikację na Windows</a></p>
</div>
<script>
let mode='login'; const $=s=>document.querySelector(s);
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{mode=b.dataset.t;
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));
  $('#l-name').hidden=mode!=='register'; $('#go').textContent=mode==='register'?'Załóż konto':'Zaloguj się';
  $('#pass').autocomplete=mode==='register'?'new-password':'current-password'; $('#err').textContent='';});
$('#f').onsubmit=async e=>{e.preventDefault(); $('#err').textContent=''; $('#go').disabled=true;
  try{
    const body=mode==='register'?{display_name:$('#name').value.trim(),email:$('#mail').value.trim(),password:$('#pass').value}:{login:$('#mail').value.trim(),password:$('#pass').value};
    const r=await fetch('../auth/'+(mode==='register'?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json().catch(()=>({})); if(!r.ok)throw new Error(d.detail||'Błąd '+r.status);
    try{localStorage.setItem('asys_token',d.token);}catch(_){}
    const dir=location.pathname.replace(/[^/]*$/,'');
    document.cookie='asys_web='+d.token+';path='+dir+';max-age='+60*86400+';samesite=strict'+(location.protocol==='https:'?';secure':'');
    location.reload();
  }catch(err){$('#err').textContent=err.message;$('#go').disabled=false;}
};
</script></body></html>"""
