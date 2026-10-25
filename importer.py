"""Import notatek z innych aplikacji: Obsidian / Notion / Joplin / Logseq / Bear (Markdown), Google Docs / Word /
OneNote (.docx), strony HTML (Google Docs „Strona internetowa”, Notion HTML), Evernote (.enex), Google Keep (Takeout .json)
i paczki .zip z tym wszystkim (także cały folder sejfu Obsidiana).

Przebieg: przeglądarka wysyła pliki partiami do /api/import/files (sesja w katalogu tymczasowym), potem /api/import/run
zamienia wszystko na bloki notatek (tekst, kod, wzory, checklisty, ramki, rozwijane, PDF-y), obrazy zapisuje w uploads/,
foldery zamienia na zeszyty albo grupy, a linki [[notatka]] między importowanymi notatkami — na linki w aplikacji."""
import base64
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse, parse_qs

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import get_conn, DATA_DIR

router = APIRouter()

NOTE_EXT = {".md", ".markdown", ".mdown", ".txt", ".html", ".htm", ".docx", ".enex", ".json", ".csv"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_SESSION = 600 * 1024 * 1024      # razem w jednej sesji importu
MAX_ENTRY = 150 * 1024 * 1024        # pojedynczy plik w .zip
COLORS = ["#5A96F2", "#4FD1B8", "#E3B25C", "#E5675F", "#8DB6FF", "#54d996", "#ff7a90", "#ffb454"]
CALLOUT_KIND = {**dict.fromkeys("tip hint success check done".split(), "tip"),
                **dict.fromkeys("warning caution attention failure fail missing danger error bug".split(), "warn"),
                "important": "important", "definition": "def", "def": "def"}
_uid = lambda: uuid.uuid4().hex[:7]


def _tmp_root():
    return DATA_DIR / "import_tmp"


def _safe_rel(p: str) -> str:
    """Ścieżka względna bez wychodzenia poza katalog (.., /, C:)."""
    parts = [x for x in re.split(r"[\\/]+", p or "") if x not in ("", ".", "..")]
    parts = [re.sub(r'[\x00-\x1f<>:"|?*]', "_", x)[:120] for x in parts]
    return "/".join(parts)


# ===================== wirtualny system plików (pliki z sesji + rozpakowane .zip) =====================
class VFS:
    def __init__(self):
        self.files = {}           # ścieżka → funkcja zwracająca bajty
        self.size = {}
        self._zips = []
        self._fobjs = []

    def add_dir(self, root):
        for dp, _, fns in os.walk(root):
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                if rel.lower().endswith(".zip"):
                    f = open(full, "rb")
                    self._fobjs.append(f)
                    self.add_zip(f, rel[:-4])
                else:
                    self.files[rel] = (lambda f=full: open(f, "rb").read())
                    self.size[rel] = os.path.getsize(full)

    def add_zip(self, fobj, prefix, depth=0):
        try:
            z = zipfile.ZipFile(fobj)
        except zipfile.BadZipFile:
            return
        self._zips.append(z)
        names = [i.filename for i in z.infolist() if not i.is_dir() and not _skip_path(i.filename)]
        tops = {n.split("/")[0] for n in names}
        if len(tops) == 1 and all("/" in n for n in names):
            prefix = posixpath.dirname(prefix)        # paczka.zip/Sejf/… → Sejf/…
        total = 0
        for info in z.infolist():
            if info.is_dir() or info.file_size > MAX_ENTRY:
                continue
            name = info.filename
            if not (info.flag_bits & 0x800):          # stare archiwa z Windows: nazwy w CP437 zamiast UTF-8
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            rel = _safe_rel(prefix + "/" + name)
            total += info.file_size
            if total > MAX_SESSION * 2:
                break
            if rel.lower().endswith(".zip") and depth < 2:   # Notion: paczka w paczce (Part-1.zip)
                self.add_zip(io.BytesIO(z.read(info)), rel[:-4], depth + 1)
                continue
            self.files[rel] = (lambda z=z, i=info: z.read(i))
            self.size[rel] = info.file_size

    def close(self):
        for z in self._zips + self._fobjs:
            try:
                z.close()
            except Exception:
                pass


def _skip_path(p: str) -> bool:
    parts = p.split("/")
    return any(x.startswith(".") or x in ("__MACOSX", "node_modules") for x in parts) \
        or parts[-1].lower() in ("thumbs.db", "desktop.ini")


NOTION_ID = re.compile(r"\s+[0-9a-f]{32}$", re.I)


def _clean_name(s: str) -> str:
    s = NOTION_ID.sub("", s)                  # Notion dopisuje identyfikator strony do nazwy
    return s.strip() or "Bez nazwy"


# ===================== HTML → drzewo =====================
class Node:
    __slots__ = ("tag", "attrs", "kids")

    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.kids = tag, attrs or {}, []

    def text(self):
        return "".join(k if isinstance(k, str) else k.text() for k in self.kids)

    def find(self, tag):
        for k in self.kids:
            if isinstance(k, Node):
                if k.tag == tag:
                    return k
                f = k.find(tag)
                if f:
                    return f
        return None


VOID = {"br", "img", "hr", "input", "meta", "link", "col", "wbr", "area", "base", "source", "en-media", "en-todo"}
SKIP = {"script", "style", "title", "noscript", "svg", "iframe", "object", "template", "head", "button", "form", "select", "textarea"}
BLOCKISH = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "table", "pre", "blockquote", "hr",
            "section", "article", "header", "footer", "main", "figure", "details", "en-note", "body", "html", "aside", "nav"}
CLOSE_AT = {"li": {"ul", "ol"}, "p": {"div", "li", "td", "th", "blockquote", "body", "section", "article"},
            "tr": {"table", "tbody", "thead"}, "td": {"tr"}, "th": {"tr"}, "dt": {"dl"}, "dd": {"dl"}}


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.skip = 0
        self.raw = None
        self.css, self.title = "", ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            if tag in ("style", "title"):
                self.raw = tag
            if tag != "head":
                self.skip += 1
            return
        if self.skip:
            return
        if tag in CLOSE_AT:
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t == tag:
                    del self.stack[i:]
                    break
                if t in CLOSE_AT[tag]:
                    break
        n = Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        if tag in SKIP or self.skip:
            return
        self.stack[-1].kids.append(Node(tag, {k: (v or "") for k, v in attrs}))

    def handle_endtag(self, tag):
        if tag in SKIP:
            if tag != "head":
                self.skip = max(0, self.skip - 1)
            self.raw = None
            return
        if self.skip or tag in VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.raw == "style":
            self.css += data
        elif self.raw == "title":
            self.title += data
        elif not self.skip and data:
            self.stack[-1].kids.append(data)


def parse_html(html: str):
    p = _Parser()
    p.feed(html or "")
    p.close()
    return p


def _css_classes(css: str) -> dict:
    """Google Docs zapisuje pogrubienia itp. w klasach (.c3{font-weight:700}) — przepisz na style."""
    out = {}
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css or ""):
        for s in sel.split(","):
            m = re.fullmatch(r"\s*\.([\w-]+)\s*", s)
            if m:
                out[m.group(1)] = out.get(m.group(1), "") + ";" + body
    return out


# ===================== drzewo → bloki notatki =====================
class Ctx:
    """Wspólny stan importu: zapisane obrazy (bez duplikatów), odwołania do plików i licznik ostrzeżeń."""
    def __init__(self, vfs, by_name):
        self.vfs, self.by_name = vfs, by_name
        self.saved = {}            # sha1 → url
        self.used = set()          # ścieżki plików użytych w notatkach (obrazy, PDF-y)
        self.images = 0
        self.missing = 0
        self.uploads = None

    def find(self, ref, base_dir):
        if not ref:
            return None
        ref = unquote(ref.split("#")[0].split("?")[0]).strip().strip("<>")
        if not ref:
            return None
        cands = [posixpath.normpath(posixpath.join(base_dir, ref)), posixpath.normpath(ref.lstrip("/"))]
        for c in cands:
            if c in self.vfs.files:
                return c
        hit = self.by_name.get(posixpath.basename(ref).lower())
        return hit[0] if hit else None

    def save_bytes(self, data: bytes, name: str):
        h = hashlib.sha1(data).hexdigest()
        if h in self.saved:
            return self.saved[h]
        if self.uploads is None:
            self.uploads = DATA_DIR / "uploads"
            self.uploads.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(name))[-60:] or "plik"
        fn = uuid.uuid4().hex[:10] + "_" + safe
        (self.uploads / fn).write_bytes(data)
        try:
            import sync
            sync.push_file_soon(fn)
        except Exception:
            pass
        url = "uploads/" + fn
        self.saved[h] = url
        if os.path.splitext(fn)[1].lower() in IMG_EXT:
            self.images += 1
        return url

    def asset(self, ref, base_dir, kinds=IMG_EXT):
        """Plik z notatki → url w uploads/ (albo None)."""
        if not ref:
            return None
        if ref.startswith("data:image/"):
            m = re.match(r"data:image/(png|jpe?g|gif|webp);base64,(.+)", ref, re.S)
            if not m:
                return None
            try:
                return self.save_bytes(base64.b64decode(m.group(2)), "obraz." + m.group(1).replace("jpeg", "jpg"))
            except Exception:
                return None
        if re.match(r"https?://", ref):
            return ref if kinds is IMG_EXT else None
        p = self.find(ref, base_dir)
        if not p or os.path.splitext(p)[1].lower() not in kinds:
            self.missing += 1
            return None
        self.used.add(p)
        return self.save_bytes(self.vfs.files[p](), p)


