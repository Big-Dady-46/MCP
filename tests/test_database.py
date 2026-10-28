"""Tests for database.py — run on a TEMPORARY database, never the real one."""

import pytest

import database as db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point database.py at a fresh file inside pytest's temp folder."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    yield db
    # Nothing to clean up: tmp_path is removed by pytest itself.


def test_init_db_creates_tables(temp_db):
    with temp_db.get_conn() as conn:
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"tasks", "activity"} <= tables


def test_create_task(temp_db):
    task = temp_db.create_task("Learn MCP")
    assert task["id"] >= 1
    assert task["title"] == "Learn MCP"
    assert task["completed"] is False
    assert task["created_at"]


def test_create_task_is_persisted(temp_db):
    temp_db.create_task("Persisted")
    titles = [t["title"] for t in temp_db.list_tasks()]
    assert "Persisted" in titles


def test_list_tasks_filters(temp_db):
    a = temp_db.create_task("A")
    b = temp_db.create_task("B")
    temp_db.complete_task(b["id"])
    assert len(temp_db.list_tasks("all")) == 2
    assert [t["id"] for t in temp_db.list_tasks("pending")] == [a["id"]]
    assert [t["id"] for t in temp_db.list_tasks("completed")] == [b["id"]]


def test_list_tasks_rejects_bad_filter(temp_db):
    with pytest.raises(ValueError):
        temp_db.list_tasks("banana")


def test_complete_task(temp_db):
    task = temp_db.create_task("Finish")
    done = temp_db.complete_task(task["id"])
    assert done["completed"] is True
    assert temp_db.get_task(task["id"])["completed"] is True


def test_complete_unknown_task(temp_db):
    with pytest.raises(ValueError):
        temp_db.complete_task(9999)


def test_delete_task(temp_db):
    task = temp_db.create_task("Delete me")
    deleted = temp_db.delete_task(task["id"])
    assert deleted["id"] == task["id"]
    assert temp_db.get_task(task["id"]) is None
    assert temp_db.list_tasks() == []


def test_delete_unknown_task(temp_db):
    with pytest.raises(ValueError):
        temp_db.delete_task(9999)


@pytest.mark.parametrize("bad_title", ["", "   ", None, 123])
def test_empty_or_invalid_titles_rejected(temp_db, bad_title):
    with pytest.raises(ValueError):
        temp_db.create_task(bad_title)


def test_title_too_long_rejected(temp_db):
    with pytest.raises(ValueError):
        temp_db.create_task("x" * 201)


def test_invalid_task_ids(temp_db):
    for bad in (0, -5, "abc", None):
        with pytest.raises(ValueError):
            temp_db.get_task(bad)
        with pytest.raises(ValueError):
            temp_db.complete_task(bad)
        with pytest.raises(ValueError):
            temp_db.delete_task(bad)


def test_stats(temp_db):
    assert temp_db.get_stats() == {"total": 0, "completed": 0, "pending": 0}
    a = temp_db.create_task("A")
    temp_db.create_task("B")
    temp_db.complete_task(a["id"])
    assert temp_db.get_stats() == {"total": 2, "completed": 1, "pending": 1}


def test_activity_recorded_with_source(temp_db):
    temp_db.create_task("Logged", source="mcp")
    rows = temp_db.list_activities()
    assert rows[0]["action"] == "task_created"
    assert rows[0]["source"] == "mcp"
    assert rows[0]["result"] == "success"


def test_title_is_normalised(temp_db):
    assert temp_db.create_task("  spaced   out  ")["title"] == "spaced out"
