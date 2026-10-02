"""Asystent — aplikacja desktop: serwer FastAPI w tle + natywne okno (pywebview).
Dane w %APPDATA%\\Asystent (Windows) / ~/.local/share/Asystent.
Uruchom ze źródeł:  pip install -r requirements.txt pywebview  &&  python desktop.py
"""
import os
import socket
import threading
import time
from pathlib import Path

DATA = Path(os.getenv("APPDATA") or Path.home() / ".local" / "share") / "Asystent"
os.environ.setdefault("ASYSTENT_DATA", str(DATA))   # musi być przed importem server/db

import uvicorn   # noqa: E402
import webview   # noqa: E402
from server import app   # noqa: E402


def pick_port() -> int:
    # 8000 = adres zarejestrowany w Spotify (Redirect URI); zajęty → losowy wolny
    for port in (8000, 0):
        with socket.socket() as s:
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
    server.should_exit = True


if __name__ == "__main__":
    main()
