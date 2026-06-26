# switchboard — development log

A blow-by-blow account of getting a phone call to a Claude agent working, from
scaffold to first conversation. Written as a record of the dead-ends, red
herrings, and breakthroughs — because the interesting parts of a project like
this are never the parts that worked first try.

## 1. The scaffold (starting point)

The repo shipped as a well-architected scaffold: Asterisk config templates, an
AudioSocket bridge with echo/mirror mode, and stubbed STT/TTS adapters. The core
design decision — landing the Docker boundary on the AudioSocket TCP seam rather
than on RTP — was already correct and didn't need changing. The work was all in
getting it actually running and swapping the stubs for real adapters.

## 2. Asterisk on macOS — the build problem

Homebrew dropped Asterisk from core. The repo includes `scripts/build-asterisk-macos.sh`
which compiles Asterisk 22.2.0 from source on Apple Silicon, patching pjproject
and Asterisk themselves (adapted from `nedimzecic/asterisk-macos`). Key tweaks:
prefix into `$HOME/asterisk` (no sudo), non-interactive menuselect, and explicitly
enabling `app_audiosocket` / `res_audiosocket` modules.

**First gotcha:** the built Asterisk couldn't load `res_rtp_asterisk.so` (and
half the PJSIP stack) because `libpjsua2.dylib.2` wasn't found at runtime. The
fix: start Asterisk with `DYLD_LIBRARY_PATH=$HOME/asterisk/lib`. Without this,
Asterisk could process SIP signaling but had no RTP engine — calls would connect
but carry no audio.

## 3. Mirror mode — proving the transport

The first goal was echo mode: call the DID, hear yourself back. This proves the
full transport path (PSTN → MaxoTel → SIP → Asterisk → AudioSocket → bridge →
back) without needing any API keys.

### 3a. The dialplan was edited during debugging

Someone had replaced `AudioSocket(...)` in `extensions.conf` with `Echo()` (the
built-in Asterisk echo app) to test native echo, and left stray `same => n,Echo()`
lines in the file header causing a parse error. Restored the original
`extensions.conf` from the repo and reloaded the dialplan.

### 3b. SIP registration alive, but no inbound calls

Asterisk showed `Registered` to MaxoTel, but calls to the DID rang out — no
INVITE arrived at Asterisk. The PJSIP history was empty.

**Root cause:** the registration's `Contact` header advertised
`sip:732014@150.228.151.241:5060`, but the router's NAT was mapping the source
port to a random high port (e.g. `:18866`). MaxoTel sent the INVITE to `:5060`
(per the Contact), but nothing was listening there on the router — the NAT
pinhole was on `:18866`.

**Fix:** port-forward UDP 5060 on the router to the Mac. After this, INVITEs
arrived and calls connected.

### 3c. Call connects, but no audio (0 RTP frames)

The bridge logged `call connected` + `call id` but `audio frames=0, bytes=0`.
The AudioSocket TCP connection was established, but Asterisk sent zero audio
frames to the bridge. RTP debug (`rtp set debug on`) showed 0 `Got RTP` lines.

**Red herring — codec translation:** initially suspected that AudioSocket needed
slin format and the call was negotiating PCMU/PCMA. But `core show translation`
showed all codec paths were available (ulaw→slin8 works), and the AudioSocket
app handles transcoding internally. Not the issue.

**Red herring — `CHANNEL(audionativeformat)=slin`:** tried forcing the channel
format in the dialplan. The `Set()` failed with "Unknown or unavailable item" —
it's read-only. Not the issue, just a warning.

**Real cause — RTP port forwarding:** Asterisk answered with SDP
`m=audio 10090 RTP/AVP 0 8 101`, telling MaxoTel to send RTP to
`150.228.151.241:10090`. But the router was only forwarding UDP 5060 (SIP), not
the RTP range (10000-10100). Added a port-forward for UDP 10000-10100.

### 3d. Still no audio — the two-interface problem

The Mac had two active network interfaces:
- `en0` (Wi-Fi) — 192.168.0.227
- `en6` (Ethernet dongle) — 192.168.0.160

