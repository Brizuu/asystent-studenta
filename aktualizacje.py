"""Aktualizacje aplikacji desktop: sprawdzanie najnowszego wydania na GitHubie, pobranie instalatora
i uruchomienie go (instalator sam zamyka starą wersję, nadpisuje pliki i uruchamia nową).
Dane użytkownika (%APPDATA%\\Asystent) nie są przy tym ruszane."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
REPO = os.getenv("ASYSTENT_UPDATE_REPO", "Brizuu/asystent-studenta")
API = os.getenv("ASYSTENT_UPDATE_API", "https://api.github.com")
ASSET = "AsystentSetup.exe"


def app_version() -> str:
    try:
        return (APP_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except Exception:
        return "0.0.0"


def _ver(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


_cache: dict = {"t": 0, "data": None}
_dl: dict = {"state": "idle", "done": 0, "total": 0, "error": "", "path": ""}
on_quit = None   # desktop.py podpina zamknięcie okna — instalator może wtedy nadpisać pliki


def _latest(force: bool = False) -> dict:
    if not force and _cache["data"] and time.time() - _cache["t"] < 3600:
        return _cache["data"]
    req = urllib.request.Request(f"{API}/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "Asystent-updater"})
    with urllib.request.urlopen(req, timeout=10) as r:
        rel = json.loads(r.read().decode("utf-8"))
    asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
    data = {"latest": (rel.get("tag_name") or "").lstrip("v"), "notes": (rel.get("body") or "")[:4000],
            "published": rel.get("published_at"), "page": rel.get("html_url"),
            "url": asset and asset.get("browser_download_url"), "size": asset and asset.get("size")}
    _cache.update(t=time.time(), data=data)
    return data


@router.get("/api/update/check")
def check(force: bool = False):
    cur = app_version()
    try:
        d = _latest(force)
    except Exception:
        return {"current": cur, "available": False, "error": "Nie udało się sprawdzić aktualizacji (brak internetu?)."}
    avail = bool(d["latest"]) and _ver(d["latest"]) > _ver(cur) and bool(d["url"])
    return {"current": cur, **d, "available": avail,
            "can_install": avail and os.name == "nt" and bool(os.getenv("ASYSTENT_DESKTOP"))}


def _clean_env() -> dict:
    """Środowisko bez śladów PyInstallera: instalator uruchamia na końcu nową wersję, która inaczej
    dziedziczy _MEIPASS2/_PYI_* starej i szuka DLL w jej (już usuniętym) folderze tymczasowym
    → „Failed to load Python DLL … _MEIxxxx\\python312.dll”."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI_") or k.startswith("_MEI") or k == "_MEIPASS2")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # PyInstaller ≥ 6: proces potomny startuje jako samodzielna aplikacja
    for k in ("TCL_LIBRARY", "TK_LIBRARY", "SSL_CERT_FILE"):
        if os.environ.get(k, "").find("_MEI") >= 0:
            env.pop(k, None)
    path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(p for p in path.split(os.pathsep) if "_MEI" not in p)
    return env


def _download(url: str, size: int | None):
    try:
        dest = Path(tempfile.gettempdir()) / ASSET
        req = urllib.request.Request(url, headers={"User-Agent": "Asystent-updater"})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            _dl["total"] = int(r.headers.get("Content-Length") or size or 0)
            while chunk := r.read(256 * 1024):
                f.write(chunk)
                _dl["done"] += len(chunk)
        if _dl["total"] and dest.stat().st_size != _dl["total"]:
            raise IOError("pobrany plik jest niekompletny")
        _dl.update(state="installing", path=str(dest))
        # /SILENT: okno z paskiem postępu bez pytań; instalator zamyka starą wersję i uruchamia nową
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                         creationflags=flags, close_fds=True, env=_clean_env())
        time.sleep(1.5)
        if on_quit:
            on_quit()
    except Exception as e:
        _dl.update(state="error", error=str(e))


@router.post("/api/update/install")
def install():
    if not (os.name == "nt" and os.getenv("ASYSTENT_DESKTOP")):
        raise HTTPException(400, "Automatyczna aktualizacja działa w aplikacji na Windows.")
    if _dl["state"] in ("downloading", "installing"):
        return _dl
    d = check(force=True)
    if not d.get("available"):
        raise HTTPException(400, "Masz najnowszą wersję.")
    _dl.update(state="downloading", done=0, total=d.get("size") or 0, error="", path="")
    threading.Thread(target=_download, args=(d["url"], d.get("size")), daemon=True).start()
    return _dl


@router.get("/api/update/status")
def status():
    return _dl