def _style_of(n: Node, cls: dict) -> str:
    st = n.attrs.get("style", "")
    for c in n.attrs.get("class", "").split():
        st += ";" + cls.get(c, "")
    return st.lower()


def _wraps(st: str) -> list:
    w = []
    m = re.findall(r"font-weight\s*:\s*(\w+)", st)
    if m and (m[-1] == "bold" or (m[-1].isdigit() and int(m[-1]) >= 600)):
        w.append("b")
    m = re.findall(r"font-style\s*:\s*(\w+)", st)
    if m and m[-1] == "italic":
        w.append("i")
    if re.search(r"text-decoration[^;]*underline", st):
        w.append("u")
    if re.search(r"text-decoration[^;]*line-through", st):
        w.append("s")
    m = re.findall(r"vertical-align\s*:\s*(\w+)", st)
    if m and m[-1] in ("super", "sub"):
        w.append("sup" if m[-1] == "super" else "sub")
    m = re.findall(r"background(?:-color)?\s*:\s*([^;]+)", st)
    if m:
        v = m[-1].strip()
        if v and not re.match(r"(transparent|none|inherit|initial|#fff(fff)?\b|white|rgba?\(\s*255\s*,\s*255\s*,\s*255)", v) and not v.startswith("url"):
            w.append("mark")
    return w


def _google_href(h: str) -> str:
    if re.match(r"https?://(www\.)?google\.[a-z.]+/url\?", h):
        q = parse_qs(urlparse(h).query).get("q")
        if q:
            return q[0]
    return h


def _is_task_li(li: Node):
    """None albo True/False (zaznaczone) dla punktu z polem wyboru."""
    for k in li.kids:
        if isinstance(k, str):
            if k.strip():
                if k.lstrip()[:1] in "☐☑☒✓✔":
                    return k.lstrip()[0] != "☐"
                return None
            continue
        if k.tag == "input" and k.attrs.get("type", "").lower() == "checkbox":
            return "checked" in k.attrs
        if k.tag == "en-todo":
            return k.attrs.get("checked", "").lower() == "true"
        if k.tag in ("p", "span", "div", "label"):
            return _is_task_li(k)
        if k.tag in ("ul", "ol"):
            return None
        return None
    return None


TEX_RE = re.compile(r"\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)|\$(?!\s)([^$\n]*?[^\s$\\])\$(?!\d)")


def tex_spans(html: str) -> str:
    """$…$ / $$…$$ w tekście → <span class="itex"> (jak aiMath w aplikacji: kwota „$5” zostaje tekstem)."""
    if "$" not in html and "\\(" not in html and "\\[" not in html:
        return html
    out, code = [], 0
    for part in re.split(r"(<[^>]+>)", html):
        if part.startswith("<"):
            t = re.match(r"</?(\w+)", part)
            if t and t.group(1).lower() in ("code", "pre"):
                code += -1 if part.startswith("</") else 1
            out.append(part)
            continue
        if code > 0:
            out.append(part)
            continue
        def rep(m):
            tex = unescape(next(g for g in m.groups() if g is not None)).strip()
            disp = m.group(1) is not None or m.group(2) is not None
            if not tex or (not disp and not re.search(r"[\\_^{}=+\-*/<>|]|^[a-zA-Z]$|^-?[\d.,]+$|^[a-zA-Z]?\d*[a-zA-Z]?$", tex)):
                return m.group(0)
            return f'<span class="itex{" disp" if disp else ""}" contenteditable="false" data-tex="{escape(tex)}"></span>'
        out.append(TEX_RE.sub(rep, part))
    return "".join(out)


