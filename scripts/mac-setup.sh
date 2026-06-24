#!/usr/bin/env bash
# switchboard — native Asterisk on macOS (Apple Silicon).
# SIP/RTP in Docker-for-Mac is painful, so Asterisk runs natively and owns the
# only RTP leg (Asterisk <-> MaxoTel). The agent speaks AudioSocket (TCP) to it.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
brew list asterisk >/dev/null 2>&1 || brew install asterisk
brew list gettext  >/dev/null 2>&1 || brew install gettext    # provides envsubst

[ -f .env ] || { echo "Copy .env.example to .env and fill it in first."; exit 1; }

ASTDIR="$(brew --prefix)/etc/asterisk"
mkdir -p "$ASTDIR"

# render templated configs (pjsip.conf, globals.conf) from .env into the Asterisk dir
./scripts/render-config.sh "$ASTDIR"

# install the static configs
cp asterisk/extensions.conf "$ASTDIR/extensions.conf"
cp asterisk/rtp.conf        "$ASTDIR/rtp.conf"

cat <<'EOF'

switchboard: Asterisk configured.

  Start Asterisk:   asterisk -cvvv         (watch for "Registered to sip.maxo.com.au")
  Then the bridge:  cd agent && python3 -m venv .venv && . .venv/bin/activate \
                      && pip install -r requirements.txt && python bridge.py

EOF
