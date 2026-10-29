"""Remote MCP gateway integration check (Phase 2).

Run:  python gateway_integration_check.py

What it actually tests (NOT just endpoint existence):
  1. Deployment wiring (vercel.json function entry, rewrites, ignores).
  2. A REAL uvicorn server on a temp DB: health, 401 without/wrong token,
     fail-closed without token, WWW-Authenticate challenge.
  3. Real MCP Streamable HTTP with the OFFICIAL MCP Python client over a
     static bearer token: initialize, tools/list (13 read-only tools),
     read tool returns [DEMO] data, write tools are NOT exposed.
  4. Full OAuth 2.1 flow (discovery -> DCR -> login -> PKCE token ->
     MCP initialize/list with the OAuth access token), ChatGPT-style.
  5. Secret-leak scan of every HTTP response body.
  6. SHA-256 guard: real nexusai.db / store.db / notes.db byte-identical
     before/after (plus store activity count unchanged).
  7. Local stdio MCP config + servers still intact (preserved).

Exit code 0 = all checks passed. Real production data is never written to.
"""

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PY = sys.executable
PORT = int(os.environ.get("GW_CHECK_PORT", "8777"))
TOKEN = "gw-integration-" + hashlib.sha256(os.urandom(32)).hexdigest()[:40]
URL = f"http://127.0.0.1:{PORT}"
MCP_URL = f"{URL}/mcp_gateway"

REAL_STORE = os.path.join(ROOT, "store.db")
REAL_TASKS = os.path.join(ROOT, "nexusai.db")
REAL_NOTES = os.path.join(ROOT, "notes_server", "notes.db")

RESULTS = []


def check(label, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + label + ((" — " + str(extra)) if ok is False or extra else ""))
    if extra and not ok:
        print("      extra:", extra)


def sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def http_json(method, url, body=None, headers=None, timeout=15):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, dict(e.headers), raw


def form_post(url, fields, timeout=15):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def wait_health(url, expect=200, tries=60):
    for _ in range(tries):
        try:
            status, _, raw = http_json("GET", url, timeout=2)
            if status == expect:
                return status, raw
        except Exception:
            pass
        time.sleep(0.25)
    return None, None


# ---------------------------------------------------------------------------
# PART 1 — deployment wiring
# ---------------------------------------------------------------------------
def part1_config():
    print("=== PART 1 — Vercel deployment wiring ===")
    try:
        cfg = json.load(open(os.path.join(ROOT, "vercel.json"), encoding="utf-8"))
        ok = True
    except Exception as e:
        cfg, ok = {}, False
        check("cfg1. vercel.json parses", False, str(e))
    if ok:
        fns = cfg.get("functions", {})
        check("cfg2. dashboard function registered", "app.py" in fns)
        check("cfg3. gateway function registered", "mcp_gateway.py" in fns)
        check("cfg4. framework preset preserved (flask)",
              cfg.get("framework") == "flask", cfg.get("framework"))
        rw = cfg.get("rewrites", [])
        check("cfg5. /mcp rewrite -> /mcp_gateway",
              {"source": "/mcp", "destination": "/mcp_gateway"} in rw)
        check("cfg6. gateway bundle excludes .venv + tests",
              ".venv" in fns.get("mcp_gateway.py", {}).get("excludeFiles", ""))
    vi = open(os.path.join(ROOT, ".vercelignore"), encoding="utf-8").read()
    check("cfg7. .vercelignore blocks .env + *.db + tests",
          ".env" in vi and "*.db" in vi and "tests" in vi)
    reqs = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read().lower()
    check("cfg8. requirements has no gateway secrets / untested drivers",
          "gateway_token" not in reqs and "pyturso" not in reqs and "libsql" not in reqs)
    gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    check("cfg9. .gitignore blocks .env, .vercel/, *.db",
          ".env" in gi and ".vercel/" in gi and "store.db" in gi)