The router's port-forward pointed to .160 (en6), but Asterisk was binding on
`0.0.0.0` so it should have received on both. To eliminate ambiguity, Wi-Fi was
disabled, leaving only en6 (.160).

### 3e. SIP ALG

Disabled SIP ALG on the router (TP-Link AX6000). SIP ALG mangles SDP and breaks
RTP. This didn't fix the issue on its own, but was necessary.

### 3f. SPI firewall

Disabled the SPI (Stateful Packet Inspection) firewall on the router. Didn't fix
it either.

### 3g. The router port-forward wasn't working at all

Tested by running `nc -u -l 10000` on the Mac and sending a UDP packet to the
public IP:10000 from outside. The packet never arrived. Even port 5060 failed
the same test when Asterisk was stopped. This meant the router's port-forward
rules weren't actually forwarding WAN→LAN for *any* UDP port.

**Discovery — how SIP was actually working:** SIP only worked because Asterisk's
outbound REGISTER created a stateful NAT pinhole through all NAT layers. The
INVITE rode back through that pinhole. The port-forward rules were doing nothing.

### 3h. The real blocker — triple NAT + CGNAT

A traceroute revealed the actual network topology:

```
Mac (.160) → TP-Link (192.168.0.1) → Starlink router (192.168.1.1) → CGNAT (100.64.0.1) → public (150.228.151.241)
```

Three layers of NAT, with `100.64.0.0/10` being **CGNAT** (carrier-grade NAT) —
Starlink shares one public IP among multiple customers. This means:
- Port-forwarding on the TP-Link is useless (CGNAT blocks all inbound)
- Port-forwarding on the Starlink router is useless (same reason)
- SIP works only via the outbound registration pinhole
- RTP has no pinhole and cannot work through CGNAT

### 3i. Fixing RTP through CGNAT — the keepalive trick

The same trick that makes SIP work (outbound pinhole) can be applied to RTP.
Configured:
- `rtpkeepalive=5` in `rtp.conf` (Asterisk sends RTP keepalive packets every 5s)
- `rtp_keepalive=5` on the endpoint config
- `strictrtp=no` (accept RTP from any source, since CGNAT may remap the source)
- Short registration interval (`expiration=30`) to keep the SIP pinhole alive

After all this, **mirror mode worked** — calling the DID and hearing yourself
echoed back. The full transport was proven.

**Note on CGNAT reliability:** the CGNAT pinhole is flaky. Calls work when the
pinhole is fresh but drop after ~30s if the registration expires. The permanent
fix is either a public IP from Starlink or a VPS relay (Asterisk on a VPS with
a real public IP, AudioSocket bridge over Tailscale/WireGuard to the Mac).

## 4. Wiring real STT/TTS

### 4a. Provider selection

Chose fully local, on-device STT/TTS (no API costs, no cloud dependency):
- **STT:** `faster-whisper` (base model, CPU, int8)
- **TTS:** Piper (neural TTS, runs locally, outputs raw PCM)

### 4b. The venv gotcha

The bridge was started with system Python (`/usr/bin/python3`) which didn't have
the installed dependencies. Had to explicitly use the venv:
`~/Sites/switchboard/agent/.venv/bin/python`. Also, the bridge reads env vars
from `os.environ` — the `.env` file must be sourced before starting the bridge
(`set -a && . ./.env && set +a`).

### 4c. macOS `say` TTS — the WAV chunk bug

The original TTS adapter used macOS `say` and stripped a fixed 44-byte WAV header.
But `say` emits a non-standard WAV with `JUNK` + `fmt ` + `FLLR` chunks before
the `data` chunk — audio starts at offset 4096, not 44. The fixed strip was
cutting into the fmt chunk and corrupting the audio. Fixed by properly parsing
WAV chunks to find the `data` chunk.

### 4d. Piper TTS — a better local option

Switched to Piper for more reliable TTS. Piper outputs 16kHz PCM natively; the
adapter decimates to 8kHz for AudioSocket. A voice model
(`en_GB-alan-low.onnx`) is downloaded from the rhasspy/piper-voices HuggingFace
repo into `agent/voices/`.

