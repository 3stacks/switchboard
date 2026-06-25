"""
switchboard — agent-agnostic coding-agent layer.

A pluggable backend that takes a transcribed instruction, does real coding work
in a project directory, and returns a SHORT spoken-style summary (for TTS), while
persisting session state so the next call resumes context (resume-most-recent).

    AGENT_PROVIDER=claude    -> Claude Agent SDK (claude-agent-sdk), subscription auth
    AGENT_PROVIDER=opencode  -> opencode CLI (OpenRouter), `opencode run -c`

The bridge calls `make_agent().run(text)` and never knows which backend answered.
This module is deliberately standalone (no import from bridge.py) so it can be
verified in isolation before being wired into the live call flow.
"""
import os
import json
import asyncio
import logging
from pathlib import Path

log = logging.getLogger("switchboard.agent")

AGENT_PROVIDER = os.environ.get("AGENT_PROVIDER", "claude").lower()
# Fixed working dir — sessions live under ~/.claude/projects/<encoded-cwd>/, so this
# MUST stay stable for resume to find the prior session.
PROJECT_DIR = os.environ.get("AGENT_PROJECT_DIR", os.path.expanduser("~/Sites"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "40"))
AGENT_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", "900"))  # wall-clock ceiling per turn
SESSION_FILE = Path(os.environ.get(
    "AGENT_SESSION_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "agent-session.json"),
))

# Spoken-summary directive appended to each instruction. We shape the FINAL reply
# (read aloud over a phone) without touching Claude Code's built-in coding prompt.
SPOKEN_SUFFIX = (
    "\n\n---\n"
    "Do the work above using your tools. When finished, reply with ONE or TWO short "
    "sentences of plain spoken English summarizing what you did or found — it is read "
    "aloud over a phone call, so: no code, no markdown, no file paths, no lists. "
    "If you could not finish, say so in one sentence."
)


def _read_session_id():
    try:
        return json.loads(SESSION_FILE.read_text()).get("session_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_session_id(session_id: str) -> None:
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({"session_id": session_id}))


def clear_session() -> None:
    """'new session' voice command — forget the resumed thread; next call starts fresh."""
    SESSION_FILE.unlink(missing_ok=True)


class CodingAgent:
    """Backend-agnostic. run(text) -> short spoken reply; resumes most-recent session."""

    async def run(self, text: str) -> str:
        raise NotImplementedError


class ClaudeBackend(CodingAgent):
    """Claude Agent SDK (claude-agent-sdk), billing the Claude subscription."""

    def __init__(self):
        # Subscription auth: if a Claude Code OAuth token is present, scrub
        # ANTHROPIC_API_KEY so the agent bills the subscription, not API credits
        # (the API key otherwise takes precedence). Mirrors the verified PONG test.
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and os.environ.pop("ANTHROPIC_API_KEY", None):
            log.info("agent: using CLAUDE_CODE_OAUTH_TOKEN (subscription); scrubbed ANTHROPIC_API_KEY")

    async def run(self, text: str) -> str:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        options = ClaudeAgentOptions(
            cwd=PROJECT_DIR,
            permission_mode="bypassPermissions",  # no prompts — nobody at a keyboard mid-call
            model=CLAUDE_MODEL,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            resume=_read_session_id(),            # resume most-recent; None = fresh
            max_turns=AGENT_MAX_TURNS,            # the real guard (max_budget_usd is inert on subscription)
        )

        captured = {"session_id": None, "reply": None, "subtype": None}

        async def _drive():
            agen = query(prompt=text + SPOKEN_SUFFIX, options=options)
            try:
                async for message in agen:
                    if isinstance(message, ResultMessage):
                        captured["session_id"] = message.session_id
                        captured["subtype"] = message.subtype
                        if message.result is not None:
                            captured["reply"] = message.result
            finally:
                aclose = getattr(agen, "aclose", None)
                if aclose:
                    await aclose()  # don't orphan the CLI subprocess on cancel/timeout

        try:
            await asyncio.wait_for(_drive(), timeout=AGENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("agent: turn exceeded %ss", AGENT_TIMEOUT_S)
            return "Sorry, that took too long, so I stopped it. Try a smaller step."

        if captured["session_id"]:
            _write_session_id(captured["session_id"])
        if captured["subtype"] and captured["subtype"] != "success":
            log.error("agent: turn ended non-success (%s)", captured["subtype"])
        return captured["reply"] or "I finished, but I don't have anything to report back."


class OpencodeBackend(CodingAgent):
    """opencode CLI driven over OpenRouter. `opencode run -c` continues the most-recent
    session in the cwd (= resume-most-recent). NOTE: confirm the clean-output flag and
    parse the reply once OPENROUTER_API_KEY is set — stdout may carry log noise."""

    async def run(self, text: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "opencode", "run", "-c", text + SPOKEN_SUFFIX,
            cwd=PROJECT_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("opencode: turn exceeded %ss", AGENT_TIMEOUT_S)
            return "Sorry, that took too long, so I stopped it. Try a smaller step."
        if proc.returncode != 0:
            log.error("opencode: exit %s: %s", proc.returncode,
                      (err or b"").decode(errors="replace")[:300])
            return "Sorry, the coding agent hit an error."
        return (out or b"").decode(errors="replace").strip() or "Done."


def make_agent() -> CodingAgent:
    if AGENT_PROVIDER == "claude":
        return ClaudeBackend()
    if AGENT_PROVIDER == "opencode":
        return OpencodeBackend()
    raise NotImplementedError(f"unknown AGENT_PROVIDER={AGENT_PROVIDER}")