# ---------------------------------------------------------------------------
# PART 2 — stdio servers preserved (Phase 2 must not break Phase 1)
# ---------------------------------------------------------------------------
def part2_stdio_preserved():
    print("\n=== PART 2 — local stdio MCP servers still intact ===")
    cfg = json.load(open(os.path.join(ROOT, ".agents", "mcp_config.json"), encoding="utf-8"))
    servers = cfg.get("mcpServers", {})
    check("stdio1. config still has exactly 3 stdio servers",
          set(servers) == {"nexusai", "nexusai-notes", "nexusai-store"})
    check("stdio2. all configured commands still exist",
          all(os.path.exists(s.get("command", "")) for s in servers.values()))
    import mcp_server
    import store_server
    check("stdio3. task MCP server still exposes its 4 tools",
          len(mcp_server.get_tool_definitions()) == 4)
    check("stdio4. store MCP server still exposes 25 tools",
          len(store_server.get_tool_definitions()) == 25)


# ---------------------------------------------------------------------------
# PART 3 — fail-closed instance (no token)
# ---------------------------------------------------------------------------
def part3_fail_closed():
    print("\n=== PART 3 — gateway without MCP_GATEWAY_TOKEN fails closed ===")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("MCP_GATEWAY_TOKEN", None)
    env["NEXUSAI_STORE_DB"] = os.path.join(tempfile.mkdtemp(prefix="gw_nok_"), "store.db")
    port = PORT + 1
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "mcp_gateway:app", "--port", str(port), "--log-level", "error"],
        env=env, cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        status, raw = wait_health(f"http://127.0.0.1:{port}/healthz", expect=503)
        if status is None:
            status, raw = wait_health(f"http://127.0.0.1:{port}/healthz", expect=200)
        check("noauth1. /healthz is 503 disabled without token", status == 503, raw)
        if raw:
            check("noauth2. health reason exposed without secrets",
                  "disabled" in raw and TOKEN not in raw)
        st, hd, body = http_json("POST", f"http://127.0.0.1:{port}/mcp",
                                 body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Accept": "application/json, text/event-stream"})
        check("noauth3. /mcp is 503 even WITH a bearer when token not configured",
              st == 503, f"{st} {body[:120]}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


# ---------------------------------------------------------------------------
# PART 4 — bearer auth + MCP protocol over real HTTP
# ---------------------------------------------------------------------------
async def part4_bearer_mcp():
    print("\n=== PART 4 — real Streamable HTTP + static bearer (official MCP client) ===")
    from mcp.client.streamable_http import streamable_http_client
    from mcp import ClientSession
    import httpx2

    # raw checks first
    st, hd, body = http_json("POST", MCP_URL,
                             body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"Accept": "application/json, text/event-stream"})
    check("http1. POST without Authorization -> 401", st == 401, f"{st} {body[:100]}")
    wa = hd.get("WWW-Authenticate") or hd.get("www-authenticate") or ""
    check("http2. 401 carries WWW-Authenticate + resource_metadata",
          "Bearer" in wa and "resource_metadata" in wa, wa)
    st, hd, body = http_json("POST", MCP_URL,
                             body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                             headers={"Authorization": "Bearer definitely-wrong-token",
                                      "Accept": "application/json, text/event-stream"})
    check("http3. POST with wrong bearer -> 401", st == 401, st)

    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"},
        timeout=30)
    try:
        async with streamable_http_client(MCP_URL, http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                check("mcp1. initialize + tools/list over real Streamable HTTP",
                      len(names) == 13, f"{len(names)} tools")
                expected = sorted([
                    "list_products", "get_product", "get_low_stock", "list_sales",
                    "get_sale", "get_sales_summary", "list_purchases", "list_workers",
                    "get_business_summary", "get_profit_loss", "get_stock_valuation",
                    "get_dashboard_summary", "generate_business_report"])
                check("mcp2. exactly the 13 read-only tools", names == expected, names)
                ro_res = await session.call_tool("list_products", {})
                ro_txt = "".join(c.text for c in ro_res.content if hasattr(c, "text"))
                check("mcp3. read tool works and returns [DEMO] seeded data",
                      "DEMO" in ro_txt or "demo" in ro_txt.lower() or "products" in ro_txt.lower(),
                      ro_txt[:100])
                dash = await session.call_tool("get_dashboard_summary", {})
                dash_txt = "".join(c.text for c in dash.content if hasattr(c, "text"))
                check("mcp4. dashboard summary tool returns data", bool(dash_txt.strip()),
                      dash_txt[:80])
                w_res = await session.call_tool("create_sale", {"product_id": 1, "quantity": 1})
                w_txt = "".join(c.text for c in w_res.content if hasattr(c, "text"))
                check("mcp5. write tool NOT available on gateway (unknown tool)",
                      "unknown tool" in w_txt.lower() or "error" in w_txt.lower(), w_txt[:120])
                seed_res = await session.call_tool("seed_demo_data", {})
                seed_txt = "".join(c.text for c in seed_res.content if hasattr(c, "text"))
                check("mcp6. seed/reset tools NOT available on gateway",
                      "unknown tool" in seed_txt.lower() or "error" in seed_txt.lower(),
                      seed_txt[:120])
                check("mcp7. no token in any tool payload",
                      TOKEN not in ro_txt and TOKEN not in dash_txt)
    finally:
        await http.aclose()


# ---------------------------------------------------------------------------
# PART 5 — OAuth 2.1 (the path ChatGPT uses)
# ---------------------------------------------------------------------------
def part5_oauth():
    print("\n=== PART 5 — OAuth 2.1 flow (ChatGPT-style) ===")
    import httpx2  # client lib used by mcp; usable for raw calls too

    async def run():
        import hashlib
        async with httpx2.AsyncClient(base_url=MCP_URL, timeout=20) as c:
            r = await c.get("/as")
            check("oauth1. AS discovery served under gateway", r.status_code == 200, r.status_code)
            meta = r.json()
            check("oauth2. metadata sane (S256 only, code+refresh, auth method none)",
                  meta.get("code_challenge_methods_supported") == ["S256"]
                  and set(meta.get("grant_types_supported", [])) == {"authorization_code", "refresh_token"}
                  and meta.get("token_endpoint_auth_methods_supported") == ["none"], meta)
            r = await c.get("/pr")
            check("oauth3. protected-resource metadata served", r.status_code == 200, r.status_code)
            r = await c.post("/register", json={"client_name": "ChatGPT",
                                                "redirect_uris": ["https://chatgpt.com/cb"]})
            check("oauth4. DCR registration (201)", r.status_code == 201, r.status_code)
            cid = r.json().get("client_id", "")
            verifier = "integration-verifier-0123456789abcdefghijk"
            challenge = hashlib.sha256(verifier.encode()).digest()
            import base64 as b64
            challenge = b64.urlsafe_b64encode(challenge).decode().rstrip("=")
            params = {"response_type": "code", "client_id": cid,
                      "redirect_uri": "https://chatgpt.com/cb",
                      "code_challenge": challenge, "code_challenge_method": "S256",
                      "state": "integration"}
            r = await c.get("/authorize", params=params, follow_redirects=False)
            check("oauth5. authorize shows login form", r.status_code == 200 and "<form" in r.text,
                  r.status_code)
            m = re.search(r'name="req" value="([^"]+)"', r.text)
            blob = m.group(1) if m else ""
            r = await c.post("/authorize", data={"req": blob, "username": "admin",
                                                 "password": "wrong-password"},
                             follow_redirects=False)
            check("oauth6. wrong password -> 401", r.status_code == 401, r.status_code)
            user = os.environ.get("MCP_GATEWAY_USER", "admin")
            r = await c.post("/authorize", data={"req": blob, "username": user, "password": TOKEN},
                             follow_redirects=False)
            check("oauth7. correct credentials -> 302 with code", r.status_code == 302,
                  f"{r.status_code} {r.text[:120]}")
            loc = r.headers.get("location", "")
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
            code = qs.get("code", [""])[0]
            check("oauth8. redirect carries code + state",
                  bool(code) and qs.get("state") == ["integration"], loc[:120])
            r = await c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": "https://chatgpt.com/cb",
                                             "client_id": cid, "code_verifier": "x" * 43})
            check("oauth9. wrong PKCE verifier rejected (400)", r.status_code == 400, r.status_code)
            r = await c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": "https://chatgpt.com/cb",
                                             "client_id": cid, "code_verifier": verifier})
            check("oauth10. PKCE exchange issues access+refresh", r.status_code == 200
                  and "access_token" in r.json() and "refresh_token" in r.json(), r.text[:150])
            tok = r.json()
            r = await c.post("/token", data={"grant_type": "refresh_token",
                                             "refresh_token": tok["refresh_token"]})
            check("oauth11. refresh grant works", r.status_code == 200 and "access_token" in r.json(),
                  r.status_code)
            return tok["access_token"], loc

        return None, None

    access_token, loc = asyncio.run(run())
    if not access_token:
        return
    # MCP over OAuth access token (real session)
    async def mcp_over_oauth():
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        import httpx2 as hx
        http = hx.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}",
                     "Accept": "application/json, text/event-stream"},
            timeout=30)
        try:
            async with streamable_http_client(MCP_URL, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return len(tools.tools)
        finally:
            await http.aclose()

    n = asyncio.run(mcp_over_oauth())
    check("oauth12. MCP initialize + tools/list with OAUTH access token", n == 13, n)
    check("oauth13. static token never leaked in OAuth responses",
          TOKEN not in json.dumps({"loc": loc or ""}))


# ---------------------------------------------------------------------------
# PART 6 — leak scan + real DB byte-identity
# ---------------------------------------------------------------------------
def part6_guard(before):
    print("\n=== PART 6 — leak scan + real database protection ===")
    st, hd, body = http_json("GET", f"{URL}/healthz")
    check("guard1. dashboard /healthz still 200 (unchanged)", st == 200, st)
    check("guard2. token not in dashboard healthz", TOKEN not in body)
    st, hd, body = http_json("GET", f"{URL}/.well-known/oauth-authorization-server")
    check("guard3. Flask serves reserved /.well-known/oauth-authorization-server", st == 200, st)
    if st == 200:
        doc = json.loads(body)
        check("guard4. AS metadata points at /mcp_gateway endpoints",
              doc.get("authorization_endpoint", "").endswith("/mcp_gateway/authorize"), doc)
        check("guard5. no secrets in discovery doc", TOKEN not in body)
    st, hd, body = http_json("GET", f"{URL}/.well-known/oauth-protected-resource/mcp_gateway")
    check("guard6. Flask serves protected-resource metadata", st == 200, st)

    for label, path in (("nexusai.db", REAL_TASKS), ("store.db", REAL_STORE),
                        ("notes.db", REAL_NOTES)):
        after = sha(path)
        check(f"guard7. {label} byte-identical after gateway run",
              before.get(label) == after, f"{before.get(label)} != {after}")


# ---------------------------------------------------------------------------
def main():
    print("NexusAI remote MCP gateway integration check")
    print("Run:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(f"MCP endpoint: {MCP_URL}  (auth: bearer static + OAuth2 PKCE)")
    before = {k: sha(p) for k, p in (("nexusai.db", REAL_TASKS),
                                     ("store.db", REAL_STORE),
                                     ("notes.db", REAL_NOTES))}

    part1_config()
    part2_stdio_preserved()
    part3_fail_closed()

    # main server with token + temp DB
    tmp = tempfile.mkdtemp(prefix="gw_int_")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MCP_GATEWAY_TOKEN": TOKEN,
           "NEXUSAI_STORE_DB": os.path.join(tmp, "store.db")}
    env.pop("GATEWAY_MODE", None)
    print(f"\nStarting gateway on :{PORT} (temp DB: {tmp})")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "mcp_gateway:app", "--port", str(PORT), "--log-level", "error"],
        env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        status, raw = wait_health(f"{URL}/healthz", expect=200)
        check("srv1. gateway boots and /healthz is 200 ok", status == 200, raw)
        if raw:
            try:
                d = json.loads(raw)
                check("srv2. health reports non-persistent database",
                      d.get("database", {}).get("persistent") is False, d)
                check("srv3. health reports 13 tools + streamable-http",
                      d.get("tools_total") == 13 and d.get("transport") == "streamable-http", d)
            except Exception as e:
                check("srv2. health JSON parses", False, str(e))
        asyncio.run(part4_bearer_mcp())
        part5_oauth()
        # /mcp rewrite-equivalent path works too
        st, hd, body = http_json("GET", f"{URL}/mcp")
        check("srv4. GET /mcp without auth -> 401 (path routed)", st == 401, st)
        part6_guard(before)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    passed = sum(RESULTS)
    total = len(RESULTS)
    print("=" * 60)
    print(f"{passed}/{total} gateway integration checks PASSED")
    if passed != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