### 4e. Whisper STT

Implemented `faster-whisper` adapter: resamples 8kHz slin to 16kHz (linear
interpolation), converts to float32, runs Whisper with VAD filter. The `base`
model loads in ~9s on first call (then cached). Transcription quality is good
for short utterances.

### 4f. VAD tuning

The energy-based VAD (RMS gate) had issues:
- `RMS_GATE=500` was too low — line noise registered as speech
- One utterance collected 37 seconds of "speech" (mostly silence) before
  endpointing, because the gate never went below threshold
- Raised to `RMS_GATE=1500`, added `MAX_UTTERANCE_MS=10000` as a safety valve
- Whisper's built-in VAD filter (`vad_filter=True`) helps clean up the audio
  before transcription

## 5. TTS truncation — root cause and fix

The full pipeline works: STT transcribes, Claude responds, TTS synthesizes valid
audio (verified — correct PCM, real content, 3-4s duration). The bridge logs
`tts: synthesized 63584 bytes (4.0s)` and `tts: sent to asterisk`. But the caller
only heard the first word.

**Root cause (confirmed against the Asterisk 22.2.0 source).** `send_audio`
wrote all ~199 frames into the TCP socket in a tight loop, then drained once.
Reading `apps/app_audiosocket.c`, the playback side has *no pacing of its own*:

```c
ms = -1;
targetChan = ast_waitfor_nandfds(&chan, 1, &svc, 1, NULL, &outfd, &ms);
...
if (outfd >= 0) {                       /* socket fd is readable */
    f = ast_audiosocket_receive_frame(svc);   /* read exactly one frame */
    ast_write(chan, f);                 /* push straight to RTP — no 20ms gate */
}
```

`ast_waitfor_nandfds` returns the instant the socket is readable, so when a burst
is sitting in the buffer the loop spins — one `ast_audiosocket_receive_frame` +
`ast_write` per iteration, back to back. `ast_write` hands each frame to the RTP
stack immediately, so 4 seconds of audio leaves Asterisk as a near-instant burst
of ~199 RTP packets. The **carrier/handset jitter buffer downstream overflows**,
plays out the first ~100-200 ms, and discards the rest. Hence "first word only."

This is also exactly why **mirror mode always worked**: there, frames are echoed
one-per-incoming-frame, and incoming frames are paced at 20 ms by RTP arrival —
so the return path is paced for free. The bug only shows when the bridge
*generates* audio faster than real time and dumps it.

**Fix (applied).** Pace `send_audio` at ~20 ms/frame so the RTP egress is
real-time and the jitter buffer never overflows. The sleep goes *between* frames
(skipped before the first), so the per-frame mirror-mode path — already paced by
RTP arrival — gets no extra latency:

```python
for i in range(0, len(pcm), FRAME_BYTES):
    if i:
        await asyncio.sleep(0.02)       # pace at real time; first frame is immediate
    chunk = pcm[i : i + FRAME_BYTES]
    writer.write(bytes([KIND_AUDIO]) + len(chunk).to_bytes(2, "big") + chunk)
    await writer.drain()
```

Sleeping *slightly more* than 20 ms (event-loop overhead) is safe — it just makes
playback a hair slower than real time, which the jitter buffer absorbs as comfort
noise; only sleeping *less* would re-introduce the burst. Bridge restarted on the
venv interpreter with `.env` sourced and logs to `agent/bridge.log`.

Runtime-verified phone-free by driving the real `send_audio` with a fake writer:
a 3.97s clip (the 63584-byte case above) now takes 4.33s to stream — i.e. paced
to real time — while a single-frame mirror send still returns in 0.0 ms, so the
echo path is unregressed. **Confirmed on a live call** — the caller now hears the
full reply. On `bridge.log`, the tell is the gap between `tts: synthesized …` and
`tts: sent to asterisk`, now ≈ the reply's duration (was ~instant).

## 6. Multi-turn truncation — the backlog, and switching TTS to `say`

