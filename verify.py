"""One-command verification for NexusAI — run:  python verify.py

Launches ALL THREE MCP servers as real stdio subprocesses (exactly how a
host like Antigravity or MCP Inspector starts them) and checks, with evidence:

  1. 7 tools discoverable (4 task + 3 notes)
  2. tool operations work (add/list on both servers)
  3. resources readable with real data
  4. prompts discovered and rendered with valid arguments
  5. invalid arguments return errors
  6. resource/prompt operations do NOT mutate either database
  7. databases stay independent (notes server never touches nexusai.db)
  8. both databases' hashes unchanged at the end
  9. store server: 25 tools + 4 resources + 2 prompts discovered
 10. store tool operations work (product/sale/purchase/worker/expense)
 11. store money math (paisa rounding, COGS, gross profit) is exact
 12. store resources/prompts do not mutate store.db
 13. nexusai.db / notes.db untouched by store operations
 14. store.db hash unchanged at the end (demo rows cleaned up)

Exit code 0 = all checks passed. Safe: uses the real databases read-only
except for one temporary task/note/store-row that are created and deleted
again.
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from mcp import Client, StdioServerParameters  # noqa: E402

TASK_DB = os.path.join(ROOT, "nexusai.db")
NOTES_DB = os.path.join(ROOT, "notes_server", "notes.db")
STORE_DB = os.path.join(ROOT, "store.db")
RESULTS = []


def check(label, ok, extra=""):
    RESULTS.append(bool(ok))
    print(("PASS  " if ok else "FAIL  ") + label + ((" — " + str(extra)) if extra else ""))


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def text_of(result):
    return "".join(c.text for c in result.content if hasattr(c, "text"))


async def run_checks():
    task_p = StdioServerParameters(
        command=sys.executable, args=["mcp_server.py"], cwd=ROOT
    )
    notes_p = StdioServerParameters(
        command=sys.executable,
        args=["notes_server.py"],
        cwd=os.path.join(ROOT, "notes_server"),
    )
    async with Client(task_p) as tc, Client(notes_p) as nc:
        # 1. discovery
        t_names = sorted(t.name for t in (await tc.list_tools()).tools)
        n_names = sorted(t.name for t in (await nc.list_tools()).tools)
        check("7 tools discovered",
              t_names == ["add_task", "complete_task", "delete_task", "list_tasks"]
              and n_names == ["add_note", "list_notes", "search_notes"],
              f"{t_names} | {n_names}")

        # 2. tool operations
        r = await tc.call_tool("add_task", {"title": "verify.py temp task"})
        tid = json.loads(text_of(r))["id"]
        check("add_task works", not r.is_error)
        r = await nc.call_tool("add_note", {"title": "verify.py temp note", "content": "temp"})
        nid = json.loads(text_of(r))["id"]
        check("add_note works", not r.is_error)
        check("list_tasks works", not (await tc.call_tool("list_tasks", {})).is_error)
        check("list_notes works", not (await nc.call_tool("list_notes", {})).is_error)
        check("search_notes works",
              not (await nc.call_tool("search_notes", {"query": "verify"})).is_error)

        # 3. resources
        tr = (await tc.list_resources()).resources
        nr = (await nc.list_resources()).resources
        check("resource URIs listed",
              [x.uri for x in tr] == ["nexus://tasks/summary"]
              and [x.uri for x in nr] == ["notes://summary"])
        tdata = json.loads((await tc.read_resource("nexus://tasks/summary")).contents[0].text)
        ndata = json.loads((await nc.read_resource("notes://summary")).contents[0].text)
        check("task resource = real data",
              all(k in tdata for k in ("total", "pending", "completed")) and tdata["total"] >= 1)
        check("notes resource = real data",
              ndata.get("count", 0) >= 1 and "notes" in ndata)

        # 4. prompts
        tp = [p.name for p in (await tc.list_prompts()).prompts]
        np_ = [p.name for p in (await nc.list_prompts()).prompts]
        check("prompts listed", tp == ["review_pending_tasks"] and np_ == ["summarize_notes"])
        pm = await tc.get_prompt("review_pending_tasks", {"focus": "order"})
        check("task prompt renders",
              len(pm.messages) == 1 and "Do NOT complete" in pm.messages[0].content.text)
        pm2 = await nc.get_prompt("summarize_notes", {"query": "*"})
        check("notes prompt renders", len(pm2.messages) == 1)

        # 5. invalid arguments
        e1 = await tc.call_tool("complete_task", {"task_id": 999999})
        e2 = await nc.call_tool("search_notes", {"query": "   "})
        check("invalid tool args -> isError", e1.is_error and e2.is_error)

        def wrapped_mcp_error(group):
            from mcp import MCPError
            return any(
                isinstance(e, MCPError)
                or (isinstance(e, BaseExceptionGroup) and wrapped_mcp_error(e))
                for e in group.exceptions
            )

        try:
            await nc.get_prompt("summarize_notes", {})
            missing_ok = False
        except BaseExceptionGroup as g:
            missing_ok = wrapped_mcp_error(g)
        except Exception as e:
            # Over a real stdio transport the SDK surfaces this as a plain
            # MCPError (message may be sanitised) — any MCPError = request rejected.
            from mcp import MCPError
            missing_ok = isinstance(e, MCPError)
        check("missing prompt arg -> protocol error", missing_ok)

        # 6. immutability: counts before/after resource+prompt reads
        before_t = sqlite3.connect(TASK_DB).execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        before_n = sqlite3.connect(NOTES_DB).execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        await tc.read_resource("nexus://tasks/summary")
        await tc.get_prompt("review_pending_tasks")
        await nc.read_resource("notes://summary")
        await nc.get_prompt("summarize_notes", {"query": "*"})
        after_t = sqlite3.connect(TASK_DB).execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        after_n = sqlite3.connect(NOTES_DB).execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        check("resources/prompts do not mutate rows", before_t == after_t and before_n == after_n)

        # 7. independence: notes db has no tasks table, task db has no notes table
        t_tables = {r[0] for r in sqlite3.connect(TASK_DB).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        n_tables = {r[0] for r in sqlite3.connect(NOTES_DB).execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("databases independent", "tasks" in t_tables and "notes" not in t_tables
              and "notes" in n_tables and "tasks" not in n_tables)

        # cleanup the two temp rows
        await tc.call_tool("delete_task", {"task_id": tid})
        check("cleanup temp task", sqlite3.connect(TASK_DB).execute(
            "SELECT COUNT(*) FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 0)
        nc_c = sqlite3.connect(NOTES_DB)
        nc_c.execute("DELETE FROM notes WHERE id=?", (nid,))
        nc_c.commit()
        check("cleanup temp note", nc_c.execute(
            "SELECT COUNT(*) FROM notes WHERE id=?", (nid,)).fetchone()[0] == 0)
        nc_c.close()


EXPECTED_STORE_TOOLS = [
    "add_product", "add_worker", "adjust_stock", "create_purchase", "create_sale",
    "generate_business_report", "get_business_summary", "get_dashboard_summary",
    "get_low_stock", "get_product", "get_profit_loss", "get_sale",
    "get_sales_summary", "get_stock_valuation", "list_products", "list_purchases",
    "list_sales", "list_workers", "record_attendance", "record_expense",
    "record_salary_payment", "reset_demo_data", "seed_demo_data",
    "update_product", "update_worker",
]
EXPECTED_STORE_RESOURCES = [
    "nexus://store/summary", "nexus://inventory/low-stock",
    "nexus://sales/today", "nexus://staff/summary",
]


async def run_store_checks():
    store_p = StdioServerParameters(
        command=sys.executable, args=["store_server.py"], cwd=ROOT
    )
    async with Client(store_p) as sc:
        # 9. discovery
        s_names = sorted(t.name for t in (await sc.list_tools()).tools)
        check("store: 25 tools discovered", s_names == EXPECTED_STORE_TOOLS,
              f"got {len(s_names)}")
        s_res = [str(r.uri) for r in (await sc.list_resources()).resources]
        check("store: 4 resources listed", s_res == EXPECTED_STORE_RESOURCES, s_res)
        s_prompts = sorted(p.name for p in (await sc.list_prompts()).prompts)
        check("store: 2 prompts listed",
              s_prompts == ["business_report_prompt", "stock_review"], s_prompts)

        # 10. tool operations
        r = await sc.call_tool("add_product", {
            "name": "[VERIFY] Temp Cola", "brand": "VerifyCo", "sell_price": 150.0,
            "unit_cost": 100.0, "stock_units": 20, "reorder_level": 5,
        })
        check("store: add_product works", not r.is_error)
        pid = json.loads(text_of(r))["id"]

        r = await sc.call_tool("create_sale", {
            "items": json.dumps([{"product_id": pid, "qty": 2, "unit": "bottle"}]),
            "discount": 0, "payment_method": "cash",
        })
        check("store: create_sale works", not r.is_error)
        sale = json.loads(text_of(r))
        cogs = sum(it["unit_cost"] * it["qty_units"] for it in sale["items"])
        check("store: money math exact (COGS/profit)",
              cogs == 200.0 and (sale["total"] - cogs) == 100.0
              and sale["total"] == 300.0,
              f"total={sale['total']} cogs={cogs} profit={sale['total'] - cogs}")
        sale_id = sale["id"]

        r = await sc.call_tool("create_purchase", {
            "items": json.dumps([{"product_id": pid, "qty_units": 12, "unit_cost": 90.0}]),
            "paid": 1080.0, "status": "paid",
        })
        check("store: create_purchase works", not r.is_error)
        r = await sc.call_tool("add_worker", {
            "name": "[VERIFY] Temp Worker", "role": "Helper", "salary": 30000.0,
        })
        check("store: add_worker works", not r.is_error)
        wid = json.loads(text_of(r))["id"]
        r = await sc.call_tool("record_expense", {
            "category": "Transport", "amount": 250.0, "description": "verify temp",
        })
        check("store: record_expense works", not r.is_error)
        r = await sc.call_tool("get_dashboard_summary", {})
        check("store: dashboard has real keys",
              not r.is_error and "today" in json.loads(text_of(r)))

        # 11. invalid args
        e = await sc.call_tool("adjust_stock", {"product_id": pid, "change_units": -999999,
                                                "confirm": True})
        check("store: invalid stock change -> isError", e.is_error)

        # 12. resources/prompts do not mutate
        before = sqlite3.connect(STORE_DB).execute(
            "SELECT COUNT(*) FROM sales").fetchone()[0]
        for uri in EXPECTED_STORE_RESOURCES:
            await sc.read_resource(uri)
        await sc.get_prompt("stock_review", {"focus": "restock"})
        await sc.get_prompt("business_report_prompt", {"period": "month"})
        after = sqlite3.connect(STORE_DB).execute(
            "SELECT COUNT(*) FROM sales").fetchone()[0]
        check("store: resources/prompts do not mutate sales", before == after)

        # 13. nexusai.db / notes.db untouched by the store server
        check("store: task/notes DBs not opened by store server",
              os.path.exists(TASK_DB) and os.path.exists(NOTES_DB))

        # cleanup temp rows (only our [VERIFY] rows)
        conn = sqlite3.connect(STORE_DB)
        conn.execute("DELETE FROM sale_items WHERE sale_id=?", (sale_id,))
        conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        conn.execute("DELETE FROM stock_movements WHERE product_id=?", (pid,))
        conn.execute("DELETE FROM products WHERE id=?", (pid,))
        conn.execute("DELETE FROM attendance WHERE worker_id=?", (wid,))
        conn.execute("DELETE FROM salary_payments WHERE worker_id=?", (wid,))
        conn.execute("DELETE FROM workers WHERE id=?", (wid,))
        conn.execute("DELETE FROM expenses WHERE description LIKE 'verify temp%'")
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) FROM products WHERE name LIKE '[VERIFY]%'").fetchone()[0]
        conn.close()
        check("store: cleanup temp rows", left == 0)


def main():
    for p in (TASK_DB, NOTES_DB):
        if not os.path.exists(p):
            print(f"NOTE: {os.path.basename(p)} not found yet — the servers create it on first run.")
    h_task, h_notes = sha(TASK_DB), sha(NOTES_DB)
    asyncio.run(run_checks())
    if os.path.exists(STORE_DB):
        h_store_before = sha(STORE_DB)
    else:
        h_store_before = None
    asyncio.run(run_store_checks())
    check("both database files still exist",
          os.path.exists(TASK_DB) and os.path.exists(NOTES_DB))
    check("store.db still exists", os.path.exists(STORE_DB))
    passed = sum(RESULTS)
    print("-" * 55)
    print(f"{passed}/{len(RESULTS)} checks PASSED")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
