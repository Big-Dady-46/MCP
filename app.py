"""NexusAI — MCP Learning Workspace : local Flask web application.

Binds to 127.0.0.1 only (localhost). No cloud, no API key, no external
services. All business logic lives in database.py so the MCP server and
this app always operate on the same data.
"""

import os
import platform
import secrets
import sys

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import database as db
import mcp_server
import oauth_meta
import store_routes

APP_NAME = "NexusAI"
SUBTITLE = "MCP Learning Workspace"

app = Flask(__name__)


def _resolve_secret_key() -> str:
    """Secret handling that is safe on every platform.

    - Production (VERCEL=1 or NEXUSAI_REQUIRE_SECRET=1): a strong secret MUST
      be provided via the NEXUSAI_SECRET environment variable, otherwise we
      refuse to start (a random per-process key would silently break session
      cookies and CSRF protection across cold starts / multiple instances).
    - Local development: fall back to a random per-process key (convenient,
      never persisted, never logged).
    """
    secret = os.environ.get("NEXUSAI_SECRET", "").strip()
    if secret:
        return secret
    if os.environ.get("VERCEL") or os.environ.get("NEXUSAI_REQUIRE_SECRET"):
        raise RuntimeError(
            "NEXUSAI_SECRET environment variable is required in production. "
            "Set it in your deployment settings (e.g. `vercel env add "
            "NEXUSAI_SECRET production`) with a long random value."
        )
    return secrets.token_hex(32)


app.secret_key = _resolve_secret_key()
app.register_blueprint(store_routes.bp)

NAV_ITEMS = [
    ("overview", "Overview", "i-grid"),
    ("tasks", "My Tasks", "i-check-square"),
    ("store.dashboard", "Store", "i-cart"),
    ("store.products", "Products", "i-box"),
    ("store.sales", "Sales", "i-cash"),
    ("store.purchases", "Purchases", "i-truck"),
    ("store.workers", "Workers", "i-users"),
    ("store.expenses", "Expenses", "i-receipt"),
    ("store.reports", "Reports", "i-chart"),
    ("store.demo", "Demo Data", "i-flask"),
    ("tools", "MCP Tools", "i-wrench"),
    ("status", "Server Status", "i-pulse"),
    ("activity", "Activity Logs", "i-list"),
    ("settings", "Settings", "i-sliders"),
]


# ---------------------------------------------------------------------------
# CSRF protection: one token per browser session, checked on every POST
# ---------------------------------------------------------------------------

@app.before_request
def protect_post():
    if request.method != "POST":
        return None
    token = session.get("csrf_token")
    if not token:
        abort(400, description="Session expired. Please reload the page.")
    submitted = request.form.get("csrf_token", "")
    if not secrets.compare_digest(str(submitted), token):
        abort(400, description="Invalid CSRF token. Please reload the page.")
    return None


@app.context_processor
def inject_globals():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return {
        "csrf_token": session["csrf_token"],
        "nav_items": NAV_ITEMS,
        "app_name": APP_NAME,
        "subtitle": SUBTITLE,
        "active": request.endpoint or "overview",
    }


@app.errorhandler(400)
def bad_request(err):
    flash(str(getattr(err, "description", "Bad request.")), "error")
    return redirect(url_for("overview"))


# ---------------------------------------------------------------------------
# Health check (for uptime monitors and post-deploy verification)
# ---------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    """Lightweight JSON health endpoint. 200 = fully healthy, 503 = degraded."""
    checks = {
        "app": True,
        "database": False,
        "mcp": False,
    }
    try:
        db.get_stats()
        checks["database"] = True
    except Exception:
        checks["database"] = False
    try:
        mcp_server.get_tool_definitions()
        checks["mcp"] = True
    except Exception:
        checks["mcp"] = False
    healthy = all(checks.values())
    return (
        jsonify({"status": "ok" if healthy else "degraded", "checks": checks}),
        200 if healthy else 503,
    )


# ---------------------------------------------------------------------------
# OAuth discovery for the remote MCP gateway
#
# On Vercel, /.well-known/* is a RESERVED path: it cannot be rewritten to
# another function, so the main app must serve these two documents. The
# actual authorize/token/register endpoints live in mcp_gateway.py under
# /mcp_gateway/*. Metadata builders are shared (oauth_meta.py) so the two
# sources can never drift (enforced by tests/test_gateway.py).
# ---------------------------------------------------------------------------

@app.route("/.well-known/oauth-protected-resource")
@app.route("/.well-known/oauth-protected-resource/<path:resource_path>")
def well_known_protected_resource(resource_path=None):
    return jsonify(oauth_meta.protected_resource_metadata(
        oauth_meta.normalize_origin(request.url_root)))


@app.route("/.well-known/oauth-authorization-server")
def well_known_authorization_server():
    return jsonify(oauth_meta.authorization_server_metadata(
        oauth_meta.normalize_origin(request.url_root)))


# ---------------------------------------------------------------------------
# Validation helper (same rules as the MCP tools)
# ---------------------------------------------------------------------------

