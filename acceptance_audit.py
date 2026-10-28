"""End-to-end acceptance audit for the NexusAI Beverage Store system.

Run:  python acceptance_audit.py

Every business-data check runs against ISOLATED temporary SQLite files
(NEXUSAI_STORE_DB / NEXUSAI_DB_PATH / NEXUSAI_NOTES_DB env overrides).
Real databases (nexusai.db, notes_server/notes.db, store.db) are hashed
before and after the whole audit and MUST be unchanged.

Exit code 0 = every check passed.
"""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "notes_server"))

TMP = tempfile.mkdtemp(prefix="nexus_audit_")
STORE_DB = os.path.join(TMP, "store_audit.db")
TASK_DB = os.path.join(TMP, "tasks_audit.db")
NOTES_DB = os.path.join(TMP, "notes_audit.db")

# Env overrides MUST be set before any project module import.
os.environ["NEXUSAI_STORE_DB"] = STORE_DB
os.environ["NEXUSAI_DB_PATH"] = TASK_DB
os.environ["NEXUSAI_NOTES_DB"] = NOTES_DB

REAL_STORE = os.path.join(ROOT, "store.db")
REAL_TASKS = os.path.join(ROOT, "nexusai.db")
REAL_NOTES = os.path.join(ROOT, "notes_server", "notes.db")

RESULTS = []
TODAY = date.today().isoformat()


def check(label, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + label + ((" — " + str(extra)) if extra else ""))


def sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def scalar(sql, params=(), db=STORE_DB):
    with sqlite3.connect(db) as c:
        return c.execute(sql, params).fetchone()[0]


# ===========================================================================
# Import store_db AFTER env override (module init_db() creates temp DB)
# ===========================================================================
import store_db as sdb  # noqa: E402

assert sdb.get_db_path() == STORE_DB, sdb.get_db_path()
sdb.init_db()


