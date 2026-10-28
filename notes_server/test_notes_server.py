"""Tests for the Notes Manager MCP server.

Every test runs against a TEMPORARY database inside pytest's tmp folder, so
the real notes.db (and the Task Manager's nexusai.db) are never touched.
"""

import asyncio
import json
import os
import sys

import pytest
from mcp import Client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import notes_server  # noqa: E402  (path set up above)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(notes_server, "NOTES_DB_PATH", str(tmp_path / "notes_test.db"))
    notes_server.init_db()
    yield


def call(tool, args):
    async def scenario():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(scenario())


def json_of(result):
    return json.loads("".join(c.text for c in result.content if hasattr(c, "text")))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discovery_finds_three_tools():
    async def scenario():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return (await c.list_tools()).tools

    tools = asyncio.run(scenario())
    names = {t.name for t in tools}
    assert names == {"add_note", "list_notes", "search_notes"}
    for t in tools:
        assert t.description
        assert t.input_schema.get("type") == "object"


def test_tool_definitions_metadata():
    defs = notes_server.get_tool_definitions()
    assert len(defs) == 3
    modes = {d["name"]: d["read_only"] for d in defs}
    assert modes["add_note"] is False
    assert modes["list_notes"] is True
    assert modes["search_notes"] is True


# ---------------------------------------------------------------------------
# add_note
# ---------------------------------------------------------------------------

def test_add_note_returns_saved_note():
    r = call("add_note", {"title": "My first note", "content": "Hello MCP"})
    assert r.is_error is False
    note = json_of(r)
    assert note["title"] == "My first note"
    assert note["content"] == "Hello MCP"
    assert note["id"] >= 1
    assert note["created_at"]


def test_add_note_persists_to_its_own_database():
    call("add_note", {"title": "Persist", "content": "still here"})
    stored = [n["title"] for n in notes_server.db_list_notes()]
    assert "Persist" in stored


def test_add_note_empty_title_rejected():
    r = call("add_note", {"title": "   ", "content": "x"})
    assert r.is_error is True


def test_add_note_empty_content_rejected():
    r = call("add_note", {"title": "t", "content": ""})
    assert r.is_error is True


def test_add_note_missing_argument_rejected_by_schema():
    r = call("add_note", {"title": "only title"})
    assert r.is_error is True


def test_add_note_title_too_long_rejected():
    r = call("add_note", {"title": "x" * 201, "content": "c"})
    assert r.is_error is True


# ---------------------------------------------------------------------------
# list_notes
# ---------------------------------------------------------------------------

def test_list_notes_empty():
    r = call("list_notes", {})
    assert r.is_error is False
    assert json_of(r) == []


def test_list_notes_returns_newest_first():
    call("add_note", {"title": "First", "content": "a"})
    call("add_note", {"title": "Second", "content": "b"})
    notes = json_of(call("list_notes", {}))
    assert [n["title"] for n in notes] == ["Second", "First"]


# ---------------------------------------------------------------------------
# search_notes
# ---------------------------------------------------------------------------

def test_search_matches_title():
    call("add_note", {"title": "Grocery list", "content": "milk, eggs"})
    call("add_note", {"title": "Meeting", "content": "talk about MCP"})
    hits = json_of(call("search_notes", {"query": "grocery"}))
    assert len(hits) == 1
    assert hits[0]["title"] == "Grocery list"


def test_search_matches_content():
    call("add_note", {"title": "A", "content": "remember the MCP protocol"})
    call("add_note", {"title": "B", "content": "unrelated text"})
    hits = json_of(call("search_notes", {"query": "protocol"}))
    assert [n["title"] for n in hits] == ["A"]


def test_search_is_case_insensitive():
    call("add_note", {"title": "Python tips", "content": "use virtualenvs"})
    hits = json_of(call("search_notes", {"query": "PYTHON"}))
    assert len(hits) == 1


def test_search_no_match_returns_empty_array():
    call("add_note", {"title": "Only note", "content": "text"})
    hits = json_of(call("search_notes", {"query": "zzz-not-found"}))
    assert hits == []


