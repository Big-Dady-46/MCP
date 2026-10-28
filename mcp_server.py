"""NexusAI MCP Server — exposes 4 task tools over the Model Context Protocol.

SDK: official `mcp` Python SDK v2 (installed from PyPI as `mcp[cli]`).

How it works (beginner notes):
  1. `MCPServer(...)` creates the server object.
  2. `@mcp.tool()` registers a plain Python function as an MCP tool.
  3. The SDK reads the function NAME (tool name), DOCSTRING (description)
     and TYPE HINTS (JSON Schema input) — no hand-written schema needed.
  4. `mcp.run(transport="stdio")` starts the server. A host (MCP Inspector,
     Antigravity, ...) spawns this file as a child process and talks to it
     over stdin/stdout using the MCP protocol.

Every tool calls the SAME database.py used by the Flask web app, so a task
created through MCP instantly shows up in the web interface.

Run it:  python mcp_server.py        (stdio server, waits for a host)
Test it: mcp dev mcp_server.py       (opens MCP Inspector)
"""

import json
from typing import Annotated, Literal, Optional

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import Field

import database as db

# NOTE: schema is created lazily on first get_conn() use (no import-time
# writes) so importing this module is safe on read-only filesystems.

# Create the server. `instructions` is text the host shows to its AI model.
mcp = MCPServer(
    "nexusai-task-manager",
    instructions=(
        "Local task manager for the NexusAI MCP Learning Workspace. "
        "Use add_task, list_tasks, complete_task and delete_task to manage "
        "tasks stored in the shared SQLite database."
    ),
)


def _task_payload(task: dict) -> str:
    """Turn a task dict into readable JSON text for the model/client."""
    return json.dumps(task, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 1 — add_task (modifies data)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,      # it writes to the database
    destructive_hint=False,    # but it does not destroy anything
    idempotent_hint=False,     # calling twice creates two tasks
    open_world_hint=False,     # it only touches this local database
))
def add_task(
    title: Annotated[str, Field(description="Title of the task to create.",
                                min_length=1, max_length=200)],
) -> str:
    """Create a new task in the shared SQLite database.

    Validates the title (non-empty, max 200 characters), inserts a row and
    returns the created task as JSON. Raises a tool error on invalid input.
    """
    try:
        task = db.create_task(title, source="mcp")
    except ValueError as exc:
        # ToolError -> the host/model receives is_error=True with this message.
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while creating task: {exc}") from exc
    return _task_payload(task)


# ---------------------------------------------------------------------------
# Tool 2 — list_tasks (read-only)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=True,       # reads only, changes nothing
    open_world_hint=False,
))
def list_tasks(
    status: Annotated[
        Literal["all", "pending", "completed"],
        Field(description="Filter by completion status. Default: all"),
    ] = "all",
) -> str:
    """List tasks from the shared SQLite database.

    Returns a JSON array of task objects (id, title, completed,
    created_at, updated_at), newest first.
    """
    try:
        tasks = db.list_tasks(status)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while listing tasks: {exc}") from exc
    if not tasks:
        return json.dumps([], indent=2)
    return json.dumps(tasks, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 3 — complete_task (modifies data)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,      # completing twice has the same effect
    open_world_hint=False,
))
def complete_task(
    task_id: Annotated[int, Field(description="ID of the task to mark complete.",
                                  ge=1)],
) -> str:
    """Mark an existing task as completed.

    Returns the updated task as JSON, or raises a tool error when the
    id does not exist.
    """
    try:
        task = db.complete_task(task_id, source="mcp")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while completing task: {exc}") from exc
    return _task_payload(task)


# ---------------------------------------------------------------------------
# Tool 4 — delete_task (modifies data / destructive)
# ---------------------------------------------------------------------------

