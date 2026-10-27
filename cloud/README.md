# Serwer kont Asystenta

Logowanie, profil (avatar), znajomi, udostępnianie notatek/zeszytów, synchronizacja danych między urządzeniami
jednego konta i panel administratora.

## Lokalnie (do testów)

```bash
pip install -r cloud/requirements.txt
python -m uvicorn cloud.app:app --port 8100
```

Lokalny serwer: uruchom aplikację z `ASYSTENT_CLOUD_URL=http://127.0.0.1:8100` albo zmień adres linkiem „zmień” pod logowaniem.

## Konto testowe (do sprawdzenia znajomych i udostępniania)

```bash
python cloud/demo.py twoj@email.pl
```
Zakłada konto „Anna Testowa”, wysyła Ci zaproszenie, po jego akceptacji w aplikacji udostępnia
Ci przykładową notatkę i zeszyt, a na koniec wypisuje jej dane logowania (do testu w drugą stronę).

## Produkcja: https://zenexus.pl/asystent (za nginx)

API działa w Dockerze i słucha tylko lokalnie (`127.0.0.1:8100`); domenę i HTTPS obsługuje istniejący nginx.

1. **Na serwerze — jedna komenda** (instaluje Dockera, jeśli trzeba, buduje i uruchamia API):
   ```bash
   curl -fsSL https://raw.githubusercontent.com/Brizuu/asystent-studenta/main/cloud/install.sh | bash
   ```
   albo z wgranego folderu: `bash deploy.sh` (opcjonalnie: `bash deploy.sh admin1@mail.pl,admin2@mail.pl 8100`).
2. **nginx** — gotowy plik `nginx-zenexus.conf` (instrukcja w nagłówku pliku) albo wklej `nginx-asystent.conf` do bloku
   `server { … }` domeny zenexus.pl (tego z SSL), potem `sudo nginx -t && sudo systemctl reload nginx`.
   Sprawdzenie: `curl https://zenexus.pl/asystent/health` → `{"ok":true}`.
3. **Aplikacja** łączy się z `https://zenexus.pl/asystent` domyślnie (od 1.9.36; wcześniej zenfix.pl — zapisany stary adres
   jest sam przepisywany na nowy, a stara domena dalej obsługuje API, dopóki wszyscy się nie zaktualizują). Do testów lokalnych:
   zmienna `ASYSTENT_CLOUD_URL=http://127.0.0.1:8100` albo link „zmień” pod logowaniem.

**Wersja webowa:** `https://zenexus.pl/asystent/app/` — ta sama aplikacja w przeglądarce (też na telefonie: „Dodaj do ekranu
głównego”). Bez zalogowania pokazuje tylko stronę logowania; każde konto ma osobną bazę w wolumenie (`/data/web/u<id>`),
a dane synchronizują się z aplikacją na Windows przez ten sam serwer. Nagrywanie wykładów jest tylko w aplikacji na Windows.

**Strona pobierania:** ten sam serwer pod `https://zenexus.pl/asystent/` pokazuje stronę z przyciskiem „Pobierz na Windows”
(zawsze najnowszy instalator z GitHuba). Generuje ją `python cloud/build_site.py` ze strony startowej aplikacji (`site/index.html`).

**Aktualizacja:** ta sama komenda co przy instalacji (dane zostają w wolumenie `cloud-data`).
**Kopia bazy** (baza działa w trybie WAL — kopiuj przez SQLite, nie sam plik):
`cd /opt/asystent/cloud && docker compose exec api python -c "import sqlite3;sqlite3.connect('/data/cloud.db').backup(sqlite3.connect('/data/kopia.db'))" && docker compose cp api:/data/kopia.db ./kopia-$(date +%F).db`
**Logi:** `docker compose logs -f api`

## Synchronizacja urządzeń
- Aplikacja po zalogowaniu synchronizuje się co 5 minut, przy powrocie do okna i ręcznie (Konto → Synchronizacja).
- Synchronizowane: zeszyty, grupy, notatki, plan, zadania z checklistami, koszty, wydatki, dochody.
  Załączniki (obrazy/PDF) i nagrania zostają na urządzeniu, na którym powstały.
- Konflikt: wygrywa nowsza zmiana rekordu. Zajęcia z USOS zaimportowane na kilku urządzeniach są scalane.
- Limit: 200 MB danych na konto, 2 MB na jedną notatkę.

db ./cloud-backup.db`.

## Bezpieczeństwo
- Hasła: scrypt z losową solą; sesje: losowe tokeny (w bazie tylko ich hash), ważne 60 dni, wylogowanie je usuwa.
- Limit prób logowania/rejestracji: 10 na 5 min na IP.
- Udostępniać można tylko znajomym (zaakceptowane zaproszenie); treść widzi tylko nadawca i odbiorca.
- Aplikacja przy podglądzie/imporcie czyści HTML z udostępnionej notatki (bez skryptów i niebezpiecznych linków).
- Synchronizacja: dane konta widzi tylko jego właściciel (każde żądanie wymaga tokenu sesji).
- Panel administratora: lista kont bez haseł (w bazie są tylko skróty scrypt) i reset hasła; dostęp tylko dla `ADMIN_EMAILS`.
