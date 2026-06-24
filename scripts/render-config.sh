#!/usr/bin/env bash
# Render *.template -> real config using vars from .env.
# Asterisk has no native env substitution, so we envsubst the templates. The
# rendered pjsip.conf / globals.conf are gitignored (they hold creds + PII).
set -euo pipefail
cd "$(dirname "$0")/.."
DST="${1:-asterisk}"     # where rendered files go (default: in-repo, gitignored)

[ -f .env ] || { echo "No .env — copy .env.example to .env first."; exit 1; }
set -a; . ./.env; set +a
: "${AUDIOSOCKET_PORT:=9000}"
: "${EXTERNAL_IP:=$(curl -s --max-time 5 https://api.ipify.org || echo '')}"
export EXTERNAL_IP AUDIOSOCKET_PORT

# envsubst ships with gettext (brew install gettext on macOS)
ENVSUBST="$(command -v envsubst || echo "$(brew --prefix gettext 2>/dev/null)/bin/envsubst")"
[ -x "$ENVSUBST" ] || { echo "envsubst not found — brew install gettext"; exit 1; }

mkdir -p "$DST"
"$ENVSUBST" < asterisk/pjsip.conf.template   > "$DST/pjsip.conf"
"$ENVSUBST" < asterisk/globals.conf.template > "$DST/globals.conf"
echo "switchboard: rendered pjsip.conf + globals.conf -> $DST"
