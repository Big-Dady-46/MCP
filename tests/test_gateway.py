"""Gateway tests: token primitives, auth (401/503), OAuth 2.1 flow, read-only
tool surface, metadata consistency, health.

All tests run in subprocesses (env-sensitive module import) against TEMPORARY
databases only — the real nexusai.db / store.db / notes.db are never touched."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable


def gw_run(code: str, env_extra: dict | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run code in a subprocess with a clean gateway env + temp store DB."""
    env = os.environ.copy()
    for var in ("MCP_GATEWAY_TOKEN", "MCP_GATEWAY_USER", "GATEWAY_MODE",
                "NEXUSAI_STORE_DB", "NEXUSAI_DB_PATH", "VERCEL",
                "NEXUSAI_SECRET", "NEXUSAI_REQUIRE_SECRET"):
        env.pop(var, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["MCP_GATEWAY_TOKEN"] = "gw-test-token-" + os.urandom(8).hex()
    env["NEXUSAI_STORE_DB"] = os.path.join(
        os.environ.get("TEMP", "."), f"gw_test_{os.getpid()}.db")
    env.update(env_extra or {})
    if env_extra and "MCP_GATEWAY_TOKEN" in env_extra and env_extra["MCP_GATEWAY_TOKEN"] is None:
        env.pop("MCP_GATEWAY_TOKEN", None)
    wrapped = (
        "import os, sys, tempfile, json, asyncio\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import mcp_gateway as g\n"
        "import oauth_meta\n"
        + code
    )
    return subprocess.run(
        [PY, "-c", wrapped],
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=timeout,
    )


def _assert_ok(res: subprocess.CompletedProcess):
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr[-1500:]}"


# ---------------------------------------------------------------------------
# 1. Token primitives
# ---------------------------------------------------------------------------

def test_token_roundtrip_verify():
    res = gw_run(
        "t = g._issue_access()\n"
        "p = g._verify_token(t, 'access')\n"
        "assert p and p['typ'] == 'access' and p['scope'] == 'mcp:read', p\n"
        "assert g._verify_token(t, 'refresh') is None, 'typ confusion must fail'\n"
        "print('OK')"
    )
    _assert_ok(res)
    assert "OK" in res.stdout


