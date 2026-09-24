# Asystent

Asystent studenta — aplikacja webowa: plan dnia, finanse, źródła dochodu i moduł „Nauka"
(zeszyty + notatki blokowe z formatowaniem, korektą pisowni, eksportem do PDF,
kalkulatorem naukowym, notatkami AI i widżetem Spotify).

Backend: **FastAPI + uvicorn**, dane w **SQLite** (stdlib `sqlite3`, bez ORM).
Frontend: pojedynczy plik `index.html` (vanilla JS, bez frameworka).

## Uruchomienie

```bash
pip install -r requirements.txt
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Następnie otwórz **http://127.0.0.1:8000**.

## Konfiguracja

- **Notatki AI (Gemini):** wklej swój klucz do pliku `.gemini_key` (wzór: `.gemini_key.example`)
  albo ustaw zmienną środowiskową `GEMINI_API_KEY`.
- **Spotify:** utwórz darmową aplikację na developer.spotify.com, a w jej ustawieniach
  dodaj Redirect URI: `http://127.0.0.1:8000/` (musi być `127.0.0.1`, nie `localhost`).
  Sterowanie odtwarzaniem wymaga konta Premium.

## Uwagi

- Baza `asystent.db` tworzy się automatycznie przy pierwszym uruchomieniu.
- Pliki `.gemini_key`, `asystent.db` i katalog `uploads/` są celowo pominięte w repo
  (patrz `.gitignore`) — zawierają sekrety i dane prywatne.