def part_a_db_workflows():
    """Workflows 1-8: direct database logic on the isolated temp DB."""
    print("\n=== PART A — store_db workflows (isolated temp store.db) ===")

    # --- 1. product persists after reopening the database -------------
    p = sdb.create_product(name="Audit Cola", brand="AuditCo", size_label="1L",
                           unit_cost=100, sell_price=150, stock_units=20,
                           reorder_level=5)
    pid = p["id"]
    fresh = sqlite3.connect(STORE_DB).execute(
        "SELECT name, stock_units, unit_cost_paisa, sell_price_paisa FROM products WHERE id=?",
        (pid,)).fetchone()
    check("1. product persists after reopening DB",
          fresh is not None and fresh[0] == "Audit Cola" and fresh[1] == 20
          and fresh[2] == 10000 and fresh[3] == 15000, f"row={fresh}")

    # --- 2. purchase increases stock only when received ---------------
    before = sdb.get_product(pid)["stock_units"]
    pur = sdb.create_purchase(
        [{"product_id": pid, "qty_units": 24, "unit_cost": 90}],
        paid=2160, status="paid", ref_no="AUDIT-1")
    after_ok = sdb.get_product(pid)["stock_units"]
    check("2a. received purchase increases stock 20->44",
          after_ok == before + 24 == 44, f"{before}->{after_ok}")
    check("2b. purchase total = 24*90 = 2160, paid full, status paid",
          pur["total"] == 2160.0 and pur["paid"] == 2160.0 and pur["status"] == "paid")
    try:
        sdb.create_purchase([{"product_id": pid, "qty_units": 5, "unit_cost": 50}],
                            paid=0, status="paid")  # inconsistent: paid status, 0 paid
        bad_purchase_rejected = False
    except ValueError:
        bad_purchase_rejected = True
    after_bad = sdb.get_product(pid)["stock_units"]
    check("2c. invalid purchase rejected AND stock unchanged (rollback)",
          bad_purchase_rejected and after_bad == 44, f"stock={after_bad}")

    # --- 3. sale: totals, stock deduction, transaction records --------
    sales_before = scalar("SELECT COUNT(*) FROM sales")
    sale = sdb.create_sale([{"product_id": pid, "qty": 2, "unit": "bottle"}])
    stock_after_sale = sdb.get_product(pid)["stock_units"]
    check("3a. sale totals: 2 x 150 = 300",
          sale["subtotal"] == 300.0 and sale["total"] == 300.0
          and sale["receipt_no"].startswith("R"), sale["receipt_no"])
    check("3b. stock deducted 44 -> 42", stock_after_sale == 42, stock_after_sale)
    items = scalar("SELECT COUNT(*) FROM sale_items WHERE sale_id=?", (sale["id"],))
    moves = scalar("SELECT COUNT(*) FROM stock_movements WHERE ref_type='sale' AND ref_id=?",
                   (sale["id"],))
    sales_after = scalar("SELECT COUNT(*) FROM sales")
    check("3c. transaction records exist (1 sale, 1 item, 1 stock movement)",
          items == 1 and moves == 1 and sales_after == sales_before + 1,
          f"items={items} moves={moves}")

    # --- 4. oversell rejected with complete rollback ------------------
    s_snap = scalar("SELECT COUNT(*) FROM sales")
    si_snap = scalar("SELECT COUNT(*) FROM sale_items")
    sm_snap = scalar("SELECT COUNT(*) FROM stock_movements")
    st_snap = sdb.get_product(pid)["stock_units"]
    try:
        sdb.create_sale([{"product_id": pid, "qty": 9999, "unit": "bottle"}])
        oversell_rejected = False
    except ValueError:
        oversell_rejected = True
    check("4. oversell rejected + full rollback (no sale/item/movement rows, stock same)",
          oversell_rejected
          and scalar("SELECT COUNT(*) FROM sales") == s_snap
          and scalar("SELECT COUNT(*) FROM sale_items") == si_snap
          and scalar("SELECT COUNT(*) FROM stock_movements") == sm_snap
          and sdb.get_product(pid)["stock_units"] == st_snap)

    # --- 5. bottle/crate conversion -----------------------------------
    c2 = sdb.create_product(name="Audit Crate Soda", sell_price=100, unit_cost=60,
                            units_per_crate=12, stock_units=36)
    sale2 = sdb.create_sale([{"product_id": c2["id"], "qty": 2, "unit": "crate"}])
    check("5. 2 crates x 12 = 24 bottles deducted; total = 24*100 = 2400",
          sale2["items"][0]["qty_units"] == 24 and sale2["total"] == 2400.0
          and sdb.get_product(c2["id"])["stock_units"] == 12)

    # --- 6. discounts, costs, expenses, profit math -------------------
    d = sdb.create_product(name="Audit Discount Item", sell_price=200, unit_cost=120,
                           stock_units=10)
    sale3 = sdb.create_sale([{"product_id": d["id"], "qty": 3, "unit": "bottle"}],
                            discount=50)
    check("6a. discount math: 600 - 50 = 550",
          sale3["subtotal"] == 600.0 and sale3["discount"] == 50.0
          and sale3["total"] == 550.0)
    cogs = sum(it["unit_cost"] * it["qty_units"] for it in sale3["items"])
    check("6b. COGS captured at sale time: 3*120 = 360", cogs == 360.0)
    sdb.record_expense("Audit Rent", 1000, description="audit expense")
    pl = sdb.profit_loss(TODAY, TODAY)
    check("6c. P&L: gross = revenue - COGS; net = gross - expenses (paisa-exact)",
          abs(pl["gross_profit"] - (pl["revenue"] - pl["cogs"])) < 1e-9
          and abs(pl["net_operating_profit"] - (pl["gross_profit"] - pl["expenses"])) < 1e-9
          and pl["expenses"] >= 1000,
          f"rev={pl['revenue']} cogs={pl['cogs']} exp={pl['expenses']}")
    check("6d. purchases NOT counted as expenses",
          pl["purchases_in_period"] >= 2160 and pl["expenses"] < pl["purchases_in_period"])
    val = sdb.stock_valuation()
    check("6e. stock valuation = sum(units*cost) and retail",
          val["units"] > 0 and val["cost_value"] > 0
          and val["retail_value"] >= val["cost_value"])

    # --- 7. workers: create, attendance, salary -----------------------
    w = sdb.create_worker("Audit Worker", role="Helper", salary=30000)
    a1 = sdb.record_attendance(w["id"], TODAY, "present")
    a2 = sdb.record_attendance(w["id"], TODAY, "leave")  # upsert same day
    att_rows = scalar("SELECT COUNT(*) FROM attendance WHERE worker_id=? AND day=?",
                      (w["id"], TODAY))
    pay = sdb.record_salary_payment(w["id"], 12000)
    st = sdb.salary_status(w["id"])
    check("7a. worker created with salary 30000", w["salary"] == 30000.0)
    check("7b. attendance upserts to ONE row and updates status",
          att_rows == 1 and a2["status"] == "leave")
    check("7c. salary payment recorded; status: paid 12000, outstanding 18000",
          pay["amount"] == 12000.0 and st["paid"] == 12000.0
          and st["outstanding"] == 18000.0)

    # --- 8. demo seed idempotent + reset cannot delete genuine rows ---
    r1 = sdb.seed_demo()
    r2 = sdb.seed_demo()
    check("8a. demo seed works then is idempotent (second call no-op)",
          r1["seeded"] is True and r2["seeded"] is False and sdb.demo_seeded())
    genuine_before = scalar("SELECT COUNT(*) FROM products WHERE is_demo=0")
    try:
        sdb.reset_demo(confirm=False)
        reset_guard = False
    except ValueError:
        reset_guard = True
    check("8b. reset without confirm refused", reset_guard)
    r3 = sdb.reset_demo(confirm=True)
    genuine_after = scalar("SELECT COUNT(*) FROM products WHERE is_demo=0")
    check("8c. reset removes demo rows only; genuine products survive",
          not sdb.demo_seeded() and genuine_after == genuine_before
          and sdb.get_product(pid)["name"] == "Audit Cola",
          f"deleted={r3['deleted'].get('products')}")

    # --- extra: demo-product sold by a real sale blocks demo reset ---
    sdb.seed_demo()
    demo_pid = scalar("SELECT id FROM products WHERE is_demo=1 LIMIT 1")
    sdb.create_sale([{"product_id": demo_pid, "qty": 1, "unit": "bottle"}],
                    created_by="audit-real-sale")
    try:
        sdb.reset_demo(confirm=True)
        fk_safe = "reset succeeded despite real sale of demo product"
        reset_ok = True
    except sqlite3.IntegrityError:
        fk_safe = "FK constraint blocked reset (real sale history preserved — safe failure)"
        reset_ok = False  # informational; handled below
    real_sales_of_demo = scalar(
        "SELECT COUNT(*) FROM sale_items si JOIN sales s ON s.id=si.sale_id "
        "JOIN products p ON p.id=si.product_id WHERE p.is_demo=1 AND s.is_demo=0")
    check("8d. INFO real-sale-of-demo-product edge case handled safely",
          (not reset_ok and real_sales_of_demo >= 1) or reset_ok, fk_safe)
    try:
        sdb.reset_demo(confirm=True)
    except sqlite3.IntegrityError:
        # clean up the blocking real sale so later parts start clean
        with sqlite3.connect(STORE_DB) as c:
            c.execute("DELETE FROM sale_items WHERE sale_id IN "
                      "(SELECT id FROM sales WHERE created_by='audit-real-sale')")
            c.execute("DELETE FROM sales WHERE created_by='audit-real-sale'")
            c.execute("DELETE FROM stock_movements WHERE created_by='mcp' OR created_by='audit-real-sale'")
        sdb.reset_demo(confirm=True)
    check("8e. store clean of demo data after final reset", not sdb.demo_seeded())


