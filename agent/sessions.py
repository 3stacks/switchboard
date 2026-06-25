"""
switchboard — session/context store (SQLite) for keyword commands.

A "session" pairs a working directory (context) with an agent backend and the
agent's resumable session id. One session is "active" at a time (v1); bare speech
routes to it. The schema already supports many named contexts so multi-session
(switch / list) drops in later without migration.

    switchboard session start [<context>] [<agent>]   -> create + activate
    <anything else>                                    -> piped to the active session
"""
from __future__ import annotations

import os
import time
import sqlite3
import contextlib

DB_PATH = os.environ.get(
    "SWITCHBOARD_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "switchboard.db"),
)

# Known contexts -> working dir. Unknown context names fall back to ~/Sites/<context>.
CONTEXTS = {
    "default": os.path.expanduser("~/Sites"),
    "personal": os.path.expanduser("~/personal"),
    "switchboard": os.path.expanduser("~/Sites/switchboard"),
}


def cwd_for(context: str) -> str:
    return CONTEXTS.get(context, os.path.expanduser(f"~/Sites/{context}"))


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS sessions(
               context    TEXT PRIMARY KEY,
               cwd        TEXT NOT NULL,
               agent      TEXT NOT NULL,
               session_id TEXT,
               active     INTEGER NOT NULL DEFAULT 0,
               updated_at REAL NOT NULL
           )"""
    )
    c.commit()  # persist the schema before any caller closes the connection
    return c


def start_session(context: str, cwd: str, agent: str) -> None:
    """Create or replace a context and make it the single active session, starting a
    FRESH agent thread (session_id reset)."""
    with contextlib.closing(_conn()) as c, c:   # closing() closes; `c` commits the txn
        c.execute("UPDATE sessions SET active=0")
        c.execute(
            """INSERT INTO sessions(context, cwd, agent, session_id, active, updated_at)
               VALUES(?,?,?,NULL,1,?)
               ON CONFLICT(context) DO UPDATE SET
                   cwd=excluded.cwd, agent=excluded.agent,
                   session_id=NULL, active=1, updated_at=excluded.updated_at""",
            (context, cwd, agent, time.time()),
        )


def active_session() -> sqlite3.Row | None:
    with contextlib.closing(_conn()) as c:
        return c.execute("SELECT * FROM sessions WHERE active=1").fetchone()


def set_session_id(context: str, session_id: str) -> None:
    with contextlib.closing(_conn()) as c, c:
        c.execute(
            "UPDATE sessions SET session_id=?, updated_at=? WHERE context=?",
            (session_id, time.time(), context),
        )


def list_sessions() -> list[sqlite3.Row]:
    with contextlib.closing(_conn()) as c:
        return c.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
