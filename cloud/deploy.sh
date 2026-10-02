#!/usr/bin/env bash
# Serwer kont Asystenta — wdrożenie / aktualizacja jedną komendą (Ubuntu/Debian, root albo sudo).
#
#   bash deploy.sh                          # adres https://<ip-serwera>.sslip.io (bez własnej domeny)
#   bash deploy.sh konta.twojadomena.pl     # własna domena (rekord A → IP serwera)
#   bash deploy.sh konta.twojadomena.pl ja@mail.pl,kolega@mail.pl   # + administratorzy
#
# Ponowne uruchomienie = aktualizacja (dane kont zostają w wolumenie Dockera).
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${1:-}"
ADMINS="${2:-fabian26012006@gmail.com}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say() { printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }

# 1. Docker (z wtyczką compose)
if ! command -v docker >/dev/null 2>&1; then
  say "Instaluję Dockera…"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO systemctl enable --now docker >/dev/null 2>&1 || true

# 2. Adres: własna domena albo darmowa <ip>.sslip.io (Let's Encrypt działa dla niej od ręki)
if [ -z "$DOMAIN" ] && [ -f .env ]; then
  DOMAIN="$(grep -E '^DOMAIN=' .env | cut -d= -f2- || true)"
fi
if [ -z "$DOMAIN" ]; then
  IP="$(curl -fsS4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
  DOMAIN="${IP//./-}.sslip.io"
fi

# 3. Konfiguracja
say "Konfiguracja: https://$DOMAIN (administratorzy: $ADMINS)"
cat > .env <<EOF
DOMAIN=$DOMAIN
ALLOWED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000
ADMIN_EMAILS=$ADMINS
EOF

# 4. Zapora (jeśli włączona): HTTP i HTTPS dla certyfikatu i aplikacji
if command -v ufw >/dev/null 2>&1 && $SUDO ufw status | grep -q "Status: active"; then
  $SUDO ufw allow 80/tcp >/dev/null; $SUDO ufw allow 443/tcp >/dev/null
fi

# 5. Start / aktualizacja
say "Buduję i uruchamiam kontenery…"
$SUDO docker compose up -d --build

# 6. Czekam na certyfikat i odpowiedź serwera
say "Czekam, aż serwer odpowie przez HTTPS (certyfikat pobiera się przy pierwszym starcie)…"
for i in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1; then
    printf '\n\033[1;32m✓ Gotowe!\033[0m Serwer kont działa: \033[1mhttps://%s\033[0m\n' "$DOMAIN"
    echo "  W aplikacji: link „zmień” pod logowaniem (albo Administrator → Serwer kont) → wpisz ten adres."
    echo "  Logi: docker compose logs -f   ·   Kopia bazy: docker compose cp api:/data/cloud.db ./kopia.db"
    exit 0
  fi
  sleep 3
done
echo
echo "Serwer nie odpowiedział przez HTTPS w 3 minuty. Sprawdź:"
echo "  • czy porty 80 i 443 są otwarte w panelu dostawcy VPS (firewall),"
echo "  • czy domena $DOMAIN wskazuje na IP tego serwera,"
echo "  • logi: docker compose logs caddy api"
exit 1