class Converter:
    """Drzewo HTML jednej notatki → lista bloków aplikacji."""
    def __init__(self, ctx: Ctx, base_dir: str, css: str = "", media: dict | None = None):
        self.ctx, self.base, self.cls, self.media = ctx, base_dir, _css_classes(css), media or {}
        self.blocks, self.buf, self.later = [], [], []

    # ---- tekst w linii ----
    def render(self, n, pre=False) -> str:
        if isinstance(n, str):
            return escape(n if pre else re.sub(r"\s+", " ", n), quote=False)
        t, a = n.tag, n.attrs
        inner = lambda: "".join(self.render(k, pre or t == "pre") for k in n.kids)
        if t == "br":
            return "<br>"
        if t == "img":
            w = a.get("data-width") or a.get("width", "")
            url = self.ctx.asset(a.get("src", ""), self.base)
            if not url:
                return ""
            st = f' style="width:{int(w)}px;max-width:100%"' if w.isdigit() and 16 <= int(w) <= 2000 else ""
            return f'<img class="noteimg" src="{escape(url)}" alt="{escape(a.get("alt", "")[:120])}"{st}>'
        if t == "en-media":
            m = self.media.get(a.get("hash", ""))
            if not m:
                return ""
            if m["kind"] == "img":
                return f'<img class="noteimg" src="{escape(m["url"])}" alt="">'
            self.later.append({"id": _uid(), "type": "embed", "url": m["url"]})
            return ""
        if t == "span" and "data-pdf" in a:
            return f'<span data-pdf="{escape(a["data-pdf"])}"></span>'
        if t == "input" and a.get("type", "").lower() == "checkbox":
            return "☑ " if "checked" in a else "☐ "
        if t == "en-todo":
            return "☑ " if a.get("checked", "").lower() == "true" else "☐ "
        if t == "a":
            body = inner()
            if "data-wiki" in a:
                return f'<a data-wiki="{escape(a["data-wiki"])}">{body}</a>'
            h = _google_href(a.get("href", "").strip())
            if re.match(r"(https?:|mailto:)", h, re.I):
                return f'<a href="{escape(h)}">{body}</a>'
            if h and not h.startswith(("#", "javascript:")):
                low = unquote(h.split("#")[0]).lower()
                if low.endswith((".md", ".html", ".htm", ".markdown", ".txt", ".docx")):
                    tgt = posixpath.normpath(posixpath.join(self.base, unquote(h.split("#")[0])))
                    return f'<a data-wiki="{escape(tgt)}">{body}</a>'
                url = self.ctx.asset(h, self.base, IMG_EXT | {".pdf"})   # link do załącznika z sejfu / eksportu
                if url:
                    return f'<a href="{escape(url)}">{body}</a>'
            return body
        if t == "pre":
            return "<pre><code>" + escape(n.text(), quote=False) + "</code></pre>"
        if t == "div" and "math-disp" in a.get("class", ""):
            return "<p>$$" + escape(n.text(), quote=False) + "$$</p>"
        if t == "div" and "data-embed" in a:
            self.later.append({"id": _uid(), "type": "embed", "url": a["data-embed"]})
            return ""
        body = inner()
        wraps = _wraps(_style_of(n, self.cls)) if ("style" in a or "class" in a) else []
        tagmap = {"strong": "b", "b": "b", "em": "i", "i": "i", "u": "u", "ins": "u", "s": "s", "strike": "s", "del": "s",
                  "mark": "mark", "sub": "sub", "sup": "sup", "code": "code", "kbd": "code", "tt": "code",
                  "h1": "h1", "h2": "h2", "h3": "h3", "h4": "h4", "h5": "h4", "h6": "h4", "p": "p", "div": "div",
                  "ul": "ul", "ol": "ol", "li": "li", "blockquote": "blockquote",
                  "table": "table", "thead": "thead", "tbody": "tbody", "tr": "tr", "td": "td", "th": "th"}
        if t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            wraps = [w for w in wraps if w != "b"]
        for w in wraps:
            body = f"<{w}>{body}</{w}>"
        if t in ("td", "th"):
            span = "".join(f' {k}="{int(a[k])}"' for k in ("colspan", "rowspan") if a.get(k, "").isdigit() and int(a[k]) > 1)
            return f"<{t}{span}>{body}</{t}>"
        if t == "summary" or t == "dt":
            return f"<p><b>{body}</b></p>"
        if t in ("caption", "figcaption", "dd"):
            return f"<p>{body}</p>"
        if t == "details":
            return body
        tg = tagmap.get(t)
        if not tg:
            return body
        if tg in ("p", "div") and not re.sub(r"<br>|\s|&nbsp;| ", "", body):
            return "<p><br></p>"
        if tg in ("ul", "ol", "table", "thead", "tbody", "tr") and not body.strip():
            return ""
        return f"<{tg}>{body}</{tg}>"

    # ---- bloki ----
    def flush(self):
        html = "".join(self.buf).strip()
        self.buf = []
        html = re.sub(r"(?:<p><br></p>\s*){2,}", "<p><br></p>", html)
        html = re.sub(r"^(?:<p><br></p>)+|(?:<p><br></p>)+$", "", html)
        if re.sub(r"<[^>]+>|\s|&nbsp;", "", html) or "<img" in html:
            if self.blocks and self.blocks[-1]["type"] == "text" and not self.later:
                self.blocks[-1]["html"] += html
            else:
                self.blocks.append({"id": _uid(), "type": "text", "html": html})
        self.blocks.extend(self.later)
        self.later = []

    def add(self, b):
        self.flush()
        self.blocks.append(b)

    def visit(self, nodes):
        for n in nodes:
            if isinstance(n, str):
                if n.strip():
                    self.buf.append(self.render(n))
                continue
            t, a = n.tag, n.attrs
            if t in ("root", "html", "body", "en-note", "article", "section", "main", "header", "footer", "aside", "figure", "center") \
                    or (t in ("div", "span") and "math-disp" not in a.get("class", "") and "data-embed" not in a
                        and any(isinstance(k, Node) and k.tag in BLOCKISH for k in n.kids)):
                self.visit(n.kids)
                continue
            if t == "pre":
                code = n.find("code")
                lang = ""
                for src in (code.attrs.get("class", "") if code else "", a.get("class", "")):
                    m = re.search(r"(?:language|lang)-([\w+#-]+)", src)
                    if m:
                        lang = m.group(1).lower()
                        break
                self.add({"id": _uid(), "type": "code", "lang": lang or "plaintext", "code": n.text().rstrip("\n")})
            elif t == "hr":
                self.add({"id": _uid(), "type": "divider"})
            elif t in ("ul", "ol") and self._checklist(n):
                items = [k for k in n.kids if isinstance(k, Node) and k.tag == "li"]
                self.add({"id": _uid(), "type": "checklist", "items": [
                    {"text": re.sub(r"^[☐☑☒✓✔]\s*", "", re.sub(r"\s+", " ", li.text()).strip())[:500], "done": bool(_is_task_li(li))}
                    for li in items]})
            elif t == "blockquote" and "data-callout" in a:
                kind = CALLOUT_KIND.get(a["data-callout"].lower(), "info")
                title = a.get("data-title", "").strip()
                html = "".join(self.render(k) for k in n.kids)
                if title:
                    html = f"<p><b>{escape(title, quote=False)}</b></p>" + html
                self.add({"id": _uid(), "type": "callout", "kind": kind, "html": tex_spans(html)})
            elif t == "details":
                summ = next((k for k in n.kids if isinstance(k, Node) and k.tag == "summary"), None)
                rest = [k for k in n.kids if k is not summ]
                self.add({"id": _uid(), "type": "toggle", "open": False,
                          "summary": tex_spans("".join(self.render(k) for k in summ.kids)) if summ else "Więcej",
                          "html": tex_spans("".join(self.render(k) for k in rest))})
            elif t == "div" and "math-disp" in a.get("class", ""):
                self.add({"id": _uid(), "type": "latex", "tex": n.text().strip()})
            elif t == "div" and "data-embed" in a:
                self.add({"id": _uid(), "type": "embed", "url": a["data-embed"]})
            else:
                h = self.render(n)
                if t in ("span", "a", "b", "strong", "i", "em", "u", "code", "mark", "img", "s", "sub", "sup") and h:
                    h = f"<p>{h}</p>"
                self.buf.append(h)
                if self.later:
                    self.flush()

    @staticmethod
    def _checklist(n: Node) -> bool:
        lis = [k for k in n.kids if isinstance(k, Node)]
        if not lis or any(k.tag != "li" for k in lis):
            return False
        for li in lis:
            if _is_task_li(li) is None or li.find("ul") or li.find("ol") or li.find("img"):
                return False
        return True

    def run(self, root) -> list:
        self.visit(root.kids if isinstance(root, Node) else root)
        self.flush()
        return self.blocks


# ===================== Markdown → HTML (Obsidian, Notion, Joplin, Logseq, Bear, Typora…) =====================
LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])(\s+|$)(.*)$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
HTML_BLOCK = re.compile(r"^\s*<(div|table|details|p|ul|ol|blockquote|pre|h[1-6]|figure|center|section|img)\b", re.I)
_P = "\x01%d\x02"


def _width(ws: str) -> int:
    return len(ws.replace("\t", "    "))