def _validated_title() -> str | None:
    """Return a clean title or None (after flashing an error)."""
    try:
        return db.validate_title(request.form.get("title", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return None


def _task_id_or_none(raw) -> int | None:
    try:
        return db._validate_id(raw)
    except ValueError as exc:
        flash(str(exc), "error")
        return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def overview():
    stats = db.get_stats()
    try:
        tools = mcp_server.get_tool_definitions()
        tool_count = len(tools)
        mcp_ok, mcp_msg = True, f"{tool_count} tools registered and importable"
    except Exception as exc:
        tool_count = 0
        mcp_ok, mcp_msg = False, f"MCP server module error: {exc}"
    return render_template(
        "index.html",
        stats=stats,
        tool_count=tool_count,
        mcp_ok=mcp_ok,
        mcp_msg=mcp_msg,
        activities=db.list_activities(6),
        inspector_cmd=inspector_command(),
    )


@app.route("/tasks")
def tasks():
    status = request.args.get("status", "all")
    if status not in ("all", "pending", "completed"):
        status = "all"
    try:
        items = db.list_tasks(status)
        error = None
    except Exception as exc:
        items = []
        error = f"Could not read tasks: {exc}"
    return render_template(
        "tasks.html", tasks=items, status=status, error=error,
        stats=db.get_stats(),
    )


@app.route("/tasks", methods=["POST"])
def task_create():
    title = _validated_title()
    if title is None:
        return redirect(url_for("tasks", status=request.form.get("back", "all")))
    try:
        task = db.create_task(title, source="web")
        flash(f'Task #{task["id"]} created.', "success")
    except Exception as exc:
        flash(f"Could not create task: {exc}", "error")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/complete", methods=["POST"])
def task_complete(task_id):
    try:
        task = db.complete_task(task_id, source="web")
        flash(f'Task #{task["id"]} marked complete.', "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Could not update task: {exc}", "error")
    return redirect(url_for("tasks"))


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id):
    try:
        task = db.delete_task(task_id, source="web")
        flash(f'Task #{task["id"]} deleted.', "success")
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception as exc:
        flash(f"Could not delete task: {exc}", "error")
    return redirect(url_for("tasks"))


@app.route("/tools")
def tools():
    try:
        tool_defs = mcp_server.get_tool_definitions()
        error = None
    except Exception as exc:
        tool_defs = []
        error = f"Could not read tools from the MCP server module: {exc}"
    return render_template(
        "tools.html", tools=tool_defs, error=error,
        inspector_cmd=inspector_command(),
    )


@app.route("/status")
def status():
    web_ok = True
    db_ok, db_msg = db.db_available()
    try:
        tool_defs = mcp_server.get_tool_definitions()
        mcp_ok = True
        mcp_msg = (
            f"Server module loads and exposes {len(tool_defs)} tools. "
            "It is a stdio server, so it only runs while a host "
            "(MCP Inspector or Antigravity) has launched it."
        )
        tool_count = len(tool_defs)
    except Exception as exc:
        mcp_ok = False
        mcp_msg = f"MCP server module failed to load: {exc}"
        tool_count = 0
    return render_template(
        "status.html",
        web_ok=web_ok, db_ok=db_ok, db_msg=db_msg,
        mcp_ok=mcp_ok, mcp_msg=mcp_msg, tool_count=tool_count,
        db_path=db.get_db_path(),
        missing_deps=missing_dependencies(),
        inspector_cmd=inspector_command(),
    )


@app.route("/activity")
def activity():
    return render_template("activity.html", activities=db.list_activities(200))


@app.route("/settings")
def settings():
    import flask
    import mcp as mcp_pkg
    info = {
        "app_name": APP_NAME,
        "subtitle": SUBTITLE,
        "db_path": db.get_db_path(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "flask": flask.__version__,
        "mcp_sdk": getattr(mcp_pkg, "__version__", "installed (see pip list)"),
        "platform": platform.platform(),
        "host": "127.0.0.1",
        "port": 5000,
        "venv": os.environ.get("VIRTUAL_ENV", "not active in this process"),
    }
    return render_template("settings.html", info=info,
                           inspector_cmd=inspector_command())


# ---------------------------------------------------------------------------
# Helpers for status/settings pages
# ---------------------------------------------------------------------------

def inspector_command() -> str:
    """The exact command that launches MCP Inspector for this file."""
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        exe = os.path.join(venv, "Scripts", "mcp.exe")
        if os.path.exists(exe):
            return f'"{exe}" dev mcp_server.py'
    return "mcp dev mcp_server.py"


def missing_dependencies() -> list:
    missing = []
    try:
        import flask  # noqa: F401
    except ImportError:
        missing.append("flask — pip install -r requirements.txt")
    try:
        import mcp  # noqa: F401
    except ImportError:
        missing.append('mcp[cli] — pip install -r requirements.txt')
    return missing


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    # Localhost only. This is a learning project, not a public service.
    app.run(host="127.0.0.1", port=5000, debug=False)
