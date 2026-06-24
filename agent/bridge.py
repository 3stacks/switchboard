#!/usr/bin/env python3
"""
switchboard — AudioSocket bridge.

Asterisk's AudioSocket() app connects here over TCP and streams the live call
audio as 8 kHz, 16-bit, mono signed-linear PCM ("slin"). We turn that into text,
ask Claude, synthesize a spoken reply, and stream it back down the same socket.
RTP never reaches this process — Asterisk owns it (that's the whole point of
landing the Docker boundary on this TCP seam).

Run modes (set in .env):
  mirror   (STT_PROVIDER=echo & TTS_PROVIDER=echo — the default)
           Echo audio straight back. Proves the transport end to end: you hear
           yourself, like Asterisk's Echo(). No LLM, no API keys.
  pipeline (any real STT/TTS)
           Endpoint speech -> STT -> Claude -> TTS -> playback.

AudioSocket framing: [kind:1][length:2 big-endian][payload]
  0x00 hangup   0x01 uuid(16B)   0x03 dtmf(1B)   0x10 audio(slin)   0xff error
"""
import os
import math
import array
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("switchboard")

# --- AudioSocket protocol ---
KIND_HANGUP, KIND_UUID, KIND_DTMF, KIND_AUDIO, KIND_ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF
SAMPLE_RATE = 8000     # slin on a narrowband channel
FRAME_BYTES = 320      # 20 ms @ 8 kHz, 16-bit mono

# --- config ---
PORT = int(os.environ.get("AUDIOSOCKET_PORT", "9000"))
STT_PROVIDER = os.environ.get("STT_PROVIDER", "echo").lower()
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "echo").lower()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
MIRROR_MODE = STT_PROVIDER == "echo" and TTS_PROVIDER == "echo"

# Endpointing (very basic energy VAD — swap for webrtcvad / silero for real use).
RMS_GATE = 500          # int16 RMS above this = speech
SILENCE_HANG_MS = 800   # this much trailing silence ends the turn

SYSTEM_PROMPT = (
    "You are a voice assistant reached over a phone call. Your replies are spoken "
    "aloud, so answer in one or two short sentences. No markdown, no lists, no "
    "emoji. If a request needs a longer background job, say so briefly and confirm."
)


def rms(pcm: bytes) -> float:
    """RMS amplitude of 16-bit mono PCM (stdlib only; audioop is deprecated)."""
    if len(pcm) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in a) / len(a)) if a else 0.0


# ----------------------------- adapters --------------------------------------
class STT:
    """Speech -> text."""

    async def transcribe(self, pcm: bytes) -> str:
        if STT_PROVIDER == "deepgram":
            # TODO: send `pcm` to Deepgram (nova phonecall model handles 8 kHz).
            # For low latency, stream live rather than per-utterance. pip: deepgram-sdk
            raise NotImplementedError("wire Deepgram STT here")
        if STT_PROVIDER == "whisper":
            # TODO: faster-whisper — resample 8 kHz slin -> 16 kHz float32 first.
            raise NotImplementedError("wire faster-whisper STT here")
        raise NotImplementedError(f"unknown STT_PROVIDER={STT_PROVIDER}")


class TTS:
    """Text -> 8 kHz mono slin PCM."""

    async def synthesize(self, text: str) -> bytes:
        if TTS_PROVIDER == "say":
            return await _macos_say(text)
        if TTS_PROVIDER in ("cartesia", "elevenlabs"):
            # TODO: request 8 kHz mono PCM (or resample). Both stream — synthesize
            # per sentence and play as it arrives to cut latency.
            raise NotImplementedError(f"wire {TTS_PROVIDER} TTS here")
        raise NotImplementedError(f"unknown TTS_PROVIDER={TTS_PROVIDER}")


async def _macos_say(text: str) -> bytes:
    """Local macOS TTS: `say` renders straight to 8 kHz mono LEI16 WAV; we strip
    the 44-byte header to get raw slin. Mac host only (not in the container)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "o.wav")
        proc = await asyncio.create_subprocess_exec(
            "say", "--data-format=LEI16@8000", "-o", wav, text
        )
        await proc.wait()
        with open(wav, "rb") as f:
            data = f.read()
    return data[44:] if data[:4] == b"RIFF" else data


class ClaudeAgent:
    """One conversation per call. Streaming + adaptive thinking (Claude API skill)."""

    def __init__(self):
        from anthropic import Anthropic
        self.client = Anthropic()   # reads ANTHROPIC_API_KEY
        self.history = []           # multi-turn within the call

    async def respond(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})

        def _call():
            # Voice replies are short -> small max_tokens; effort "low" keeps the
            # turn snappy. Drop output_config if your SDK version predates it.
            with self.client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=self.history,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
            ) as stream:
                return stream.get_final_message()

        msg = await asyncio.to_thread(_call)
        # Append the full content (incl. thinking blocks) per the skill's guidance.
        self.history.append({"role": "assistant", "content": msg.content})
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# ----------------------------- framing ---------------------------------------
async def read_frame(reader: asyncio.StreamReader):
    hdr = await reader.readexactly(3)
    length = int.from_bytes(hdr[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return hdr[0], payload


async def send_audio(writer: asyncio.StreamWriter, pcm: bytes):
    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i : i + FRAME_BYTES]
        writer.write(bytes([KIND_AUDIO]) + len(chunk).to_bytes(2, "big") + chunk)
    await writer.drain()


# ----------------------------- call handling ---------------------------------
async def turn(pcm, stt, agent, tts, writer):
    try:
        text = await stt.transcribe(pcm)
        if not text.strip():
            return
        log.info("caller: %s", text)
        reply = await agent.respond(text)
        log.info("claude: %s", reply)
        await send_audio(writer, await tts.synthesize(reply))
    except NotImplementedError as e:
        log.error("pipeline stub: %s — implement an STT/TTS adapter (see .env)", e)
    except Exception:
        log.exception("turn failed")


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    mode = "mirror" if MIRROR_MODE else "pipeline"
    log.info("call connected from %s (%s mode)", peer, mode)

    agent = None if MIRROR_MODE else ClaudeAgent()
    stt, tts = (None, None) if MIRROR_MODE else (STT(), TTS())
    buf, in_speech, silence_ms = bytearray(), False, 0.0

    try:
        while True:
            try:
                kind, payload = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break

            if kind == KIND_HANGUP:
                break
            elif kind == KIND_UUID:
                log.info("call id %s", payload.hex())
            elif kind == KIND_DTMF:
                # DTMF-kind support varies by Asterisk version; may arrive in-band.
                log.info("DTMF %s", payload.decode(errors="replace"))
                # TODO: e.g. '0' -> transfer to the handset / approval flow.
            elif kind == KIND_AUDIO:
                if MIRROR_MODE:
                    await send_audio(writer, payload)   # straight echo
                    continue
                buf.extend(payload)
                if rms(payload) >= RMS_GATE:
                    in_speech, silence_ms = True, 0.0
                elif in_speech:
                    silence_ms += len(payload) / 2 / SAMPLE_RATE * 1000
                    if silence_ms >= SILENCE_HANG_MS:
                        utterance, _ = bytes(buf), buf.clear()
                        in_speech, silence_ms = False, 0.0
                        await turn(utterance, stt, agent, tts, writer)
            elif kind == KIND_ERROR:
                log.warning("AudioSocket error %s", payload.hex())
    finally:
        writer.close()
        log.info("call ended %s", peer)


async def main():
    if not MIRROR_MODE and not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set — pipeline mode needs it")
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("switchboard bridge listening on :%d (%s mode)", PORT, "mirror" if MIRROR_MODE else "pipeline")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