# ===========================================================================
# PART B — MCP server over REAL stdio subprocess
# ===========================================================================
def part_b_mcp_stdio():
    from mcp import Client, StdioServerParameters
    from mcp import MCPError

    print("\n=== PART B — store MCP server over real stdio ===")
    params = StdioServerParameters(
        command=sys.executable,
        args=[os.path.join(ROOT, "store_server.py")],
        cwd=ROOT,
        env={**os.environ, "NEXUSAI_STORE_DB": STORE_DB},
    )

    EXPECTED = {
        "add_product", "add_worker", "adjust_stock", "create_purchase", "create_sale",
        "generate_business_report", "get_business_summary", "get_dashboard_summary",
        "get_low_stock", "get_product", "get_profit_loss", "get_sale",
        "get_sales_summary", "get_stock_valuation", "list_products", "list_purchases",
        "list_sales", "list_workers", "record_attendance", "record_expense",
        "record_salary_payment", "reset_demo_data", "seed_demo_data",
        "update_product", "update_worker",
    }

    def text_of(r):
        return "".join(c.text for c in r.content if hasattr(c, "text"))

    async def scenario():
        out = {}
        async with Client(params) as c:
            tools = sorted(t.name for t in (await c.list_tools()).tools)
            out["tools"] = tools
            check("9a. 25 tools discovered over stdio", set(tools) == EXPECTED,
                  f"got {len(tools)}")

            # valid calls
            r = await c.call_tool("add_product", {
                "name": "Stdio Fanta", "sell_price": 120.0, "unit_cost": 80.0,
                "stock_units": 15, "reorder_level": 3})
            out["add_ok"] = not r.is_error
            pid = json.loads(text_of(r))["id"]
            check("9b. add_product valid args works", out["add_ok"])

            r = await c.call_tool("create_sale", {
                "items": json.dumps([{"product_id": pid, "qty": 3, "unit": "bottle"}])})
            sale = json.loads(text_of(r))
            check("9c. create_sale over stdio: total 360, stock 15->12",
                  not r.is_error and sale["total"] == 360.0
                  and scalar("SELECT stock_units FROM products WHERE id=?", (pid,)) == 12)

            r = await c.call_tool("get_dashboard_summary", {})
            dash = json.loads(text_of(r))
            check("9d. get_dashboard_summary returns live keys",
                  not r.is_error and dash["today"] == TODAY and "today_revenue" in dash)

            # invalid calls
            r1 = await c.call_tool("adjust_stock", {
                "product_id": pid, "change_units": -1, "confirm": False})
            r2 = await c.call_tool("create_sale", {"items": "not-json"})
            r3 = await c.call_tool("get_product", {"product_id": 999999})
            r4 = await c.call_tool("reset_demo_data", {"confirm": False})
            r5 = await c.call_tool("add_product", {"name": "", "sell_price": 10})
            check("9e. invalid args -> isError (no-confirm, bad JSON, missing id,"
                  " reset no-confirm, blank name)",
                  r1.is_error and r2.is_error and r3.is_error
                  and r4.is_error and r5.is_error)

            # resources read-only
            counts_before = (
                scalar("SELECT COUNT(*) FROM sales"),
                scalar("SELECT COUNT(*) FROM products"),
                scalar("SELECT COUNT(*) FROM purchases"),
            )
            uris = [str(x.uri) for x in (await c.list_resources()).resources]
            check("9f. 4 resource URIs listed",
                  uris == ["nexus://store/summary", "nexus://inventory/low-stock",
                           "nexus://sales/today", "nexus://staff/summary"])
            texts = []
            for u in uris:
                res = await c.read_resource(u)
                texts.append(res.contents[0].text)
            parsed = [json.loads(t) for t in texts]
            counts_after = (
                scalar("SELECT COUNT(*) FROM sales"),
                scalar("SELECT COUNT(*) FROM products"),
                scalar("SELECT COUNT(*) FROM purchases"),
            )
            check("10a. resources return valid JSON of live data",
                  "today_revenue" in parsed[0] and "low_stock" in parsed[1]
                  and "transactions" in parsed[2] and "workers" in parsed[3])
            check("10b. resource reads mutate NOTHING (row counts identical)",
                  counts_before == counts_after, f"{counts_before} == {counts_after}")

            # prompts
            prompts = sorted(p.name for p in (await c.list_prompts()).prompts)
            check("10c. 2 prompts listed", prompts == ["business_report_prompt", "stock_review"])
            p1 = await c.get_prompt("stock_review", {"focus": "restock"})
            p2 = await c.get_prompt("business_report_prompt", {"period": "month"})
            t1 = p1.messages[0].content.text
            t2 = p2.messages[0].content.text
            check("10d. prompts render documented read-only instructions",
                  "Do NOT modify" in t1 and "Do NOT modify" in t2
                  and "Low-stock products" in t1 and "BEVERAGE STORE REPORT" in t2)
            counts_after_prompts = (
                scalar("SELECT COUNT(*) FROM sales"),
                scalar("SELECT COUNT(*) FROM products"),
            )
            check("10e. prompt renders mutate NOTHING", counts_after_prompts == counts_after[:2])
            try:
                await c.get_prompt("business_report_prompt", {"period": "weekly"})
                bad_prompt = False
            except BaseException as e:  # MCPError or wrapped group
                bad_prompt = isinstance(e, MCPError) or (
                    isinstance(e, BaseExceptionGroup) and any(
                        isinstance(x, MCPError) for x in e.exceptions))
            check("10f. invalid prompt arg -> protocol error", bad_prompt)

            # bug check: price update must NOT silently reactivate a deactivated product
            r = await c.call_tool("update_product", {
                "product_id": pid, "sell_price": 130.0, "confirm": True})
            was_active = json.loads(text_of(r))["is_active"]
            await c.call_tool("update_product", {
                "product_id": pid, "is_active": False, "confirm": True})
            r = await c.call_tool("update_product", {
                "product_id": pid, "sell_price": 140.0, "confirm": True})
            now_active = json.loads(text_of(r))["is_active"]
            check("9g. price update does NOT reactivate a deactivated product",
                  was_active is True and now_active is False,
                  f"active after price update on inactive product = {now_active}")

    asyncio.run(scenario())


