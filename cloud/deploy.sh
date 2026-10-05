#!/usr/bin/env bash
# Serwer kont Asystenta za nginx — wdrożenie / aktualizacja jedną komendą (Ubuntu/Debian).
#
#   bash deploy.sh                                   # admin: fabian26012006@gmail.com, port 8100
#   bash deploy.sh ja@mail.pl,kolega@mail.pl 8100    # inni administratorzy / inny port
#
# Domenę i HTTPS obsługuje nginx (patrz nginx-asystent.conf). Ponowne uruchomienie = aktualizacja,
# konta i dane zostają w wolumenie Dockera.
set -euo pipefail
cd "$(dirname "$0")"

ADMINS="${1:-fabian26012006@gmail.com}"
PORT="${2:-8100}"
PUBLIC_URL="${PUBLIC_URL:-https://zenfix.pl/asystent}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
say() { printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  say "Instaluję Dockera…"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO systemctl enable --now docker >/dev/null 2>&1 || true

say "Konfiguracja (administratorzy: $ADMINS, port lokalny: $PORT)"
cat > .env <<CONF
ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
ADMIN_EMAILS=$ADMINS
PORT=$PORT
CONF

# Pliki aplikacji dla wersji webowej (/asystent/app/): w paczce są już w app/, w repo — katalog wyżej
APPFILES="server.py db.py finanse.py zadania.py quiz.py sync.py aktualizacje.py nagrania.py usos.py web.py index.html logo.png icon-192.png icon-512.png VERSION"
if [ -f ../server.py ]; then
  mkdir -p app && for f in $APPFILES; do cp "../$f" app/; done
fi
[ -f app/server.py ] || { echo "Brak plików aplikacji w app/ — wgraj pełną paczkę asystent-serwer.zip"; exit 1; }

say "Buduję i uruchamiam serwer kont + wersję webową…"
$SUDO docker compose up -d --build --remove-orphans

for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 2
  [ "$i" = 30 ] && { echo "API nie wystartowało — logi: docker compose logs api"; exit 1; }
done
printf '\n\033[1;32m✓ API działa lokalnie:\033[0m http://127.0.0.1:%s\n' "$PORT"

if curl -fsS --max-time 5 "$PUBLIC_URL/health" >/dev/null 2>&1; then
  printf '\033[1;32m✓ Publicznie:\033[0m %s — gotowe.\n' "$PUBLIC_URL"
  printf '  Strona pobierania: %s/   ·   Wersja webowa (po zalogowaniu): %s/app/\n' "$PUBLIC_URL" "$PUBLIC_URL"
else
  echo
  echo "Jeszcze nie widać go pod $PUBLIC_URL — dodaj do nginx (gotowy plik: nginx-zenfix.conf):"
  echo "------------------------------------------------------------------"
  sed "s/127.0.0.1:8100/127.0.0.1:$PORT/" nginx-asystent.conf
  echo "------------------------------------------------------------------"
  echo "potem:  sudo nginx -t && sudo systemctl reload nginx"
  echo "i sprawdź:  curl $PUBLIC_URL/health    (ma zwrócić {\"ok\":true})"
fi
echo
echo "Logi: docker compose logs -f   ·   Kopia bazy: docker compose cp api:/data/cloud.db ./kopia.db"
