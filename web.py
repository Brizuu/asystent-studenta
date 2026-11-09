"""Wersja webowa Asystenta (https://zenexus.pl/asystent/app/) — ta sama aplikacja co na komputerze,
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
<title>Asystent studenta — zaloguj się</title><meta name="theme-color" content="#0B0E14"><meta name="color-scheme" content="dark">
<link rel="icon" href="icon-192.png"><link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0B0E14;--ink:#EEF1F6;--soft:#9AA3B2;--dim:#7A8394;--acc:#5A96F2;--acc2:#8DB6FF;--live:#4FD1B8;--btn:#E6EDF9;--ease:cubic-bezier(.2,.8,.2,1)}
*{box-sizing:border-box;margin:0}
html,body{min-height:100%}
body{min-height:100vh;min-height:100dvh;color:var(--ink);overflow-x:hidden;background:var(--bg);font:15px/1.5 Manrope,system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
/* tło jak na stronie pobierania: dwie nieruchome poświaty */
.sky{position:fixed;inset:0;z-index:-2;overflow:hidden;pointer-events:none}
.sky i{position:absolute;border-radius:50%;filter:blur(110px);opacity:.42}
.sky i:nth-child(1){width:58vmax;height:46vmax;right:-14vmax;top:-22vmax;background:#24457F}
.sky i:nth-child(2){width:46vmax;height:40vmax;left:-18vmax;bottom:-16vmax;background:#164E4C;opacity:.3}
.sky i:nth-child(3){display:none}
.wrap{min-height:100vh;min-height:100dvh;display:grid;grid-template-columns:1.05fr .95fr;grid-template-rows:auto 1fr auto;align-items:center;column-gap:56px;max-width:1200px;margin:0 auto;padding:22px 32px}
.top{grid-column:1/-1;display:flex;align-items:center;gap:12px}
.brand{white-space:nowrap;display:flex;align-items:center;gap:12px;color:var(--ink);text-decoration:none;font-weight:800;font-size:18px;letter-spacing:-.01em}
.brand svg{width:34px;height:34px}
.top .back{margin-left:auto;color:var(--soft);text-decoration:none;font-weight:600;font-size:14px;display:inline-flex;gap:6px;transition:color .2s}.top .back:hover{color:var(--ink)}
/* lewa strona */
.hero{position:relative}
.hero h1{font-size:clamp(38px,4.8vw,60px);line-height:1.04;letter-spacing:-.045em;font-weight:800}
.hero p.lead{margin-top:16px;font-size:17px;line-height:1.6;color:var(--soft);max-width:470px}
.points{list-style:none;margin:28px 0 0;padding:0;display:flex;flex-direction:column;gap:12px;max-width:470px}
.points li{display:flex;gap:12px;align-items:flex-start;color:var(--ink);font-size:15.5px;line-height:1.5}
.points li::before{content:"";width:18px;height:18px;flex:none;margin-top:2px;border-radius:50%;background:rgba(79,209,184,.2) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M4.5 8.2l2.2 2.2L11.5 5.6' fill='none' stroke='%23EEF1F6' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/18px no-repeat}
.points small{display:block;color:var(--soft);font-size:14px}
@keyframes in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.rise{opacity:0;animation:in .7s var(--ease) forwards}
/* karta logowania */
.card{position:relative;width:100%;max-width:430px;justify-self:center;padding:30px;border-radius:26px;overflow:hidden;
  background:linear-gradient(160deg,rgba(22,27,37,.82),rgba(14,18,26,.88));border:1px solid rgba(255,255,255,.1);
  backdrop-filter:blur(28px) saturate(140%);-webkit-backdrop-filter:blur(28px) saturate(140%);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 40px 90px -36px rgba(0,0,0,.9);opacity:0;animation:in .7s var(--ease) .15s forwards}
.card h2{font-size:25px;letter-spacing:-.025em;font-weight:800} .card .sub{color:var(--soft);font-size:14px;margin:4px 0 20px}
.tabs{position:relative;display:grid;grid-template-columns:1fr 1fr;padding:4px;border-radius:14px;background:rgba(0,0,0,.28);border:1px solid rgba(255,255,255,.06);margin-bottom:20px}
.tabs::before{content:"";position:absolute;top:4px;bottom:4px;left:4px;width:calc(50% - 4px);border-radius:10px;background:rgba(90,150,242,.2);border:1px solid rgba(141,182,255,.35);transition:transform .45s var(--ease)}
.tabs.reg::before{transform:translateX(100%)}
.tabs button{position:relative;padding:10px;border:0;background:transparent;color:var(--soft);font:inherit;font-weight:700;cursor:pointer;transition:color .2s}
.tabs button.on{color:var(--ink)}
.fld{position:relative;margin-bottom:12px}
.fld[hidden]{display:none}
.fld>svg{position:absolute;left:14px;top:50%;width:18px;height:18px;margin-top:-9px;fill:none;stroke:var(--dim);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;transition:stroke .2s;pointer-events:none}
.fld input{width:100%;padding:15px 44px 15px 44px;border-radius:14px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:var(--ink);font:inherit;outline:none;transition:border-color .2s,box-shadow .2s,background .2s}
.fld input::placeholder{color:var(--dim)}
.fld input:focus{border-color:rgba(141,182,255,.7);background:rgba(255,255,255,.06);box-shadow:0 0 0 4px rgba(90,150,242,.18)}
.fld:focus-within>svg{stroke:var(--acc2)}
.fld input:-webkit-autofill{-webkit-box-shadow:0 0 0 40px #151a24 inset;-webkit-text-fill-color:var(--ink);caret-color:var(--ink)}
.eye{position:absolute;right:8px;top:50%;margin-top:-17px;width:34px;height:34px;border:0;border-radius:10px;background:transparent;color:var(--dim);cursor:pointer;display:flex;align-items:center;justify-content:center}
.eye:hover{background:rgba(255,255,255,.06);color:var(--ink)} .eye svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.meter{display:flex;gap:4px;margin:-4px 2px 12px} .meter i{flex:1;height:4px;border-radius:2px;background:rgba(255,255,255,.1);transition:background .25s}
.meter[hidden]{display:none}
.err{min-height:20px;margin:2px 2px 8px;color:#F19A93;font-size:13.5px}
.go{position:relative;width:100%;padding:15px;border:0;border-radius:14px;font:inherit;font-weight:800;font-size:15.5px;color:var(--bg);cursor:pointer;overflow:hidden;
  background:var(--btn);box-shadow:0 16px 36px -20px rgba(90,150,242,.7);transition:transform .2s var(--ease),background .2s}
.go:hover{background:#fff} .go:active{transform:scale(.985)} .go:focus-visible,.tabs button:focus-visible,.eye:focus-visible{outline:2px solid var(--acc2);outline-offset:2px}
.go[disabled]{opacity:.75;cursor:default}
.go .spin{display:none;width:18px;height:18px;margin:-3px 8px -3px 0;border-radius:50%;border:2.5px solid rgba(11,14,20,.2);border-top-color:var(--bg);animation:sp .8s linear infinite;vertical-align:middle}
.go.busy .spin{display:inline-block} @keyframes sp{to{transform:rotate(360deg)}}
.note{display:flex;align-items:center;gap:10px;margin-top:18px;padding:12px 14px;border-radius:14px;background:rgba(79,209,184,.06);border:1px solid rgba(79,209,184,.16);font-size:12.5px;color:var(--soft)}
.note svg{width:18px;height:18px;flex:none;fill:none;stroke:var(--live);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.foot{margin-top:16px;text-align:center;font-size:13px;color:var(--dim)} .foot a{color:var(--acc2);text-decoration:none;border-bottom:1px solid rgba(141,182,255,.3)} .foot a:hover{border-color:var(--acc2)}
.credit{grid-column:1/-1;display:flex;justify-content:center;align-items:center;gap:8px;padding-top:18px;color:var(--dim);font-size:12.5px}
.credit a{display:inline-flex;align-items:center;gap:6px;color:var(--soft);text-decoration:none;font-weight:700;transition:color .2s}.credit a:hover{color:var(--ink)}
.credit svg{width:15px;height:15px;fill:currentColor}
@media(max-width:900px){
  .wrap{grid-template-columns:1fr;row-gap:22px;padding:18px 16px 22px}
  .hero{text-align:center} .hero h1{font-size:36px} .hero p.lead{margin:10px auto 0;font-size:15.5px}
  .points{display:none} .card{padding:24px 20px;border-radius:24px}
  .brand{font-size:16px}.brand svg{width:30px;height:30px}.top .back span{display:none}.top .back{font-size:18px}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}.card,.rise{opacity:1}}
</style></head>
<body><div class="sky" aria-hidden="true"><i></i><i></i><i></i></div>
<main class="wrap">
  <div class="top rise"><a class="brand" href="../"><svg viewBox="0 0 64 64" aria-hidden="true"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5E9BF4"/><stop offset="1" stop-color="#3F7BE0"/></linearGradient></defs><rect width="64" height="64" rx="16" fill="url(#lg)"/><g fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 26.5L32 17l20 9.5L32 36z"/><path d="M20.5 31v8.5c6.5 5 16.5 5 23 0V31"/><path d="M52 26.5v11"/></g></svg>Asystent studenta</a>
    <a class="back" href="../">← <span>Strona główna</span></a></div>
  <section class="hero">
    <h1 class="rise" style="animation-delay:.05s">Cały semestr.<br>Jedno logowanie.</h1>
    <p class="lead rise" style="animation-delay:.1s">Asystent studenta w przeglądarce. To samo konto co w aplikacji na Windows i Androida.</p>
    <ul class="points rise" style="animation-delay:.15s">
      <li><span>Plan zajęć z USOS<small>Wklejasz link raz, plan aktualizuje się sam.</small></span></li>
      <li><span>Notatki z AI<small>Z PDF, zdjęć albo nagrania wykładu.</small></span></li>
      <li><span>Na każdym urządzeniu<small>Zmiany z telefonu widzisz tu od razu.</small></span></li>
    </ul>
  </section>

  <form class="card" id="f" novalidate>
    <h2 id="ttl">Witaj ponownie</h2><p class="sub" id="sub">Zaloguj się, żeby otworzyć swoje notatki.</p>
    <div class="tabs" id="tabs"><button type="button" class="on" data-t="login">Logowanie</button><button type="button" data-t="register">Załóż konto</button></div>
    <div class="fld" id="l-name" hidden><input id="name" autocomplete="name" placeholder="Imię i nazwisko"><svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.6"/><path d="M5 20a7 7 0 0114 0"/></svg></div>
    <div class="fld"><input id="mail" type="email" autocomplete="email" placeholder="E-mail" required><svg viewBox="0 0 24 24"><rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M4 7l8 6 8-6"/></svg></div>
    <div class="fld"><input id="pass" type="password" autocomplete="current-password" placeholder="Hasło" required minlength="8"><svg viewBox="0 0 24 24"><rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8 10.5V8a4 4 0 018 0v2.5"/></svg>
      <button type="button" class="eye" id="eye" aria-label="Pokaż hasło"><svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/></svg></button></div>
    <div class="meter" id="meter" hidden><i></i><i></i><i></i><i></i></div>
    <div class="err" id="err" role="alert"></div>
    <button class="go" id="go"><span class="spin"></span><span id="go-l">Zaloguj się</span></button>
    <div class="note"><svg viewBox="0 0 24 24"><path d="M20 11.5A8 8 0 006.3 6.3L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 12.5a8 8 0 0013.7 5.2L20 15.5"/><path d="M20 20v-4.5h-4.5"/></svg>Notatki, plan i pliki synchronizują się z aplikacją na Windows i Androida.</div>
    <p class="foot">Wolisz aplikację? <a href="../">Pobierz na Windows lub Androida</a></p>
  </form>
  <div class="credit rise" style="animation-delay:.3s">Tworzy <a href="https://github.com/Brizuu" target="_blank" rel="noopener"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5a9.5 9.5 0 00-3 18.5c.5.1.7-.2.7-.5v-1.7c-2.7.6-3.2-1.2-3.2-1.2-.4-1.1-1.1-1.4-1.1-1.4-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.5 2.3 1.1 2.9.8.1-.6.3-1.1.6-1.3-2.1-.2-4.4-1.1-4.4-4.8 0-1 .4-1.9 1-2.6-.1-.2-.4-1.2.1-2.5 0 0 .8-.3 2.6 1a9 9 0 014.8 0c1.8-1.3 2.6-1 2.6-1 .5 1.3.2 2.3.1 2.5.6.7 1 1.6 1 2.6 0 3.7-2.3 4.6-4.4 4.8.3.3.7.9.7 1.8v2.7c0 .3.2.6.7.5A9.5 9.5 0 0012 2.5z"/></svg>Brizuu</a></div>
</main>
<script>
let mode='login'; const $=s=>document.querySelector(s);
function setMode(m){mode=m; const reg=m==='register';
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.t===m)); $('#tabs').classList.toggle('reg',reg);
  $('#l-name').hidden=!reg; $('#meter').hidden=!reg; $('#go-l').textContent=reg?'Załóż konto':'Zaloguj się';
  $('#ttl').textContent=reg?'Zaczynamy':'Witaj ponownie'; $('#sub').textContent=reg?'Konto zajmie chwilę i działa też w aplikacji na Windows i Androida.':'Zaloguj się, żeby otworzyć swoje notatki.';
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
