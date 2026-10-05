"""Buduje cloud/site/index.html — samodzielną stronę pobierania Asystenta (serwowaną przez serwer kont
pod https://zenfix.pl/asystent/). Wygląd, logo i sekcje są brane wprost ze strony startowej aplikacji
(index.html), więc po zmianach w aplikacji wystarczy uruchomić:  python cloud/build_site.py"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "index.html").read_text(encoding="utf-8")
SETUP = "https://github.com/Brizuu/asystent-studenta/releases/latest/download/AsystentSetup.exe"
WEB_BTN = ('<a class="dl" href="app/"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.8"><circle cx="12" cy="12" r="8.5"/>'
           '<path d="M3.5 12h17M12 3.5c2.5 2.6 3.7 5.4 3.7 8.5s-1.2 5.9-3.7 8.5c-2.5-2.6-3.7-5.4-3.7-8.5s1.2-5.9 3.7-8.5z"/></svg>'
           '<span><small>Otwórz w</small><b>przeglądarce</b></span></a>')


def between(start: str, end: str, s: str = SRC, inclusive_end: bool = False) -> str:
    i = s.index(start)
    j = s.index(end, i)
    return s[i:j + (len(end) if inclusive_end else 0)]


def func(name: str) -> str:
    """Wycina funkcję najwyższego poziomu `function name(...){...}` (albo `async function`)."""
    m = re.search(r"^(async )?function " + re.escape(name) + r"\(", SRC, re.M)
    i, depth, k = m.start(), 0, SRC.index("{", m.start())
    for k in range(k, len(SRC)):
        depth += {"{": 1, "}": -1}.get(SRC[k], 0)
        if depth == 0:
            return SRC[i:k + 1]
    raise ValueError(name)


style = between("<style>", "</style>", inclusive_end=True)
aura = between('<div class="aura" aria-hidden="true">', '\n<div class="sky"')
landing = between('    <section class="view" id="view-start">', '    <section class="view active" id="view-pulpit">')

# --- treść strony: bez wersji przeglądarkowej i telefonu (wymagają lokalnej aplikacji), bez paska uczelni ---
landing = landing.replace('class="view" id="view-start"', 'class="view active" id="view-start"')
landing = re.sub(r'<button class="lp-go" onclick="go\(\'pulpit\'\)">Otwórz w przeglądarce(<svg.*?</svg>)</button>',
                 lambda m: f'<a class="lp-go lp-win-main" href="{SETUP}" download>Pobierz na Windows{m.group(1)}</a>' + WEB_BTN, landing, flags=re.S)
landing = re.sub(r'\s*<a class="dl lp-win" href="[^"]*">.*?</a>', '', landing, flags=re.S)
landing = re.sub(r'\s*<button class="dl" onclick="lpPhone\(\)">.*?</button>', '', landing, flags=re.S)
landing = re.sub(r'<div class="lp-sec" style="max-width:none;padding-left:0;padding-right:0">.*?<div class="unis lp-reveal" id="lp-unis"></div>\s*</div>\s*',
                 '', landing, flags=re.S)
landing = landing.replace('<p class="lp-meta" id="lp-meta">Za darmo · Windows 10 i 11 · iPhone i Android z ekranu głównego</p>',
                          '<p class="lp-meta" id="lp-meta">Za darmo · Windows 10 i 11 · albo w przeglądarce, także na telefonie</p>')
landing = landing.replace("Otwórz albo pobierz</h3><p>Działa od razu w przeglądarce. Możesz też pobrać aplikację na Windows albo dodać ją na telefonie.",
                          "Pobierz i zainstaluj</h3><p>Instalator zajmuje kilka sekund. Aplikacja sama się aktualizuje, gdy wyjdzie nowa wersja.")
assert "lp-win-main" in landing and "lpPhone" not in landing and "lp-unis" not in landing, "zmienił się układ strony startowej"

js = "\n".join([
    "const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];",
    "const reduceMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;",
    "let _lgN=0;",
    func("logoSVG"), func("paintLogos"),
    """// przycisk: bezpośredni link do najnowszego instalatora z GitHuba + wersja i rozmiar
(async()=>{try{const r=await fetch('https://api.github.com/repos/Brizuu/asystent-studenta/releases/latest',{headers:{Accept:'application/vnd.github+json'}});
  if(!r.ok)return; const rel=await r.json(), a=(rel.assets||[]).find(x=>x.name==='AsystentSetup.exe'); if(!a)return;
  const ver=(rel.tag_name||'').replace(/^v/,''), mb=Math.round(a.size/1048576);
  $$('.lp-win-main').forEach(b=>{b.href=a.browser_download_url;b.firstChild.textContent='Pobierz na Windows · '+ver+' ';});
  $('#lp-meta').textContent=`Za darmo · wersja ${ver} · ${mb} MB · Windows 10 i 11 · albo w przeglądarce, także na telefonie`;
}catch(_){}})();
paintLogos();
// pojawianie się sekcji przy przewijaniu
(()=>{const els=$$('.lp-reveal'); if(!('IntersectionObserver' in window)||reduceMotion()){els.forEach(e=>e.classList.add('in'));return;}
  const io=new IntersectionObserver(es=>es.forEach(x=>{if(x.isIntersecting){x.target.classList.add('in');io.unobserve(x.target);}}),{threshold:.12});
  els.forEach(e=>{const sib=[...e.parentNode.children].filter(c=>c.classList.contains('lp-reveal'));e.style.transitionDelay=(sib.length>2?sib.indexOf(e)%3*80:0)+'ms';io.observe(e);});})();
// kafelki: lekka paralaksa za kursorem; chowają się po przewinięciu
addEventListener('pointermove',e=>{if(reduceMotion()||window._px)return;window._px=requestAnimationFrame(()=>{window._px=0;const a=$('.aura');
  a.style.setProperty('--px',(e.clientX/innerWidth*2-1).toFixed(3));a.style.setProperty('--py',(e.clientY/innerHeight*2-1).toFixed(3));});});
addEventListener('scroll',()=>document.body.classList.toggle('lp-down',scrollY>innerHeight*.45),{passive:true});
$$('.lp-more').forEach(b=>b.onclick=()=>$('#lp-feat').scrollIntoView({behavior:reduceMotion()?'auto':'smooth'}));""",
])

favicon = "data:image/svg+xml," + re.sub(r"\s+", " ", """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='15' fill='%237a86ff'/><path d='M32 15.5l24 10.2-24 10.3L8 25.7z' fill='white'/><path d='M19 31v8c0 3.7 5.8 6.5 13 6.5s13-2.8 13-6.5v-8l-13 5.6z' fill='white' fill-opacity='.82'/></svg>""")

page = f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asystent studenta — pobierz na Windows</title>
<meta name="description" content="Plan zajęć, zeszyty z notatkami, nagrania wykładów zamieniane przez AI w notatki, zadania i budżet — aplikacja dla studentów na Windows.">
<meta name="theme-color" content="#0b0b14">
<meta name="color-scheme" content="dark">
<link rel="icon" href="{favicon}">
<!-- wygenerowane przez cloud/build_site.py z index.html — nie edytuj ręcznie -->
{style}
<style>a.lp-go,a.dl{{text-decoration:none}} body.on-start .main{{padding:0}}</style>
</head>
<body class="on-start">
{aura}
<div class="app"><main class="main">
{landing}
</main></div>
<script>
{js}
</script>
</body>
</html>
"""
out = Path(__file__).parent / "site" / "index.html"
out.parent.mkdir(exist_ok=True)
out.write_text(page, encoding="utf-8")
print(f"zapisano {out} ({len(page) // 1024} KB)")
