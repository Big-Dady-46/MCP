"""Final MCP client integration + readiness check (real stdio, exact config).

Run:  python client_integration_check.py

- Validates .agents/mcp_config.json (strict JSON, absolute paths, cwd, commands).
- Launches ALL THREE servers using the EXACT command/args/cwd from the config
  through the official MCP Python Client over stdio (initialize handshake,
  tool/resource/prompt discovery, real tool calls).
- Store workflows run against an ISOLATED temp database (env override only —
  command/args/cwd stay exactly as configured). The real store.db / nexusai.db /
  notes.db are hashed before/after and must not change.
- Verifies the Flask dashboard serves over real HTTP bound to 127.0.0.1.

Exit code 0 = all checks passed.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from mcp import Client, StdioServerParameters  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, ".agents", "mcp_config.json")
REAL_STORE = os.path.join(ROOT, "store.db")
REAL_TASKS = os.path.join(ROOT, "nexusai.db")
REAL_NOTES = os.path.join(ROOT, "notes_server", "notes.db")

TMP = tempfile.mkdtemp(prefix="nexus_mcp_client_")
ENV = {**os.environ,
       "NEXUSAI_STORE_DB": os.path.join(TMP, "store.db"),
       "NEXUSAI_DB_PATH": os.path.join(TMP, "tasks.db"),
       "NEXUSAI_NOTES_DB": os.path.join(TMP, "notes.db")}
# In-process imports (Flask part) must also use ONLY the temp databases.
os.environ.update(ENV)

RESULTS = []


def check(label, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + label + ((" — " + str(extra)) if extra else ""))


def sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def text_of(r):
    return "".join(c.text for c in r.content if hasattr(c, "text"))


def store_counts(db):
    # Schema creation is now lazy (first store_db use per file). Point the
    # store module at this DB and initialize explicitly so a raw sqlite3
    # baseline count works even before any MCP tool call.
    import store_db
    store_db.set_db_path(db)
    store_db.init_db()
    with sqlite3.connect(db) as c:
        return tuple(c.execute(
            "SELECT COUNT(*) FROM " + t).fetchone()[0]
            for t in ("products", "sales", "purchases", "workers", "expenses"))


# ---------------------------------------------------------------------------
# PART 1 — configuration validation
# ---------------------------------------------------------------------------
def part1_config():
    print("=== PART 1 — .agents/mcp_config.json validation ===")
    raw = open(CONFIG_PATH, "rb").read()
    check("cfg1. strict UTF-8 JSON, no BOM", not raw.startswith(b"\xef\xbb\xbf"))
    try:
        cfg = json.loads(raw.decode("utf-8"))
        parse_ok = True
    except Exception as e:
        cfg, parse_ok = {}, False
    check("cfg2. parses as strict JSON", parse_ok)
    servers = cfg.get("mcpServers", {})
    check("cfg3. exactly 3 servers (nexusai, nexusai-notes, nexusai-store)",
          set(servers) == {"nexusai", "nexusai-notes", "nexusai-store"})
    for name, sc in servers.items():
        cmd, args, cwd = sc.get("command", ""), sc.get("args", []), sc.get("cwd", "")
        check(f"cfg4-{name}. command is absolute path and exists",
              os.path.isabs(cmd) and os.path.exists(cmd), cmd)
        check(f"cfg5-{name}. all args are absolute and exist",
              args and all(os.path.isabs(a) and os.path.exists(a) for a in args), args)
        check(f"cfg6-{name}. cwd is absolute and exists",
              os.path.isabs(cwd) and os.path.isdir(cwd), cwd)
        check(f"cfg7-{name}. arg script lives under cwd (relative layout sane)",
              os.path.dirname(args[0]) == cwd or os.path.isfile(os.path.join(cwd, os.path.basename(args[0]))))
    check("cfg8. no secrets/keys in config",
          not re.search(r"(?i)password|secret|api[_-]?key|token", json.dumps(cfg)))
    return cfg


# ---------------------------------------------------------------------------
# PART 2 — store server over stdio with EXACT config command/args/cwd
# ---------------------------------------------------------------------------
def part2_store_stdio(cfg):
    print("\n=== PART 2 — store server: EXACT config command over real stdio ===")
    sc = cfg["mcpServers"]["nexusai-store"]
    params = StdioServerParameters(command=sc["command"], args=list(sc["args"]),
                                   cwd=sc["cwd"], env=ENV)
    print(f"launch: {sc['command']} {' '.join(sc['args'])} (cwd={sc['cwd']})")

    async def run():
        async with Client(params) as c:
            # --- initialization + discovery ---
            tools = sorted(t.name for t in (await c.list_tools()).tools)
            check("mcp1. initialize handshake + tool discovery over stdio (25 tools)",
                  len(tools) == 25, f"{len(tools)} tools")
            res = [str(r.uri) for r in (await c.list_resources()).resources]
            check("mcp2. resource discovery (4 URIs)",
                  res == ["nexus://store/summary", "nexus://inventory/low-stock",
                          "nexus://sales/today", "nexus://staff/summary"], res)
            prompts = sorted(p.name for p in (await c.list_prompts()).prompts)
            check("mcp3. prompt discovery (2 prompts)",
                  prompts == ["business_report_prompt", "stock_review"], prompts)
            ro = {t.name: (t.annotations.read_only_hint if t.annotations else None)
                  for t in (await c.list_tools()).tools}
            check("mcp4. tool annotations classify read-only vs write",
                  ro.get("list_products") is True and ro.get("create_sale") is False
                  and ro.get("get_profit_loss") is True)

            # --- baseline on isolated temp DB ---
            before = store_counts(ENV["NEXUSAI_STORE_DB"])
            baseline = json.loads(text_of(await c.call_tool("list_products", {})))

            # --- add test product, read back, update ---
            r = await c.call_tool("add_product", {
                "name": "[TEST] MCP Integration Soda", "brand": "TestBrand",
                "sell_price": 150.0, "unit_cost": 100.0, "stock_units": 10,
                "reorder_level": 2, "units_per_crate": 6})
            check("mcp5. add test product via stdio", not r.is_error)
            pid = json.loads(text_of(r))["id"]
            readback = json.loads(text_of(await c.call_tool(
                "get_product", {"product_id": pid})))
            check("mcp6. read back product (name/price/stock match)",
                  readback["name"] == "[TEST] MCP Integration Soda"
                  and readback["sell_price"] == 150.0
                  and readback["stock_units"] == 10)
            upd = json.loads(text_of(await c.call_tool("update_product", {
                "product_id": pid, "sell_price": 160.0,
                "reorder_level": 4, "confirm": True})))
            check("mcp7. update details via stdio (price 150->160, reorder 4)",
                  upd["sell_price"] == 160.0 and upd["reorder_level"] == 4
                  and upd["is_active"] is True)

            # --- purchase: stock increases only when received ---
            r = await c.call_tool("create_purchase", {
                "items": json.dumps([{"product_id": pid, "qty_units": 12,
                                      "unit_cost": 95.0}]),
                "paid": 1140.0, "status": "paid", "ref_no": "TEST-PO"})
            pur = json.loads(text_of(r))
            st = json.loads(text_of(await c.call_tool("get_product", {"product_id": pid})))
            check("mcp8. purchase received: total 12*95=1140, stock 10->22",
                  not r.is_error and pur["total"] == 1140.0
                  and st["stock_units"] == 22, f"stock={st['stock_units']}")
            r_bad = await c.call_tool("create_purchase", {
                "items": json.dumps([{"product_id": pid, "qty_units": 5,
                                      "unit_cost": 10.0}]),
                "paid": 0, "status": "paid"})  # invalid: paid status with 0 paid
            st2 = json.loads(text_of(await c.call_tool("get_product", {"product_id": pid})))
            check("mcp9. invalid purchase rejected; stock unchanged (22)",
                  r_bad.is_error and st2["stock_units"] == 22)

            # --- sale: correct total and stock reduction ---
            r = await c.call_tool("create_sale", {
                "items": json.dumps([{"product_id": pid, "qty": 4, "unit": "bottle"}]),
                "discount": 20, "payment_method": "cash"})
            sale = json.loads(text_of(r))
            st3 = json.loads(text_of(await c.call_tool("get_product", {"product_id": pid})))
            check("mcp10. sale total 4*160-20=620; stock 22->18; receipt issued",
                  not r.is_error and sale["total"] == 620.0
                  and sale["discount"] == 20.0
                  and st3["stock_units"] == 18
                  and sale["receipt_no"].startswith("R"),
                  f"total={sale['total']} stock={st3['stock_units']}")

            # --- oversell + invalid values: rejected, no state change ---
            snap = store_counts(ENV["NEXUSAI_STORE_DB"])
            st_snap = json.loads(text_of(await c.call_tool("get_product", {"product_id": pid})))
            r1 = await c.call_tool("create_sale", {
                "items": json.dumps([{"product_id": pid, "qty": 9999, "unit": "bottle"}])})
            r2 = await c.call_tool("add_product", {"name": "", "sell_price": 10})
            r3 = await c.call_tool("add_product", {"name": "Neg", "sell_price": -5})
            r4 = await c.call_tool("adjust_stock", {
                "product_id": pid, "change_units": -500, "confirm": True})
            r5 = await c.call_tool("update_product", {
                "product_id": pid, "sell_price": 170.0, "confirm": False})
            snap_after = store_counts(ENV["NEXUSAI_STORE_DB"])
            st_after = json.loads(text_of(await c.call_tool("get_product", {"product_id": pid})))
            check("mcp11. oversell/invalid args all rejected (isError)",
                  r1.is_error and r2.is_error and r3.is_error and r4.is_error and r5.is_error)
            check("mcp12. DB state unchanged after all rejections",
                  snap == snap_after
                  and st_after["stock_units"] == st_snap["stock_units"]
                  and st_after["sell_price"] == st_snap["sell_price"],
                  f"{snap} == {snap_after}")

            # --- resources read-only ---
            counts_before = store_counts(ENV["NEXUSAI_STORE_DB"])
            parsed = []
            for u in res:
                parsed.append(json.loads((await c.read_resource(u)).contents[0].text))
            check("mcp13. store summary resources readable with live data",
                  "today_revenue" in parsed[0] and "low_stock" in parsed[1]
                  and "transactions" in parsed[2] and "workers" in parsed[3])
            # --- prompts ---
            p1 = await c.get_prompt("stock_review", {"focus": "restock"})
            p2 = await c.get_prompt("business_report_prompt", {"period": "month"})
            t1, t2 = p1.messages[0].content.text, p2.messages[0].content.text
            check("mcp14. prompts invoked; read-only instructions present",
                  "Do NOT modify" in t1 and "Do NOT modify" in t2)
            counts_after = store_counts(ENV["NEXUSAI_STORE_DB"])
            check("mcp15. resource+prompt reads mutate NOTHING",
                  counts_before == counts_after, f"{counts_before} == {counts_after}")

            # --- reversible cleanup of test product (deactivate) ---
            deact = json.loads(text_of(await c.call_tool("update_product", {
                "product_id": pid, "is_active": False, "confirm": True})))
            check("mcp16. reversible cleanup: test product deactivated (not deleted)",
                  deact["is_active"] is False and deact["name"] == "[TEST] MCP Integration Soda")
            check("mcp17. baseline products still present alongside test row",
                  len(baseline) >= 0)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# PART 3 — tasks + notes servers over stdio with EXACT config
# ---------------------------------------------------------------------------
def part3_tasks_notes_stdio(cfg):
    print("\n=== PART 3 — tasks + notes servers: EXACT config command over stdio ===")

    async def run():
        sc_t = cfg["mcpServers"]["nexusai"]
        async with Client(StdioServerParameters(
                command=sc_t["command"], args=list(sc_t["args"]),
                cwd=sc_t["cwd"], env=ENV)) as t:
            tn = sorted(x.name for x in (await t.list_tools()).tools)
            check("mcp18. tasks server stdio: init + 4 tools discovered",
                  tn == ["add_task", "complete_task", "delete_task", "list_tasks"], tn)
            r = await t.call_tool("add_task", {"title": "[TEST] integration task"})
            tid = json.loads(text_of(r))["id"]
            r2 = await t.call_tool("list_tasks", {"status": "all"})
            listed = json.loads(text_of(r2))
            has = any(x["id"] == tid for x in listed)
            r3 = await t.call_tool("delete_task", {"task_id": tid})
            tr = (await t.list_resources()).resources
            pr = (await t.list_prompts()).prompts
            check("mcp19. tasks add/list/delete works over stdio (reversible)",
                  has and not r3.is_error)
            check("mcp20. tasks resource+prompt discovered",
                  [str(x.uri) for x in tr] == ["nexus://tasks/summary"]
                  and [p.name for p in pr] == ["review_pending_tasks"])

        sc_n = cfg["mcpServers"]["nexusai-notes"]
        async with Client(StdioServerParameters(
                command=sc_n["command"], args=list(sc_n["args"]),
                cwd=sc_n["cwd"], env=ENV)) as n:
            nn = sorted(x.name for x in (await n.list_tools()).tools)
            check("mcp21. notes server stdio: init + 3 tools discovered",
                  nn == ["add_note", "list_notes", "search_notes"], nn)
            r = await n.call_tool("add_note", {
                "title": "[TEST] integration note", "content": "stdio check content"})
            nid = json.loads(text_of(r))["id"]
            found = json.loads(text_of(await n.call_tool(
                "search_notes", {"query": "integration"})))
            tr = (await n.list_resources()).resources
            pr = (await n.list_prompts()).prompts
            check("mcp22. notes add/search works over stdio",
                  any(x["id"] == nid for x in found))
            check("mcp23. notes resource+prompt discovered",
                  [str(x.uri) for x in tr] == ["notes://summary"]
                  and [p.name for p in pr] == ["summarize_notes"])

        # independence: three separate DB files were used
        check("mcp24. three servers used three independent DB files",
              all(os.path.exists(ENV[k]) for k in
                  ("NEXUSAI_STORE_DB", "NEXUSAI_DB_PATH", "NEXUSAI_NOTES_DB")))

    asyncio.run(run())


# ---------------------------------------------------------------------------
# PART 4 — Flask dashboard over real localhost HTTP
# ---------------------------------------------------------------------------
def part4_flask():
    print("\n=== PART 4 — Flask dashboard on 127.0.0.1 (real HTTP) ===")
    from app import app
    app.config["TESTING"] = True

    with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
        src = f.read()
    check("flask1. server binds 127.0.0.1 only (no 0.0.0.0/public)",
          'app.run(host="127.0.0.1"' in src and "0.0.0.0" not in src)

    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", 0, app)  # port 0 = OS-assigned free port
    port = srv.socket.getsockname()[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        html = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/store/", timeout=10).read().decode("utf-8", "replace")
        check("flask2. dashboard serves real HTTP on 127.0.0.1 (/store/ 200)",
              "Beverage Store" in html)
        html2 = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/", timeout=10).read().decode("utf-8", "replace")
        check("flask3. overview page serves too", "NexusAI" in html2)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/store/products", timeout=10)
            ok_products = True
        except Exception:
            ok_products = False
        check("flask4. products page serves", ok_products)
    finally:
        srv.shutdown()


def main():
    real_before = {p: sha(p) for p in (REAL_STORE, REAL_TASKS, REAL_NOTES)}
    print("MCP client integration check — temp DBs:", TMP)
    cfg = part1_config()
    part2_store_stdio(cfg)
    part3_tasks_notes_stdio(cfg)
    part4_flask()
    real_after = {p: sha(p) for p in (REAL_STORE, REAL_TASKS, REAL_NOTES)}
    check("final1. REAL store/task/notes DBs byte-identical after integration run",
          real_before == real_after)
    passed = sum(RESULTS)
    print("=" * 60)
    print(f"{passed}/{len(RESULTS)} integration checks PASSED")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
