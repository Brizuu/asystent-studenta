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

## Aplikacja desktop (Windows)

Gotowy plik **`Asystent.exe`** buduje GitHub Actions przy każdym pushu na `main`
(zakładka *Actions* → „Aplikacja desktop (Windows)” → artefakt *Asystent-windows*).
Tag `v*` (np. `v1.0`) publikuje exe w zakładce *Releases*.

- Jeden plik, bez instalacji — okno aplikacji (WebView2, wbudowany w Windows 10/11).
- Dane: `%APPDATA%\Asystent` (baza `asystent.db`, `uploads/`, klucz `.gemini_key`).
- **Eksport danych** (pasek menu, na dole) zapisuje kopię ZIP do folderu *Pobrane*;
  **Import danych** wczytuje taką kopię albo sam plik `asystent.db` (np. ze starej wersji
  uruchamianej przez uvicorn). Poprzednia baza zostaje jako `asystent.db.bak`.

### Nagrywanie wykładów (tylko desktop)

W notatce: **● Nagraj wykład** → mikrofon nagrywa w tle (także gdy przejdziesz do innego widoku),
plik zapisuje się na bieżąco w `%APPDATA%\Asystent\recordings`, cięty na części po 15 min (~3 MB, OGG/Opus).
Później **Zamień na tekst** wysyła części po kolei do Gemini (1 część = 1 zapytanie, 7 h ≈ 28 zapytań),
a gotowy zapis trafia do notatki jako blok „Zapis wykładu”. Po dziennym limicie albo awarii zrobione części
zostają — **Dokończ zamianę** wznawia od miejsca przerwania. Zamknięcie okna w trakcie nagrywania domyka plik.

Ze źródeł: `pip install -r requirements.txt pywebview sounddevice soundfile` i `python desktop.py`.

## Plan zajęć z USOS

Plan dnia → **Importuj plan z USOS** → plik `.ics` (USOSweb → Mój plan → eksport do iCalendar).
Z pliku brane jest to, co USOS podaje: przedmiot, typ (W → Wykład, CW → Ćwiczenia, LAB → Laboratorium…),
termin, sala, budynek, adres i link do zajęć. Ponowny import aktualizuje plan bez dublowania
(Twoje oznaczenia „zrobione”, notatki i powiązania zostają).

## Konfiguracja

- **Notatki AI (Gemini):** wklej swój klucz do pliku `.gemini_key` (wzór: `.gemini_key.example`;
  w aplikacji desktop: `%APPDATA%\Asystent\.gemini_key`)
  albo ustaw zmienną środowiskową `GEMINI_API_KEY`.
- **Spotify:** utwórz darmową aplikację na developer.spotify.com, a w jej ustawieniach
  dodaj Redirect URI: `http://127.0.0.1:8000/` (musi być `127.0.0.1`, nie `localhost`).
  Sterowanie odtwarzaniem wymaga konta Premium.

## Uwagi

- Baza `asystent.db` tworzy się automatycznie przy pierwszym uruchomieniu.
- Pliki `.gemini_key`, `asystent.db` i katalog `uploads/` są celowo pominięte w repo
  (patrz `.gitignore`) — zawierają sekrety i dane prywatne.