With pacing fixed the *first* reply played in full — but **every reply after it
came back truncated**: the agent answered the opening question, then met each
follow-up with "I think your request got cut off." Same root shape as the pacing
bug, one layer up. `turn()` runs inline in the read loop, so for the several
seconds of STT + Claude + (now real-time) playback the bridge stops reading the
socket. Asterisk keeps streaming caller audio — plus any line-echo of our own
reply — and it piles up in the socket buffer. The next turn reads that backlog as
the *head* of the utterance: it pads the buffer and trips `MAX_UTTERANCE_MS`
mid-sentence, so the caller's real follow-up is cut off. Turn 1 works only because
it starts from an empty buffer.

**Fix (applied).** After each reply, `flush_backlog()` drains everything queued on
the socket before listening again, then VAD state is reset. It deliberately does
*not* wrap the read in `wait_for` (a timeout could cancel mid-frame and desync the
AudioSocket stream) — it reads whole frames and uses *timing* as the stop signal:
backlogged frames are already buffered and read instantly, so once a read costs
~20 ms (one live RTP frame interval) we've caught up and stop. Unit-tested against
a fake reader: drops a 200-frame backlog in ~20 ms, stops at the first live frame,
honors hangup. *Pending live confirmation* — the tell is the
`flushed N backlog frame(s)` log line: **N in the hundreds** = working as designed;
**N single-digit** = it broke early (a buffered read crossed the 15 ms threshold —
switch to a buffer-emptiness drain rather than nudging the number); **N > 500** =
it's eating live audio and turn-2 openings will clip.

**TTS switched back to `say`.** Piper worked, but `say --data-format=LEI16@8000`
(verified mono 8 kHz 16-bit, so the 320-byte/20 ms pacing math is unchanged)
sounded better on the line. The adapter and WAV-chunk parser from §4c were already
there — just an `.env` flip + restart.

**Still open: barge-in.** `flush_backlog` discards anything said *during* a reply,
so the caller must let each reply finish before speaking. Real barge-in means
playing in a background task and interrupting on detected speech — the next step.

## 7. Going cloud — Deepgram STT + Google TTS

`base` Whisper on 8 kHz phone audio mishears badly (you had to over-enunciate),
and `say`/Piper aren't assistant-grade voices. Weighed a local upgrade (Silero
VAD + Parakeet V3 / GPU Whisper — genuinely viable for a personal line, and the
VAD swap alone would fix the word-chopping) against cloud, and chose cloud for the
telephony tuning. Both dropped in as adapters beside the existing dispatch:

- **STT → Deepgram** (`nova-3`, pre-recorded). The endpointed utterance is already
  8 kHz mono slin — Deepgram's raw `linear16` input — so it POSTs straight through,
  no conversion. `DEEPGRAM_API_KEY`, `DEEPGRAM_MODEL` (try `nova-2-phonecall`).
- **TTS → Google Cloud** (`en-AU-Neural2-B` default). Request `LINEAR16 @ 8 kHz`
  and it returns telephony-rate mono in a WAV container — reuse §4c's chunk parser,
  no resampling. `GOOGLE_CLOUD_API_KEY`, `GOOGLE_TTS_VOICE` (e.g. a Chirp 3 HD voice
  for the most natural sound).

Verified phone-free with the real keys by round-tripping: Google synthesises a
pangram → Deepgram transcribes it back verbatim. Both adapters use `httpx` (already
pulled in by the anthropic SDK).

**Gotcha — API key in the logs.** Passing the Google key as a `?key=` query param
leaked it: `httpx` logs full request URLs at INFO, so every synth would have
written the key into `bridge.log`. Fixed by sending it as the `X-Goog-Api-Key`
header instead, and pinning `httpx`/`httpcore` loggers to WARNING (also keeps the
call log clean). Deepgram was already safe — it authenticates via an
`Authorization` header, which isn't part of the logged URL.

Still batch STT (the RMS gate still does endpointing). Streaming Deepgram — its
own VAD + partials + lower latency — is the next step and would retire the energy
gate entirely.

## 8. The pivot — a coding agent you phone, agent-agnostic, on a subscription

