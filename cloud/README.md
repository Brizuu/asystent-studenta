# Serwer kont Asystenta

Logowanie, profil (avatar), znajomi, udostępnianie notatek/zeszytów, synchronizacja danych między urządzeniami
jednego konta i panel administratora.

## Lokalnie (do testów)

```bash
pip install -r cloud/requirements.txt
python -m uvicorn cloud.app:app --port 8100
```

Aplikacja domyślnie łączy się z `http://127.0.0.1:8100`
(zmiana: Administrator → Serwer kont, link „zmień” na ekranie logowania albo zmienna `ASYSTENT_CLOUD_URL`).

## Konto testowe (do sprawdzenia znajomych i udostępniania)

```bash
python cloud/demo.py twoj@email.pl
```
Zakłada konto „Anna Testowa”, wysyła Ci zaproszenie, po jego akceptacji w aplikacji udostępnia
Ci przykładową notatkę i zeszyt, a na koniec wypisuje jej dane logowania (do ## Produkcja na VPS (Docker + automatyczny HTTPS)

Wystarczy najmniejszy VPS (1 vCPU, 1 GB RAM, Ubuntu 24.04) i domena (albo subdomena).

1. **DNS:** rekord **A** `konta.twojadomena.pl` → IP VPS-a (propagacja zwykle kilka minut).
2. **Docker** na VPS-ie (jednorazowo):
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. **Serwer kont:**
   ```bash
   git clone https://github.com/Brizuu/asystent-studenta.git && cd asystent-studenta/cloud
   cp .env.example .env
   nano .env    # DOMAIN=konta.twojadomena.pl, ADMIN_EMAILS=twoj@mail.pl
   docker compose up -d --build
   curl https://konta.twojadomena.pl/health     # {"ok":true}
   ```
   Caddy sam pobiera i odnawia certyfikat Let's Encrypt (porty 80 i 443 muszą być otwarte).
   Baza (konta, udostępnienia, dane synchronizacji) leży w wolumenie `cloud-data`.
4. **W aplikacji:** zaloguj się kontem z `ADMIN_EMAILS` → **Administrator → Serwer kont** →
   `https://konta.twojadomena.pl` → Zapisz. (Przed zalogowaniem: link „zmień” pod formularzem logowania.)
   Żeby nowe instalacje od razu łączyły się z Twoim serwerem, ustaw domyślny adres w `server.py`
   (`ASYSTENT_CLOUD_URL`) i wydaj nową wersję.

**Aktualizacja serwera:** `cd asystent-studenta && git pull && cd cloud && docker compose up -d --build`
**Kopia bazy:** `docker compose cp api:/data/cloud.db ./cloud-backup-$(date +%F).db` (warto w cronie raz dziennie)
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
