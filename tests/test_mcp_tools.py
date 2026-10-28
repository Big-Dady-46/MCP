"""Tests for the MCP tools using the official SDK's in-memory Client.

`Client(mcp)` talks to the server object directly — no subprocess, no port —
exactly how the SDK's own test suite exercises its examples.
"""

import asyncio
import json

import pytest
from mcp import Client

import database as db
import mcp_server


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Both the tools and direct database calls use this temporary file,
    so tests can never damage the real nexusai.db."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    yield


def run(coro):
    return asyncio.run(coro)


def call(tool, args):
    """Call a tool through the in-memory SDK client and return the result."""

    async def scenario():
        async with Client(mcp_server.mcp, raise_exceptions=True) as client:
            return await client.call_tool(tool, args)

    return run(scenario())


def text_of(result):
    return "".join(c.text for c in result.content if hasattr(c, "text"))


def json_of(result):
    return json.loads(text_of(result))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_tool_discovery_finds_all_four_tools():
    async def scenario():
        async with Client(mcp_server.mcp, raise_exceptions=True) as client:
            return (await client.list_tools()).tools

    tools = run(scenario())
    names = {t.name for t in tools}
    assert names == {"add_task", "list_tasks", "complete_task", "delete_task"}
    for t in tools:
        assert t.description, f"{t.name} must have a description"
        assert t.input_schema.get("type") == "object", f"{t.name} schema"


def test_get_tool_definitions_matches_registered_tools():
    defs = mcp_server.get_tool_definitions()
    assert len(defs) == 4
    assert {d["name"] for d in defs} == {
        "add_task", "list_tasks", "complete_task", "delete_task"
    }
    read_only = {d["name"]: d["read_only"] for d in defs}
    assert read_only["list_tasks"] is True
    assert read_only["add_task"] is False


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------

def test_add_task_returns_created_task():
    result = call("add_task", {"title": "Built via MCP"})
    assert result.is_error is False
    task = json_of(result)
    assert task["title"] == "Built via MCP"
    assert task["completed"] is False
    # The row really landed in SQLite:
    assert db.get_task(task["id"])["title"] == "Built via MCP"


def test_add_task_empty_title_is_a_tool_error():
    result = call("add_task", {"title": "   "})
    assert result.is_error is True
    assert "empty" in text_of(result).lower()


def test_add_task_missing_title_rejected_by_schema():
    result = call("add_task", {})
    assert result.is_error is True


def test_add_task_wrong_type_rejected_by_schema():
    result = call("add_task", {"title": 12345})
    assert result.is_error is True


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

def test_list_tasks_empty_database():
    result = call("list_tasks", {})
    assert result.is_error is False
    assert json_of(result) == []


def test_list_tasks_returns_created_tasks():
    call("add_task", {"title": "One"})
    call("add_task", {"title": "Two"})
    tasks = json_of(call("list_tasks", {}))
    assert {t["title"] for t in tasks} == {"One", "Two"}


def test_list_tasks_status_filter():
    created = json_of(call("add_task", {"title": "Done later"}))
    call("complete_task", {"task_id": created["id"]})
    pending = json_of(call("list_tasks", {"status": "pending"}))
    completed = json_of(call("list_tasks", {"status": "completed"}))
    assert pending == []
    assert [t["id"] for t in completed] == [created["id"]]


def test_list_tasks_invalid_status_is_tool_error():
    result = call("list_tasks", {"status": "banana"})
    assert result.is_error is True


# ---------------------------------------------------------------------------
# complete_task / delete_task
# ---------------------------------------------------------------------------

def test_complete_task_updates_database():
    created = json_of(call("add_task", {"title": "Finish me"}))
    result = call("complete_task", {"task_id": created["id"]})
    assert result.is_error is False
    assert json_of(result)["completed"] is True
    assert db.get_task(created["id"])["completed"] is True


def test_complete_unknown_task_is_tool_error():
    result = call("complete_task", {"task_id": 424242})
    assert result.is_error is True
    assert "424242" in text_of(result)


def test_delete_task_removes_row():
    created = json_of(call("add_task", {"title": "Temporary"}))
    result = call("delete_task", {"task_id": created["id"]})
    assert result.is_error is False
    assert json_of(result)["deleted"] is True
    assert db.get_task(created["id"]) is None


def test_delete_unknown_task_is_tool_error():
    result = call("delete_task", {"task_id": 424242})
    assert result.is_error is True


def test_delete_task_rejects_negative_id():
    result = call("delete_task", {"task_id": -1})
    assert result.is_error is True


# ---------------------------------------------------------------------------
# Shared data between web app and MCP tools
# ---------------------------------------------------------------------------

def test_web_created_task_visible_to_mcp_tool():
    web_task = db.create_task("Created in the web UI", source="web")
    tasks = json_of(call("list_tasks", {}))
    assert any(t["id"] == web_task["id"] for t in tasks)


