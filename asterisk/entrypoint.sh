#!/usr/bin/env bash
# Linux/Docker path: render switchboard templates from env, then run Asterisk
# in the foreground. Templates are mounted read-only at /etc/asterisk-template.
set -euo pipefail
TPL=/etc/asterisk-template
DST=/etc/asterisk

: "${EXTERNAL_IP:=$(curl -s --max-time 5 https://api.ipify.org || echo '')}"
: "${AUDIOSOCKET_PORT:=9000}"
export EXTERNAL_IP AUDIOSOCKET_PORT

# static configs
for f in extensions.conf rtp.conf; do
  [ -f "$TPL/$f" ] && cp -f "$TPL/$f" "$DST/$f"
done
# rendered configs (secrets / PII injected from env)
envsubst < "$TPL/pjsip.conf.template"   > "$DST/pjsip.conf"
envsubst < "$TPL/globals.conf.template" > "$DST/globals.conf"

echo "switchboard: rendered pjsip.conf + globals.conf; starting Asterisk"
exec asterisk -f -vvvv
