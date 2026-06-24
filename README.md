# switchboard

Call a phone number, talk to a Claude agent. A dedicated [MaxoTel](https://www.maxo.com.au/)
SIP trunk lands on a local Asterisk, which streams the call audio over **AudioSocket**
to a small bridge: **speech → STT → Claude (Agent SDK) → TTS → speech**. Your real
phone line is never touched.

## Why it's built this way

The Docker boundary lands on the **AudioSocket TCP seam, not on RTP.**

```
   PSTN / mobile                ┌──────────── your server ────────────┐
        │                       │                                      │
   [MaxoTel DID] ──SIP/RTP──> [Asterisk] ──AudioSocket (TCP :9000)──> [agent]
                                │ (native)                          (container)
                                └─ Dial(PJSIP/ht802) ─> Grandstream HT802 ─> handset
```

- **Asterisk owns the only RTP leg** (Asterisk ↔ MaxoTel — one NAT hop, and a
  registration trunk so nothing is exposed inbound on `5060`).
- **AudioSocket carries call audio over plain TCP**, so the agent — and anything
  containerised — just speaks TCP to `localhost:9000`. RTP never crosses the
  container boundary. That's why native-Asterisk + Dockerised-agent is *correct*,
  not a workaround.

## Two ways to run

| Host | Asterisk | Agent |
|------|----------|-------|
| **macOS** (e.g. a headless Mac) | **native** — `brew install asterisk` | container *or* native venv |
| **Linux** | container, `network_mode: host` | container |

`docker compose up` is a *Linux* promise. On macOS, run `scripts/mac-setup.sh` and
Asterisk lives on the host; only the agent need be containerised.

## Call routing

- **Inbound** to the dedicated DID → caller-ID allowlist (`ALLOWED_CLID_9`, last 9
  digits, normalises `+61…/0…/61…`). Match → the agent answers; anything else is
  rejected. This number is private — it is **not** your house line.
- **Handset** → pick up the HT802 and dial `*99` → the agent.
- **No general PSTN outbound** on this trunk (toll-fraud guard). Outbound dialling
  stays on your primary line, not this one.

## Quickstart (macOS host)

```bash
cp .env.example .env          # MaxoTel creds, your mobile's last 9 digits, ANTHROPIC_API_KEY
./scripts/mac-setup.sh        # brew install asterisk + gettext, render configs from .env
asterisk -cvvv                # start Asterisk; watch it REGISTER to sip.maxo.com.au
cd agent
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python bridge.py              # AudioSocket bridge on :9000
```

Prove the transport first with `STT_PROVIDER=echo TTS_PROVIDER=echo` (you hear
yourself — like Asterisk's `Echo()` but over the socket), then swap in real
adapters in `agent/bridge.py`.

## Security

- Secrets live **only** in `.env` (gitignored). Asterisk config ships as `.template`
  and is rendered with `envsubst` — the committed repo never holds the MaxoTel
  password, `ANTHROPIC_API_KEY`, or your mobile number (PII).
- Registration trunk → nothing on `5060` to brute-force. Keep a **spend cap** on the
  MaxoTel account as a backstop against the one residual vector (outbound toll relay),
  which the dialplan also forbids.

## Layout

```
asterisk/   pjsip.conf.template (MaxoTel trunk + HT802) · extensions.conf · globals.conf.template · rtp.conf
agent/      bridge.py (AudioSocket ↔ STT ↔ Claude ↔ TTS) · Dockerfile · requirements.txt
scripts/    mac-setup.sh · render-config.sh
```

> **Status: scaffold.** AudioSocket protocol + routing are wired; STT/TTS/LLM ship as
> swappable adapters with an `echo` default. Not yet deployed.