class Markdown:
    def __init__(self):
        self.store = []

    def keep(self, html):
        self.store.append(html)
        return _P % (len(self.store) - 1)

    def restore(self, s):
        for _ in range(3):
            if "\x01" not in s:
                break
            s = re.sub(r"\x01(\d+)\x02", lambda m: self.store[int(m.group(1))], s)
        return s

    # ---- w linii ----
    def inline(self, s: str) -> str:
        k = self.keep
        s = re.sub(r"(`+)(.+?)\1", lambda m: k("<code>" + escape(m.group(2).strip(), quote=False) + "</code>"), s)
        s = re.sub(r"\\\$", lambda m: k("\\$"), s)
        s = re.sub(r"\$\$(.+?)\$\$", lambda m: k("$$" + escape(m.group(1), quote=False) + "$$"), s)
        s = re.sub(r"(?<![\\$\w])\$(?=\S)([^$\n]+?)(?<=\S)\$(?![\w$])", lambda m: k("$" + escape(m.group(1), quote=False) + "$"), s)
        s = re.sub(r"\\\((.+?)\\\)|\\\[(.+?)\\\]", lambda m: k(escape(m.group(0), quote=False)), s)
        # znaczniki HTML zostają, reszta tekstu jest escapowana
        s = re.sub(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?/?>|<!--.*?-->", lambda m: k(m.group(0)), s)
        s = re.sub(r"&(?!#?\w+;)", "&amp;", s).replace("<", "&lt;").replace(">", "&gt;")
        # osadzenia Obsidiana ![[plik.png|300]] / ![[notatka]]
        def embed(m):
            target, _, opt = m.group(1).partition("|")
            ext = os.path.splitext(target.split("#")[0])[1].lower()
            if ext in IMG_EXT:
                w = opt.split("x")[0].strip() if opt else ""
                return k(f'<img src="{escape(target)}" data-width="{escape(w)}" alt="">')
            if ext == ".pdf":
                return k(f'<span data-pdf="{escape(target)}"></span>')
            label = opt or posixpath.basename(target.split("#")[0]) or target
            return k(f'<a data-wiki="{escape(target.split("#")[0])}">{escape(label)}</a>')
        s = re.sub(r"!\[\[([^\]\n]+)\]\]", embed, s)
        def image(m):
            alt, src = m.group(1), (m.group(2) or m.group(3) or "").strip()
            alt, _, w = alt.partition("|")
            if src.lower().split("?")[0].endswith(".pdf"):
                return k(f'<span data-pdf="{escape(src)}"></span>')
            return k(f'<img src="{escape(unescape(src))}" alt="{escape(alt)}" data-width="{escape(w.strip())}">')
        s = re.sub(r'!\[([^\]\n]*)\]\(\s*(?:&lt;(.+?)&gt;|([^)\s]+))(?:\s+&quot;[^)]*&quot;|\s+"[^)]*")?\s*\)', image, s)
        def wiki(m):
            target, _, alias = m.group(1).partition("|")
            target = target.replace("\\", "")
            label = alias or target.split("#")[0] or target
            return k(f'<a data-wiki="{escape(target.split("#")[0])}">{label}</a>')
        s = re.sub(r"\[\[([^\]\n]+)\]\]", wiki, s)
        def link(m):
            text, href = m.group(1), unescape((m.group(2) or m.group(3) or "").strip())
            return k(f'<a href="{escape(href)}">') + text + k("</a>")
        s = re.sub(r'\[([^\]\n]+)\]\(\s*(?:&lt;(.+?)&gt;|([^)\s]+))(?:\s+&quot;[^)]*&quot;|\s+"[^)]*")?\s*\)', link, s)
        s = re.sub(r"&lt;(https?://[^\s&]+)&gt;", lambda m: k(f'<a href="{m.group(1)}">{m.group(1)}</a>'), s)
        s = re.sub(r"(?<![\"'=\w/])(https?://[^\s<\x01]+[^\s<\x01.,;:!?)\]])", lambda m: k(f'<a href="{m.group(1)}">{m.group(1)}</a>'), s)
        s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", s)
        s = re.sub(r"(?<![*\w])\*(?=\S)(.+?)(?<=\S)\*(?![*\w])", r"<i>\1</i>", s)
        s = re.sub(r"(?<![_\w])_(?=\S)(.+?)(?<=\S)_(?![_\w])", r"<i>\1</i>", s)
        s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)
        s = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r"<mark>\1</mark>", s)
        s = re.sub(r"\s\^[\w-]{4,}$", "", s)          # identyfikatory bloków Obsidiana ^abc123
        return self.restore(s)

    # ---- bloki ----
    def convert(self, text: str) -> str:
        text = re.sub(r"%%.*?%%", "", text, flags=re.S)                 # komentarze Obsidiana
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return self.blocks(lines)

    def is_start(self, line, nxt=""):
        return bool(re.match(r"^\s{0,3}(#{1,6}\s|>|```|~~~|\$\$)", line) or HR_RE.match(line) or LIST_RE.match(line)
                    or ("|" in line and "|" in (nxt or "") and TABLE_SEP.match(nxt or "")))

    def blocks(self, lines) -> str:
        out, para = [], []
        i, n = 0, len(lines)

        def flush():
            if para:
                out.append("<p>" + "<br>".join(self.inline(x.strip()) for x in para) + "</p>")
                para.clear()

        while i < n:
            line = lines[i]
            nxt = lines[i + 1] if i + 1 < n else ""
            st = line.strip()
            if not st:
                flush()
                i += 1
                continue
            m = FENCE_RE.match(line)
            if m:
                flush()
                fence, lang, code = m.group(2), m.group(3), []
                i += 1
                while i < n and not lines[i].strip().startswith(fence):
                    code.append(lines[i][_width(m.group(1)):] if lines[i][:len(m.group(1))].isspace() else lines[i])
                    i += 1
                i += 1
                body = escape("\n".join(code), quote=False)
                if lang.lower() in ("math", "latex", "tex"):
                    out.append(f'<div class="math-disp">{body}</div>')
                else:
                    out.append(f'<pre><code class="language-{escape(lang or "plaintext")}">{body}</code></pre>')
                continue
            if st.startswith("$$") and not (st.count("$$") >= 2 and not (st.endswith("$$") and st.count("$$") == 2)):
                flush()
                if len(st) > 4 and st.endswith("$$"):
                    out.append(f'<div class="math-disp">{escape(st[2:-2], quote=False)}</div>')
                    i += 1
                    continue
                tex = [st[2:]]
                i += 1
                while i < n and "$$" not in lines[i]:
                    tex.append(lines[i])
                    i += 1
                if i < n:
                    tex.append(lines[i].split("$$")[0])
                i += 1
                out.append(f'<div class="math-disp">{escape(chr(10).join(tex).strip(), quote=False)}</div>')
                continue
            m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if m:
                flush()
                lv = len(m.group(1))
                out.append(f"<h{min(lv, 4)}>{self.inline(m.group(2))}</h{min(lv, 4)}>")
                i += 1
                continue
            if para and re.match(r"^\s{0,3}(=+|-+)\s*$", line) and len(para) == 1 and "![" not in para[0] and len(para[0]) < 120:       # nagłówek podkreślony === / ---
                lv = 1 if "=" in line else 2
                txt = " ".join(x.strip() for x in para)
                para.clear()
                out.append(f"<h{lv}>{self.inline(txt)}</h{lv}>")
                i += 1
                continue
            if HR_RE.match(line):
                flush()
                out.append("<hr>")
                i += 1
                continue
            if "|" in line and "|" in nxt and TABLE_SEP.match(nxt) and not LIST_RE.match(line):
                flush()
                rows = [line]
                i += 2
                while i < n and "|" in lines[i] and lines[i].strip():
                    rows.append(lines[i])
                    i += 1
                out.append(self.table(rows))
                continue
            if re.match(r"^\s{0,3}>", line):
                flush()
                q = []
                while i < n and re.match(r"^\s{0,3}>", lines[i]):
                    q.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                    i += 1
                out.append(self.quote(q))
                continue
            if LIST_RE.match(line) and not (para and not line[:1].isspace() and re.match(r"^\s*\d", line) and not line.lstrip().startswith("1")):
                flush()
                html, i = self.list(lines, i)
                out.append(html)
                continue
            if HTML_BLOCK.match(line) and not para:
                raw = []
                while i < n and lines[i].strip():
                    raw.append(lines[i])
                    i += 1
                out.append("\n".join(raw))
                continue
            para.append(re.sub(r"(\\|\s{2,})$", "", line))
            i += 1
        flush()
        return "".join(out)

    def table(self, rows) -> str:
        def cells(r):
            r = r.strip()
            if r.startswith("|"):
                r = r[1:]
            if r.endswith("|") and not r.endswith("\\|"):
                r = r[:-1]
            return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", r)]
        head = cells(rows[0])
        h = "<table><thead><tr>" + "".join(f"<th>{self.inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
        for r in rows[1:]:
            cs = cells(r)
            cs += [""] * (len(head) - len(cs))
            h += "<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in cs[:max(len(head), 1)]) + "</tr>"
        return h + "</tbody></table>"

    def quote(self, q) -> str:
        m = re.match(r"^\s*\[!([\w-]+)\]([+-]?)\s*(.*)$", q[0]) if q else None
        if m:
            body = self.blocks(q[1:])
            kind, title = m.group(1).lower(), m.group(3).strip()
            if m.group(2) and title:                       # zwijana ramka Obsidiana → blok rozwijany
                return f"<details><summary>{self.inline(title)}</summary>{body}</details>"
            return f'<blockquote data-callout="{escape(kind)}" data-title="{escape(title)}">{body}</blockquote>'
        return "<blockquote>" + self.blocks(q) + "</blockquote>"

    def list(self, lines, i):
        """Lista (także zagnieżdżona i z zadaniami [ ] / [x]) → HTML; zwraca (html, następny wiersz)."""
        root = {"items": [], "tag": None, "indent": -1}
        stack = []
        n = len(lines)
        while i < n:
            line = lines[i]
            m = LIST_RE.match(line)
            if m:
                ind = _width(m.group(1))
                tag = "ol" if m.group(2)[0].isdigit() else "ul"
                while stack and ind < stack[-1]["indent"] - 1:
                    stack.pop()
                if not stack:
                    lst = {"items": [], "tag": tag, "indent": ind}
                    root["items"].append(lst)
                    stack.append(lst)
                elif ind > stack[-1]["indent"] + 1 and stack[-1]["items"]:
                    lst = {"items": [], "tag": tag, "indent": ind}
                    stack[-1]["items"][-1]["kids"].append(lst)
                    stack.append(lst)
                text = m.group(4)
                tm = re.match(r"^\[([ xX/-])\]\s*(.*)$", text)
                task = None
                if tm:
                    task, text = tm.group(1) in "xX", tm.group(2)
                top = stack[-1]
                if top["items"] and (top["tag"] != tag or (top["items"][-1]["task"] is None) != (task is None)):
                    lst = {"items": [], "tag": tag, "indent": top["indent"]}
                    parent = stack[-2]["items"][-1]["kids"] if len(stack) > 1 else root["items"]
                    parent.append(lst)
                    stack[-1] = lst
                stack[-1]["items"].append({"text": [text], "task": task, "kids": []})
                i += 1
                continue
            if not line.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and LIST_RE.match(lines[j]):
                    i = j
                    continue
                if j < n and stack and _width(re.match(r"^\s*", lines[j]).group(0)) >= 2:
                    stack[-1]["items"][-1]["text"].append("")
                    i = j
                    continue
                break
            lead = _width(re.match(r"^\s*", line).group(0))
            if lead >= 2 or not self.is_start(line, lines[i + 1] if i + 1 < n else ""):
                if stack and stack[-1]["items"]:
                    stack[-1]["items"][-1]["text"].append(line.strip())
                    i += 1
                    continue
            break

        def render(lst):
            h = f"<{lst['tag']}>"
            for it in lst["items"]:
                txt = "<br>".join(self.inline(t) for t in it["text"] if t is not None)
                box = "" if it["task"] is None else ('<input type="checkbox" checked>' if it["task"] else '<input type="checkbox">')
                h += "<li>" + box + txt + "".join(render(k) for k in it["kids"]) + "</li>"
            return h + f"</{lst['tag']}>"
        return "".join(render(x) for x in root["items"]), i


def _front_matter(text: str):
    meta = {}
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if m:
        for ln in m.group(1).split("\n"):
            mm = re.match(r"^([\w-]+)\s*:\s*(.*)$", ln)
            if mm:
                meta[mm.group(1).lower()] = mm.group(2).strip().strip("\"'")
        text = text[m.end():]
    return meta, text.lstrip("﻿")


def _md_note(raw: str, name: str):
    meta, text = _front_matter(raw)
    title = meta.get("title", "")
    lines = text.split("\n")
    k = 0
    while k < len(lines) and not lines[k].strip():
        k += 1
    if k < len(lines):
        m = re.match(r"^#\s+(.+?)\s*#*\s*$", lines[k])
        if m and (not title or m.group(1).strip() == title):
            title = title or m.group(1).strip()
            lines = lines[k + 1:]
    # Logseq: każdy akapit to punkt „- ” — zostaje listą (czytelne), właściwości „klucz:: wartość” usuwamy
    body = "\n".join(ln for ln in lines if not re.match(r"^\s*[\w-]+::\s", ln))
    body = re.sub(r"\[\[(\d{4}-\d{2}-\d{2}|\w{3} \d{1,2}(st|nd|rd|th), \d{4})\]\]", r"\1", body)
    return title or name, Markdown().convert(body), meta


# ===================== Word / Google Docs (.docx) =====================
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
VML = "{urn:schemas-microsoft-com:vml}"
TEXSYM = {"α": "\\alpha", "β": "\\beta", "γ": "\\gamma", "δ": "\\delta", "ε": "\\varepsilon", "ϵ": "\\epsilon", "ζ": "\\zeta",
          "η": "\\eta", "θ": "\\theta", "ϑ": "\\vartheta", "ι": "\\iota", "κ": "\\kappa", "λ": "\\lambda", "μ": "\\mu", "ν": "\\nu",
          "ξ": "\\xi", "π": "\\pi", "ρ": "\\rho", "σ": "\\sigma", "τ": "\\tau", "υ": "\\upsilon", "φ": "\\varphi", "ϕ": "\\phi",
          "χ": "\\chi", "ψ": "\\psi", "ω": "\\omega", "Γ": "\\Gamma", "Δ": "\\Delta", "Θ": "\\Theta", "Λ": "\\Lambda", "Ξ": "\\Xi",
          "Π": "\\Pi", "Σ": "\\Sigma", "Φ": "\\Phi", "Ψ": "\\Psi", "Ω": "\\Omega", "∞": "\\infty", "≤": "\\le", "≥": "\\ge",
          "≠": "\\ne", "±": "\\pm", "∓": "\\mp", "×": "\\times", "·": "\\cdot", "⋅": "\\cdot", "÷": "\\div", "→": "\\to",
          "←": "\\leftarrow", "⇒": "\\Rightarrow", "⇔": "\\Leftrightarrow", "↔": "\\leftrightarrow", "∈": "\\in", "∉": "\\notin",
          "⊂": "\\subset", "⊆": "\\subseteq", "∪": "\\cup", "∩": "\\cap", "∅": "\\emptyset", "∀": "\\forall", "∃": "\\exists",
          "∂": "\\partial", "∇": "\\nabla", "≈": "\\approx", "≡": "\\equiv", "∼": "\\sim", "∝": "\\propto", "°": "^\\circ",
          "′": "'", "…": "\\ldots", "⋯": "\\cdots", "ℝ": "\\mathbb{R}", "ℕ": "\\mathbb{N}", "ℤ": "\\mathbb{Z}", "ℚ": "\\mathbb{Q}",
          "ℂ": "\\mathbb{C}", "¬": "\\neg", "∧": "\\land", "∨": "\\lor", "√": "\\surd", "−": "-", "∗": "*"}
NARY = {"∑": "\\sum", "∏": "\\prod", "∫": "\\int", "∬": "\\iint", "∭": "\\iiint", "∮": "\\oint", "⋃": "\\bigcup", "⋂": "\\bigcap"}
ACC = {"̂": "\\hat", "̃": "\\tilde", "̇": "\\dot", "̈": "\\ddot", "⃗": "\\vec", "̄": "\\bar", "̅": "\\bar", "ˆ": "\\hat"}
FUNCS = {"sin", "cos", "tan", "tg", "ctg", "cot", "log", "ln", "exp", "lim", "max", "min", "sup", "inf", "det", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh"}


def _tex_text(s: str) -> str:
    out = []
    for ch in s:
        if ch in TEXSYM:
            out.append(TEXSYM[ch] + " ")
        elif ch in "{}%#&$_":
            out.append("\\" + ch)
        elif ch == "\\":
            out.append("\\backslash ")
        else:
            out.append(ch)
    return "".join(out)


def omml(el) -> str:
    """Równanie z Worda / Google Docs (OMML) → LaTeX."""
    def ch(e, name):
        return e.find(M + name)

    def sub(e, name):
        x = ch(e, name)
        return omml(x) if x is not None else ""

    def val(e, path, default):
        x = e.find(path)
        return x.get(M + "val", default) if x is not None else default

    t = el.tag
    if t == M + "r":
        txt = "".join(x.text or "" for x in el.iter(M + "t"))
        return "\\" + txt + " " if txt in FUNCS else _tex_text(txt)
    if t == M + "f":
        return "\\frac{" + sub(el, "num") + "}{" + sub(el, "den") + "}"
    if t == M + "sSup":
        return "{" + sub(el, "e") + "}^{" + sub(el, "sup") + "}"
    if t == M + "sSub":
        return "{" + sub(el, "e") + "}_{" + sub(el, "sub") + "}"
    if t == M + "sSubSup":
        return "{" + sub(el, "e") + "}_{" + sub(el, "sub") + "}^{" + sub(el, "sup") + "}"
    if t == M + "sPre":
        return "{}_{" + sub(el, "sub") + "}^{" + sub(el, "sup") + "}{" + sub(el, "e") + "}"
    if t == M + "rad":
        deg = sub(el, "deg").strip()
        return ("\\sqrt[" + deg + "]{" if deg else "\\sqrt{") + sub(el, "e") + "}"
    if t == M + "d":
        beg = val(el, f"{M}dPr/{M}begChr", "(")
        end = val(el, f"{M}dPr/{M}endChr", ")")
        sep = val(el, f"{M}dPr/{M}sepChr", "|")
        fix = lambda c: "." if not c else {"{": "\\{", "}": "\\}", "⟨": "\\langle", "⟩": "\\rangle", "‖": "\\|", "⌊": "\\lfloor", "⌋": "\\rfloor", "⌈": "\\lceil", "⌉": "\\rceil"}.get(c, c)
        parts = [omml(x) for x in el.findall(M + "e")]
        return "\\left" + fix(beg) + (" " + sep + " ").join(parts) + "\\right" + fix(end)
    if t == M + "nary":
        op = NARY.get(val(el, f"{M}naryPr/{M}chr", "∫"), "\\int")
        lo, hi = sub(el, "sub").strip(), sub(el, "sup").strip()
        return op + ("_{" + lo + "}" if lo else "") + ("^{" + hi + "}" if hi else "") + " " + sub(el, "e")
    if t == M + "func":
        name = sub(el, "fName").strip()
        return (name if name.startswith("\\") else "\\operatorname{" + name + "}") + "{" + sub(el, "e") + "}"
    if t == M + "acc":
        return ACC.get(val(el, f"{M}accPr/{M}chr", "̂"), "\\hat") + "{" + sub(el, "e") + "}"
    if t == M + "bar":
        return ("\\underline{" if val(el, f"{M}barPr/{M}pos", "top") == "bot" else "\\overline{") + sub(el, "e") + "}"
    if t in (M + "limLow", M + "limUpp"):
        base, lim = sub(el, "e").strip(), sub(el, "lim")
        if t == M + "limLow":
            return base + "_{" + lim + "}" if base.startswith("\\") else "\\underset{" + lim + "}{" + base + "}"
        return "\\overset{" + lim + "}{" + base + "}"
    if t == M + "groupChr":
        pos = val(el, f"{M}groupChrPr/{M}pos", "bot")
        return ("\\underbrace{" if pos == "bot" else "\\overbrace{") + sub(el, "e") + "}"
    if t == M + "borderBox":
        return "\\boxed{" + sub(el, "e") + "}"
    if t == M + "m":
        rows = [" & ".join(omml(e) for e in mr.findall(M + "e")) for mr in el.findall(M + "mr")]
        return "\\begin{matrix}" + " \\\\ ".join(rows) + "\\end{matrix}"
    if t == M + "eqArr":
        return "\\begin{aligned}" + " \\\\ ".join(omml(e).replace("&", "&") for e in el.findall(M + "e")) + "\\end{aligned}"
    if t.endswith("Pr"):
        return ""
    if t == W + "r":
        return _tex_text("".join(x.text or "" for x in el.iter(W + "t")))
    return "".join(omml(x) for x in el)


class Docx:
    def __init__(self, data: bytes, ctx: Ctx, name: str):
        self.z = zipfile.ZipFile(io.BytesIO(data))
        self.ctx, self.name = ctx, name
        self.rels = {}
        try:
            for r in ET.fromstring(self.z.read("word/_rels/document.xml.rels")):
                self.rels[r.get("Id")] = (r.get("Target", ""), r.get("TargetMode", ""))
        except KeyError:
            pass
        self.styles, self.style_num = {}, {}
        try:
            for s in ET.fromstring(self.z.read("word/styles.xml")).iter(W + "style"):
                nm = s.find(W + "name")
                sid = s.get(W + "styleId")
                self.styles[sid] = (nm.get(W + "val") if nm is not None else "").lower()
                ni, il = s.find(f"{W}pPr/{W}numPr/{W}numId"), s.find(f"{W}pPr/{W}numPr/{W}ilvl")
                if ni is not None and ni.get(W + "val") != "0":
                    self.style_num[sid] = (ni.get(W + "val"), il.get(W + "val") if il is not None else "0")
        except KeyError:
            pass
        self.numfmt = {}
        try:
            root = ET.fromstring(self.z.read("word/numbering.xml"))
            absn = {}
            for an in root.iter(W + "abstractNum"):
                lv = {}
                for l in an.iter(W + "lvl"):
                    f, tx = l.find(W + "numFmt"), l.find(W + "lvlText")
                    lv[l.get(W + "ilvl")] = ((f.get(W + "val") if f is not None else "bullet"), (tx.get(W + "val") if tx is not None else ""))
                absn[an.get(W + "abstractNumId")] = lv
            for nm in root.iter(W + "num"):
                a = nm.find(W + "abstractNumId")
                if a is not None:
                    self.numfmt[nm.get(W + "numId")] = absn.get(a.get(W + "val"), {})
        except KeyError:
            pass
        self.title = ""

    def image(self, rid):
        tgt = self.rels.get(rid, ("", ""))[0]
        if not tgt:
            return ""
        path = posixpath.normpath(posixpath.join("word", tgt)) if not tgt.startswith("/") else tgt.lstrip("/")
        try:
            data = self.z.read(path)
        except KeyError:
            return ""
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMG_EXT:
            return ""
        return f'<img class="noteimg" src="{escape(self.ctx.save_bytes(data, posixpath.basename(path)))}" alt="">'

    def runs(self, el) -> str:
        out = []
        for x in el:
            t = x.tag
            if t == W + "r":
                out.append(self.run(x))
            elif t == W + "hyperlink":
                inner = self.runs(x)
                tgt, mode = self.rels.get(x.get(R + "id"), ("", ""))
                out.append(f'<a href="{escape(tgt)}">{inner}</a>' if re.match(r"(https?:|mailto:)", tgt) else inner)
            elif t in (W + "smartTag", W + "ins", W + "fldSimple", W + "sdt", W + "sdtContent", W + "customXml"):
                out.append(self.runs(x))
            elif t == M + "oMath":
                out.append("$" + escape(omml(x).strip(), quote=False) + "$")
            elif t == M + "oMathPara":
                out.append("".join("$$" + escape(omml(m).strip(), quote=False) + "$$" for m in x.iter(M + "oMath")))
        return "".join(out)

    def run(self, r) -> str:
        pr = r.find(W + "rPr")
        txt = []
        for x in r:
            t = x.tag
            if t == W + "t":
                txt.append(escape(x.text or "", quote=False))
            elif t == W + "tab":
                txt.append(" ")
            elif t in (W + "br", W + "cr"):
                txt.append("<br>")
            elif t == W + "drawing":
                for b in x.iter(A + "blip"):
                    txt.append(self.image(b.get(R + "embed")))
            elif t == W + "pict":
                for b in x.iter(VML + "imagedata"):
                    txt.append(self.image(b.get(R + "id")))
            elif t == W + "sym":
                txt.append("•")
        s = "".join(txt)
        if not s or pr is None:
            return s
        on = lambda tag: (lambda e: e is not None and e.get(W + "val", "true") not in ("0", "false", "none"))(pr.find(W + tag))
        if on("b"):
            s = f"<b>{s}</b>"
        if on("i"):
            s = f"<i>{s}</i>"
        if on("u"):
            s = f"<u>{s}</u>"
        if on("strike") or on("dstrike"):
            s = f"<s>{s}</s>"
        va = pr.find(W + "vertAlign")
        if va is not None and va.get(W + "val") in ("superscript", "subscript"):
            tg = "sup" if va.get(W + "val") == "superscript" else "sub"
            s = f"<{tg}>{s}</{tg}>"
        hl, shd = pr.find(W + "highlight"), pr.find(W + "shd")
        if (hl is not None and hl.get(W + "val") not in ("none", None)) or \
                (shd is not None and shd.get(W + "fill", "auto").lower() not in ("auto", "ffffff", "")):
            s = f"<mark>{s}</mark>"
        return s

    def para(self, p):
        """(html, poziom nagłówka, (numId, ilvl) albo None)"""
        ppr = p.find(W + "pPr")
        style, num = "", None
        if ppr is not None:
            ps = ppr.find(W + "pStyle")
            if ps is not None:
                sid = ps.get(W + "val", "")
                style = self.styles.get(sid, sid.lower())
                num = self.style_num.get(sid)
            np_ = ppr.find(W + "numPr")
            if np_ is not None:
                ni, il = np_.find(W + "numId"), np_.find(W + "ilvl")
                if ni is not None:
                    num = None if ni.get(W + "val") == "0" else (ni.get(W + "val"), il.get(W + "val") if il is not None else "0")
                elif il is not None and num:
                    num = (num[0], il.get(W + "val"))
        if not num and re.match(r"list (bullet|number)", style):
            num = ("_" + style.split()[1], "0")
        html = self.runs(p)
        lvl = 0
        if style in ("title", "tytuł"):
            lvl = 1
        else:
            m = re.match(r"(heading|nagłówek)\s*(\d)", style)
            if m:
                lvl = min(int(m.group(2)) + 1, 4)
            elif "list paragraph" not in style and style in ("subtitle", "podtytuł"):
                lvl = 3
        return html, lvl, num

    def table(self, tbl) -> str:
        h = "<table>"
        for tr in tbl.findall(W + "tr"):
            h += "<tr>"
            for tc in tr.findall(W + "tc"):
                span = tc.find(f"{W}tcPr/{W}gridSpan")
                cs = f' colspan="{span.get(W + "val")}"' if span is not None and span.get(W + "val", "1") != "1" else ""
                cell = "<br>".join(self.runs(p) for p in tc.iter(W + "p"))
                h += f"<td{cs}>{cell}</td>"
            h += "</tr>"
        return h + "</table>"

    def html(self) -> str:
        body = ET.fromstring(self.z.read("word/document.xml")).find(W + "body")
        out, lst = [], []          # lst: stos otwartych list [(tag, ilvl)]

        def close(to=-1):
            while len(lst) > to + 1:
                out.append(f"</li></{lst.pop()[0]}>")

        def elements(parent):
            for el in parent:
                if el.tag in (W + "sdt",):
                    c = el.find(W + "sdtContent")
                    if c is not None:
                        yield from elements(c)
                elif el.tag == W + "customXml":
                    yield from elements(el)
                else:
                    yield el

        for el in elements(body):
            if el.tag == W + "p":
                html, lvl, num = self.para(el)
                if num:
                    il = int(num[1] or 0)
                    fmt, ltxt = self.numfmt.get(num[0], {}).get(str(il), ("decimal" if num[0] == "_number" else "bullet", ""))
                    tag = "ul" if fmt in ("bullet", "none") else "ol"
                    box = '<input type="checkbox">' if ltxt in ("☐", "□", "❑", "", "") else ""
                    if lst and lst[-1][1] == il and lst[-1][0] == tag:
                        out.append("</li><li>")
                    else:
                        while lst and lst[-1][1] > il:
                            out.append(f"</li></{lst.pop()[0]}>")
                        if lst and lst[-1][1] == il:
                            out.append(f"</li></{lst.pop()[0]}>")
                        out.append(f"<{tag}><li>")
                        lst.append((tag, il))
                    out.append(box + html)
                    continue
                close()
                if lvl == 1 and not self.title and re.sub(r"<[^>]+>", "", html).strip():
                    self.title = unescape(re.sub(r"<[^>]+>", "", html)).strip()
                    continue
                if re.fullmatch(r"\s*\$\$.*\$\$\s*", html, re.S) and html.count("$$") == 2:
                    out.append(f'<div class="math-disp">{html.strip()[2:-2]}</div>')
                elif lvl:
                    out.append(f"<h{lvl}>{html}</h{lvl}>" if html.strip() else "")
                else:
                    out.append(f"<p>{html}</p>" if html.strip() else "<p><br></p>")
            elif el.tag == W + "tbl":
                close()
                out.append(self.table(el))
        close()
        return "".join(out)


# ===================== Evernote (.enex) =====================
def _enex_notes(data: bytes, ctx: Ctx, group: str):
    root = ET.fromstring(data)
    for note in root.iter("note"):
        title = (note.findtext("title") or "").strip() or "Bez tytułu"
        media = {}
        for res in note.findall("resource"):
            d = res.find("data")
            if d is None or not (d.text or "").strip():
                continue
            try:
                blob = base64.b64decode(d.text)
            except Exception:
                continue
            mime = (res.findtext("mime") or "").lower()
            fname = res.findtext("resource-attributes/file-name") or ("plik." + mime.split("/")[-1])
            h = hashlib.md5(blob).hexdigest()
            if mime.startswith("image/") and mime.split("/")[-1] in ("png", "jpeg", "jpg", "gif", "webp"):
                media[h] = {"kind": "img", "url": ctx.save_bytes(blob, fname)}
            elif mime == "application/pdf":
                media[h] = {"kind": "pdf", "url": ctx.save_bytes(blob, fname if fname.lower().endswith(".pdf") else fname + ".pdf")}
        content = note.findtext("content") or ""
        content = re.sub(r"<\?xml[^>]*\?>|<!DOCTYPE[^>]*>", "", content)
        tree = parse_html(content)
        blocks = Converter(ctx, "", tree.css, media).run(tree.root)
        created = _enex_date(note.findtext("created"))
        yield {"title": title, "blocks": blocks, "group": group, "created": created}


def _enex_date(s):
    try:
        return datetime.strptime(s or "", "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ===================== Google Keep (Takeout) =====================
def _keep_note(d: dict, ctx: Ctx, base: str):
    if d.get("isTrashed"):
        return None
    blocks = []
    txt = d.get("textContent") or ""
    if txt.strip():
        paras = [p for p in re.split(r"\n\s*\n", txt.strip())]
        html = "".join("<p>" + escape(p, quote=False).replace("\n", "<br>") + "</p>" for p in paras)
        tree = parse_html(re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', html))
        blocks += Converter(ctx, base).run(tree.root)
    items = d.get("listContent") or []
    if items:
        blocks.append({"id": _uid(), "type": "checklist",
                       "items": [{"text": (x.get("text") or "")[:500], "done": bool(x.get("isChecked"))} for x in items]})
    imgs = []
    for att in d.get("attachments") or []:
        fp = att.get("filePath") or ""
        url = ctx.asset(fp, base)
        if not url and fp:                    # Takeout bywa niekonsekwentny: .jpeg zamiast .jpg itp.
            stem = os.path.splitext(fp)[0].lower()
            for p in ctx.vfs.files:
                if posixpath.dirname(p) == base and os.path.splitext(posixpath.basename(p))[0].lower() == stem:
                    url = ctx.asset(p, "")
                    ctx.missing -= 1
                    break
        if url:
            imgs.append(f'<p><img class="noteimg" src="{escape(url)}" alt=""></p>')
    if imgs:
        blocks.append({"id": _uid(), "type": "text", "html": "".join(imgs)})
    if not blocks and not d.get("title"):
        return None
    title = (d.get("title") or "").strip() or (txt.strip().split("\n")[0].strip()[:60] or "Notatka z Keep")
    labels = [x.get("name") for x in d.get("labels") or [] if x.get("name")]
    created = None
    us = d.get("createdTimestampUsec") or d.get("userEditedTimestampUsec")
    if us:
        try:
            created = datetime.fromtimestamp(int(us) / 1e6, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            pass
    return {"title": title, "blocks": blocks, "label": labels[0] if labels else "", "created": created}


# ===================== składanie wszystkiego =====================
def collect(vfs: VFS, ctx: Ctx):
    """Wszystkie notatki z plików: [{path, dir, title, blocks, group?, created?}] + lista pominiętych."""
    notes, skipped = [], []
    keep_stems = {p[:-5] for p in vfs.files if p.lower().endswith(".json")}
    paths = sorted(p for p in vfs.files if not _skip_path(p))
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        d, fn = posixpath.dirname(p), posixpath.basename(p)
        stem = os.path.splitext(fn)[0]
        try:
            if ext in (".md", ".markdown", ".mdown", ".txt"):
                raw = vfs.files[p]().decode("utf-8", "replace")
                title, html, meta = _md_note(raw, _clean_name(stem))
                tree = parse_html(html)
                blocks = Converter(ctx, d).run(tree.root)
                created = meta.get("created") or meta.get("date")
                notes.append({"path": p, "dir": d, "title": title, "blocks": blocks, "created": _iso(created)})
            elif ext in (".html", ".htm"):
                if p[:-len(ext)] in keep_stems:      # Keep: ta sama notatka jest też w .json (pełniejsza)
                    continue
                raw = vfs.files[p]().decode("utf-8", "replace")
                tree = parse_html(raw)
                title = unescape(tree.title).strip()
                h1 = tree.root.find("h1")
                if h1 is not None and (not title or h1.text().strip() == title):
                    title = title or h1.text().strip()
                    _remove(tree.root, h1)
                blocks = Converter(ctx, d, tree.css).run(tree.root)
                if fn.lower() == "index.html" and not blocks:
                    continue
                notes.append({"path": p, "dir": d, "title": _clean_name(title or stem), "blocks": blocks})
            elif ext == ".docx":
                dx = Docx(vfs.files[p](), ctx, fn)
                tree = parse_html(dx.html())
                blocks = Converter(ctx, d).run(tree.root)
                notes.append({"path": p, "dir": d, "title": _clean_name(stem) if not dx.title or len(dx.title) > 120 else dx.title, "blocks": blocks})
            elif ext == ".enex":
                for nt in _enex_notes(vfs.files[p](), ctx, _clean_name(stem)):
                    notes.append({"path": p + "#" + str(len(notes)), "dir": d, **nt})
            elif ext == ".json":
                try:
                    data = json.loads(vfs.files[p]().decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(data, dict) and ("textContent" in data or "listContent" in data) and "title" in data:
                    nt = _keep_note(data, ctx, d)
                    if nt:
                        notes.append({"path": p, "dir": d, **nt})
            elif ext == ".csv":
                if stem.endswith("_all"):
                    continue
                raw = vfs.files[p]().decode("utf-8-sig", "replace")
                rows = [r for r in csv.reader(io.StringIO(raw))][:500]
                if rows:
                    w = max(len(r) for r in rows)
                    rows = [[c[:300] for c in r] + [""] * (w - len(r)) for r in rows]
                    notes.append({"path": p, "dir": d, "title": _clean_name(stem), "blocks": [{"id": _uid(), "type": "table", "data": rows}]})
        except Exception as e:   # jeden zepsuty plik nie zatrzymuje reszty
            skipped.append(f"{fn}: {type(e).__name__}")
    for n in notes:   # osadzone PDF-y → bloki PDF (zanim policzymy, których PDF-ów nic nie używa)
        n["blocks"] = _pdf_markers(n["blocks"], ctx, n["dir"] if "#" not in n["path"] else "")
    # PDF-y, do których nie odwołuje się żadna notatka — każdy jako osobna notatka z podglądem pliku
    for p in paths:
        if p.lower().endswith(".pdf") and p not in ctx.used:
            url = ctx.save_bytes(vfs.files[p](), posixpath.basename(p))
            notes.append({"path": p, "dir": posixpath.dirname(p), "title": _clean_name(os.path.splitext(posixpath.basename(p))[0]),
                          "blocks": [{"id": _uid(), "type": "embed", "url": url}]})
    return notes, skipped


def _remove(root: Node, target: Node):
    for k in root.kids:
        if k is target:
            root.kids.remove(k)
            return True
        if isinstance(k, Node) and _remove(k, target):
            return True
    return False


def _iso(s):
    if not s:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?", str(s))
    return (m.group(1) + " " + (m.group(2) or "12:00") + (":00" if m.group(2) and len(m.group(2)) == 5 else "")) if m else None


def _pdf_markers(blocks, ctx: Ctx, base: str):
    """Osadzone PDF-y (![[plik.pdf]]) — wynieś z tekstu do osobnych bloków PDF pod akapitem."""
    out = []
    for b in blocks:
        if b["type"] != "text" or "data-pdf" not in b["html"]:
            out.append(b)
            continue
        urls = []
        def take(m):
            u = ctx.asset(unescape(m.group(1)), base, {".pdf"})
            if u:
                urls.append(u)
            return ""
        b["html"] = re.sub(r'<span data-pdf="([^"]*)"></span>', take, b["html"])
        b["html"] = re.sub(r"<p>\s*</p>", "", b["html"])
        if re.sub(r"<[^>]+>|\s", "", b["html"]) or "<img" in b["html"]:
            out.append(b)
        out.extend({"id": _uid(), "type": "embed", "url": u} for u in urls)
    return out


def _strip_root(notes):
    """Wspólny folder nadrzędny (nazwa sejfu / paczki) → nazwa; ścieżki bez niego."""
    name = ""
    while notes:
        heads = {n["dir"].split("/")[0] if n["dir"] else "" for n in notes}
        if len(heads) != 1 or "" in heads:
            break
        h = heads.pop()
        name = h
        for n in notes:
            n["dir"] = n["dir"][len(h) + 1:]
    return _clean_name(name) if name else ""


def run_import(vfs: VFS, target, name: str, folders: str):
    by_name = {}
    for p in vfs.files:
        by_name.setdefault(posixpath.basename(p).lower(), []).append(p)
    ctx = Ctx(vfs, by_name)
    notes, skipped = collect(vfs, ctx)
    keys = {}                                   # ścieżka / nazwa pliku / tytuł → notatka (do linków [[…]])
    for n in notes:
        p = n["path"].split("#")[0]
        keys.setdefault(os.path.splitext(p)[0].lower(), n)
        keys.setdefault(os.path.splitext(posixpath.basename(p))[0].lower(), n)
        keys.setdefault(_clean_name(os.path.splitext(posixpath.basename(p))[0]).lower(), n)
        keys.setdefault(n["title"].lower(), n)
    root_name = _strip_root(notes)
    if not notes:
        return {"notes": 0, "notebooks": [], "images": 0, "skipped": skipped, "missing": ctx.missing}

    with get_conn() as c:
        made, nb_ids, groups = [], {}, {}

        def notebook(nm):
            if nm in nb_ids:
                return nb_ids[nm]
            col = COLORS[(c.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0]) % len(COLORS)]
            nid = c.execute("INSERT INTO notebooks(name,color,purpose) VALUES(?,?,?)",
                            (nm[:80], col, "Zaimportowane notatki")).lastrowid
            nb_ids[nm] = nid
            made.append({"id": nid, "name": nm[:80]})
            return nid

        def group(nb, gname):
            if not gname:
                return None
            k = (nb, gname)
            if k not in groups:
                ex = c.execute("SELECT id FROM note_groups WHERE notebook_id=? AND name=?", (nb, gname[:60])).fetchone()
                if ex:
                    groups[k] = ex[0]
                else:
                    pos = c.execute("SELECT COALESCE(MAX(position),0)+1 FROM note_groups WHERE notebook_id=?", (nb,)).fetchone()[0]
                    groups[k] = c.execute("INSERT INTO note_groups(notebook_id,name,position) VALUES(?,?,?)", (nb, gname[:60], pos)).lastrowid
            return groups[k]

        base_name = (name or root_name or "Import " + datetime.now().strftime("%d.%m.%Y")).strip()[:80]
        existing = None
        if isinstance(target, int):
            row = c.execute("SELECT id,name FROM notebooks WHERE id=?", (target,)).fetchone()
            if not row:
                raise HTTPException(404, "Nie ma takiego zeszytu.")
            existing = row[0]
            made.append({"id": row[0], "name": row[1]})
        for n in notes:
            parts = [_clean_name(x) for x in n["dir"].split("/") if x]
            gname = n.get("group") or n.get("label") or ""
            if "group" in n or "label" in n:      # Evernote: notes z pliku .enex, Keep: etykieta
                parts = parts if folders == "notebooks" else []
            if existing:
                nb, g = existing, " / ".join(parts) or gname
            elif folders == "notebooks" and parts:
                nb, g = notebook(parts[0]), " / ".join(parts[1:]) or gname
            else:
                nb, g = notebook(base_name), " / ".join(parts) or gname
            n["id"] = c.execute(
                "INSERT INTO notes(notebook_id,title,content,group_id,created_at,updated_at) VALUES(?,?,?,?,COALESCE(?,datetime('now')),datetime('now'))",
                (nb, (n["title"] or "Bez tytułu")[:200], "[]", group(nb, g[:60]), n.get("created"))).lastrowid
            n["nb"] = nb
        # linki między notatkami: [[Tytuł]] / link do pliku .md → link do notatki w aplikacji
        links = 0
        for n in notes:
            def wiki(m):
                nonlocal links
                tgt, label = unescape(m.group(1)), m.group(2)
                t = tgt.lower()
                cand = keys.get(os.path.splitext(t)[0]) or keys.get(t) or keys.get(os.path.splitext(posixpath.basename(t))[0]) \
                    or keys.get(_clean_name(os.path.splitext(posixpath.basename(t))[0]).lower())
                if not cand:
                    return label
                links += 1
                return (f'<a href="javascript:void 0" contenteditable="false" class="wikilink" data-note="{cand["id"]}">'
                        f'[[{escape(cand["title"], quote=False)}]]</a>')
            for b in n["blocks"]:
                for f in ("html", "summary"):
                    if isinstance(b.get(f), str) and "data-wiki" in b[f]:
                        b[f] = re.sub(r'<a data-wiki="([^"]*)">(.*?)</a>', wiki, b[f], flags=re.S)
            c.execute("UPDATE notes SET content=? WHERE id=?", (json.dumps(n["blocks"], ensure_ascii=False), n["id"]))
    counts = {}
    for n in notes:
        counts[n["nb"]] = counts.get(n["nb"], 0) + 1
    for m in made:
        m["notes"] = counts.get(m["id"], 0)
    return {"notes": len(notes), "notebooks": made, "images": ctx.images, "links": links,
            "skipped": skipped[:30], "missing": ctx.missing}


# ===================== API =====================
class ImportFile(BaseModel):
    path: str
    data: str            # base64
    append: bool = False  # kolejny kawałek dużego pliku (wysyłane po kilkanaście MB)


class ImportFiles(BaseModel):
    session: str = ""
    files: list[ImportFile]


class ImportRun(BaseModel):
    session: str
    target: int | None = None       # istniejący zeszyt albo None = nowy
    name: str = ""
    folders: str = "groups"         # groups | notebooks


def _session_dir(sid: str):
    if not re.fullmatch(r"[0-9a-f]{16}", sid or ""):
        raise HTTPException(400, "Zła sesja importu.")
    return _tmp_root() / sid


def _cleanup_old():
    root = _tmp_root()
    if not root.exists():
        return
    for d in root.iterdir():
        try:
            if time.time() - d.stat().st_mtime > 6 * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


@router.post("/api/import/files")
def import_files(p: ImportFiles):
    if not p.session:
        _cleanup_old()
        p.session = uuid.uuid4().hex[:16]
    d = _session_dir(p.session)
    d.mkdir(parents=True, exist_ok=True)
    used = sum(f.stat().st_size for f in d.path().rglob("*") if f.is_file())
    for f in p.files:
        rel = _safe_rel(f.path)
        if not rel:
            continue
        raw = f.data.split(",", 1)[1] if f.data.startswith("data:") else f.data
        try:
            blob = base64.b64decode(raw)
        except Exception:
            raise HTTPException(400, "Niepoprawne dane pliku: " + rel)
        used += len(blob)
        if used > MAX_SESSION:
            raise HTTPException(413, "Za dużo danych naraz (limit 600 MB) — zaimportuj mniejsze części.")
        dest = d.path() / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "ab" if f.append else "wb") as fh:
            fh.write(blob)
    return {"session": p.session, "bytes": used}


@router.post("/api/import/run")
def import_run(p: ImportRun):
    d = _session_dir(p.session)
    if not d.exists():
        raise HTTPException(404, "Sesja importu wygasła — wybierz pliki jeszcze raz.")
    vfs = VFS()
    try:
        vfs.add_dir(d.path())
        if not any(os.path.splitext(x)[1].lower() in NOTE_EXT | {".pdf"} for x in vfs.files):
            raise HTTPException(400, "Nie znaleziono notatek. Obsługiwane: .md, .txt, .html, .docx, .enex, Google Keep (.json), .zip.")
        return run_import(vfs, p.target, p.name, p.folders)
    finally:
        vfs.close()
        shutil.rmtree(d.path(), ignore_errors=True)


@router.delete("/api/import/{sid}")
def import_cancel(sid: str):
    shutil.rmtree(_session_dir(sid).path(), ignore_errors=True)
    return {"ok": True}
