"""Konto testowe do sprawdzenia znajomych i udostępniania.

    python cloud/demo.py twoj@email.pl            (serwer lokalny http://127.0.0.1:8100)
    python cloud/demo.py twoj@email.pl https://konta.twojadomena.pl

Co robi:
 1. zakłada (albo loguje) konto „Anna Testowa” (anna.testowa@demo.pl / demo12345),
 2. wysyła Ci zaproszenie do znajomych — zaakceptuj je w aplikacji (Znajomi),
 3. po akceptacji udostępnia Ci przykładową notatkę i zeszyt,
 4. żeby sprawdzić udostępnianie w drugą stronę: udostępnij coś Annie i zaloguj się
    jako ona (okno prywatne przeglądarki) — zobaczysz to w jej zakładce Znajomi.
"""
import json
import sys
import time
import urllib.error
import urllib.request

EMAIL, PASSWORD, NAME = "anna.testowa@demo.pl", "demo12345", "Anna Testowa"


def call(base, path, body=None, token=None, method=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.loads(e.read() or b"{}").get("detail", e.code))


NOTE = {"title": "Mikroekonomia — elastyczność popytu", "purpose": "Egzamin", "description": "Notatka od Anny (test udostępniania)",
        "content": [
            {"id": "a1", "type": "heading", "text": "Elastyczność cenowa popytu"},
            {"id": "a2", "type": "text", "html": "<p><strong>Elastyczność</strong> mówi, o ile procent zmieni się popyt, gdy cena wzrośnie o 1%.</p>"
                                                    "<ul><li>|E| &gt; 1 — popyt elastyczny</li><li>|E| &lt; 1 — popyt nieelastyczny</li></ul>"},
            {"id": "a3", "type": "checklist", "items": [{"text": "Powtórzyć wzór", "done": True}, {"text": "Zrobić zadania 1–5", "done": False}]},
            {"id": "a4", "type": "table", "data": [["Dobro", "Elastyczność"], ["Chleb", "0,3"], ["Bilety lotnicze", "1,8"]]},
        ]}
NOTEBOOK = {"name": "Podstawy zarządzania (od Anny)", "color": "#ffb98c", "purpose": "Przedmiot",
            "notes": [
                {"title": "Funkcje zarządzania", "content": [{"id": "b1", "type": "text", "html": "<p>Planowanie, organizowanie, motywowanie, kontrolowanie.</p>"}]},
                {"title": "Style kierowania", "content": [{"id": "b2", "type": "text", "html": "<p>Autokratyczny, demokratyczny, liberalny.</p>"}]},
            ]}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    you, base = sys.argv[1].strip().lower(), (sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8100").rstrip("/")
    try:
        token = call(base, "/auth/register", {"display_name": NAME, "email": EMAIL, "password": PASSWORD})["token"]
        print(f"✓ Założono konto testowe: {NAME}")
    except RuntimeError:
        token = call(base, "/auth/login", {"login": EMAIL, "password": PASSWORD})["token"]
        print(f"✓ Zalogowano na istniejące konto testowe: {NAME}")

    def friend():
        return next((f for f in call(base, "/friends", token=token)["friends"] if f["email"] == you), None)

    if not friend():
        try:
            r = call(base, "/friends/request", {"email": you}, token=token)
            print("✓ Jesteście znajomymi (wcześniej zaprosiłeś Annę)." if r["status"] == "accepted"
                  else f"→ Wysłano zaproszenie do {you}. Zaakceptuj je w aplikacji: Znajomi → Akceptuj.")
        except RuntimeError as e:
            print("!", e)
            if "Nie ma konta" in str(e):
                print("  Najpierw załóż konto w aplikacji tym e-mailem, potem uruchom skrypt jeszcze raz.")
                sys.exit(1)
        print("  Czekam na akceptację (do 10 minut)…", flush=True)
        for _ in range(200):
            if friend():
                break
            time.sleep(3)
        else:
            print("! Nie zaakceptowano zaproszenia — uruchom skrypt ponownie po akceptacji.")
            sys.exit(1)
        print("✓ Zaproszenie zaakceptowane.")
    f = friend()
    call(base, "/shares", {"to": [f["id"]], "kind": "note", "title": NOTE["title"], "payload": NOTE}, token=token)
    call(base, "/shares", {"to": [f["id"]], "kind": "notebook", "title": NOTEBOOK["name"], "payload": NOTEBOOK}, token=token)
    print("✓ Anna udostępniła Ci notatkę i zeszyt — zobacz: Znajomi → Anna Testowa (Podgląd / Importuj).")
    print(f"\nTest w drugą stronę: udostępnij coś Annie, potem zaloguj się jako ona w oknie prywatnym:\n  e-mail: {EMAIL}\n  hasło:  {PASSWORD}")


if __name__ == "__main__":
    main()