The real goal surfaced here: switchboard isn't a generic voice assistant, it's a
voice front-end to a *coding agent* on this Mac — call in, delegate work on the real
repos, get told when it's done. That reframed the brain from the raw Messages API to
a proper agent SDK and fixed the design: **agent-agnostic** (so opencode/OpenRouter
can stand in for Claude), **resume the most-recent session**, and fire-and-forget
delivered by **muted-hold** — you stay on the one call, muted, and the answer plays
back when the agent finishes.

**Agent-agnostic layer (`agent/coding_agent.py`).** Same swappable-adapter shape as
STT/TTS: `AGENT_PROVIDER=claude|opencode` → a `CodingAgent.run(text)` that works in a
fixed `cwd` and returns a short *spoken* summary.
- `claude` → Claude Agent SDK (`query()` + `resume=session_id`,
  `permission_mode="bypassPermissions"` since no one's at a keyboard mid-call). Session
  id persisted to `state/agent-session.json`; "new session" clears it.
- `opencode` → `opencode run -c` (its `--continue` *is* resume-most-recent) over
  OpenRouter. Wired; verify the output parse + set a model when an OpenRouter key is in.

**Toolchain (Phase 0).** The SDK needs Python 3.10+ and Node; the box had Python 3.9
and no Node. Installed Node + Python 3.12 + the claude & opencode CLIs and built
`agent/.venv312` *alongside* the working `.venv`, so the live bridge kept running until
cutover.

**Subscription auth, not API credits.** An autonomous Opus agent on per-token billing
gets expensive fast. `claude setup-token` mints a long-lived OAuth token tied to the
Claude subscription; we set `CLAUDE_CODE_OAUTH_TOKEN` and — because a set
`ANTHROPIC_API_KEY` *takes precedence* — scrub the API key from the process at startup.
The startup log asserts it: `oauth_token=True anthropic_api_key=False`.

**Muted-hold flow (`bridge.py`).** A turn no longer blocks the read loop on the agent
(which can run for minutes). On an endpointed utterance: STT → speak a quick *"on it"*
→ launch `agent.run()` as a **background task tracked module-level so it outlives the
call**. The read loop keeps running on your muted silence (a pre-roll cap stops the
hold from bloating the next utterance); when the task finishes it takes a write-lock
and plays the result to whatever call is live — else persists it
(`state/last-result.json`) to speak on your next call if you'd hung up.

**The bug that mattered.** The "one task at a time" guard had to be **module-level,
not per-call**. A per-call flag resets on a callback: delegate A → hang up (A keeps
running) → call back → fresh handle, guard clear → delegate B → A and B both `resume=`
the same session and corrupt it. The cross-call guard plus three other non-call
behaviors (persist→deliver-next-call, "new session", pre-roll cap) were unit-tested on
`.venv312` before cutover.

**Live.** Cut `run-agent.sh` over to `.venv312`, restarted. First real call: asked a
question, heard *"On it, I'll report back,"* then heard the answer — and a second
question in the same call resumed context. Phone → STT → agent → TTS → phone, on the
subscription, end to end.

## 9. Keyword commands — a wake-word grammar in front of the agent

Rather than pipe every utterance to the LLM, the bridge now parses a small command
grammar first. An utterance starting with the wake word **"switchboard"** is a command
handled in `dispatch()` *before* any agent runs; anything else is piped to the **active
session**.

- `switchboard status` → spoken health: operational + active session + Deepgram balance
  (Management API; set `DEEPGRAM_PROJECT_ID` to pin the project). The Claude
  subscription-limit % stays out until there's a real source — no fake numbers.
- `switchboard session start [<context>] [<agent>]` → activates a session: a context maps
  to a cwd (`personal`→`~/personal`, …), the agent picks the backend (claude | opencode,
  validated). v1 no-args → `default` / `~/Sites` / claude.
- Bare speech → routed to the active session's agent (cwd + resume from the store); no
  active session → a prompt to start one.

State lives in **SQLite** (`state/switchboard.db`, opened via `contextlib.closing` so a
long-lived bridge doesn't leak connections) — one active session for v1, schema ready
for multi-session. `coding_agent.run(text, cwd, session_id)` became per-session; the
store owns the session id.

Verified offline (store, status, session-start, routing, unknown-agent reject,
agent-error-spoken, no-session), then **proven live in the most fitting way possible:
the phone agent committed this very feature** — *"session start"*, then *"commit the
keyword-command work"* → it branched and committed `agent/{bridge,coding_agent,
sessions}.py` itself, over the phone. The call dropped on the way out and the muted-hold
**fallback persisted the result for the next call** — which surfaced a bug: an abrupt
drop raised an unhandled `BrokenPipeError`/`ConnectionResetError` in the read loop (only
`IncompleteReadError` was caught). Fixed: the read loop, `flush_backlog`, and `_speak`
now treat any `ConnectionError`/`OSError` as a clean hangup.

## Roadmap

- **Multi-session management** — today it's a single resume-most-recent session.
  Add named / per-project sessions: *"work on lector"* / *"switch to switchboard"* sets
  `cwd` to that repo and resumes (or forks) its own session id; *"list sessions"*. A
  `{project → session_id}` store mapped onto the SDK's `resume=` (and opencode's `-s`).
- **Keyword commands** ✓ — done in v1: `status`, `session start`, active-session
  routing (see §9). Remaining verbs: *"stop"/"cancel"* (abort `_current_task`),
  *"repeat"* (replay last result), *"switch to <project>"* / *"list sessions"* (with
  multi-session), and DTMF→command mapping (the bridge already receives DTMF).
- **Streaming Deepgram** — replace the energy-RMS VAD with Deepgram's streaming
  endpointing (partials + lower latency); retires the gate and most of `flush_backlog`.
- **opencode backend** — verify the `opencode run` output parse and pick an OpenRouter
  model (wired, not yet exercised).
- **Filesystem scoping** — the agent runs `bypassPermissions` over `~/Sites`; consider
  the SDK `sandbox` / `add_dirs` to bound a misfire.
- **Spoken-summary prompt** — lean it more "coding assistant" than "general Q&A".

## Architecture (as built)

```
   PSTN / mobile                ┌──────────── your server ────────────┐
        │                       │                                      │
   [MaxoTel DID] ──SIP/RTP──> [Asterisk] ──AudioSocket (TCP :9000)──> [bridge.py]
                                │ (native, macOS)                      (Python venv)
                                │                                        │
                                │                          ┌───────────┴───────────┐
                                │                          │                       │
                                │                    faster-whisper            Piper TTS
                                │                    (STT, 8k→16k)        (16k→8k decimate)
                                │                          │                       │
                                │                          └─────► Claude ◄────────┘
                                │                            (Anthropic API)
                                │
                                └── CGNAT (Starlink) ── RTP keepalive punches pinhole
```

## Key lessons

1. **AudioSocket is the right seam.** Keeping RTP in Asterisk and only crossing
   to TCP for the agent pipeline is correct. No RTP-in-Docker, no NAT complexity
   for the agent.

2. **CGNAT is the enemy of SIP/RTP.** Starlink's carrier-grade NAT blocks all
   inbound traffic. SIP survives via outbound registration pinholes; RTP needs
   the same trick (`rtpkeepalive`). The only permanent fix is a public IP or a
   VPS relay.

3. **`say` emits non-standard WAV files.** Don't assume a 44-byte header.
   Parse the chunks properly.

4. **Asterisk on macOS needs `DYLD_LIBRARY_PATH`.** The source build puts
   `libpjsua2.dylib.2` in `$HOME/asterisk/lib`, which isn't in the default
   dyld search path.

5. **Test each layer independently.** Mirror mode (echo) proved the transport
   before touching STT/TTS. A `stub` STT provider (fixed phrase) proved the TTS
   path before wiring real speech recognition. Each layer was verified in
   isolation before composing them.

6. **VAD is hard.** Energy-based endpointing is fragile. Line noise, codec
   artifacts, and silence detection thresholds all interact. A real VAD
   (webrtcvad, Silero, or streaming STT with built-in endpointing) is needed
   for production.
