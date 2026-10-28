"""Tests for the Beverage Store MCP server (in-memory SDK client)."""

import asyncio
import json

import pytest
from mcp import Client

import store_db as sdb
import store_server

EXPECTED_TOOLS = {
    "add_product", "add_worker", "adjust_stock", "create_purchase", "create_sale",
    "generate_business_report", "get_business_summary", "get_dashboard_summary",
    "get_low_stock", "get_product", "get_profit_loss", "get_sale",
    "get_sales_summary", "get_stock_valuation", "list_products", "list_purchases",
    "list_sales", "list_workers", "record_attendance", "record_expense",
    "record_salary_payment", "reset_demo_data", "seed_demo_data",
    "update_product", "update_worker",
}


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sdb, "DB_PATH", str(tmp_path / "store_test.db"))
    sdb.init_db()
    yield


def run(coro):
    return asyncio.run(coro)


def call(tool, args):
    async def scenario():
        async with Client(store_server.mcp) as client:
            return await client.call_tool(tool, args)

    return run(scenario())


def json_of(result):
    return json.loads("".join(c.text for c in result.content if hasattr(c, "text")))


def test_discovery_25_tools():
    async def scenario():
        async with Client(store_server.mcp, raise_exceptions=True) as client:
            return (await client.list_tools()).tools

    tools = run(scenario())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.description
        assert t.input_schema.get("type") == "object"


def test_resource_and_prompt_discovery():
    async def scenario():
        async with Client(store_server.mcp, raise_exceptions=True) as client:
            res = [str(r.uri) for r in (await client.list_resources()).resources]
            prompts = sorted(p.name for p in (await client.list_prompts()).prompts)
            return res, prompts

    res, prompts = run(scenario())
    assert res == ["nexus://store/summary", "nexus://inventory/low-stock",
                   "nexus://sales/today", "nexus://staff/summary"]
    assert prompts == ["business_report_prompt", "stock_review"]


def test_add_product_and_sale_via_tools():
    r = json_of(call("add_product", {"name": "Sprite", "sell_price": 120.0,
                                     "unit_cost": 80.0, "stock_units": 10,
                                     "reorder_level": 2}))
    pid = r["id"]
    sale = json_of(call("create_sale", {
        "items": json.dumps([{"product_id": pid, "qty": 3, "unit": "bottle"}])}))
    assert sale["total"] == 360.0
    cogs = sum(it["unit_cost"] * it["qty_units"] for it in sale["items"])
    assert cogs == 240.0
    assert json_of(call("get_product", {"product_id": pid}))["stock_units"] == 7


def test_purchase_and_worker_and_expense_tools():
    pid = json_of(call("add_product", {"name": "Fanta", "sell_price": 100.0,
                                       "unit_cost": 70.0}))["id"]
    pur = json_of(call("create_purchase", {
        "items": json.dumps([{"product_id": pid, "qty_units": 12, "unit_cost": 65.0}]),
        "paid": 780.0, "status": "paid"}))
    assert pur["total"] == 780.0
    wid = json_of(call("add_worker", {"name": "Sara", "salary": 25000.0}))["id"]
    json_of(call("record_attendance", {"worker_id": wid, "day": sdb._today()}))
    json_of(call("record_salary_payment", {"worker_id": wid, "amount": 5000.0}))
    e = json_of(call("record_expense", {"category": "Utilities", "amount": 1200.0}))
    assert e["amount"] == 1200.0


def test_adjust_stock_requires_confirm():
    pid = json_of(call("add_product", {"name": "Limca", "sell_price": 90.0,
                                       "stock_units": 5}))["id"]
    r = call("adjust_stock", {"product_id": pid, "change_units": -1, "confirm": False})
    assert r.is_error
    r = json_of(call("adjust_stock", {"product_id": pid, "change_units": -2,
                                      "confirm": True}))
    assert r["stock_units"] == 3


def test_update_product_requires_confirm():
    pid = json_of(call("add_product", {"name": "Dew", "sell_price": 90.0}))["id"]
    r = call("update_product", {"product_id": pid, "sell_price": 110.0, "confirm": False})
    assert r.is_error
    r = json_of(call("update_product", {"product_id": pid, "sell_price": 110.0,
                                        "confirm": True, "is_active": True}))
    assert r["sell_price"] == 110.0


def test_invalid_items_json_rejected():
    r = call("create_sale", {"items": "not-json"})
    assert r.is_error


def test_resources_return_real_data_and_do_not_mutate():
    call("add_product", {"name": "ThumsUp", "sell_price": 100.0, "unit_cost": 60.0,
                         "stock_units": 3, "reorder_level": 5})

    async def scenario():
        async with Client(store_server.mcp, raise_exceptions=True) as client:
            summary = json.loads((await client.read_resource("nexus://store/summary")).contents[0].text)
            low = json.loads((await client.read_resource("nexus://inventory/low-stock")).contents[0].text)
            sales = json.loads((await client.read_resource("nexus://sales/today")).contents[0].text)
            staff = json.loads((await client.read_resource("nexus://staff/summary")).contents[0].text)
            return summary, low, sales, staff

    summary, low, sales, staff = run(scenario())
    assert "today_revenue" in summary
    assert any(p["name"] == "ThumsUp" for p in low["low_stock"])
    assert "transactions" in sales
    assert "workers" in staff and "salary_status" in staff
    assert sdb.demo_seeded() is False  # no mutation


def test_prompts_render_and_are_read_only():
    async def scenario():
        async with Client(store_server.mcp, raise_exceptions=True) as client:
            p1 = await client.get_prompt("stock_review", {"focus": "restock"})
            p2 = await client.get_prompt("business_report_prompt", {"period": "month"})
            return p1, p2

    p1, p2 = run(scenario())
    assert len(p1.messages) == 1 and "Do NOT modify" in p1.messages[0].content.text
    assert len(p2.messages) == 1 and "Do NOT modify" in p2.messages[0].content.text


def test_seed_and_reset_via_tools():
    r = json_of(call("seed_demo_data", {}))
    assert r["seeded"] is True and sdb.demo_seeded()
    r2_pre = call("reset_demo_data", {"confirm": False})
    assert r2_pre.is_error
    r2 = json_of(call("reset_demo_data", {"confirm": True}))
    assert sum(r2["deleted"].values()) > 0
    assert not sdb.demo_seeded()


def test_get_definitions_counts():
    defs = store_server.get_tool_definitions()
    assert len(defs) == 25
    ro = {d["name"]: d["read_only"] for d in defs}
    assert ro["list_products"] is True
    assert ro["create_sale"] is False
