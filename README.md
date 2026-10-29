# NexusAI — MCP Learning Workspace

## Lesson 4 — Beverage Store Management System (added 27 Oct 2026)

Full store management controlled by MCP — same host config, new **third server**:

- **`store_server.py`** — 25 tools, 4 resources (`nexus://store/summary`, `nexus://inventory/low-stock`, `nexus://sales/today`, `nexus://staff/summary`), 2 read-only prompts (`stock_review`, `business_report_prompt`)
- **`store_db.py`** — separate `store.db` (never touches `nexusai.db` / `notes.db`); money stored as INTEGER paisa; atomic sales & purchases (stock moves in the same transaction); COGS captured per sale line; `[DEMO]` rows can be seeded/reset safely
- **Web UI** — `/store/` dashboard, products, POS sales, purchases/suppliers, workers (attendance + salaries), expenses, reports, demo-data page (same CSRF-protected Flask app)
- **Host config** — `.agents/mcp_config.json` now has `nexusai-store` (same pattern as `nexusai` and `nexusai-notes`)
- **Verify** — `python verify.py` now runs 34 checks across all three servers; `python -m pytest -q` → **101 passed** (72 original + 29 store)

Tool list: list_products, get_product, add_product, update_product, adjust_stock, get_low_stock, create_sale, list_sales, get_sale, get_sales_summary, create_purchase, list_purchases, list_workers, add_worker, update_worker, record_attendance, record_salary_payment, record_expense, get_business_summary, get_profit_loss, get_stock_valuation, seed_demo_data, reset_demo_data, get_dashboard_summary, generate_business_report.

---

A small, fully local task manager built for one purpose: **learning the Model Context Protocol (MCP)**.
The task manager is the practice project; MCP is the lesson.

- **No paid services, no API keys, no cloud.** Everything runs on your machine.
- **One SQLite database** shared by the Flask web app and the MCP server — create a task in one, see it in the other.
- Stack: Python + Flask + SQLite + official `mcp` Python SDK (v2) + vanilla HTML/CSS/JS.

---

## Quick Start (Windows — copy/paste)

```powershell
cd "C:\Users\Abdul-Hannan\Desktop\Projects\MCP"
.\.venv\Scripts\Activate.ps1        # activate the existing venv (once per terminal)
python app.py                       # web app  ->  http://127.0.0.1:5000
```

If dependencies are ever missing (fresh clone): `pip install -r requirements.txt`

**Tests & verification**
```powershell
python -m pytest -q                 # full suite: 101 tests
python verify.py                    # live check of all three MCP servers (34 checks)
python client_integration_check.py  # stdio client integration, temp DBs only (45 checks)
```

**MCP Inspector (one terminal per server)**
```powershell
.\.venv\Scripts\mcp.exe dev mcp_server.py                 # Task Manager
.\.venv\Scripts\mcp.exe dev notes_server\notes_server.py  # Notes Manager
.\.venv\Scripts\mcp.exe dev store_server.py               # Store Manager
```

**What each server exposes**

| | Task Manager (`mcp_server.py`) | Notes Manager (`notes_server/notes_server.py`) |
|---|---|---|
| Tools | `add_task`, `list_tasks`, `complete_task`, `delete_task` | `add_note`, `list_notes`, `search_notes` |
| Resource URI | `nexus://tasks/summary` | `notes://summary` |
| Prompt | `review_pending_tasks` (arg: `focus`, optional) | `summarize_notes` (arg: `query`, required; `*` = all) |
| Database | `nexusai.db` | `notes_server/notes.db` (independent) |

**Tools vs Resources vs Prompts:** a **tool** is an action the *model* calls (may change data); a **resource** is read-only data addressed by a URI (never changes data); a **prompt** is a template the *user* picks from a menu (fills text into the chat, never runs tools).

**Host connection:** `.agents/mcp_config.json` already registers all three servers for Antigravity-style hosts (`mcpServers` → `command` + `args` + `cwd`). For other hosts, point them at the same three commands using that host's documented stdio config format (see the *Connect an MCP Client* section below). Common problems: missing `.venv` activation → `ModuleNotFoundError`; Node.js missing → Inspector won't start (free LTS from nodejs.org); port 5000 busy → edit `app.run(...)` in `app.py`. More troubleshooting: section 8 below.

