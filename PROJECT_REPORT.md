# NEXUSAI — MCP LEARNING WORKSPACE
## Project Report (current state)

**Project path:** `C:\Users\Abdul-Hannan\Desktop\Projects\MCP`
**Report date:** 29 October 2026 (Phase 2: remote MCP gateway added)
**Final automated runs (actual execution):**

| Gate | Result | Exit code |
|---|---|---|
| `pytest -q` | **144 passed** (incl. 18 gateway tests) | 0 |
| `acceptance_audit.py` | **62/62 checks passed** | 0 |
| `client_integration_check.py` | **45/45 checks passed** (stdio preserved) | 0 |
| `gateway_integration_check.py` | **51/51 checks passed** (real HTTP) | 0 |
| `verify.py` (earlier run, 28 Oct) | **34/34 passed** | 0 |
| Real-DB SHA-256 guard | **all 3 databases byte-identical (H0 == final)** | — |

Full logs: `TEST_OUTPUT.txt`, `AUDIT_OUTPUT.txt`, `INTEGRATION_OUTPUT.txt`,
`GATEWAY_OUTPUT.txt`, `VERIFY_OUTPUT.txt`.

---

## 1. What the project is

A local, free (no paid services / no cloud / no API keys), beginner-friendly workspace that
teaches the **Model Context Protocol (MCP)** through a real practice app:

- **Flask web dashboard** (`app.py`, localhost) — tasks + beverage-store management UI.
- **Three independent MCP servers** (stdio), connectable simultaneously from one AI host:
  1. `mcp_server.py` — Task Manager (7 tools, 2 resources, 1 prompt)
  2. `notes_server/notes_server.py` — Notes Manager (3 tools, 1 resource, 1 prompt)
  3. `store_server.py` — Beverage Store PKR (25 tools, 4 resources, 2 prompts)
- **Shared SQLite databases** — `nexusai.db`, `store.db`, `notes_server/notes.db` (WAL mode).
- **Host config** — `.agents/mcp_config.json` with all three servers (absolute paths).

## 2. Technology stack (verified on this machine)

| Component | Actual version / detail |
|---|---|
| Python | 3.14.8 (venv `.venv`) |
| MCP SDK | `mcp 2.3.0` — official v2 API |
| Web framework | Flask 3.1.3 |
| Database | SQLite (stdlib, WAL) |
| Testing | pytest 9.1.1 |
| Frontend | HTML5 + CSS3 + vanilla JS (no framework) |
| Node | 24.21.0 (only for MCP Inspector / npx tooling) |

No React, Next.js, PostgreSQL, Docker, paid APIs or paid hosting are used.

## 3. Test coverage (what actually ran)

- **126 pytest tests** — database layer, MCP tools (tasks + store), Flask routes,
  security (CSRF, secrets), and **25 deployment-readiness tests**
  (`tests/test_deploy_readiness.py`, added 28 Oct).
- **62/62 acceptance audit** — workflows, stdio MCP sessions, Flask pages,
  security, real-DB byte-identity.
- **45/45 client integration** — real MCP stdio handshakes with the exact
  `.agents/mcp_config.json` command/args/cwd, temp-DB isolation, Flask HTTP checks.
- **34/34 verify** — store workflows + append-only activity integrity.

Every gate uses **temporary databases** only. The real `nexusai.db` / `store.db` /
`notes.db` were hashed before and after (SHA-256) and are **byte-identical**:

```
nexusai.db   4D4F0F8D…EAF045   (matches H0)
store.db     34A47A09…8B87EB5   (matches H0)
notes.db     82220958…306CC97   (matches H0)
```

## 4. Vercel deployment preparation (done 28 Oct)

Ready on our side (all verified locally, 25 dedicated tests green):

- **No import-time filesystem writes.** Schema creation is now lazy (first DB use per file)
  in `database.py` and `store_db.py`; `mcp_server.py` / `store_server.py` no longer call
  `init_db()` at import. `app.py` imports cleanly even with a non-existent DB path —
  required because Vercel's filesystem is read-only.
- **`init_db()` still exists and works** for all explicit callers (MCP servers, tests, scripts).
- **Path defaults:** local → project folder (unchanged); on Vercel (`VERCEL=1`) → `/tmp`
  (the only writable directory there; **ephemeral — data resets on cold start**).
- **Secret handling:** `NEXUSAI_SECRET` env var is **required** when `VERCEL=1` (or
  `NEXUSAI_REQUIRE_SECRET=1`) — the app refuses to boot without it. Local dev keeps a
  random per-process secret. The secret is never logged.
- **`GET /healthz`** — JSON status, 200 ok / 503 degraded, booleans only, no paths leaked.
- **Deployment files:** `vercel.json` (Flask preset + `excludeFiles` for `.venv`, tests,
  DBs, outputs, audit scripts, `notes_server/`), `.vercelignore`, `.python-version` (`3.14`).
