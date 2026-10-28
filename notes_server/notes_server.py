"""Notes Manager â€” a SECOND, independent MCP server (beginner learning project).

Purpose: learn how ONE MCP host (e.g. Antigravity) can connect to MULTIPLE
MCP servers at the same time. This server has:

  * its OWN SQLite database (notes.db in this folder) â€” the Task Manager's
    nexusai.db is never touched;
  * its OWN process â€” it is launched separately by the host;
  * three tools â€” add_note, list_notes, search_notes.

Same official SDK style as the Task Manager (mcp v2 API):

    from mcp.server import MCPServer
    mcp = MCPServer("...")          # server object
    @mcp.tool()                     # registers a plain function as a tool
    def f(...) -> str: ...          # name from function, description from
                                    # docstring, JSON Schema from type hints

Run standalone (stdio, waits for a host):  python notes_server.py
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import Field
from typing import Annotated

# ---------------------------------------------------------------------------
# Independent database (NOT the Task Manager's nexusai.db)
# ---------------------------------------------------------------------------

NOTES_DIR = os.path.dirname(os.path.abspath(__file__))
NOTES_DB_PATH = os.environ.get("NEXUSAI_NOTES_DB") or os.path.join(NOTES_DIR, "notes.db")

MAX_TITLE = 200
MAX_CONTENT = 5000
MAX_QUERY = 100


def set_db_path(path: str) -> str:
    """Change the database file (used by tests so they never touch notes.db)."""
    global NOTES_DB_PATH
    NOTES_DB_PATH = path
    return NOTES_DB_PATH


def get_db_path() -> str:
    return NOTES_DB_PATH


@contextmanager
def _conn():
    conn = sqlite3.connect(NOTES_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        c.execute("PRAGMA journal_mode=WAL")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


def validate_title(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Note title is required and must be text.")
    clean = " ".join(value.split())
    if len(clean) > MAX_TITLE:
        raise ValueError(f"Note title must be {MAX_TITLE} characters or fewer.")
    return clean


def validate_content(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Note content is required and must be text.")
    if len(value) > MAX_CONTENT:
        raise ValueError(f"Note content must be {MAX_CONTENT} characters or fewer.")
    return value.strip()


def validate_query(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Search query is required and must be text.")
    q = value.strip()
    if len(q) > MAX_QUERY:
        raise ValueError(f"Search query must be {MAX_QUERY} characters or fewer.")
    return q


def _validate_id(value) -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ValueError("Note id must be a number.") from None
    if i < 1:
        raise ValueError("Note id must be greater than zero.")
    return i


# ---------------------------------------------------------------------------
# Plain database operations (used by the tools, kept separate for clarity)
# ---------------------------------------------------------------------------

def db_create_note(title: str, content: str) -> dict:
    t = validate_title(title)
    c = validate_content(content)
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, content, created_at) VALUES (?, ?, ?)",
            (t, c, now),
        )
        note_id = cur.lastrowid
    return {"id": note_id, "title": t, "content": c, "created_at": now}


def db_list_notes() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
    return [_row(r) for r in rows]


def db_search_notes(query: str) -> list:
    """Case-insensitive match on title or content (SQL LIKE, parameterised)."""
    q = validate_query(query)
    pattern = f"%{q}%"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE title LIKE ? OR content LIKE ? ORDER BY id DESC",
            (pattern, pattern),
        ).fetchall()
    return [_row(r) for r in rows]


# Create tables when the module loads, so a standalone stdio process
# always finds a ready database (tests override the path first).
init_db()


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "nexusai-notes-manager",
    instructions=(
        "Local notes manager for the NexusAI MCP Learning Workspace. "
        "Independent from the task server: it uses its own notes.db file. "
        "Use add_note, list_notes and search_notes."
    ),
)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=False,
    idempotent_hint=False, open_world_hint=False,
))
def add_note(
    title: Annotated[str, Field(description="Short title for the note.",
                                min_length=1, max_length=200)],
    content: Annotated[str, Field(description="Body text of the note.",
                                  min_length=1, max_length=5000)],
) -> str:
    """Save a new note (title + content) to the independent notes database.

    Validates both fields, inserts a row and returns the created note as
    JSON. Raises a tool error on invalid input.
    """
    try:
        note = db_create_note(title, content)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while saving note: {exc}") from exc
    return json.dumps(note, indent=2, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def list_notes() -> str:
    """List all saved notes, newest first, as a JSON array."""
    try:
        notes = db_list_notes()
    except Exception as exc:
        raise ToolError(f"Database error while listing notes: {exc}") from exc
    return json.dumps(notes, indent=2, ensure_ascii=False)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def search_notes(
    query: Annotated[str, Field(description="Text to find in note titles and content.",
                                min_length=1, max_length=100)],
) -> str:
    """Search notes by title and content (case-insensitive substring match).

    Returns a JSON array of matching notes. Raises a tool error on an
    empty or invalid query.
    """
    try:
        notes = db_search_notes(query)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while searching notes: {exc}") from exc
    return json.dumps(notes, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# RESOURCE (Lesson 3) — read-only, uses ONLY the notes database.
# ---------------------------------------------------------------------------

@mcp.resource(
    "notes://summary",
    mime_type="application/json",
    description="Read-only summary of saved notes: count and titles.",
)
def notes_summary() -> dict:
    """Safe, read-only notes summary. Counts and titles only — no paths."""
    try:
        notes = db_list_notes()
    except Exception as exc:
        raise MCPError(code=INTERNAL_ERROR,
                       message=f"Could not read the notes database: {exc}") from exc
    return {
        "count": len(notes),
        "notes": [
            {"id": n["id"], "title": n["title"], "created_at": n["created_at"]}
            for n in notes[:50]
        ],
    }


# ---------------------------------------------------------------------------
# PROMPT (Lesson 3) — user picks it in the host; rendering never writes.
# `query` is REQUIRED (no default). Use "*" to summarize every note.
# ---------------------------------------------------------------------------

@mcp.prompt(title="Summarize saved notes")
def summarize_notes(
    query: Annotated[str, Field(
        description="Keyword to focus on. Use '*' to summarize all notes.")],
) -> str:
    """Organize and summarize saved notes (read-only, never modifies them)."""
    if not isinstance(query, str) or len(query) > 100:
        raise MCPError(code=INVALID_PARAMS,
                       message="query must be text of 100 characters or fewer.")
    q = query.strip()
    try:
        notes = db_search_notes(q) if q and q != "*" else db_list_notes()
    except ValueError as exc:
        raise MCPError(code=INVALID_PARAMS, message=str(exc)) from exc
    except Exception as exc:
        raise MCPError(code=INTERNAL_ERROR,
                       message=f"Could not read the notes database: {exc}") from exc
    if notes:
        listing = "\n".join(
            f"- [{n['id']}] {n['title']}: {n['content'][:200]}" for n in notes[:20]
        )
    else:
        listing = "(no notes matched)"
    return (
        f"Please summarize my saved notes (focus: {q or 'all'}).\n\n"
        f"Notes:\n{listing}\n\n"
        "Please: 1) group related notes together, 2) give a short summary of "
        "each group, 3) list any open questions or next steps you notice. "
        "Do NOT modify, create or delete any note — only report."
    )


TOOL_META = {
    "add_note": {"mode": "modifies data", "read_only": False},
    "list_notes": {"mode": "read-only", "read_only": True},
    "search_notes": {"mode": "read-only", "read_only": True},
}


def get_tool_definitions() -> list:
    """Real registered tools (used for verification/reporting)."""
    import asyncio
    import inspect

    result = mcp.list_tools()
    tools = asyncio.run(result) if inspect.isawaitable(result) else result
    if hasattr(tools, "tools"):
        tools = tools.tools
    out = []
    for t in tools:
        meta = TOOL_META.get(t.name, {"mode": "unknown", "read_only": None})
        ann = t.annotations
        out.append({
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema,
            "annotations": {
                "read_only_hint": getattr(ann, "read_only_hint", None) if ann else None,
                "destructive_hint": getattr(ann, "destructive_hint", None) if ann else None,
            },
            "mode": meta["mode"],
            "read_only": meta["read_only"],
        })
    return out


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