---

## Connect an MCP Client

**Verified 27 Oct 2026 — stdio tested, GUI not tested.** All three servers were launched over
**real stdio** with the exact `command`/`args`/`cwd` from `.agents/mcp_config.json`, using the
official Python MCP client: initialize handshake, tool/resource/prompt discovery, a full write
workflow (add product → receive purchase → sell → read receipt), rejected invalid calls with
state left unchanged, reversible cleanup, plus the Flask UI over real localhost HTTP — and the
real databases were byte-identical afterwards. **45/45 checks passed** (log:
`INTEGRATION_OUTPUT.txt`; re-run with `python client_integration_check.py`).
**Not verified:** clicking around inside a host GUI (Antigravity IDE or any chat UI) — the
protocol and config are proven; the host-side steps below are manual.

**Exact launch commands** (one process per server, stdio only — no ports, no network, no keys):

| Server | Command | cwd |
|---|---|---|
| `nexusai` (tasks) | `.venv\Scripts\python.exe mcp_server.py` | project root |
| `nexusai-notes` | `.venv\Scripts\python.exe notes_server.py` | `notes_server\` |
| `nexusai-store` (store) | `.venv\Scripts\python.exe store_server.py` | project root |

**Antigravity** (schema verified at <https://antigravity.google/docs/mcp/>): the workspace file
`.agents/mcp_config.json` in this project already registers all three servers — open this folder
as the workspace, then **… (agent panel) → MCP Servers → Manage MCP Servers → View raw config** to
inspect it, and refresh/restart so the tools appear. If a build ignores the workspace file, merge
the same `mcpServers` object into the global `%USERPROFILE%\.gemini\config\mcp_config.json`
(that file exists here and is currently empty — merge, don't wipe other entries).

**OpenCode:** no `opencode.json` exists in this project yet. To connect, create one at the
project root (format verified at <https://opencode.ai/docs/mcp-servers/>), restart OpenCode after
saving:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "nexusai": {
      "type": "local",
      "command": ["C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\.venv\\Scripts\\python.exe",
                  "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\mcp_server.py"],
      "cwd": "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP",
      "enabled": true
    },
    "nexusai-notes": {
      "type": "local",
      "command": ["C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\.venv\\Scripts\\python.exe",
                  "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\notes_server\\notes_server.py"],
      "cwd": "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\notes_server",
      "enabled": true
    },
    "nexusai-store": {
      "type": "local",
      "command": ["C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\.venv\\Scripts\\python.exe",
                  "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\store_server.py"],
      "cwd": "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP",
      "enabled": true
    }
  }
}
```

**Safe first test in any host:** start read-only — *"list low-stock products using nexusai-store"*
or read the resource `nexus://store/summary`. For a write test, `add_product` a clearly named
`[TEST]` item, read it back, then deactivate it (`update_product` with `confirm=true,
is_active=false`) instead of deleting. Don't run `seed_demo_data` / `reset_demo_data` unless you
actually want demo rows (reset only removes `[DEMO]` rows).

**Without a host:** `.venv\Scripts\mcp.exe dev store_server.py` opens MCP Inspector (Node.js
required), or run `python client_integration_check.py` — 45 automated stdio checks against temp
databases; your real `nexusai.db` / `notes.db` / `store.db` are never touched.

---

## Deploy to Vercel (web dashboard — ready, with one honest caveat)

The Flask dashboard is **prepared** for Vercel Functions (Python preset, `app.py`):

- Importing the app never touches the filesystem (schema is created lazily on first DB use),
  so the read-only deployment filesystem is safe.
- On Vercel (`VERCEL=1`) the SQLite files default to `/tmp` — the only writable directory
  there — and **`/tmp` is ephemeral: all data is lost on every cold start / redeploy.**
  The dashboard and `/healthz` work; the DB simply resets. Persistent storage needs a
  remote database driver (Turso/libsql etc.), which is **not yet enabled** — do not enable
  it blindly; it must be tested on the deployment platform first.
- `NEXUSAI_SECRET` is **required** in production (refuses to boot without it). Locally a
  random per-process secret is used so nothing changes in development.
