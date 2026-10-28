"""Shared SQLite database module.

Both the Flask web app (app.py) and the MCP server (mcp_server.py) import
this module, so they always read and write the SAME database file.

Every SQL statement uses parameterised queries (?, placeholders), so user
input can never be executed as SQL.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_db_dir() -> str:
    """Local: project folder. On Vercel (VERCEL=1): /tmp — the only writable
    directory there, and it is EPHEMERAL (data lost on cold start). Persistent
    storage requires a remote database driver, which is not yet enabled."""
    if os.environ.get("VERCEL"):
        return "/tmp"
    return BASE_DIR


# The database lives in the project folder (never in a temp folder).
# Override with the NEXUSAI_DB_PATH environment variable if you ever need to.
DB_PATH = os.environ.get("NEXUSAI_DB_PATH") or os.path.join(_default_db_dir(), "nexusai.db")

MAX_TITLE_LENGTH = 200

# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------


def set_db_path(path: str) -> str:
    """Change the database file used by this module (used by tests)."""
    global DB_PATH
    DB_PATH = path
    return DB_PATH


def get_db_path() -> str:
    return DB_PATH


_initialized_paths: set = set()


def _ensure_schema(conn) -> None:
    """Create tables if missing. Idempotent; safe under concurrent callers."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            completed   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            action      TEXT NOT NULL,
            detail      TEXT NOT NULL,
            source      TEXT NOT NULL,
            result      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )
    # WAL lets the Flask app and the MCP server write at the same time.
    conn.execute("PRAGMA journal_mode=WAL")


@contextmanager
def get_conn():
    """Open a SQLite connection, commit on success, rollback on failure.

    Schema creation happens lazily on first use of each database file, so
    merely importing this module (or app.py) never writes to the filesystem.
    That is required on read-only deployment filesystems such as Vercel.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        if DB_PATH not in _initialized_paths:
            _ensure_schema(conn)
            _initialized_paths.add(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the tables if they do not exist yet. Safe to call repeatedly.

    Kept for explicit callers (MCP servers, tests, scripts); normal operations
    trigger the same schema creation lazily through get_conn().
    """
    with get_conn() as conn:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_task(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "completed": bool(row["completed"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def validate_title(title) -> str:
    """Validate and clean a task title. Raises ValueError on bad input."""
    if title is None:
        raise ValueError("Task title is required.")
    if not isinstance(title, str):
        raise ValueError("Task title must be text.")
    clean = " ".join(title.split())
    if not clean:
        raise ValueError("Task title cannot be empty.")
    if len(clean) > MAX_TITLE_LENGTH:
        raise ValueError(f"Task title must be {MAX_TITLE_LENGTH} characters or fewer.")
    return clean


def _validate_id(task_id) -> int:
    """Validate a task id. Raises ValueError on bad input."""
    try:
        value = int(task_id)
    except (TypeError, ValueError):
        raise ValueError("Task id must be a number.") from None
    if value < 1:
        raise ValueError("Task id must be greater than zero.")
    return value


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def create_task(title: str, source: str = "web") -> dict:
    """Insert a task and record the activity. Returns the created task."""
    clean = validate_title(title)
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (title, completed, created_at, updated_at) "
            "VALUES (?, 0, ?, ?)",
            (clean, now, now),
        )
        task_id = cur.lastrowid
    task = {
        "id": task_id,
        "title": clean,
        "completed": False,
        "created_at": now,
        "updated_at": now,
    }
    record_activity("task_created", f'Task #{task_id} "{clean}" created', source)
    return task


def list_tasks(status: str = "all") -> list:
    """Return tasks. status = all | pending | completed."""
    if status not in ("all", "pending", "completed"):
        raise ValueError("status must be one of: all, pending, completed")
    query = "SELECT * FROM tasks"
    params: tuple = ()
    if status == "pending":
        query += " WHERE completed = 0"
    elif status == "completed":
        query += " WHERE completed = 1"
    query += " ORDER BY id DESC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_task(r) for r in rows]


def get_task(task_id) -> dict | None:
    tid = _validate_id(task_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    return _row_to_task(row) if row else None


def complete_task(task_id, source: str = "web") -> dict:
    """Mark a task completed. Raises ValueError if it does not exist."""
    tid = _validate_id(task_id)
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE tasks SET completed = 1, updated_at = ? WHERE id = ?",
            (now, tid),
        )
        if cur.rowcount == 0:
            raise ValueError(f"No task found with id {tid}.")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    task = _row_to_task(row)
    record_activity("task_completed", f'Task #{tid} "{task["title"]}" completed', source)
    return task


def delete_task(task_id, source: str = "web") -> dict:
    """Delete a task. Raises ValueError if it does not exist."""
    tid = _validate_id(task_id)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise ValueError(f"No task found with id {tid}.")
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    task = _row_to_task(row)
    record_activity("task_deleted", f'Task #{tid} "{task["title"]}" deleted', source)
    return task


def get_stats() -> dict:
    """Real counts, straight from the tasks table."""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE completed = 1"
        ).fetchone()["c"]
    return {
        "total": total,
        "completed": done,
        "pending": total - done,
    }


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


def record_activity(action: str, detail: str, source: str = "web",
                    result: str = "success") -> None:
    """Record an event. Never pass secrets or keys into this function."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO activity (action, detail, source, result, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (action, detail[:500], source, result, _now()),
            )
    except Exception:
        # Logging must never break the main operation.
        pass


def list_activities(limit: int = 100) -> list:
    limit = max(1, min(int(limit), 500))
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def db_available() -> tuple:
    """Return (ok: bool, message: str) after a real test query."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return True, f"SQLite reachable at {DB_PATH}"
    except Exception as exc:  # pragma: no cover - only on broken setups
        return False, f"Database error: {exc}"


# NOTE: no database writes happen at import time. Schema creation is lazy
# (first get_conn() use per file) so importing this module is safe on
# read-only filesystems (Vercel Functions). Call init_db() explicitly if you
# want to create the schema up front.
