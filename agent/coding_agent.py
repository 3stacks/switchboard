"""
switchboard — agent-agnostic coding-agent layer.

A pluggable backend: take an instruction, do real coding work in a given working
directory (resuming a prior session if supplied), and return a SHORT spoken-style
summary plus the new session id (so the caller's store can resume next time).

    make_agent("claude"|"opencode").run(text, cwd, session_id) -> (reply, new_session_id)

Session bookkeeping lives in the store (sessions.py), not here — this layer is
stateless across calls.
"""
from __future__ import annotations

import os
import asyncio
import logging

log = logging.getLogger("switchboard.agent")

KNOWN_AGENTS = ("claude", "opencode")
DEFAULT_AGENT = os.environ.get("AGENT_PROVIDER", "claude").lower()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "40"))
AGENT_TIMEOUT_S = float(os.environ.get("AGENT_TIMEOUT_S", "900"))  # wall-clock ceiling per turn

SPOKEN_SUFFIX = (
    "\n\n---\n"
    "Do the work above using your tools. When finished, reply with ONE or TWO short "
    "sentences of plain spoken English summarizing what you did or found — it is read "
    "aloud over a phone call, so: no code, no markdown, no file paths, no lists. "
    "If you could not finish, say so in one sentence."
)


def ensure_subscription_auth() -> dict:
    """If a Claude Code OAuth token is present, scrub ANTHROPIC_API_KEY so the agent
    bills the subscription (the API key otherwise takes precedence). Returns the
    resulting auth state for a startup log line."""
    has_token = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    if has_token:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    return {"oauth_token": has_token, "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))}


class CodingAgent:
    """Backend-agnostic. run(text, cwd, session_id) -> (spoken reply, new session id)."""

    async def run(self, text: str, cwd: str, session_id: str | None) -> tuple[str, str | None]:
        raise NotImplementedError


class ClaudeBackend(CodingAgent):
    async def run(self, text: str, cwd: str, session_id: str | None) -> tuple[str, str | None]:
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        options = ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="bypassPermissions",  # no prompts — nobody at a keyboard mid-call
            model=CLAUDE_MODEL,
            allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            resume=session_id,
            max_turns=AGENT_MAX_TURNS,            # the real guard (max_budget_usd is inert on subscription)
        )
        cap = {"sid": session_id, "reply": None, "subtype": None}

        async def _drive():
            agen = query(prompt=text + SPOKEN_SUFFIX, options=options)
            try:
                async for message in agen:
                    if isinstance(message, ResultMessage):
                        cap["sid"] = message.session_id or cap["sid"]
                        cap["subtype"] = message.subtype
                        if message.result is not None:
                            cap["reply"] = message.result
            finally:
                aclose = getattr(agen, "aclose", None)
                if aclose:
                    await aclose()  # don't orphan the CLI subprocess on cancel/timeout

        try:
            await asyncio.wait_for(_drive(), timeout=AGENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("agent: turn exceeded %ss", AGENT_TIMEOUT_S)
            return "Sorry, that took too long, so I stopped it. Try a smaller step.", cap["sid"]
        if cap["subtype"] and cap["subtype"] != "success":
            log.error("agent: turn ended non-success (%s)", cap["subtype"])
        return cap["reply"] or "I finished, but I don't have anything to report back.", cap["sid"]


class OpencodeBackend(CodingAgent):
    """opencode over OpenRouter. v1: continue the cwd's session via -c (or -s if we have
    an id). Capturing opencode's own new session id from stdout is TODO — verify when an
    OPENROUTER_API_KEY is set."""

    async def run(self, text: str, cwd: str, session_id: str | None) -> tuple[str, str | None]:
        args = ["opencode", "run"] + (["-s", session_id] if session_id else ["-c"]) + [text + SPOKEN_SUFFIX]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            return "Sorry, that took too long, so I stopped it.", session_id
        if proc.returncode != 0:
            log.error("opencode: exit %s: %s", proc.returncode, (err or b"").decode(errors="replace")[:300])
            return "Sorry, the coding agent hit an error.", session_id
        return (out or b"").decode(errors="replace").strip() or "Done.", session_id


def make_agent(provider: str | None = None) -> CodingAgent:
    provider = (provider or DEFAULT_AGENT).lower()
    if provider == "claude":
        return ClaudeBackend()
    if provider == "opencode":
        return OpencodeBackend()
    raise NotImplementedError(f"unknown agent provider: {provider}")