- `GET /healthz` returns JSON `{"status":"ok","checks":{...}}` — 200 healthy, 503 degraded.
- `vercel.json` + `.vercelignore` + `.python-version` keep `.venv`, DBs, tests and outputs
  out of the bundle.

**Deploy steps (needs a free Vercel account — authorization is yours to perform):**

```powershell
# 1. Install the CLI once (or use the web dashboard import from GitHub)
npm install -g vercel
vercel login

# 2. From this project folder — link, set the secret, deploy
vercel link
vercel env add NEXUSAI_SECRET production   # paste a long random value
vercel deploy --prod

# 3. Verify
#    open https://<your-project>.vercel.app/healthz   →  {"status":"ok",...}
```

**Hobby plan is free** for non-commercial use (limits: 300s function timeout, pauses after
inactivity). The stdio MCP servers stay **stdio-only on your machine**; the web dashboard
plus the Phase 2 remote gateway below are what run on Vercel.

---

## Phase 2: Remote MCP Gateway — control it from ANY AI (ChatGPT, etc.)

`mcp_gateway.py` exposes the store over **MCP Streamable HTTP** with real authentication,
so any AI host that supports remote MCP can use it (read-only demo mode).

**MCP endpoint (after deploy):** `https://<your-project>.vercel.app/mcp_gateway`
(aliased at `/mcp` via rewrite)

**Authentication — dual (both tested locally over real HTTP):**

1. **Static bearer** (for curl / scripts / OpenAI Agents API / OpenCode-over-HTTP):
   header `Authorization: Bearer <MCP_GATEWAY_TOKEN>`
2. **OAuth 2.1 (PKCE S256 + dynamic client registration)** — what **ChatGPT** uses
   (ChatGPT does *not* support static custom auth headers; only OAuth or no-auth).
   Login user = `MCP_GATEWAY_USER` (default `admin`), password = your
   `MCP_GATEWAY_TOKEN`. Discovery is served on the reserved
   `/.well-known/oauth-authorization-server` + `/.well-known/oauth-protected-resource`
   paths by the main Flask app (Vercel cannot rewrite `/.well-known`).

**Tool surface: READ-ONLY (13 of 25 store tools).** No sales/purchases/stock edits:
on Vercel the database lives in `/tmp` (ephemeral, re-seeded with `[DEMO]` data on every
cold start, `persistent: false` in `/healthz`) — write access would create false
confidence. **Real data stays local** on your PC, reachable only via the stdio servers.

**Required environment variables (Vercel):**

| Variable | Required | Purpose |
|---|---|---|
| `NEXUSAI_SECRET` | yes | Flask sessions/CSRF (dashboard) |
| `MCP_GATEWAY_TOKEN` | yes | bearer + OAuth login password (long random!) |
| `MCP_GATEWAY_USER` | no | OAuth login username (default `admin`) |
| `GATEWAY_MODE` | no | must be `readonly-demo` (default); other values = fail-closed |

**Local test:** `python gateway_integration_check.py` → 51 checks (real uvicorn +
official MCP client, bearer + full OAuth flow, secret-leak scan, real-DB hash guard).

**ChatGPT setup (after deploying):**
1. Deploy + set the env vars (section above), confirm
   `https://<proj>.vercel.app/healthz` (dashboard) and
   `https://<proj>.vercel.app/mcp_gateway` → `401` (gateway alive).
2. Verify discovery: open `https://<proj>.vercel.app/.well-known/oauth-authorization-server`
   (JSON) — if it 404s, stop and report it (routing blocker).
3. In ChatGPT: **Plugins → Add custom MCP server** →
   URL: `https://<proj>.vercel.app/mcp_gateway` → Authentication: **OAuth** →
   Create → install → when the login form appears use username `admin`
   (or `MCP_GATEWAY_USER`) and password = your `MCP_GATEWAY_TOKEN`.
4. Test in chat: *"Using the NexusAI app, list low-stock products"* — only then is the
   connection verified.

**Known limitations (honest):** stateless HMAC auth codes are valid ≤60s (PKCE-protected);
OAuth access tokens last 24h (refresh tokens 30d); database is ephemeral demo data;
live ChatGPT GUI verification requires YOUR ChatGPT account.

---

## 1. What MCP is (beginner explanation)

