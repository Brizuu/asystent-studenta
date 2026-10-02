#!/usr/bin/env bash
# Jedna komenda na czystym serwerze (pobiera kod z GitHuba i uruchamia deploy.sh):
#   curl -fsSL https://raw.githubusercontent.com/Brizuu/asystent-studenta/main/cloud/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/Brizuu/asystent-studenta/main/cloud/install.sh | bash -s -- konta.twojadomena.pl
set -euo pipefail
DIR=/opt/asystent
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
command -v git >/dev/null 2>&1 || { $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git; }
if [ -d "$DIR/.git" ]; then $SUDO git -C "$DIR" pull --ff-only; else $SUDO git clone --depth 1 https://github.com/Brizuu/asystent-studenta.git "$DIR"; fi
exec bash "$DIR/cloud/deploy.sh" "$@"
