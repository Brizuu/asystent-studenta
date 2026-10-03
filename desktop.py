"""Asystent — aplikacja desktop: serwer FastAPI w tle + natywne okno (pywebview).
Dane w %APPDATA%\\Asystent (Windows) / ~/.local/share/Asystent.
Uruchom ze źródeł:  pip install -r requirements.txt pywebview sounddevice soundfile  &&  python desktop.py
"""
import json
import os
import socket
import threading
import time
from pathlib import Path

DATA = Path(os.getenv("APPDATA") or Path.home() / ".local" / "share") / "Asystent"
os.environ.setdefault("ASYSTENT_DATA", str(DATA))   # musi być przed importem server/db
os.environ["ASYSTENT_DESKTOP"] = "1"                # włącza nagrywanie wykładów w interfejsie


def frameless_on() -> bool:
    """Własny pasek tytułu (logo, wersja, przyciski okna) zamiast paska Windows; wyłączany w Ustawieniach → Dane."""
    try:
        return bool(json.loads((DATA / "okno.json").read_text(encoding="utf-8")).get("frameless", True))
    except Exception:
        return os.name == "nt"


FRAMELESS = frameless_on()
if FRAMELESS:
    os.environ["ASYSTENT_FRAMELESS"] = "1"          # interfejs rysuje wtedy własny pasek tytułu

import uvicorn   # noqa: E402
import webview   # noqa: E402
from webview.window import FixPoint   # noqa: E402
from server import app   # noqa: E402
import nagrania   # noqa: E402  (już zaimportowany przez server)
import aktualizacje   # noqa: E402
import sync   # noqa: E402


class WindowApi:
    """Przyciski własnego paska tytułu (window.pywebview.api.* w interfejsie). Okno bez ramki nie ma przeciągania,
    maksymalizacji ani zmiany rozmiaru od Windows — interfejs woła te metody."""

    def __init__(self):
        self._win = None
        self._max = False

    def _set_max(self, v):
        self._max = v

    def minimize(self):
        self._win.minimize()

    def is_maximized(self):
        return self._max

    def toggle_maximize(self):
        if self._max:
            self._win.restore()
            self._max = False
        else:
            self._fit_work_area()
            self._win.maximize()
            self._max = True
        return self._max

    def _fit_work_area(self):
        # okno bez ramki po maksymalizacji zasłoniłoby pasek zadań — ograniczamy je do obszaru roboczego monitora
        try:
            from System import Func, Type   # pythonnet (backend WinForms pywebview)
            from System.Drawing import Rectangle
            from System.Windows.Forms import Screen
            form = self._win.native

            def fit():
                scr = Screen.FromHandle(form.Handle)
                wa, b = scr.WorkingArea, scr.Bounds
                form.MaximizedBounds = Rectangle(wa.X - b.X, wa.Y - b.Y, wa.Width, wa.Height)
            form.Invoke(Func[Type](fit)) if form.InvokeRequired else fit()
        except Exception:
            pass

    def move(self, x, y):
        if not self._max:
            self._win.move(int(x), int(y))

    def resize(self, width, height, edges=""):
        if self._max:
            return
        fix = (FixPoint.EAST if "w" in edges else FixPoint.WEST) | (FixPoint.SOUTH if "n" in edges else FixPoint.NORTH)
        self._win.resize(max(900, int(width)), max(600, int(height)), fix)

    def close(self):
        self._win.destroy()


def pick_port() -> int:
    # 8000 = adres zarejestrowany w Spotify (Redirect URI); zajęty → losowy wolny
    for port in (8000, 0):
        with socket.socket() as s:
            if os.name != "nt":   # jak uvicorn: port po niedawnym zamknięciu (TIME_WAIT) jest wolny
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("brak wolnego portu")


def main():
    port = pick_port()
    # log_config=None: w .exe bez konsoli sys.stdout jest None i domyślne logi uvicorna padają
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    api = WindowApi()
    win = webview.create_window(f"Asystent {aktualizacje.app_version()}", f"http://127.0.0.1:{port}/", width=1400, height=900,
                                min_size=(900, 600), frameless=FRAMELESS, easy_drag=False, js_api=api, background_color="#0b0b14")
    api._win = win
    win.events.maximized += lambda *a: api._set_max(True)    # np. Win+↑
    win.events.restored += lambda *a: api._set_max(False)
    aktualizacje.on_quit = win.destroy   # aktualizacja: instalator wystartował → zamknij okno, żeby mógł podmienić pliki
    # private_mode=False + storage_path: localStorage (Spotify, ustawienia) przetrwa restart
    webview.start(private_mode=False, storage_path=str(DATA / "webview"))
    nagrania.rec_stop()   # zamknięcie okna w trakcie nagrywania: domknij plik, nagranie zostaje
    # zaległe zmiany lecą na serwer kont przy zamknięciu — od razu widać je w wersji webowej / na innych urządzeniach
    t = threading.Thread(target=sync.run_saved, daemon=True)
    t.start()
    t.join(timeout=20)
    server.should_exit = True


if __name__ == "__main__":
    main()