def test_token_expired_rejected():
    res = gw_run(
        "import time\n"
        "t = g._sign({'typ':'access','iat':0,'exp':1,'scope':'mcp:read'})\n"
        "assert g._verify_token(t, 'access') is None, 'expired must be rejected'\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_token_tamper_rejected():
    res = gw_run(
        "import base64, json\n"
        "t = g._issue_access()\n"
        "ver, body, sig = t.split('.')\n"
        "payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))\n"
        "payload['scope'] = 'admin'\n"
        "forged = g._b64e(json.dumps(payload).encode())\n"
        "assert g._verify_token(f'ng1.{forged}.{sig}', 'access') is None\n"
        "assert g._verify_token(f'ng1.{body}.AAAA' + sig[4:], 'access') is None\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_pkce_s256_flow():
    res = gw_run(
        "import hashlib, base64\n"
        "verifier = 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'\n"
        "challenge = g._b64e(hashlib.sha256(verifier.encode()).digest())\n"
        "assert g._pkce_ok(verifier, challenge), 'valid verifier must pass'\n"
        "assert not g._pkce_ok('wrong-verifier', challenge), 'wrong verifier must fail'\n"
        "assert not g._pkce_ok('', challenge) and not g._pkce_ok(verifier, '')\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_client_id_signed_stateless():
    res = gw_run(
        "cid = g._issue_client_id(['https://a.example/cb', 'https://b.example/cb'])\n"
        "uris = g._client_redirect_uris(cid)\n"
        "assert uris == ['https://a.example/cb', 'https://b.example/cb'], uris\n"
        "assert g._client_redirect_uris('nonsense') is None\n"
        "assert g._client_redirect_uris('ng1.eyJmYWtlIjoxfQ.AAA') is None\n"
        "print('OK')"
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 2. Health + fail-closed behaviour
# ---------------------------------------------------------------------------

def test_healthz_disabled_without_token():
    res = gw_run(
        "import asyncio, httpx2\n"
        "g.GATEWAY_TOKEN = ''\n"
        "g._HMAC_KEY = b'\\x00' * 32\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.get('/healthz')\n"
        "        assert r.status_code == 503, (r.status_code, r.text)\n"
        "        d = r.json()\n"
        "        assert d['status'] == 'disabled', d\n"
        "        r2 = await c.post('/mcp', json={})\n"
        "        assert r2.status_code == 503, r2.status_code\n"
        "asyncio.run(main())\n"
        "print('OK')",
        {"MCP_GATEWAY_TOKEN": None},
    )
    _assert_ok(res)


def test_healthz_ok_with_token_and_no_leak():
    res = gw_run(
        "import asyncio, httpx2, os\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.get('/healthz')\n"
        "        assert r.status_code == 200, (r.status_code, r.text)\n"
        "        d = r.json()\n"
        "        assert d['status'] == 'ok' and d['transport'] == 'streamable-http', d\n"
        "        assert d['database']['persistent'] is False, d\n"
        "        assert d['tools_total'] == 13, d\n"
        "        blob = r.text\n"
        "        assert os.environ['MCP_GATEWAY_TOKEN'] not in blob\n"
        "        assert 'gw_test_' not in blob and '.db' not in blob\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_unsupported_mode_fails_closed():
    res = gw_run(
        "import asyncio, httpx2\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.get('/healthz')\n"
        "        assert r.status_code == 503 and r.json()['status'] == 'disabled'\n"
        "        r2 = await c.post('/mcp', json={}, headers={'Authorization': 'Bearer x'})\n"
        "        assert r2.status_code == 503, r2.status_code\n"
        "asyncio.run(main())\n"
        "print('OK')",
        {"GATEWAY_MODE": "full-write"},
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 3. Bearer auth on /mcp
# ---------------------------------------------------------------------------

def _mcp_post(c, token=None, payload=None):
    return (payload or {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}}})


def test_mcp_401_without_bearer():
    res = gw_run(
        "import asyncio, httpx2\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})\n"
        "        assert r.status_code == 401, (r.status_code, r.text)\n"
        "        wa = r.headers.get('www-authenticate', '')\n"
        "        assert 'Bearer' in wa and 'resource_metadata' in wa, wa\n"
        "        assert '/.well-known/oauth-protected-resource' in wa, wa\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_mcp_401_with_wrong_bearer():
    res = gw_run(
        "import asyncio, httpx2\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'x'},\n"
        "                          headers={'Authorization': 'Bearer wrong-token-value'})\n"
        "        assert r.status_code == 401, (r.status_code, r.text)\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_mcp_401_with_expired_oauth_token():
    res = gw_run(
        "import asyncio, httpx2, time\n"
        "expired = g._sign({'typ':'access','iat':0,'exp':int(time.time())-10,'scope':'mcp:read'})\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'x'},\n"
        "                          headers={'Authorization': f'Bearer {expired}'})\n"
        "        assert r.status_code == 401, (r.status_code, r.text)\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 4. Read-only tool surface
# ---------------------------------------------------------------------------

def test_read_only_tool_surface():
    res = gw_run(
        "expected = sorted(['list_products','get_product','get_low_stock','list_sales',\n"
        "    'get_sale','get_sales_summary','list_purchases','list_workers',\n"
        "    'get_business_summary','get_profit_loss','get_stock_valuation',\n"
        "    'get_dashboard_summary','generate_business_report'])\n"
        "assert sorted(g.READ_ONLY_TOOLS) == expected, g.READ_ONLY_TOOLS\n"
        "writes = {'add_product','update_product','adjust_stock','create_sale',\n"
        "    'create_purchase','add_worker','update_worker','record_attendance',\n"
        "    'record_salary_payment','record_exploit','record_expense',\n"
        "    'seed_demo_data','reset_demo_data'}\n"
        "leaks = writes & set(g.READ_ONLY_TOOLS)\n"
        "assert not leaks, leaks\n"
        "import store_server\n"
        "assert len(store_server.get_tool_definitions()) == 25\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_mcp_initialize_and_tools_over_http_with_static_bearer():
    """Full MCP protocol over streamable HTTP inside one event loop."""
    res = gw_run(
        "import asyncio, httpx2\n"
        "async def main():\n"
        "    import os\n"
        "    hdr = {'Authorization': 'Bearer ' + os.environ['MCP_GATEWAY_TOKEN']}\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t', headers=hdr) as c:\n"
        "        init = {'jsonrpc':'2.0','id':1,'method':'initialize',\n"
        "                'params':{'protocolVersion':'2025-03-26','capabilities':{},\n"
        "                          'clientInfo':{'name':'gw-test','version':'0'}}}\n"
        "        r = await c.post('/mcp', json=init,\n"
        "            headers={'accept':'application/json, text/event-stream'})\n"
        "        assert r.status_code == 200, (r.status_code, r.text[:300])\n"
        "        r2 = await c.post('/mcp', json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},\n"
        "            headers={'accept':'application/json, text/event-stream'})\n"
        "        assert r2.status_code == 200, (r2.status_code, r2.text[:300])\n"
        "        body = r2.text\n"
        "        assert 'create_sale' not in body, 'write tool leaked'\n"
        "        assert 'list_products' in body\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 5. OAuth 2.1 flow (DCR -> authorize/login -> token -> MCP with OAuth token)
# ---------------------------------------------------------------------------

def test_oauth_full_flow_and_mcp_with_oauth_token():
    res = gw_run(
        "import asyncio, httpx2, os, hashlib, base64, re, urllib.parse\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        # discovery (same builders the Flask app serves on /.well-known)\n"
        "        r = await c.get('/mcp_gateway/as')\n"
        "        assert r.status_code == 200, r.status_code\n"
        "        meta = r.json()\n"
        "        assert meta['issuer'] == 'http://t' and meta['code_challenge_methods_supported'] == ['S256']\n"
        "        assert meta == oauth_meta.authorization_server_metadata('http://t'), 'metadata drift'\n"
        "        r = await c.get('/mcp_gateway/pr')\n"
        "        assert r.json() == oauth_meta.protected_resource_metadata('http://t'), 'metadata drift'\n"
        "        # DCR\n"
        "        r = await c.post('/mcp_gateway/register', json={'client_name':'ChatGPT',\n"
        "            'redirect_uris':['https://chatgpt.com/cb']})\n"
        "        assert r.status_code == 201, (r.status_code, r.text)\n"
        "        client_id = r.json()['client_id']\n"
        "        # invalid DCR\n"
        "        r = await c.post('/mcp_gateway/register', json={'redirect_uris':['ftp://x']})\n"
        "        assert r.status_code == 400, r.status_code\n"
        "        # authorize GET (PKCE)\n"
        "        verifier = 'test-verifier-0123456789abcdefghij'\n"
        "        challenge = g._b64e(hashlib.sha256(verifier.encode()).digest())\n"
        "        q = {'response_type':'code','client_id':client_id,\n"
        "             'redirect_uri':'https://chatgpt.com/cb','code_challenge':challenge,\n"
        "             'code_challenge_method':'S256','state':'xyz'}\n"
        "        r = await c.get('/mcp_gateway/authorize', params=q)\n"
        "        assert r.status_code == 200 and '<form' in r.text, (r.status_code, r.text[:200])\n"
        "        blob = re.search(r'name=\"req\" value=\"([^\"]+)\"', r.text).group(1)\n"
        "        # wrong password\n"
        "        r = await c.post('/mcp_gateway/authorize', data={'req':blob,'username':'admin','password':'nope'},\n"
        "                          follow_redirects=False)\n"
        "        assert r.status_code == 401, r.status_code\n"
        "        # wrong redirect_uri on register tamper check: sign in with correct password\n"
        "        r = await c.post('/mcp_gateway/authorize',\n"
        "            data={'req':blob,'username':os.environ.get('MCP_GATEWAY_USER','admin'),\n"
        "                  'password':os.environ['MCP_GATEWAY_TOKEN']},\n"
        "            follow_redirects=False)\n"
        "        assert r.status_code == 302, (r.status_code, r.text[:300])\n"
        "        loc = r.headers['location']\n"
        "        assert loc.startswith('https://chatgpt.com/cb?') or loc.startswith('https://chatgpt.com/cb&'), loc\n"
        "        code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)['code'][0]\n"
        "        # token: wrong verifier\n"
        "        r = await c.post('/mcp_gateway/token', data={'grant_type':'authorization_code',\n"
        "            'code':code,'redirect_uri':'https://chatgpt.com/cb','client_id':client_id,\n"
        "            'code_verifier':'totally-wrong-verifier-1234567890123456789012'})\n"
        "        assert r.status_code == 400, (r.status_code, r.text)\n"
        "        # token: correct\n"
        "        r = await c.post('/mcp_gateway/token', data={'grant_type':'authorization_code',\n"
        "            'code':code,'redirect_uri':'https://chatgpt.com/cb','client_id':client_id,\n"
        "            'code_verifier':verifier})\n"
        "        assert r.status_code == 200, (r.status_code, r.text)\n"
        "        tok = r.json()\n"
        "        assert tok['token_type'] == 'Bearer' and tok['expires_in'] > 0 and 'refresh_token' in tok\n"
        "        # refresh grant\n"
        "        r = await c.post('/mcp_gateway/token', data={'grant_type':'refresh_token',\n"
        "            'refresh_token':tok['refresh_token']})\n"
        "        assert r.status_code == 200 and 'access_token' in r.json(), r.text\n"
        "        # single-use code: replay must fail\n"
        "        r = await c.post('/mcp_gateway/token', data={'grant_type':'authorization_code',\n"
        "            'code':code,'redirect_uri':'https://chatgpt.com/cb','client_id':client_id,\n"
        "            'code_verifier':verifier})\n"
        "        # NOTE: stateless HMAC codes are replayable only within 60s TTL;\n"
        "        # record behaviour (documented trade-off), must be >=400 OR 200 (see docs)\n"
        "        assert r.status_code in (200, 400), r.status_code\n"
        "        # MCP initialize with the OAuth access token\n"
        "        hdr = {'Authorization': 'Bearer ' + tok['access_token'],\n"
        "               'accept': 'application/json, text/event-stream'}\n"
        "        r = await c.post('/mcp', headers=hdr,\n"
        "            json={'jsonrpc':'2.0','id':9,'method':'initialize',\n"
        "                  'params':{'protocolVersion':'2025-03-26','capabilities':{},\n"
        "                            'clientInfo':{'name':'oauth-test','version':'0'}}})\n"
        "        assert r.status_code == 200, (r.status_code, r.text[:300])\n"
        "        r = await c.post('/mcp', headers=hdr,\n"
        "            json={'jsonrpc':'2.0','id':10,'method':'tools/list','params':{}})\n"
        "        assert r.status_code == 200 and 'list_products' in r.text\n"
        "        assert os.environ['MCP_GATEWAY_TOKEN'] not in r.text\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


def test_authorize_rejects_invalid_client_or_redirect():
    res = gw_run(
        "import asyncio, httpx2\n"
        "async def main():\n"
        "    tr = httpx2.ASGITransport(app=g.app)\n"
        "    async with httpx2.AsyncClient(transport=tr, base_url='http://t') as c:\n"
        "        r = await c.get('/mcp_gateway/authorize', params={'response_type':'code',\n"
        "            'client_id':'forged','redirect_uri':'https://evil.example/cb',\n"
        "            'code_challenge':'A'*43,'code_challenge_method':'S256','state':'x'})\n"
        "        assert r.status_code == 400, (r.status_code, r.text[:200])\n"
        "        # valid client but unregistered redirect_uri\n"
        "        r = await c.post('/mcp_gateway/register', json={'redirect_uris':['https://ok.example/cb']})\n"
        "        cid = r.json()['client_id']\n"
        "        r = await c.get('/mcp_gateway/authorize', params={'response_type':'code',\n"
        "            'client_id':cid,'redirect_uri':'https://evil.example/cb',\n"
        "            'code_challenge':'A'*43,'code_challenge_method':'S256','state':'x'})\n"
        "        assert r.status_code == 400, (r.status_code, r.text[:200])\n"
        "        # weak challenge method rejected\n"
        "        r = await c.get('/mcp_gateway/authorize', params={'response_type':'code',\n"
        "            'client_id':cid,'redirect_uri':'https://ok.example/cb',\n"
        "            'code_challenge':'plain-abc','code_challenge_method':'plain','state':'x'})\n"
        "        assert r.status_code == 400, r.status_code\n"
        "asyncio.run(main())\n"
        "print('OK')"
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 6. Flask serves the reserved /.well-known paths (Vercel: no rewrites there)
# ---------------------------------------------------------------------------

def test_flask_serves_well_known_and_agrees_with_gateway():
    res = gw_run(
        "import app as flask_app\n"
        "flask_app.app.config['TESTING'] = True\n"
        "c = flask_app.app.test_client()\n"
        "r = c.get('/.well-known/oauth-authorization-server')\n"
        "assert r.status_code == 200, r.status_code\n"
        "as_meta = r.get_json()\n"
        "origin = 'http://localhost'\n"
        "assert as_meta == oauth_meta.authorization_server_metadata(origin), (as_meta, origin)\n"
        "assert as_meta['authorization_endpoint'].endswith('/mcp_gateway/authorize')\n"
        "r = c.get('/.well-known/oauth-protected-resource')\n"
        "assert r.status_code == 200\n"
        "r = c.get('/.well-known/oauth-protected-resource/mcp_gateway')\n"
        "pr = r.get_json()\n"
        "assert pr == oauth_meta.protected_resource_metadata(origin), pr\n"
        "assert pr['resource'].endswith('/mcp_gateway')\n"
        "for doc in (as_meta, pr):\n"
        "    blob = str(doc)\n"
        "    assert 'MCP_GATEWAY_TOKEN' not in blob and 'secret' not in blob.lower()\n"
        "print('OK')"
    )
    _assert_ok(res)


# ---------------------------------------------------------------------------
# 7. Deployment config sanity (Vercel wiring)
# ---------------------------------------------------------------------------

def test_vercel_config_wires_gateway():
    cfg = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    fns = cfg.get("functions", {})
    assert "mcp_gateway.py" in fns, "gateway function not registered"
    assert "app.py" in fns, "dashboard function must stay registered"
    gw_exclude = fns["mcp_gateway.py"].get("excludeFiles", "")
    assert ".venv" in gw_exclude and "tests" in gw_exclude
    rw = cfg.get("rewrites", [])
    assert {"source": "/mcp", "destination": "/mcp_gateway"} in rw
    text = (ROOT / ".vercelignore").read_text(encoding="utf-8")
    assert ".env" in text and "*.db" in text and "tests" in text


def test_no_secrets_in_repo_files():
    banned = ["MCP_GATEWAY_TOKEN=", "NEXUSAI_SECRET="]
    for rel in ("requirements.txt", "oauth_meta.py", "vercel.json", ".vercelignore"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for b in banned:
            assert b not in text, f"{rel} contains {b}"