- **`requirements.txt` unchanged** — deliberately no Turso/libsql driver (see blockers).

## 4-b. Phase 2 — Remote MCP gateway (29 Oct, added)

`mcp_gateway.py` — MCP **Streamable HTTP** (stateless, serverless-safe) so any AI
(ChatGPT, agents, scripts) can control the store remotely:

- **Auth (dual, tested over real sockets):** static `Authorization: Bearer` token
  (curl/agents) + minimal stateless **OAuth 2.1** (PKCE S256, DCR, HMAC-signed
  codes/access/refresh tokens, single-user login page) — ChatGPT supports only
  OAuth/no-auth, not static custom headers (verified from OpenAI docs).
- **Reserved `/.well-known/*` paths** are served by the main Flask app (Vercel
  forbids rewriting `/.well-known`); endpoints live under `/mcp_gateway/*`.
  Shared builders in `oauth_meta.py` (consistency test enforces no drift).
- **Read-only tool surface:** exactly 13 read tools; 12 write tools (sales,
  purchases, stock, expenses, seed/reset) are NOT registered — on Vercel the DB
  is `/tmp` (ephemeral, `[DEMO]`-seeded per cold start, `persistent:false` in
  `/healthz`). No false claims of persistence; real data untouched locally.
- **Fail-closed:** no `MCP_GATEWAY_TOKEN` → healthz `disabled` (503), `/mcp` 503.
  Unsupported `GATEWAY_MODE` also fails closed.
- **New env vars:** `MCP_GATEWAY_TOKEN` (required), `MCP_GATEWAY_USER`
  (default admin), `GATEWAY_MODE` (default readonly-demo).
- **Vercel wiring:** `vercel.json` registers `mcp_gateway.py` as a second function
  (slim bundle) + `/mcp` rewrite; `GATEWAY_OUTPUT.txt` = 51/51 evidence.
- **Preserved:** stdio config/servers untouched (45/45 original integration gate).

## 5. Honest limitations / blockers (not hidden)

1. **Vercel deployment itself is NOT done** — it requires the owner's Vercel account
   (`vercel login` / web authorization). Commands are in README ("Deploy to Vercel").
2. **SQLite on Vercel is ephemeral** (`/tmp`, wiped on cold start). The dashboard and
   `/healthz` work; persistent data needs a remote DB driver that is **not yet enabled**.
3. **Remote DB driver blocked for now:** the chosen candidates cannot be installed on this
   machine (pyturso: zero Windows wheels → Rust build fails without MSVC; libsql: wheels
   only up to CPython 3.12 on Windows; this project runs 3.14). Enabling an untested driver
   would risk breaking the Vercel build, so it was **not** added to `requirements.txt`.
4. **Remote MCP gateway: implemented in Phase 2** (`mcp_gateway.py`, Streamable HTTP +
   bearer/OAuth) — but LIVE verification needs the deployed URL + your ChatGPT account.
   Stdio servers remain untouched as the local path.
5. **Vercel CLI is not installed** on this machine and `npx` is blocked by PowerShell
   execution policy — the user must install/login (`npm install -g vercel; vercel login`)
   or use the Vercel web dashboard.
6. **Hobby plan caveats:** free for non-commercial use; functions pause after inactivity
   (cold starts); 300s timeout; non-commercial usage limits apply.
7. **`/.well-known` routing on Vercel is unverified until first deploy** (Vercel marks the
   path reserved; we serve it from the main app — must be curl-verified post-deploy
   before ChatGPT setup, README step 2).

## 6. Verified vs not-tested vs requires-authorization

| Item | Status |
|---|---|
| Local app + 3 MCP servers + tests + audit + integration | **Verified on this machine** (counts above) |
| Import purity / lazy init / secret policy / healthz / Vercel config sanity | **Verified** (25 tests) |
| Real-DB byte-identity | **Verified** (SHA-256 == H0) |
| Actual `vercel deploy` + live URL behavior | **Requires your account** (external blocker) |
| Persistent storage on Vercel (Turso/libsql) | **Not enabled — install blocked on this machine** |
| Remote (HTTP) MCP endpoint | **Not implemented** (offer to design + auth it if wanted) |

## 7. How to run everything locally

```powershell
.\.venv\Scripts\Activate.ps1
python -m pytest -q            # 126 tests
python acceptance_audit.py     # 62 checks
python client_integration_check.py  # 45 checks
python app.py                  # http://127.0.0.1:5000
```

Connect an AI host with `.agents/mcp_config.json` (README § "Connect an MCP Client").
Deploy the dashboard to Vercel with README § "Deploy to Vercel" after `vercel login`.