def test_search_empty_query_is_tool_error():
    r = call("search_notes", {"query": "   "})
    assert r.is_error is True


def test_search_missing_query_is_tool_error():
    r = call("search_notes", {})
    assert r.is_error is True


# ---------------------------------------------------------------------------
# Independence from the Task Manager
# ---------------------------------------------------------------------------

def test_notes_db_is_independent_from_task_db(tmp_path):
    import sqlite3

    task_db = tmp_path / "nexusai.db"
    conn = sqlite3.connect(task_db)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO tasks (title) VALUES ('Task row')")
    conn.commit()
    conn.close()

    call("add_note", {"title": "Note only", "content": "in notes db"})

    # notes are in the notes database, not the task database
    conn = sqlite3.connect(notes_server.get_db_path())
    note_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "notes" in note_tables
    assert "tasks" not in note_tables

    # and the task db has no notes table
    conn = sqlite3.connect(task_db)
    task_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "notes" not in task_tables


# ---------------------------------------------------------------------------
# Lesson 3 — MCP Resources & Prompts (Notes Manager server)
# ---------------------------------------------------------------------------

def _n_resources():
    async def s():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return (await c.list_resources()).resources
    return asyncio.run(s())


def _n_read(uri):
    async def s():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return await c.read_resource(uri)
    return asyncio.run(s())


def _n_prompt(name, args=None):
    async def s():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return await c.get_prompt(name, args)
    return asyncio.run(s())


def _n_prompt_names():
    async def s():
        async with Client(notes_server.mcp, raise_exceptions=True) as c:
            return [p.name for p in (await c.list_prompts()).prompts]
    return asyncio.run(s())


def test_notes_resource_discovery():
    uris = {r.uri for r in _n_resources()}
    assert "notes://summary" in uris


def test_notes_resource_read_returns_real_data():
    call("add_note", {"title": "Alpha note", "content": "first body"})
    call("add_note", {"title": "Beta note", "content": "second body"})
    result = _n_read("notes://summary")
    data = json.loads(result.contents[0].text)
    assert data["count"] == 2
    assert {n["title"] for n in data["notes"]} == {"Alpha note", "Beta note"}


def test_notes_resource_read_does_not_modify_db():
    call("add_note", {"title": "Probe", "content": "body"})
    before = notes_server.db_list_notes()
    _n_read("notes://summary")
    after = notes_server.db_list_notes()
    assert [n["id"] for n in before] == [n["id"] for n in after]


def test_notes_resource_never_touches_task_db(tmp_path):
    task_db = tmp_path / "foreign.db"
    import sqlite3
    conn = sqlite3.connect(task_db)
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit(); conn.close()
    _n_read("notes://summary")
    conn = sqlite3.connect(task_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "tasks" in tables and len(tables) == 1


def test_notes_prompt_discovery():
    assert "summarize_notes" in _n_prompt_names()


def test_notes_prompt_valid_query_is_read_only():
    call("add_note", {"title": "MCP learning", "content": "resources and prompts"})
    before = len(notes_server.db_list_notes())
    result = _n_prompt("summarize_notes", {"query": "MCP"})
    text = "".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    assert "MCP learning" in text
    assert "Do NOT modify" in text
    assert len(notes_server.db_list_notes()) == before


def test_notes_prompt_star_query_lists_all():
    call("add_note", {"title": "One", "content": "aaa"})
    call("add_note", {"title": "Two", "content": "bbb"})
    result = _n_prompt("summarize_notes", {"query": "*"})
    text = "".join(m.content.text for m in result.messages if hasattr(m.content, "text"))
    assert "One" in text and "Two" in text


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


def test_notes_prompt_missing_required_query_fails():
    _assert_prompt_mcp_error(lambda: _n_prompt("summarize_notes", {}))


def test_notes_prompt_invalid_query_fails():
    _assert_prompt_mcp_error(lambda: _n_prompt("summarize_notes", {"query": "x" * 101}))