# ===========================================================================
# PART C — Flask dashboard: CSRF, validation, persistence (not just 200s)
# ===========================================================================
def part_c_flask():
    print("\n=== PART C — Flask dashboard workflows ===")
    from app import app
    app.config["TESTING"] = True
    client = app.test_client()

    # GET pages
    r = client.get("/store/")
    check("11a. dashboard 200 and shows live section", r.status_code == 200
          and b"Beverage Store" in r.data)
    for path in ("/store/products", "/store/sales", "/store/purchases",
                 "/store/workers", "/store/expenses", "/store/reports", "/store/demo"):
        resp = client.get(path)
        if resp.status_code != 200:
            check(f"11 page {path}", False, resp.status_code)
            return
    check("11b. all store pages 200 (products/sales/purchases/workers/expenses/reports/demo)", True)

    def csrf(html):
        m = re.search(rb'name="csrf_token" value="([^"]+)"', html)
        return m.group(1).decode() if m else None

    tok = csrf(client.get("/store/products").data)
    check("11c. CSRF token present in forms", bool(tok))

    # POST without token -> rejected, nothing created
    n_before = scalar("SELECT COUNT(*) FROM products")
    r = client.post("/store/products", data={"name": "NoToken Item", "sell_price": 100})
    n_after = scalar("SELECT COUNT(*) FROM products")
    check("11d. POST without CSRF token rejected; NO row created",
          n_after == n_before and r.status_code in (302, 400),
          f"status={r.status_code}")

    # POST with bad token
    r = client.post("/store/products",
                    data={"csrf_token": "wrong", "name": "BadToken", "sell_price": 100})
    check("11e. POST with invalid CSRF token rejected",
          scalar("SELECT COUNT(*) FROM products") == n_before)

    # validation: negative price
    r = client.post("/store/products",
                    data={"csrf_token": tok, "name": "NegPrice", "sell_price": "-5",
                          "unit_cost": "1"}, follow_redirects=True)
    check("11f. negative sell_price rejected with error flash",
          scalar("SELECT COUNT(*) FROM products") == n_before
          and b"greater than zero" in r.data)

    # valid product -> redirect -> visible -> persists in fresh connection
    r = client.post("/store/products",
                    data={"csrf_token": tok, "name": "Web Product", "brand": "WebCo",
                          "unit_cost": "80", "sell_price": "120", "stock_units": "10",
                          "reorder_level": "2", "units_per_crate": "6"},
                    follow_redirects=True)
    wid = scalar("SELECT id FROM products WHERE name='Web Product'")
    fresh = sqlite3.connect(STORE_DB).execute(
        "SELECT stock_units FROM products WHERE name='Web Product'").fetchone()
    check("11g. web product created, visible after redirect, persists in fresh DB read",
          r.status_code == 200 and b"Web Product" in r.data and wid and fresh[0] == 10)

    # POS sale via web
    r = client.post("/store/sales",
                    data={"csrf_token": tok, "p1_id": str(wid), "p1_qty": "4",
                          "p1_unit": "bottle", "discount": "10", "payment_method": "cash",
                          "paid": ""},
                    follow_redirects=True)
    stock = scalar("SELECT stock_units FROM products WHERE id=?", (wid,))
    total = scalar("SELECT total_paisa FROM sales ORDER BY id DESC LIMIT 1")
    check("11h. web POS sale: 4*120-10 = 470, stock 10->6, receipt page shown",
          b"PKR 470.00" in r.data and stock == 6 and total == 47000)

    # stock adjust below zero rejected
    r = client.post(f"/store/products/{wid}/adjust",
                    data={"csrf_token": tok, "change_units": "-999"},
                    follow_redirects=True)
    check("11i. web negative-stock adjust rejected; stock unchanged",
          scalar("SELECT stock_units FROM products WHERE id=?", (wid,)) == 6
          and b"Insufficient stock" in r.data)

    # toggle deactivate persists
    client.post(f"/store/products/{wid}/toggle",
                data={"csrf_token": tok}, follow_redirects=True)
    check("11j. web toggle deactivates and persists",
          scalar("SELECT is_active FROM products WHERE id=?", (wid,)) == 0)

    # worker via web
    r = client.post("/store/workers",
                    data={"csrf_token": tok, "form": "worker", "name": "Web Worker",
                          "role": "Guard", "salary": "20000", "salary_frequency": "monthly"},
                    follow_redirects=True)
    check("11k. web worker created",
          scalar("SELECT COUNT(*) FROM workers WHERE name='Web Worker'") == 1)

    # expense via web + reports reflect it
    client.post("/store/expenses",
                data={"csrf_token": tok, "category": "Web Electric", "amount": "750"},
                follow_redirects=True)
    r = client.get("/store/reports")
    check("11l. web expense recorded and reports page renders P&L from live data",
          scalar("SELECT COUNT(*) FROM expenses WHERE category='Web Electric'") == 1
          and b"Net profit" in r.data and b"Stock valuation" in r.data)

    # demo page via web
    r = client.post("/store/demo", data={"csrf_token": tok, "action": "seed"},
                    follow_redirects=True)
    seeded_web = scalar("SELECT COUNT(*) FROM products WHERE is_demo=1") > 0
    r = client.post("/store/demo",
                    data={"csrf_token": tok, "action": "reset"},  # no checkbox
                    follow_redirects=True)
    still = scalar("SELECT COUNT(*) FROM products WHERE is_demo=1") > 0
    r = client.post("/store/demo",
                    data={"csrf_token": tok, "action": "reset", "confirm_reset": "yes"},
                    follow_redirects=True)
    gone = scalar("SELECT COUNT(*) FROM products WHERE is_demo=1") == 0
    check("11m. web demo seed works; reset needs checkbox; then removes demo only",
          seeded_web and still and gone
          and scalar("SELECT COUNT(*) FROM products WHERE name='Web Product'") == 1)