**MCP (Model Context Protocol)** is an open standard — like USB for AI apps. It lets an AI application
("host") plug into external capabilities ("servers") without custom code for each one.

| Term | What it means here |
|---|---|
| **Host** | The AI app you use: Antigravity, or MCP Inspector while testing. It owns the conversation and decides when to call a tool. |
| **Client** | A library *inside* the host that speaks the MCP protocol to one server (connects, lists tools, calls them). |
| **Server** | Your `mcp_server.py` — a process that advertises tools and executes them. Not a web server; it runs over stdio (stdin/stdout). |
| **Tool** | A callable function the AI can invoke: `add_task`, `list_tasks`, `complete_task`, `delete_task`. |

### How tool discovery works
On connect, the client sends a `tools/list` request. The SDK answers with each tool's **name**
(from the function name), **description** (from the docstring) and **JSON Schema** (generated from
the Python type hints). You never hand-write the schema.

### How tool execution works
1. The model picks a tool and fills in arguments.
2. The client sends `tools/call` to the server.
3. The SDK validates the arguments **against the schema before your function runs** (wrong type → instant tool error).
4. Your Python function executes — here, a real SQL write.
5. The result returns as text the model can read; `is_error=True` signals failure.

### How arguments are validated
Two layers: the SDK rejects values violating the schema (`task_id` must be an integer ≥ 1,
`title` max 200 chars), and your code validates semantics (title not empty/whitespace).

### How the MCP server accesses the database
Both `app.py` and `mcp_server.py` import the same `database.py`, which opens
`nexusai.db` with parameterised SQL (`?` placeholders — user input is never executed as SQL).

### Web API vs MCP tool
A web API speaks HTTP to any client and returns JSON. An MCP tool speaks **MCP** to an AI host:
the tool is *described* so a model can decide when to use it, and errors are returned in a form
the model can read and correct from.

### The same task, two doors
Type a title in **My Tasks** → row in `nexusai.db`. Or ask your AI to "add a task" → the model
calls `add_task` → same table, same row format. Refresh the web page: it's there.
(Reverse works too: delete via MCP, gone from the web UI.)

---

## 2. Project structure

```
MCP/
├── app.py               # Flask web app (localhost:5000)
├── mcp_server.py        # MCP server with 4 tools (official SDK v2)
├── database.py          # Single source of truth for all SQLite operations
├── conftest.py          # Puts project root on sys.path for pytest
├── requirements.txt
├── .gitignore
├── .agents/
│   └── mcp_config.json  # Antigravity MCP config (absolute paths, ready to use)
├── templates/           # base, index, tasks, tools, status, activity, settings
├── static/css/style.css # Design tokens + full UI (light SaaS theme)
├── static/js/app.js     # Mobile nav, delete confirmation, alert auto-hide
├── tests/
│   ├── test_database.py     # 20 tests: CRUD, validation, stats, activity
│   └── test_mcp_tools.py    # 18 tests: discovery, execution, shared data, CSRF
└── nexusai.db           # Created automatically (git-ignored)
```

---

## 3. Windows setup (PowerShell)

Run from the project folder:

```powershell
# 1. Check Python (needs 3.11+; this project verified on 3.14.8)
python --version

# 2. Create the virtual environment (once)
python -m venv .venv

# 3. Activate it (each new terminal)
.\.venv\Scripts\Activate.ps1
#    If blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 4. Install dependencies (once)
pip install -r requirements.txt

# 5. Start the web app
python app.py
#    → http://127.0.0.1:5000   (Ctrl+C stops it)

# 6. Open it in the browser
Start-Http http://127.0.0.1:5000   # or just open the URL

# 7. Run the tests (38 tests, temporary DB only — your data is safe)
python -m pytest -q

# 8. Start MCP Inspector
mcp dev mcp_server.py
#    (uses npx under the hood → Node.js required: node -v to check,
#     free LTS from https://nodejs.org if missing)
```

Note: PowerShell may block `npx.ps1` (execution policy). If so, use `cmd /c "npx ..."` or run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that terminal.

---

## 4. Testing with MCP Inspector (step by step)

