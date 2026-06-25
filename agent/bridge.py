#!/usr/bin/env python3
"""
switchboard — AudioSocket bridge.

Asterisk AudioSocket() app connects here over TCP and streams the live call
audio as 8 kHz, 16-bit, mono signed-linear PCM ("slin"). We turn that into text,
ask Claude, synthesize a spoken reply, and stream it back down the same socket.

Run modes (set in .env):
  mirror   (STT_PROVIDER=echo & TTS_PROVIDER=echo — the default)
           Echo audio straight back. Proves the transport end to end.
  pipeline (any real STT/TTS)
           Endpoint speech -> STT -> Claude -> TTS -> playback.

  STT_PROVIDER=stub  — returns a fixed phrase, for testing TTS without STT.

AudioSocket framing: [kind:1][length:2 big-endian][payload]
  0x00 hangup   0x01 uuid(16B)   0x03 dtmf(1B)   0x10 audio(slin)   0xff error
"""
import os
import math
import array
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx/httpcore log full request URLs at INFO — that clutters the call log and
# would leak any API key passed as a query param. Keep them at WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
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

# Endpointing (energy VAD).
RMS_GATE = 1500         # int16 RMS above this = speech (line noise sits below 1000)
SILENCE_HANG_MS = 600   # trailing silence that ends the turn
MAX_UTTERANCE_MS = 10000  # force endpoint if speech runs longer than this

SYSTEM_PROMPT = (
    "You are a voice assistant reached over a phone call. Your replies are spoken "
    "aloud, so answer in one or two short sentences. No markdown, no lists, no "
    "emoji. If a request needs a longer background job, say so briefly and confirm."
)


def rms(pcm: bytes) -> float:
    if len(pcm) < 2:
        return 0.0
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(s * s for s in a) / len(a)) if a else 0.0