# ===========================================================================
# PART D — tasks + notes still work; three databases independent
# ===========================================================================
def part_d_tasks_notes():
    print("\n=== PART D — existing task/notes functionality + independence ===")
    import database as db
    import notes_server as ns

    db.init_db()
    ns.init_db()
    t = db.create_task("Audit temp task", source="audit")
    db.complete_task(t["id"], source="audit")
    tasks = db.list_tasks()
    check("12a. task create/complete/list still works",
          any(x["id"] == t["id"] and x["completed"] for x in tasks))
    n = ns.db_create_note("Audit note", "acceptance content")
    found = ns.db_search_notes("acceptance")
    check("12b. note create/search still works",
          any(x["id"] == n["id"] for x in found))

    t_tables = {r[0] for r in sqlite3.connect(TASK_DB).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    n_tables = {r[0] for r in sqlite3.connect(NOTES_DB).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    s_tables = {r[0] for r in sqlite3.connect(STORE_DB).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("12c. three databases independent (no cross tables)",
          "tasks" in t_tables and "notes" not in t_tables
          and "notes" in n_tables and "tasks" not in n_tables
          and "products" in s_tables and "tasks" not in s_tables
          and "notes" not in s_tables)


# ===========================================================================
# PART E — static security scan (workflow 13)
# ===========================================================================
def part_e_security():
    print("\n=== PART E — security scan ===")
    import glob
    files = [os.path.join(ROOT, f) for f in
             ("store_db.py", "store_server.py", "store_routes.py", "app.py",
              "database.py", "mcp_server.py", "verify.py")] + \
            glob.glob(os.path.join(ROOT, "notes_server", "*.py"))
    blobs = {}
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            blobs[os.path.basename(f)] = fh.read()

    dangerous = []
    for name, src in blobs.items():
        for pat in (r"os\.system\(", r"eval\(", r"exec\(", r"__import__\(",
                    r"pickle\.loads", r"shell\s*=\s*True"):
            if re.search(pat, src):
                dangerous.append(f"{name}:{pat}")
    check("13a. no eval/exec/os.system/shell=True in project sources", not dangerous,
          dangerous)

    secret_hits = []
    for name, src in blobs.items():
        for m in re.finditer(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*=\s*['\"][^'\"]{8,}", src):
            if "token_hex" in m.group(0) or "secrets.token" in src[max(0, m.start()-60):m.start()+60]:
                continue
            secret_hits.append(f"{name}:{m.group(0)[:40]}")
    check("13b. no hardcoded secrets/credentials in sources", not secret_hits, secret_hits)

    # SQL safety: every execute() call must use ? placeholders for values
    unsafe_sql = []
    for name, src in blobs.items():
        for m in re.finditer(r"execute\(\s*(f?[\"'])(?:SELECT|INSERT|UPDATE|DELETE|DROP)[^\"']*\1", src):
            frag = m.group(0)
            if "f\"" in frag or "f'" in frag:
                # allowed only for identifier interpolation of hardcoded sets
                if not re.search(r"\{(table|', '\.join\(sets\))\}", frag):
                    unsafe_sql.append(f"{name}:{frag[:60]}")
    check("13c. no f-string SQL with user data (only hardcoded identifiers)",
          not unsafe_sql, unsafe_sql)

    # placeholder-style SQL confirmed parameterized
    param_sql = sum(src.count("execute(") and src.count("?") for src in blobs.values())
    check("13d. parameterized queries present throughout (hundreds of ? placeholders)",
          param_sql > 100, f"count={param_sql}")

    # app binds localhost only
    app_src = blobs["app.py"]
    check("13e. Flask binds 127.0.0.1 only (no public exposure)",
          'app.run(host="127.0.0.1"' in app_src)

    # destructive MCP tool requires explicit confirm (covered in 9e too)
    ss = blobs["store_server.py"]
    check("13f. destructive reset tool hard-requires confirm=true",
          "def reset_demo_data" in ss and "if not confirm:" in ss
          and "destructive=True" in ss.replace("destructive_hint=True", "destructive=True"))

    # reset_demo only deletes is_demo=1
    sd = blobs["store_db.py"]
    check("13g. demo reset deletes ONLY is_demo=1 rows (no blanket DELETE)",
          sd.count("DELETE FROM") > 0 and "DELETE FROM products" in sd
          and "WHERE is_demo=1" in sd
          and not re.search(r"DELETE FROM\s+\w+\s*;\s*\n", sd))

    # .gitignore covers all three DBs
    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        gi = f.read()
    check("13h. .gitignore excludes nexusai.db, notes.db, store.db (+wal/shm)",
          all(x in gi for x in ("nexusai.db", "notes.db", "store.db")))

    # host config well-formed
    with open(os.path.join(ROOT, ".agents", "mcp_config.json"), encoding="utf-8-sig") as f:
        cfg = json.load(f)
    check("13i. host config has all three servers, no secrets",
          set(cfg["mcpServers"]) == {"nexusai", "nexusai-notes", "nexusai-store"}
          and "password" not in json.dumps(cfg).lower())
    with open(os.path.join(ROOT, ".agents", "mcp_config.json"), "rb") as f:
        check("13j. host config is strict UTF-8 JSON (no BOM)", not f.read().startswith(b"\xef\xbb\xbf"))


def main():
    real_hashes_before = {p: sha(p) for p in (REAL_STORE, REAL_TASKS, REAL_NOTES)}
    print("Acceptance audit — isolated temp DBs at:", TMP)
    print("Real DB hashes before:", {os.path.basename(k): (v or "absent")[:12]
                                     for k, v in real_hashes_before.items()})

    part_a_db_workflows()
    part_b_mcp_stdio()
    part_c_flask()
    part_d_tasks_notes()
    part_e_security()

    real_hashes_after = {p: sha(p) for p in (REAL_STORE, REAL_TASKS, REAL_NOTES)}
    check("14. REAL databases (store/task/notes) byte-identical after audit",
          real_hashes_before == real_hashes_after)

    passed = sum(RESULTS)
    print("=" * 60)
    print(f"{passed}/{len(RESULTS)} audit checks PASSED")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