1. Activate the venv (command above).
2. Run `mcp dev mcp_server.py`.
3. Inspector prints a URL (default `http://127.0.0.1:5173`) and opens automatically.
4. Open the **Tools** tab → you'll see exactly 4 tools with forms built from the schemas.
5. Call **`add_task`** → `{"title": "Created from Inspector"}` → returns the created task JSON.
6. Call **`list_tasks`** → the new task is in the list.
7. Copy its `id` → call **`complete_task`** with `{"task_id": <id>}` → `"completed": true`.
8. Call **`delete_task`** with the same id → `"deleted": true`.
9. Switch to the web app's **My Tasks** and **Activity Logs** pages → the same changes are there,
   because both sides use `nexusai.db`. That's the core lesson: *the AI changed your real data.*

Try a bad call too: `complete_task` with `task_id: 999999` → `isError: true` with a clear message.

---

## 5. Connecting Antigravity (MCP host)

Verified format from the official docs (<https://antigravity.google/docs/mcp/>):
a single `mcpServers` object; stdio servers use `command` + `args` + `cwd`.
**No API keys needed for this server.**

**This project already contains a ready config:** `.agents\mcp_config.json`
(workspace scope — use this folder as the workspace):

```json
{
  "mcpServers": {
    "nexusai": {
      "command": "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP\\mcp_server.py"],
      "cwd": "C:\\Users\\Abdul-Hannan\\Desktop\\Projects\\MCP"
    }
  }
}
```

(Paths are absolute and machine-generated — no guessed usernames. Note the doubled backslashes `\\` in JSON.)

**Alternative locations:**
- Global (all workspaces): `%USERPROFILE%\.gemini\config\mcp_config.json` — same JSON, merge into the existing `mcpServers` object.
- In the IDE: **… menu (agent side panel) → MCP Servers → Manage MCP Servers → View raw config**, then edit and save.

**Then reload:** IDE → User Settings → Customizations → *Installed MCP Servers* → **Refresh**
(or restart Antigravity).

**Verify:** the `nexusai` server shows as installed and its 4 tools (`add_task`, `list_tasks`,
`complete_task`, `delete_task`) appear in the agent's tool list. Ask the agent:
*"List my tasks using nexusai"* — it should call `list_tasks` and read from the real database.
MCP tools default to **Ask mode**: approve the call when prompted. You can set a policy
`mcp(nexusai/*)` to always allow them.

> Keep the exact JSON keys: `command`/`args`/`cwd`. The legacy `url`/`httpUrl` fields are not
> supported (use `serverUrl` only for remote servers — this project is local stdio).

---

## 6. How the four tools work

| Tool | Args | Mode | Behavior |
|---|---|---|---|
| `add_task` | `title` (1–200 chars) | modifies data | Validates → `INSERT` → returns created task JSON |
| `list_tasks` | `status`: `all`\|`pending`\|`completed` | **read-only** | Returns task array, newest first |
| `complete_task` | `task_id` (int ≥ 1) | modifies data (idempotent) | `UPDATE completed=1` → updated task or error |
| `delete_task` | `task_id` (int ≥ 1) | **destructive** | Verifies existence → `DELETE` → confirmation JSON |

Safety annotations (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`)
tell the host's UI whether to ask before running. Errors use `ToolError`, so the caller gets
`isError: true` with the real message — a failed call is never reported as success.

---

## 7. Security & limitations (read this)

- Binds to **127.0.0.1 only**; never expose it publicly — this is a learning project, not a multi-user service.
- Parameterised SQL everywhere; server-side title/id validation; POST-only state changes with **CSRF tokens**; delete confirmed in the UI.
- No shell execution, no eval, no secrets in logs or config (the Flask secret key is random per run).
- `.gitignore` excludes `.venv/`, `nexusai.db*`, `.env`, logs.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: flask/mcp` | Wrong interpreter — activate `.venv`, or use `.\.venv\Scripts\python.exe` |
| `npx` blocked (execution policy) | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or use `cmd` |
| Inspector won't start | Install Node.js LTS (free) from nodejs.org, reopen terminal |
| Antigravity shows no tools | Check absolute paths in `mcp_config.json`, then Refresh Installed MCP Servers; check the IDE's output/logs for spawn errors |
| Port 5000 busy | Edit `app.run(...)` in `app.py` |
| Tests touching my real data? | Impossible — tests point `DB_PATH` at pytest's temp folder |

---

## 9. Final checklist (verified)

- [x] Web interface starts; all 6 pages return HTTP 200
- [x] Light SaaS design with the exact palette + Inter/system typography
- [x] Navigation, task create/complete/delete all working
- [x] SQLite persistence (WAL mode, shared file)
- [x] 4 MCP tools discoverable; schema generated from type hints
- [x] Tools perform **real** database operations — verified via a real stdio client
- [x] Web + MCP share one database (proven in tests and live smoke test)
- [x] MCP Inspector: `mcp dev mcp_server.py` (SDK v2 CLI verified installed)
- [x] Antigravity config written from official docs with real absolute paths
- [x] **38/38 tests pass** (`python -m pytest -q`)
- [x] No paid service, no fabricated data anywhere

---

## Lesson 3 — MCP Resources and Prompts (added 20 Oct 2026)

### The three MCP primitives (simple definitions)
- **Tools** — actions the *model* can call (create a task, save a note). They may change data.
- **Resources** — read-only data a *client/application* can load by URI (a summary, a document). Reading never changes anything.
- **Prompts** — reusable message templates the *user* picks from a menu (slash command). Rendering fills arguments into a template; it never runs tools or writes to a database.

### Resource URIs implemented
| Server | URI | MIME type | Contents |
|---|---|---|---|
| Task Manager (`mcp_server.py`) | `nexus://tasks/summary` | `application/json` | `total`, `pending`, `completed` counts + up to 25 pending titles (real DB read) |
| Notes Manager (`notes_server/notes_server.py`) | `notes://summary` | `application/json` | `count` + up to 50 note titles with ids (own DB only) |

No secrets, environment variables or local paths are exposed. A database failure raises an MCP protocol error (`INTERNAL_ERROR`), never fake data.

### Prompts implemented
| Server | Prompt name | Arguments | Behaviour |
|---|---|---|---|
| Task Manager | `review_pending_tasks` (title: "Review pending tasks") | `focus` — optional string, default `"all"`, max 100 chars | Lists pending tasks and asks the model to summarize and suggest order. Ends with "Do NOT complete, delete or modify any task". |
| Notes Manager | `summarize_notes` (title: "Summarize saved notes") | `query` — **required** string, max 100 chars; `*` = all notes | Groups and summarizes matching notes. Ends with "Do NOT modify, create or delete any note". |

Invalid arguments (`focus` > 100 chars, missing/oversized `query`, unknown prompt name) fail with an MCP protocol error (`INVALID_PARAMS` / unknown-prompt), verified by tests.

### How to test Lesson 3 (Windows, from this folder)
```powershell
# Full suite: 72 tests (55 baseline + 17 Lesson 3)
.\.venv\Scripts\Activate.ps1
python -m pytest -q

# MCP Inspector — Task Manager: open the Resources and Prompts tabs
.\.venv\Scripts\mcp.exe dev mcp_server.py

# MCP Inspector — Notes Manager
.\.venv\Scripts\mcp.exe dev notes_server\notes_server.py
```
In Inspector: **Resources tab** → click `nexus://tasks/summary` (or `notes://summary`) → Read. **Prompts tab** → pick the prompt → fill the form → View Prompt. The Tools tab should still show all 7 tools (4 + 3).

### Host-side notes (OpenCode / Antigravity)
- MCP protocol support and a host's UI support are not identical. Protocol-level, both servers advertise resources (`resources/list`, `resources/read`) and prompts (`prompts/list`, `prompts/get`) — verified with the official SDK test client and MCP Inspector.
- Whether a host shows a **Resources** or **Prompts** menu depends on that host's own UI. In Antigravity, prompts typically appear where the host exposes prompt templates; if your host build does not surface a Resources tab, the data is still readable through Inspector or any MCP client.
- No config changes were required for Lesson 3: `.agents/mcp_config.json` already launches both servers; new capabilities are discovered automatically on connect.

### What Lesson 3 did NOT change
All 7 tool names, both SQLite databases (still independent), the 6-page web UI, CSRF protection, parameterised SQL and localhost-only binding are untouched. Old tests were not modified or weakened (17 new tests were appended).