def test_mcp_created_task_visible_to_web_layer():
    mcp_task = json_of(call("add_task", {"title": "Created via MCP"}))
    stats = db.get_stats()
    assert stats["total"] == 1
    assert db.get_task(mcp_task["id"])["title"] == "Created via MCP"


def test_activity_logs_distinguish_sources():
    db.create_task("From web", source="web")
    call("add_task", {"title": "From MCP"})
    sources = {r["source"] for r in db.list_activities()}
    assert sources == {"web", "mcp"}


# ---------------------------------------------------------------------------
# Flask app uses the same database
# ---------------------------------------------------------------------------

def test_flask_routes_share_database(tmp_path, monkeypatch):
    flask_app = __import__("app").app
    client = flask_app.test_client()

    # Create through the web form (with a valid CSRF token).
    page = client.get("/tasks")
    token = page.get_data(as_text=True).split('name="csrf_token" value="')[1].split('"')[0]
    resp = client.post("/tasks", data={"title": "From Flask", "csrf_token": token})
    assert resp.status_code == 302

    # The MCP tools see exactly that row.
    tasks = json_of(call("list_tasks", {}))
    assert any(t["title"] == "From Flask" for t in tasks)


def test_flask_rejects_post_without_csrf_token():
    flask_app = __import__("app").app
    client = flask_app.test_client()
    resp = client.post("/tasks", data={"title": "No token"})
    assert resp.status_code in (302, 400)
    # Nothing was written.
    assert db.get_stats()["total"] == 0

# ---------------------------------------------------------------------------
# Lesson 3 — MCP Resources & Prompts (Task Manager server)
# ---------------------------------------------------------------------------

def _resources():
    async def s():
        async with Client(mcp_server.mcp, raise_exceptions=True) as c:
            return (await c.list_resources()).resources
    return asyncio.run(s())


def _read(uri):
    async def s():
        async with Client(mcp_server.mcp, raise_exceptions=True) as c:
            return await c.read_resource(uri)
    return asyncio.run(s())


def _prompt(name, args=None):
    async def s():
        async with Client(mcp_server.mcp, raise_exceptions=True) as c:
            return await c.get_prompt(name, args)
    return asyncio.run(s())


def _prompt_names():
    async def s():
        async with Client(mcp_server.mcp, raise_exceptions=True) as c:
            return [p.name for p in (await c.list_prompts()).prompts]
    return asyncio.run(s())


def test_task_resource_discovery():
    uris = {r.uri for r in _resources()}
    assert "nexus://tasks/summary" in uris


def test_task_resource_read_returns_real_stats():
    db.create_task("Resource pending A")
    done = db.create_task("Resource completed B")
    db.complete_task(done["id"])
    result = _read("nexus://tasks/summary")
    data = json.loads(result.contents[0].text)
    assert data["total"] == 2
    assert data["completed"] == 1
    assert data["pending"] == 1
    assert any(t["title"] == "Resource pending A" for t in data["pending_titles"])


def test_task_resource_read_does_not_modify_db():
    db.create_task("Read-only probe")
    before_stats = db.get_stats()
    before_activity = len(db.list_activities())
    _read("nexus://tasks/summary")
    assert db.get_stats() == before_stats
    assert len(db.list_activities()) == before_activity


def test_task_prompt_discovery():
    assert "review_pending_tasks" in _prompt_names()


def test_task_prompt_renders_with_valid_args_and_is_read_only():
    task = db.create_task("Write MCP report")
    before = db.get_stats()
    result = _prompt("review_pending_tasks", {"focus": "priorities"})
    text = "".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    assert "Write MCP report" in text
    assert "priorities" in text
    assert "Do NOT complete" in text
    assert db.get_stats() == before
    assert db.get_task(task["id"])["completed"] is False


def test_task_prompt_works_without_arguments():
    result = _prompt("review_pending_tasks")
    text = "".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    assert "focus: all" in text


def _assert_prompt_mcp_error(fn):
    """In-memory anyio client wraps raised MCPError in an ExceptionGroup."""
    from mcp import MCPError

    def contains(group):
        return any(
            isinstance(e, MCPError)
            or (isinstance(e, BaseExceptionGroup) and contains(e))
            for e in group.exceptions
        )

    with pytest.raises(BaseExceptionGroup) as excinfo:
        fn()
    assert contains(excinfo.value), f"no MCPError inside: {excinfo.value!r}"


def test_task_prompt_rejects_invalid_focus():
    _assert_prompt_mcp_error(
        lambda: _prompt("review_pending_tasks", {"focus": "x" * 101})
    )


def test_task_prompt_unknown_name_fails():
    _assert_prompt_mcp_error(lambda: _prompt("does_not_exist", {}))