@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,     # it removes a row permanently
    idempotent_hint=False,
    open_world_hint=False,
))
def delete_task(
    task_id: Annotated[int, Field(description="ID of the task to delete.",
                                  ge=1)],
) -> str:
    """Delete a task from the shared SQLite database.

    Returns a JSON confirmation with the deleted task, or raises a tool
    error when the id does not exist.
    """
    try:
        task = db.delete_task(task_id, source="mcp")
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError(f"Database error while deleting task: {exc}") from exc
    return json.dumps(
        {"deleted": True, "task": task}, indent=2, ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# RESOURCE (Lesson 3) — readable data, never modifies anything
# A resource is addressed by a URI. The function runs on resources/read
# only (not on resources/list). Return value: str = text, dict = JSON text.
# ---------------------------------------------------------------------------

@mcp.resource(
    "nexus://tasks/summary",
    mime_type="application/json",
    description="Read-only summary of the task database: counts and pending titles.",
)
def tasks_summary() -> dict:
    """Safe, read-only task summary straight from the shared database.

    Contains only counts and task titles — no secrets, no file paths.
    Raises a protocol error if the database cannot be read.
    """
    try:
        stats = db.get_stats()
        pending = db.list_tasks("pending")
    except Exception as exc:
        raise MCPError(code=INTERNAL_ERROR,
                       message=f"Could not read the task database: {exc}") from exc
    return {
        "total": stats["total"],
        "pending": stats["pending"],
        "completed": stats["completed"],
        "pending_titles": [{"id": t["id"], "title": t["title"]} for t in pending[:25]],
    }


# ---------------------------------------------------------------------------
# PROMPT (Lesson 3) — a reusable template the USER picks in the host.
# Name from the function, description from the docstring, arguments from
# the parameters (a default makes an argument optional). Rendering a
# prompt never modifies the database.
# ---------------------------------------------------------------------------

@mcp.prompt(title="Review pending tasks")
def review_pending_tasks(
    focus: Annotated[str, Field(
        description="What to emphasize, e.g. 'order', 'priorities' or 'all'.")] = "all",
) -> str:
    """Summarize pending tasks and suggest next steps (read-only).

    Never completes or deletes anything — it only asks the model to report.
    """
    if not isinstance(focus, str) or len(focus) > 100:
        raise MCPError(code=INVALID_PARAMS,
                       message="focus must be text of 100 characters or fewer.")
    focus_clean = focus.strip() or "all"
    try:
        pending = db.list_tasks("pending")
    except Exception as exc:
        raise MCPError(code=INTERNAL_ERROR,
                       message=f"Could not read the task database: {exc}") from exc
    if pending:
        lines = "\n".join(
            f"- (#{t['id']}) {t['title']} — created {t['created_at']}"
            for t in pending[:25]
        )
    else:
        lines = "(no pending tasks right now)"
    return (
        f"Please review my pending tasks (focus: {focus_clean}).\n\n"
        f"Pending tasks:\n{lines}\n\n"
        "Please: 1) summarize them in a few bullets, 2) suggest a sensible "
        "order and why, 3) point out anything that looks stale or unclear. "
        "Do NOT complete, delete or modify any task — only report."
    )


# ---------------------------------------------------------------------------
# Helpers used by the Flask app (not tools) and by tests
# ---------------------------------------------------------------------------

TOOL_META = {
    "add_task": {"mode": "modifies data", "read_only": False},
    "list_tasks": {"mode": "read-only", "read_only": True},
    "complete_task": {"mode": "modifies data", "read_only": False},
    "delete_task": {"mode": "modifies data (destructive)", "read_only": False},
}


def get_tool_definitions() -> list:
    """Return the REAL registered tools: name, description, input schema,
    annotations. Used by the /tools page and the /status page."""
    import asyncio
    import inspect

    result = mcp.list_tools()
    if inspect.isawaitable(result):
        tools = asyncio.run(result)
    else:
        tools = result

    out = []
    for t in tools:
        meta = TOOL_META.get(t.name, {"mode": "unknown", "read_only": None})
        ann = t.annotations
        out.append({
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema,
            "annotations": {
                "title": getattr(ann, "title", None) if ann else None,
                "read_only_hint": getattr(ann, "read_only_hint", None) if ann else None,
                "destructive_hint": getattr(ann, "destructive_hint", None) if ann else None,
                "idempotent_hint": getattr(ann, "idempotent_hint", None) if ann else None,
                "open_world_hint": getattr(ann, "open_world_hint", None) if ann else None,
            },
            "mode": meta["mode"],
            "read_only": meta["read_only"],
        })
    return out


def main() -> None:
    """Entry point: python mcp_server.py starts a stdio MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
