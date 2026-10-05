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


LOGIN_PAGE = r"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asystent studenta — zaloguj się</title><meta name="theme-color" content="#14122a"><meta name="color-scheme" content="dark">
<link rel="icon" href="icon-192.png"><link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-192.png">
<style>
:root{--ink:#f6f4ff;--soft:#d7d2ee;--dim:#a7a2c6;--line:rgba(255,255,255,.14);--acc:#b9a8ff;--acc2:#9fd0ff;--spring:cubic-bezier(.2,.9,.25,1.15)}
*{box-sizing:border-box;margin:0}
html,body{min-height:100%}
body{min-height:100vh;min-height:100dvh;color:var(--ink);overflow-x:hidden;
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable","Segoe UI",Inter,Roboto,sans-serif;background:#1b1934}
/* tło: płynące plamy kolorów + kropki + ziarno */
.bg{position:fixed;inset:0;z-index:-2;overflow:hidden;background:linear-gradient(160deg,#2f2a55,#25264a 50%,#33284a)}
.bg i{position:absolute;border-radius:50%;filter:blur(70px);opacity:.75;animation:drift 22s ease-in-out infinite alternate}
.bg i:nth-child(1){width:52vmax;height:52vmax;left:-14vmax;top:-18vmax;background:#8f7cff;animation-duration:26s}
.bg i:nth-child(2){width:44vmax;height:44vmax;right:-12vmax;top:-10vmax;background:#ff9fb1;opacity:.55;animation-duration:30s}
.bg i:nth-child(3){width:46vmax;height:46vmax;right:-8vmax;bottom:-22vmax;background:#6fb8ff;opacity:.55;animation-duration:24s}
.bg i:nth-child(4){width:36vmax;height:36vmax;left:-8vmax;bottom:-16vmax;background:#62d6b4;opacity:.45;animation-duration:28s}
@keyframes drift{to{transform:translate(6vmax,4vmax) scale(1.08)}}
.bg::after{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,.14) 1px,transparent 1.2px);background-size:26px 26px;
  mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%);-webkit-mask-image:radial-gradient(ellipse at center,#000 30%,transparent 80%)}
.grain{position:fixed;inset:-50%;z-index:-1;pointer-events:none;opacity:.07;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
.wrap{min-height:100vh;min-height:100dvh;display:grid;grid-template-columns:1.05fr .95fr;align-items:center;gap:48px;max-width:1160px;margin:0 auto;padding:40px 28px}
/* lewa strona: marka i to, co daje aplikacja */
.hero{position:relative;animation:rise .7s var(--spring) both}
.brand{display:flex;align-items:center;gap:14px}
.brand img{width:58px;height:58px;border-radius:17px;box-shadow:0 16px 40px -12px rgba(120,100,255,.75),0 0 0 1px rgba(255,255,255,.18) inset;animation:bob 6s ease-in-out infinite}
.brand b{font-size:19px;letter-spacing:-.01em} .brand small{display:block;color:var(--dim);font-size:13px;font-weight:500}
.hero h1{margin-top:34px;font-size:clamp(36px,4.6vw,58px);line-height:1.06;letter-spacing:-.035em;font-weight:750}
.hero h1 span{background:linear-gradient(100deg,#fff 10%,#e4dcff 45%,#ffd6e0 80%);-webkit-background-clip:text;background-clip:text;color:transparent}
.hero p.lead{margin-top:16px;font-size:17px;color:var(--soft);max-width:470px}
.feats{margin-top:28px;display:grid;grid-template-columns:1fr 1fr;gap:10px;max-width:500px}
.feat{display:flex;align-items:center;gap:11px;padding:11px 13px;border-radius:15px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);font-size:13.5px;color:var(--soft);animation:rise .7s var(--spring) both}
.feat:nth-child(1){animation-delay:.08s}.feat:nth-child(2){animation-delay:.14s}.feat:nth-child(3){animation-delay:.2s}.feat:nth-child(4){animation-delay:.26s}
.feat i{width:30px;height:30px;flex:none;border-radius:10px;display:flex;align-items:center;justify-content:center;background:color-mix(in srgb,var(--c) 26%,transparent)}
.feat svg{width:16px;height:16px;fill:none;stroke:var(--c);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
/* pływające karteczki */
.float{position:absolute;padding:11px 14px;border-radius:16px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.16);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  box-shadow:0 20px 40px -20px rgba(10,8,30,.7);font-size:12.5px;color:var(--soft);animation:bob 7s ease-in-out infinite;pointer-events:none}
.float b{display:block;color:#fff;font-size:13.5px}
.f1{right:-10px;top:-6px;transform:rotate(4deg);animation-delay:-2s}
.f2{right:-30px;bottom:-96px;transform:rotate(-3deg);animation-delay:-4s}
.f1 .dots{display:flex;gap:4px;margin-top:6px}.f1 .dots i{width:7px;height:7px;border-radius:50%}
.f2 .bar{height:5px;width:120px;border-radius:3px;background:rgba(255,255,255,.15);margin-top:7px;overflow:hidden}.f2 .bar i{display:block;height:100%;width:62%;background:linear-gradient(90deg,#54d996,#5eead4)}
@keyframes bob{50%{translate:0 -8px}}
@keyframes rise{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:none}}
/* prawa strona: karta logowania */
.card{position:relative;width:100%;max-width:430px;justify-self:center;padding:30px;border-radius:30px;
  background:linear-gradient(160deg,rgba(255,255,255,.16),rgba(255,255,255,.06));border:1px solid rgba(255,255,255,.2);
  backdrop-filter:blur(28px) saturate(170%);-webkit-backdrop-filter:blur(28px) saturate(170%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 40px 80px -30px rgba(8,6,28,.85);animation:rise .8s var(--spring) .1s both}
.card h2{font-size:24px;letter-spacing:-.02em} .card .sub{color:var(--dim);font-size:14px;margin:4px 0 20px}
.tabs{position:relative;display:grid;grid-template-columns:1fr 1fr;padding:4px;border-radius:15px;background:rgba(0,0,0,.22);margin-bottom:20px}
.tabs::before{content:"";position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);border-radius:11px;background:rgba(255,255,255,.17);
  box-shadow:0 4px 14px -6px rgba(0,0,0,.5);transition:transform .35s var(--spring)}
.tabs.reg::before{transform:translateX(100%)}
.tabs button{position:relative;padding:10px;border:0;background:transparent;color:var(--dim);font:inherit;font-weight:600;cursor:pointer;transition:color .2s}
.tabs button.on{color:#fff}
.fld{position:relative;margin-bottom:12px}
.fld[hidden]{display:none}
.fld svg{position:absolute;left:14px;top:50%;width:18px;height:18px;margin-top:-9px;fill:none;stroke:var(--dim);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;transition:stroke .2s;pointer-events:none}
.fld input{width:100%;padding:15px 44px 15px 44px;border-radius:15px;border:1px solid rgba(255,255,255,.16);background:rgba(10,10,28,.32);color:var(--ink);font:inherit;outline:none;transition:border-color .2s,box-shadow .2s,background .2s}
.fld input::placeholder{color:#8f8ab0}
.fld input:focus{border-color:rgba(196,183,255,.85);background:rgba(10,10,28,.42);box-shadow:0 0 0 4px rgba(159,171,255,.2)}
.fld input:focus~svg,.fld:focus-within svg{stroke:#d9d2ff}
.fld input:-webkit-autofill{-webkit-box-shadow:0 0 0 40px #2a2850 inset;-webkit-text-fill-color:var(--ink);caret-color:var(--ink)}
.eye{position:absolute;right:8px;top:50%;margin-top:-17px;width:34px;height:34px;border:0;border-radius:10px;background:transparent;color:var(--dim);cursor:pointer;display:flex;align-items:center;justify-content:center}
.eye:hover{background:rgba(255,255,255,.08);color:#fff} .eye svg{position:static;margin:0;stroke:currentColor}
.meter{display:flex;gap:4px;margin:-4px 2px 12px} .meter i{flex:1;height:4px;border-radius:2px;background:rgba(255,255,255,.12);transition:background .25s}
.meter[hidden]{display:none}
.err{min-height:20px;margin:2px 2px 8px;color:#ffb3ab;font-size:13.5px}
.go{position:relative;width:100%;padding:15px;border:0;border-radius:15px;font:inherit;font-weight:700;font-size:15.5px;color:#1b1838;cursor:pointer;overflow:hidden;
  background:linear-gradient(120deg,#c9bcff,#a8d4ff 55%,#ffc9d6);background-size:180% 100%;box-shadow:0 16px 34px -14px rgba(160,150,255,.9);
  transition:transform .15s,background-position .5s,box-shadow .2s}
.go:hover{background-position:100% 0;box-shadow:0 18px 40px -12px rgba(160,150,255,1)} .go:active{transform:scale(.98)}
.go[disabled]{opacity:.75;cursor:default}
.go .spin{display:none;width:18px;height:18px;margin:-3px 8px -3px 0;border-radius:50%;border:2.5px solid rgba(27,24,56,.25);border-top-color:#1b1838;animation:sp .8s linear infinite;vertical-align:middle}
.go.busy .spin{display:inline-block} @keyframes sp{to{transform:rotate(360deg)}}
.note{display:flex;align-items:center;gap:10px;margin-top:18px;padding:12px 14px;border-radius:14px;background:rgba(0,0,0,.16);font-size:12.5px;color:var(--soft)}
.note svg{width:18px;height:18px;flex:none;fill:none;stroke:#9fe8c8;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.foot{margin-top:16px;text-align:center;font-size:13px;color:var(--dim)} .foot a{color:#e3dcff;text-decoration:none;border-bottom:1px solid rgba(227,220,255,.35)} .foot a:hover{border-color:#e3dcff}
@media(max-width:900px){
  .wrap{grid-template-columns:1fr;gap:26px;padding:28px 16px 32px}
  .hero{text-align:center} .brand{justify-content:center} .hero h1{margin-top:18px;font-size:34px} .hero p.lead{margin:10px auto 0;font-size:15.5px}
  .feats,.float{display:none} .card{padding:24px 20px;border-radius:26px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head>
<body><div class="bg"><i></i><i></i><i></i><i></i></div><div class="grain"></div>
<main class="wrap">
  <section class="hero">
    <div class="brand"><img src="icon-192.png" alt=""><div><b>Asystent studenta</b><small>wersja w przeglądarce</small></div></div>
    <h1><span>Całe studia<br>w jednym miejscu.</span></h1>
    <p class="lead">Plan zajęć, zeszyty z notatkami, quizy z AI, zadania i budżet — to samo konto co w aplikacji na Windows.</p>
    <div class="feats">
      <div class="feat" style="--c:#b9a8ff"><i><svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/></svg></i>Plan z USOS</div>
      <div class="feat" style="--c:#9fd0ff"><i><svg viewBox="0 0 24 24"><path d="M4 6a1.5 1.5 0 011.5-1.5H11v15H5.5A1.5 1.5 0 014 18z"/><path d="M20 6a1.5 1.5 0 00-1.5-1.5H13v15h5.5A1.5 1.5 0 0020 18z"/></svg></i>Notatki i PDF-y</div>
      <div class="feat" style="--c:#ffc28a"><i><svg viewBox="0 0 24 24"><path d="M11 4l1.8 4.7L17.5 10.5l-4.7 1.8L11 17l-1.8-4.7-4.7-1.8 4.7-1.8z"/></svg></i>Quizy i notatki AI</div>
      <div class="feat" style="--c:#7ee2b8"><i><svg viewBox="0 0 24 24"><circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5a5.5 5.5 0 0111 0"/><circle cx="17" cy="9.5" r="2.4"/><path d="M15.5 14a4.5 4.5 0 015 4.5"/></svg></i>Znajomi i grupy</div>
    </div>
    <div class="float f1"><b>Dziś · 3 zajęcia</b>Wykład o 8:00 · s. 231<div class="dots"><i style="background:#b9a8ff"></i><i style="background:#9fd0ff"></i><i style="background:#ffc28a"></i></div></div>
    <div class="float f2"><b>Quiz: Całki</b>12 z 20 nauczone<div class="bar"><i></i></div></div>
  </section>

  <form class="card" id="f" novalidate>
    <h2 id="ttl">Witaj ponownie 👋</h2><p class="sub" id="sub">Zaloguj się, żeby otworzyć swoje notatki.</p>
    <div class="tabs" id="tabs"><button type="button" class="on" data-t="login">Logowanie</button><button type="button" data-t="register">Załóż konto</button></div>
    <div class="fld" id="l-name" hidden><input id="name" autocomplete="name" placeholder="Imię i nazwisko"><svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.6"/><path d="M5 20a7 7 0 0114 0"/></svg></div>
    <div class="fld"><input id="mail" type="email" autocomplete="email" placeholder="E-mail" required><svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M4 7l8 6 8-6"/></svg></div>
    <div class="fld"><input id="pass" type="password" autocomplete="current-password" placeholder="Hasło" required minlength="8"><svg viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 018 0v2.5"/></svg>
      <button type="button" class="eye" id="eye" aria-label="Pokaż hasło"><svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/></svg></button></div>
    <div class="meter" id="meter" hidden><i></i><i></i><i></i><i></i></div>
    <div class="err" id="err" role="alert"></div>
    <button class="go" id="go"><span class="spin"></span><span id="go-l">Zaloguj się</span></button>
    <div class="note"><svg viewBox="0 0 24 24"><path d="M20 11.5A8 8 0 006.3 6.3L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 12.5a8 8 0 0013.7 5.2L20 15.5"/><path d="M20 20v-4.5h-4.5"/></svg>Notatki, plan i pliki synchronizują się z aplikacją na Windows.</div>
    <p class="foot">Wolisz aplikację? <a href="../">Pobierz na Windows</a></p>
  </form>
</main>
<script>
let mode='login'; const $=s=>document.querySelector(s);
function setMode(m){mode=m; const reg=m==='register';
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===m)); $('#tabs').classList.toggle('reg',reg);
  $('#l-name').hidden=!reg; $('#meter').hidden=!reg; $('#go-l').textContent=reg?'Załóż konto':'Zaloguj się';
  $('#ttl').textContent=reg?'Zaczynamy ✨':'Witaj ponownie 👋'; $('#sub').textContent=reg?'Konto zajmie chwilę — działa też w aplikacji na Windows.':'Zaloguj się, żeby otworzyć swoje notatki.';
  $('#pass').autocomplete=reg?'new-password':'current-password'; $('#pass').placeholder=reg?'Hasło (min. 8 znaków)':'Hasło'; $('#err').textContent='';
  (reg?$('#name'):$('#mail')).focus();}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>setMode(b.dataset.t));
$('#eye').onclick=()=>{const p=$('#pass');p.type=p.type==='password'?'text':'password';};
$('#pass').addEventListener('input',()=>{const v=$('#pass').value;let n=0;if(v.length>=8)n++;if(/[A-Z]/.test(v)&&/[a-z]/.test(v))n++;if(/\d/.test(v))n++;if(/[^\w]/.test(v)||v.length>=14)n++;
  const c=['#ff8a80','#ffb454','#ffe066','#54d996'][Math.max(0,n-1)];document.querySelectorAll('#meter i').forEach((x,i)=>x.style.background=i<n?c:'');});
$('#f').onsubmit=async e=>{e.preventDefault(); $('#err').textContent='';
  const mail=$('#mail').value.trim(), pass=$('#pass').value, name=$('#name').value.trim();
  if(mode==='register'&&!name){$('#err').textContent='Podaj imię i nazwisko.';$('#name').focus();return;}
  if(!/^\S+@\S+\.\S+$/.test(mail)){$('#err').textContent='Wpisz poprawny e-mail.';$('#mail').focus();return;}
  if(pass.length<8){$('#err').textContent='Hasło musi mieć co najmniej 8 znaków.';$('#pass').focus();return;}
  const go=$('#go'); go.disabled=true; go.classList.add('busy');
  try{
    const body=mode==='register'?{display_name:name,email:mail,password:pass}:{login:mail,password:pass};
    const r=await fetch('../auth/'+(mode==='register'?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d=await r.json().catch(()=>({})); if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'Nie udało się ('+r.status+').');
    try{localStorage.setItem('asys_token',d.token);}catch(_){}
    const dir=location.pathname.replace(/[^/]*$/,'');
    document.cookie='asys_web='+d.token+';path='+dir+';max-age='+60*86400+';samesite=strict'+(location.protocol==='https:'?';secure':'');
    $('#go-l').textContent='Otwieram…'; location.reload();
  }catch(err){$('#err').textContent=err.message;go.disabled=false;go.classList.remove('busy');}
};
setTimeout(()=>$('#mail').focus(),300);
</script></body></html>"""
