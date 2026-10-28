"""Deployment-readiness tests: import purity, lazy DB init, secret handling,
health endpoint, and Vercel config sanity. Uses temp DBs only — never the
real nexusai.db / store.db / notes.db."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import database  # noqa: E402
import store_db  # noqa: E402

PY = sys.executable


def _run(code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("NEXUSAI_DB_PATH", None)
    env.pop("NEXUSAI_STORE_DB", None)
    env.pop("VERCEL", None)
    env.pop("NEXUSAI_REQUIRE_SECRET", None)
    env.pop("NEXUSAI_SECRET", None)
    env.update(env_extra or {})
    return subprocess.run(
        [PY, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. Import purity: importing modules must never touch the filesystem
# ---------------------------------------------------------------------------

def test_import_database_pure_in_readonly_location():
    """database.py must import cleanly even when its DB path cannot be
    created (simulates read-only filesystem / Vercel /opt)."""
    code = (
        "import os, sys;"
        "os.environ['NEXUSAI_DB_PATH'] = r'C:\\nonexistent_dir_x9\\sub\\nexusai.db';"
        "sys.path.insert(0, r'%s');"
        "import database;"
        "print('IMPORT_OK')" % ROOT
    )
    res = _run(code)
    assert res.returncode == 0, res.stderr
    assert "IMPORT_OK" in res.stdout


def test_import_store_db_pure_in_readonly_location():
    code = (
        "import os, sys;"
        "os.environ['NEXUSAI_STORE_DB'] = r'C:\\nonexistent_dir_x9\\sub\\store.db';"
        "sys.path.insert(0, r'%s');"
        "import store_db;"
        "print('IMPORT_OK')" % ROOT
    )
    res = _run(code)
    assert res.returncode == 0, res.stderr
    assert "IMPORT_OK" in res.stdout


def test_import_app_pure_in_readonly_location():
    """The whole web app (app.py + blueprints + MCP servers) must import
    without creating any database file."""
    code = (
        "import os, sys;"
        "os.environ['NEXUSAI_DB_PATH'] = r'C:\\nonexistent_dir_x9\\sub\\nexusai.db';"
        "os.environ['NEXUSAI_STORE_DB'] = r'C:\\nonexistent_dir_x9\\sub\\store.db';"
        "sys.path.insert(0, r'%s');"
        "import app;"
        "print('IMPORT_OK')" % ROOT
    )
    res = _run(code)
    assert res.returncode == 0, res.stderr
    assert "IMPORT_OK" in res.stdout


# ---------------------------------------------------------------------------
# 2. Lazy init: first get_conn() use creates the schema automatically
# ---------------------------------------------------------------------------

def test_database_lazy_init_creates_schema(tmp_path):
    db_file = tmp_path / "lazy.db"
    old_path, old_inited = database.DB_PATH, set(database._initialized_paths)
    try:
        database.DB_PATH = str(db_file)
        database._initialized_paths.clear()
        with database.get_conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert {"tasks", "activity"} <= tables
    finally:
        database.DB_PATH = old_path
        database._initialized_paths.clear()
        database._initialized_paths.update(old_inited)


def test_store_db_lazy_init_creates_schema(tmp_path):
    db_file = tmp_path / "lazy_store.db"
    old_path, old_inited = store_db.DB_PATH, set(store_db._initialized_paths)
    try:
        store_db.DB_PATH = str(db_file)
        store_db._initialized_paths.clear()
        with store_db.get_conn() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "products" in tables
    finally:
        store_db.DB_PATH = old_path
        store_db._initialized_paths.clear()
        store_db._initialized_paths.update(old_inited)


def test_database_first_write_works(tmp_path):
    """Full round-trip through lazy init: add + list + stats."""
    db_file = tmp_path / "rw.db"
    old_path, old_inited = database.DB_PATH, set(database._initialized_paths)
    try:
        database.DB_PATH = str(db_file)
        database._initialized_paths.clear()
        task = database.create_task("deploy readiness", source="pytest")
        assert task["id"] > 0
        assert any(t["id"] == task["id"] for t in database.list_tasks())
        stats = database.get_stats()
        assert stats["total"] >= 1
    finally:
        database.DB_PATH = old_path
        database._initialized_paths.clear()
        database._initialized_paths.update(old_inited)


# ---------------------------------------------------------------------------
# 3. init_db() must keep working for explicit callers (tests, MCP, scripts)
# ---------------------------------------------------------------------------

def test_explicit_init_db_still_works(tmp_path):
    db_file = tmp_path / "explicit.db"
    old_path, old_inited = database.DB_PATH, set(database._initialized_paths)
    try:
        database.DB_PATH = str(db_file)
        database._initialized_paths.clear()
        database.init_db()
        database.init_db()  # idempotent
        assert db_file.exists()
        conn = sqlite3.connect(str(db_file))
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert {"tasks", "activity"} <= names
    finally:
        database.DB_PATH = old_path
        database._initialized_paths.clear()
        database._initialized_paths.update(old_inited)


def test_explicit_store_init_db_still_works(tmp_path):
    db_file = tmp_path / "explicit_store.db"
    old_path, old_inited = store_db.DB_PATH, set(store_db._initialized_paths)
    try:
        store_db.DB_PATH = str(db_file)
        store_db._initialized_paths.clear()
        store_db.init_db()
        assert db_file.exists()
    finally:
        store_db.DB_PATH = old_path
        store_db._initialized_paths.clear()
        store_db._initialized_paths.update(old_inited)


# ---------------------------------------------------------------------------
# 4. Path defaults: /tmp on Vercel, project dir locally
# ---------------------------------------------------------------------------

def test_default_db_dir_is_project_dir_locally():
    assert database._default_db_dir() == database.BASE_DIR
    assert store_db._default_db_dir() == store_db.BASE_DIR


def test_default_db_dir_is_tmp_on_vercel():
    code = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "import database, store_db;"
        "print(database._default_db_dir());"
        "print(store_db._default_db_dir())" % ROOT
    )
    res = _run(code, {"VERCEL": "1"})
    assert res.returncode == 0, res.stderr
    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    assert lines[0] == "/tmp"
    assert lines[1] == "/tmp"


def test_db_path_uses_tmp_on_vercel():
    code = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "import database, store_db;"
        "print(database.DB_PATH);"
        "print(store_db.DB_PATH)" % ROOT
    )
    res = _run(code, {"VERCEL": "1"})
    assert res.returncode == 0, res.stderr
    lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
    assert lines[0].startswith("/tmp")
    assert lines[0].endswith("nexusai.db")
    assert lines[1].startswith("/tmp")
    assert lines[1].endswith("store.db")


# ---------------------------------------------------------------------------
# 5. Secret handling
# ---------------------------------------------------------------------------

_SECRET_BOOT = (
    "import sys;"
    "sys.path.insert(0, r'%s');"
    "import app;"
    "print('BOOT_OK')" % ROOT
)


def test_app_boots_without_secret_locally():
    res = _run(_SECRET_BOOT)
    assert res.returncode == 0, res.stderr
    assert "BOOT_OK" in res.stdout


def test_app_boots_with_secret_when_vercel():
    res = _run(_SECRET_BOOT, {"VERCEL": "1", "NEXUSAI_SECRET": "s" * 48})
    assert res.returncode == 0, res.stderr
    assert "BOOT_OK" in res.stdout


def test_app_refuses_to_boot_on_vercel_without_secret():
    res = _run(_SECRET_BOOT, {"VERCEL": "1"})
    assert res.returncode != 0
    assert "NEXUSAI_SECRET" in (res.stderr + res.stdout)


def test_app_refuses_when_require_secret_flag_set():
    res = _run(_SECRET_BOOT, {"NEXUSAI_REQUIRE_SECRET": "1"})
    assert res.returncode != 0
    assert "NEXUSAI_SECRET" in (res.stderr + res.stdout)


def test_secret_never_logged():
    secret = "super-secret-" + "z" * 40
    res = _run(_SECRET_BOOT, {"VERCEL": "1", "NEXUSAI_SECRET": secret})
    assert secret not in res.stdout
    assert secret not in res.stderr


# ---------------------------------------------------------------------------
# 6. /healthz endpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "web.db"
    store_file = tmp_path / "web_store.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    monkeypatch.setattr(store_db, "DB_PATH", str(store_file))
    database._initialized_paths.clear()
    store_db._initialized_paths.clear()
    import app as app_module

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
    database._initialized_paths.clear()
    store_db._initialized_paths.clear()


def test_healthz_ok(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "ok"
    assert data["checks"]["app"] is True
    assert data["checks"]["database"] is True
    assert data["checks"]["mcp"] is True


def test_healthz_degraded_when_db_broken(client, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(database, "get_stats", boom)
    res = client.get("/healthz")
    assert res.status_code == 503
    data = res.get_json()
    assert data["status"] == "degraded"
    assert data["checks"]["database"] is False
    assert data["checks"]["app"] is True


def test_healthz_no_paths_leaked(client):
    data = client.get("/healthz").get_json()
    blob = json.dumps(data)
    assert "C:\\" not in blob
    assert "/tmp" not in blob
    assert ".db" not in blob


# ---------------------------------------------------------------------------
# 7. Vercel config sanity: deployment files exist and are sane
# ---------------------------------------------------------------------------

def test_vercel_json_exists_and_valid():
    path = ROOT / "vercel.json"
    assert path.exists(), "vercel.json missing"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg.get("$schema") or "version" in cfg or "functions" in cfg


def test_vercel_json_excludes_big_dirs():
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    exclude = cfg.get("functions", {}).get("app.py", {}).get("excludeFiles", "")
    assert ".venv" in exclude
    assert "tests" in exclude
    assert "*.db" in exclude or ".db" in exclude


def test_vercelignore_exists_and_blocks_secrets_dbs():
    path = ROOT / ".vercelignore"
    assert path.exists(), ".vercelignore missing"
    text = path.read_text(encoding="utf-8")
    assert ".venv" in text
    assert "*.db" in text
    assert ".env" in text
    assert "tests" in text


def test_python_version_file():
    path = ROOT / ".python-version"
    assert path.exists(), ".python-version missing"
    version = path.read_text(encoding="utf-8").strip()
    assert version.startswith("3."), version


def test_requirements_no_untested_turso_driver():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "pyturso" not in text
    assert "libsql" not in text


def test_no_real_db_paths_in_requirements():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "nexusai.db" not in text
