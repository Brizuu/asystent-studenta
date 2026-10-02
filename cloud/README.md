# Serwer kont Asystenta

Logowanie, profil (avatar), znajomi i udostępnianie notatek/zeszytów.
Notatki i plan zostają lokalnie w aplikacji — tu trafia tylko to, co ktoś udostępni.

## Lokalnie (do testów)

```bash
pip install -r cloud/requirements.txt
python -m uvicorn cloud.app:app --port 8100
```

Aplikacja domyślnie łączy się z `http://127.0.0.1:8100`
(zmiana: Ustawienia → Dane → Serwer kont, albo zmienna `ASYSTENT_CLOUD_URL`).

## Konto testowe (do sprawdzenia znajomych i udostępniania)

```bash
python cloud/demo.py twoj@email.pl
```
Zakłada konto „Anna Testowa”, wysyła Ci zaproszenie, po jego akceptacji w aplikacji udostępnia
Ci przykładową notatkę i zeszyt, a na koniec wypisuje jej dane logowania (do testu w drugą stronę).

## Produkcja na VPS (Docker + automatyczny HTTPS)

1. Ustaw w DNS rekord **A** domeny (np. `konta.twojadomena.pl`) na IP VPS-a.
2. Na VPS-ie (Docker z pluginem compose):
   ```bash
   git clone https://github.com/Brizuu/asystent-studenta.git && cd asystent-studenta/cloud
   cp .env.example .env        # wpisz DOMAIN=konta.twojadomena.pl
   docker compose up -d --build
   ```
   Caddy sam pobierze certyfikat Let's Encrypt; baza leży w wolumenie `cloud-data`.
3. W aplikacji: **Ustawienia → Dane → Serwer kont** → `https://konta.twojadomena.pl`.

Aktualizacja: `git pull && docker compose up -d --build`.
Kopia bazy: `docker compose cp api:/data/cloud.db ./cloud-backup.db`.

## Bezpieczeństwo
- Hasła: scrypt z losową solą; sesje: losowe tokeny (w bazie tylko ich hash), ważne 60 dni, wylogowanie je usuwa.
- Limit prób logowania/rejestracji: 10 na 5 min na IP.
- Udostępniać można tylko znajomym (zaakceptowane zaproszenie); treść widzi tylko nadawca i odbiorca.
- Aplikacja przy podglądzie/imporcie czyści HTML z udostępnionej notatki (bez skryptów i niebezpiecznych linków).