def resample_8k_to_16k(pcm_8k: bytes) -> bytes:
    """Linear interpolation 8kHz slin -> 16kHz slin (2x) for Whisper (needs 16kHz)."""
    a = array.array("h")
    a.frombytes(pcm_8k[: len(pcm_8k) // 2 * 2])
    out = array.array("h")
    for i in range(len(a) - 1):
        out.append(a[i])
        out.append((a[i] + a[i + 1]) // 2)
    if a:
        out.append(a[-1])
        out.append(a[-1])
    return out.tobytes()


# ----------------------------- STT ------------------------------------------
class STT:
    """Speech -> text."""

    def __init__(self):
        self._whisper = None

    def _get_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            log.info("loading whisper model (base)...")
            self._whisper = WhisperModel("base", device="cpu", compute_type="int8")
            log.info("whisper model loaded")
        return self._whisper

    async def transcribe(self, pcm: bytes) -> str:
        if STT_PROVIDER == "stub":
            return "Hello, this is a test of the text to speech system."
        if STT_PROVIDER == "whisper":
            return await self._transcribe_whisper(pcm)
        if STT_PROVIDER == "deepgram":
            return await self._transcribe_deepgram(pcm)
        raise NotImplementedError(f"unknown STT_PROVIDER={STT_PROVIDER}")

    async def _transcribe_whisper(self, pcm_8k: bytes) -> str:
        import numpy as np
        model = self._get_whisper()
        pcm_16k = resample_8k_to_16k(pcm_8k)
        samples = array.array("h")
        samples.frombytes(pcm_16k)
        audio = np.array(samples, dtype=np.float32) / 32768.0

        def _transcribe():
            segments, _ = model.transcribe(audio, language="en", vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.to_thread(_transcribe)

    async def _transcribe_deepgram(self, pcm_8k: bytes) -> str:
        """Deepgram pre-recorded STT. The endpointed utterance is already 8 kHz
        mono slin — exactly Deepgram's raw linear16 input, so no conversion."""
        import httpx
        params = {
            "model": os.environ.get("DEEPGRAM_MODEL", "nova-3"),
            "language": os.environ.get("DEEPGRAM_LANGUAGE", "en"),
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "channels": "1",
            "punctuate": "true",
            "smart_format": "true",
        }
        headers = {"Authorization": "Token " + os.environ["DEEPGRAM_API_KEY"]}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.deepgram.com/v1/listen",
                params=params, headers=headers, content=pcm_8k,
            )
            r.raise_for_status()
            data = r.json()
        alts = data["results"]["channels"][0]["alternatives"]
        return alts[0]["transcript"].strip() if alts else ""


# ----------------------------- TTS ------------------------------------------
class TTS:
    """Text -> 8 kHz mono slin PCM."""

    def __init__(self):
        self._piper = None

    def _get_piper(self):
        if self._piper is None:
            import piper
            model_path = os.environ.get("PIPER_VOICE", os.path.join(
                os.path.dirname(__file__), "voices", "en_GB-alan-low.onnx"))
            log.info("loading piper voice: %s", model_path)
            self._piper = piper.PiperVoice.load(model_path)
            log.info("piper voice loaded")
        return self._piper

    async def synthesize(self, text: str) -> bytes:
        if TTS_PROVIDER == "say":
            return await _macos_say(text)
        if TTS_PROVIDER == "piper":
            return await self._synthesize_piper(text)
        if TTS_PROVIDER == "google":
            return await self._synthesize_google(text)
        raise NotImplementedError(f"unknown TTS_PROVIDER={TTS_PROVIDER}")

    async def _synthesize_piper(self, text: str) -> bytes:
        import array as _array
        model = self._get_piper()

        def _synth():
            chunks = list(model.synthesize(text))
            pcm_16k = b"".join(c.audio_int16_bytes for c in chunks)
            a = _array.array("h")
            a.frombytes(pcm_16k[:len(pcm_16k)//2*2])
            # decimate 16kHz -> 8kHz
            return _array.array("h", a[::2]).tobytes()

        return await asyncio.to_thread(_synth)

    async def _synthesize_google(self, text: str) -> bytes:
        """Google Cloud TTS via REST (API-key auth). Request LINEAR16 @ 8 kHz so
        the reply is telephony-rate mono slin already — no resampling. LINEAR16
        comes back as a WAV container, so reuse the data-chunk parser."""
        import base64
        import httpx
        api_key = os.environ.get("GOOGLE_CLOUD_API_KEY") or os.environ.get("GOOGLE_TTS_API_KEY")
        if not api_key:
            raise RuntimeError("set GOOGLE_CLOUD_API_KEY (or GOOGLE_TTS_API_KEY) in .env")
        voice = os.environ.get("GOOGLE_TTS_VOICE", "en-AU-Neural2-B")
        lang = os.environ.get("GOOGLE_TTS_LANG", "-".join(voice.split("-")[:2]))
        body = {
            "input": {"text": text},
            "voice": {"languageCode": lang, "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://texttospeech.googleapis.com/v1/text:synthesize",
                headers={"X-Goog-Api-Key": api_key}, json=body,
            )
            r.raise_for_status()
            audio = base64.b64decode(r.json()["audioContent"])
        return _extract_wav_pcm(audio)


def _extract_wav_pcm(wav_bytes: bytes) -> bytes:
    """Find the data chunk in a WAV file and return raw PCM. macOS say emits
    JUNK + fmt + FLLR chunks before data, so a fixed 44-byte strip is wrong."""
    import struct
    if wav_bytes[:4] != b"RIFF":
        return wav_bytes
    offset = 12
    while offset < len(wav_bytes) - 8:
        chunk_id = wav_bytes[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        if chunk_id == b"data":
            return wav_bytes[offset + 8: offset + 8 + chunk_size]
        offset += 8 + chunk_size + (chunk_size % 2)
    return wav_bytes[44:]


async def _macos_say(text: str) -> bytes:
    """macOS say -> 8 kHz mono LEI16 WAV -> extract data chunk -> raw slin."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "o.wav")
        proc = await asyncio.create_subprocess_exec(
            "say", "--data-format=LEI16@8000", "-o", wav, text
        )
        await proc.wait()
        with open(wav, "rb") as f:
            data = f.read()
    return _extract_wav_pcm(data)


# ----------------------------- Claude ---------------------------------------
class ClaudeAgent:
    """One conversation per call."""

    def __init__(self):
        from anthropic import Anthropic
        self.client = Anthropic()
        self.history = []

    async def respond(self, text: str) -> str:
        self.history.append({"role": "user", "content": text})

        def _call():
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
        self.history.append({"role": "assistant", "content": msg.content})
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# ----------------------------- framing ---------------------------------------
async def read_frame(reader: asyncio.StreamReader):
    hdr = await reader.readexactly(3)
    length = int.from_bytes(hdr[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return hdr[0], payload


async def send_audio(writer: asyncio.StreamWriter, pcm: bytes):
    """Stream slin back to Asterisk, paced at ~20 ms/frame (real time).

    app_audiosocket drains this socket with no pacing of its own — whenever the
    fd is readable it reads one frame and ast_write()s it straight to RTP — so a
    burst of frames becomes a burst of RTP packets that overflows the carrier's
    jitter buffer downstream, and the caller hears only the first word.
    Pace *between* frames (not after each) so the per-frame mirror-mode path,
    already paced by 20 ms RTP arrival, gets no extra sleep."""
    for i in range(0, len(pcm), FRAME_BYTES):
        if i:
            await asyncio.sleep(0.02)
        chunk = pcm[i : i + FRAME_BYTES]
        writer.write(bytes([KIND_AUDIO]) + len(chunk).to_bytes(2, "big") + chunk)
        await writer.drain()


async def flush_backlog(reader: asyncio.StreamReader) -> bool:
    """Discard audio that piled up while turn() was busy (STT + Claude + speaking).

    turn() blocks the read loop for seconds, and Asterisk keeps streaming caller
    audio — plus any line-echo of our own reply — the whole time. That backlog
    sits in the socket buffer; if the next turn reads it, the stale audio pads the
    utterance and can trip MAX_UTTERANCE_MS mid-sentence, so the caller's real
    follow-up comes back truncated. Backlogged frames are already buffered and
    read instantly; once we catch up to live audio a read costs ~20 ms (one RTP
    frame interval), which is the signal that we're current again. Returns True if
    the call hung up while flushing."""
    loop = asyncio.get_event_loop()
    dropped = 0
    while dropped < 3000:  # ~60 s ceiling; a real backlog is far smaller
        t = loop.time()
        try:
            kind, _ = await read_frame(reader)
        except asyncio.IncompleteReadError:
            return True
        if kind == KIND_HANGUP:
            return True
        if loop.time() - t > 0.015:  # arrived in ~real time -> caught up to live
            break
        dropped += 1
    if dropped:
        log.info("flushed %d backlog frame(s) after reply", dropped)
    return False


# ----------------------------- call handling ---------------------------------
async def turn(pcm, stt, agent, tts, writer):
    try:
        text = await stt.transcribe(pcm)
        if not text.strip():
            return
        log.info("caller: %s", text)
        reply = await agent.respond(text)
        log.info("claude: %s", reply)
        audio = await tts.synthesize(reply)
        log.info("tts: synthesized %d bytes (%.1fs)", len(audio), len(audio)/2/8000)
        await send_audio(writer, audio)
        log.info("tts: sent to asterisk")
    except NotImplementedError as e:
        log.error("pipeline stub: %s", e)
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
                log.info("DTMF %s", payload.decode(errors="replace"))
            elif kind == KIND_AUDIO:
                if MIRROR_MODE:
                    await send_audio(writer, payload)
                    continue
                buf.extend(payload)
                buf_ms = len(buf) / 2 / SAMPLE_RATE * 1000
                if rms(payload) >= RMS_GATE:
                    in_speech, silence_ms = True, 0.0
                elif in_speech:
                    silence_ms += len(payload) / 2 / SAMPLE_RATE * 1000
                if in_speech and (silence_ms >= SILENCE_HANG_MS or buf_ms >= MAX_UTTERANCE_MS):
                        utterance, _ = bytes(buf), buf.clear()
                        in_speech, silence_ms = False, 0.0
                        await turn(utterance, stt, agent, tts, writer)
                        # Drop audio buffered while we were busy/speaking, so the
                        # caller's next turn starts from live audio (not stale
                        # backlog that would truncate it). Bail if they hung up.
                        if await flush_backlog(reader):
                            break
                        buf.clear()
                        in_speech, silence_ms = False, 0.0
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
