"""Asystent — aplikacja desktop: serwer FastAPI w tle + natywne okno (pywebview).
Dane w %APPDATA%\\Asystent (Windows) / ~/.local/share/Asystent.
Uruchom ze źródeł:  pip install -r requirements.txt pywebview sounddevice soundfile  &&  python desktop.py
"""
import os
import socket
import threading
import time
from pathlib import Path

DATA = Path(os.getenv("APPDATA") or Path.home() / ".local" / "share") / "Asystent"
os.environ.setdefault("ASYSTENT_DATA", str(DATA))   # musi być przed importem server/db
os.environ["ASYSTENT_DESKTOP"] = "1"                # włącza nagrywanie wykładów w interfejsie

import uvicorn   # noqa: E402
import webview   # noqa: E402
from server import app   # noqa: E402
import nagrania   # noqa: E402  (już zaimportowany przez server)


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
    webview.create_window("Asystent", f"http://127.0.0.1:{port}/", width=1400, height=900, min_size=(900, 600))
    # private_mode=False + storage_path: localStorage (Spotify, ustawienia) przetrwa restart
    webview.start(private_mode=False, storage_path=str(DATA / "webview"))
    nagrania.rec_stop()   # zamknięcie okna w trakcie nagrywania: domknij plik, nagranie zostaje
    server.should_exit = True


if __name__ == "__main__":
    main()
