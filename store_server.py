"""Beverage Store MCP Server — AI control over the store (stdio).

Separate process + separate store.db. Hosts connect to it the same way as
the other two servers (see .agents/mcp_config.json → "nexusai-store").
"""

import json
import os
import sys
from typing import Annotated

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_db as sdb  # noqa: E402

# NOTE: schema is created lazily on first get_conn() use (no import-time
# writes) so importing this module is safe on read-only filesystems.

mcp = MCPServer(
    "nexusai-store",
    instructions=(
        "Beverage Store manager (PKR). Tools cover inventory, sales, purchases, "
        "staff, expenses and reports. Amounts are rupees. High-impact calls "
        "(reset_demo_data) require explicit confirmation arguments."
    ),
)

def _ro():
    return ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _rw():
    return ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _destructive():
    return ToolAnnotations(read_only_hint=False, destructive_hint=True,
                           idempotent_hint=False, open_world_hint=False)


def _err(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def _j(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


# ---------------- INVENTORY ----------------

@mcp.tool(annotations=_ro())
def list_products(include_inactive: bool = False) -> str:
    """List active products with stock, prices (PKR) and unit settings."""
    try:
        return _j(sdb.list_products(active_only=not include_inactive))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_product(product_id: Annotated[int, Field(description="Product id", ge=1)]) -> str:
    """Get one product with full details."""
    try:
        return _j(sdb.get_product(product_id))
    except ValueError as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def add_product(
    name: Annotated[str, Field(description="Product name, e.g. Coca-Cola", min_length=1, max_length=120)],
    sell_price: Annotated[float, Field(description="Selling price per bottle in PKR", ge=0)],
    unit_cost: Annotated[float, Field(description="Purchase cost per bottle in PKR", ge=0)] = 0,
    brand: str = "",
    size_label: str = "",
    unit: Annotated[str, Field(description="bottle | crate | carton")] = "bottle",
    units_per_crate: Annotated[int, Field(description="Bottles per crate/carton", ge=1)] = 12,
    stock_units: Annotated[int, Field(description="Opening stock in bottles", ge=0)] = 0,
    reorder_level: Annotated[int, Field(description="Low-stock threshold in bottles", ge=0)] = 0,
    supplier_id: int = 0,
) -> str:
    """Create a product. Stock is counted in base bottles."""
    try:
        return _j(sdb.create_product(
            name, brand=brand, size_label=size_label, unit=unit,
            units_per_crate=units_per_crate, unit_cost=unit_cost,
            sell_price=sell_price, stock_units=stock_units,
            reorder_level=reorder_level, supplier_id=supplier_id or None,
        ))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def update_product(
    product_id: Annotated[int, Field(description="Product id", ge=1)],
    confirm: Annotated[bool, Field(description="Must be true to apply the change")] = False,
    name: str = "",
    sell_price: float = -1,
    unit_cost: float = -1,
    reorder_level: int = -1,
    is_active: bool | None = None,
) -> str:
    """Update product fields. Requires confirm=true. Pass -1/empty to skip a field.
    is_active is only changed when explicitly provided (never silently reactivates)."""
    if not confirm:
        raise ToolError("Set confirm=true to apply product changes.")
    fields = {}
    try:
        if name:
            fields["name"] = name
        if sell_price >= 0:
            fields["sell_price"] = sell_price
        if unit_cost >= 0:
            fields["unit_cost"] = unit_cost
        if reorder_level >= 0:
            fields["reorder_level"] = reorder_level
        if is_active is not None:
            fields["is_active"] = bool(is_active)
        if not fields:
            raise ValueError("Nothing to update.")
        return _j(sdb.update_product(product_id, **fields))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def adjust_stock(
    product_id: Annotated[int, Field(description="Product id", ge=1)],
    change_units: Annotated[int, Field(description="Bottles to add (+) or remove (-); cannot go below zero")],
    confirm: Annotated[bool, Field(description="Must be true; required for bulk adjustments")] = False,
    note: str = "",
) -> str:
    """Adjust stock by bottles. Negative stock is rejected. Requires confirm=true."""
    if not confirm:
        raise ToolError("Set confirm=true to apply a stock adjustment.")
    try:
        return _j(sdb.adjust_stock(product_id, change_units, reason="adjustment",
                                   note=note, created_by="mcp"))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_low_stock() -> str:
    """Products at or below their reorder level (from live database stock)."""
    try:
        return _j(sdb.get_low_stock())
    except Exception as e:
        raise _err(e) from e


# ---------------- SALES ----------------

@mcp.tool(annotations=_rw())
def create_sale(
    items: Annotated[str, Field(description='JSON array: [{"product_id":1,"qty":2,"unit":"bottle"}]')],
    discount: Annotated[float, Field(description="Discount in PKR", ge=0)] = 0,
    payment_method: Annotated[str, Field(description="cash|card|bank|easypaisa|jazzcash|credit")] = "cash",
    paid: float = -1,
) -> str:
    """Commit a sale atomically (stock deducted in the same transaction)."""
    try:
        parsed = json.loads(items)
        return _j(sdb.create_sale(
            parsed, discount=discount, payment_method=payment_method,
            paid=None if paid < 0 else paid, created_by="mcp",
        ))
    except json.JSONDecodeError:
        raise ToolError("items must be a JSON array string, e.g. [{\"product_id\":1,\"qty\":2,\"unit\":\"bottle\"}].")
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def list_sales(day: str = "", limit: int = 50) -> str:
    """Recent sales (optionally one YYYY-MM-DD day), newest first."""
    try:
        return _j(sdb.list_sales(day=day or None, limit=limit))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_sale(sale_id: Annotated[int, Field(description="Sale id", ge=1)]) -> str:
    """One sale with its line items (prices/costs captured at sale time)."""
    try:
        return _j(sdb.get_sale(sale_id))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_sales_summary(day: str = "") -> str:
    """Revenue, COGS and gross profit for a day (default: today)."""
    try:
        return _j(sdb.sales_summary(day=day or None))
    except Exception as e:
        raise _err(e) from e


# ---------------- PURCHASES ----------------

@mcp.tool(annotations=_rw())
def create_purchase(
    items: Annotated[str, Field(description='JSON array: [{"product_id":1,"qty_units":24,"unit_cost":180}]')],
    supplier_id: int = 0,
    ref_no: str = "",
    paid: Annotated[float, Field(description="Amount paid in PKR", ge=0)] = 0,
    status: Annotated[str, Field(description="paid | partial | unpaid")] = "unpaid",
) -> str:
    """Receive a purchase: stock increases in the same transaction."""
    try:
        parsed = json.loads(items)
        return _j(sdb.create_purchase(
            parsed, supplier_id=supplier_id or None, ref_no=ref_no,
            paid=paid, status=status, created_by="mcp",
        ))
    except json.JSONDecodeError:
        raise ToolError('items must be JSON, e.g. [{"product_id":1,"qty_units":24,"unit_cost":180}].')
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def list_purchases(limit: int = 50) -> str:
    """Purchase history with items and payment status."""
    try:
        return _j(sdb.list_purchases(limit=limit))
    except Exception as e:
        raise _err(e) from e


# ---------------- STAFF ----------------

@mcp.tool(annotations=_ro())
def list_workers() -> str:
    """Active workers with roles and salaries (PKR)."""
    try:
        return _j(sdb.list_workers())
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def add_worker(
    name: Annotated[str, Field(description="Worker name", min_length=1, max_length=120)],
    role: str = "",
    salary: Annotated[float, Field(description="Salary amount in PKR", ge=0)] = 0,
    salary_frequency: Annotated[str, Field(description="monthly | weekly | daily")] = "monthly",
    phone: str = "",
) -> str:
    """Add a worker. No sensitive data (CNIC/bank) is stored."""
    try:
        return _j(sdb.create_worker(name, role=role, phone=phone, salary=salary,
                                    salary_frequency=salary_frequency))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def update_worker(
    worker_id: Annotated[int, Field(description="Worker id", ge=1)],
    confirm: Annotated[bool, Field(description="Must be true to apply")] = False,
    role: str = "",
    salary: float = -1,
    is_active: bool | None = None,
) -> str:
    """Update worker fields. Requires confirm=true. Pass -1/empty to skip.
    is_active is only changed when explicitly provided."""
    if not confirm:
        raise ToolError("Set confirm=true to apply worker changes.")
    fields = {}
    try:
        if role:
            fields["role"] = role
        if salary >= 0:
            fields["salary"] = salary
        if is_active is not None:
            fields["is_active"] = bool(is_active)
        if not fields:
            raise ValueError("Nothing to update.")
        return _j(sdb.update_worker(worker_id, **fields))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def record_attendance(
    worker_id: Annotated[int, Field(description="Worker id", ge=1)],
    day: Annotated[str, Field(description="YYYY-MM-DD")],
    status: Annotated[str, Field(description="present | absent | leave")] = "present",
) -> str:
    """Mark attendance for a worker/day (upsert)."""
    try:
        return _j(sdb.record_attendance(worker_id, day, status))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_rw())
def record_salary_payment(
    worker_id: Annotated[int, Field(description="Worker id", ge=1)],
    amount: Annotated[float, Field(description="Amount paid in PKR", gt=0)],
    note: str = "",
) -> str:
    """Record an actual salary payment. Never marks unpaid as paid."""
    try:
        return _j(sdb.record_salary_payment(worker_id, amount, note=note))
    except Exception as e:
        raise _err(e) from e


# ---------------- ACCOUNTS ----------------

@mcp.tool(annotations=_rw())
def record_expense(
    category: Annotated[str, Field(description="e.g. Rent, Utilities, Transport", min_length=1, max_length=60)],
    amount: Annotated[float, Field(description="Amount in PKR", gt=0)],
    description: str = "",
    payment_method: str = "cash",
    spent_at: str = "",
) -> str:
    """Record an operating expense."""
    try:
        return _j(sdb.record_expense(category, amount, description=description,
                                     payment_method=payment_method, spent_at=spent_at or None))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_business_summary() -> str:
    """Combined live summary: dashboard, stock value, P&L, payables."""
    try:
        return _j(sdb.business_summary())
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_profit_loss(
    day_from: str = "", day_to: str = "",
) -> str:
    """Profit & loss: revenue - COGS = gross; gross - expenses = net. Defaults to this month."""
    try:
        return _j(sdb.profit_loss(day_from=day_from or None, day_to=day_to or None))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_stock_valuation() -> str:
    """Stock units with cost value and retail value (PKR)."""
    try:
        return _j(sdb.stock_valuation())
    except Exception as e:
        raise _err(e) from e


# ---------------- DEMO / REPORTS ----------------

@mcp.tool(annotations=_rw())
def seed_demo_data() -> str:
    """Seed DEMO products/workers/sales (idempotent; never touches real rows)."""
    try:
        return _j(sdb.seed_demo())
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_destructive())
def reset_demo_data(
    confirm: Annotated[bool, Field(description="Must be true — deletes ONLY [DEMO] rows")],
) -> str:
    """Delete only demo-flagged rows. Real business data is never deleted."""
    if not confirm:
        raise ToolError("Set confirm=true. This permanently deletes demo rows only.")
    try:
        return _j(sdb.reset_demo(confirm=True))
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def get_dashboard_summary() -> str:
    """Live dashboard metrics from the database (today's sales, stock, alerts)."""
    try:
        return _j(sdb.dashboard_summary())
    except Exception as e:
        raise _err(e) from e


@mcp.tool(annotations=_ro())
def generate_business_report(
    period: Annotated[str, Field(description="today | month")] = "month",
) -> str:
    """Text business report (revenue, profit, low stock) from real data."""
    try:
        return sdb.generate_business_report(period=period)
    except Exception as e:
        raise _err(e) from e


# ---------------- RESOURCES (read-only) ----------------

@mcp.resource("nexus://store/summary", mime_type="application/json",
              description="Live store dashboard summary.")
def store_summary_resource() -> dict:
    try:
        return sdb.dashboard_summary()
    except Exception as e:
        raise MCPError(code=INTERNAL_ERROR, message=str(e)) from e


@mcp.resource("nexus://inventory/low-stock", mime_type="application/json",
              description="Products at or below reorder level.")
def low_stock_resource() -> dict:
    try:
        return {"low_stock": sdb.get_low_stock()}
    except Exception as e:
        raise MCPError(code=INTERNAL_ERROR, message=str(e)) from e


@mcp.resource("nexus://sales/today", mime_type="application/json",
              description="Today's sales totals and gross profit.")
def sales_today_resource() -> dict:
    try:
        return sdb.sales_summary()
    except Exception as e:
        raise MCPError(code=INTERNAL_ERROR, message=str(e)) from e


@mcp.resource("nexus://staff/summary", mime_type="application/json",
              description="Worker list and this month's salary status.")
def staff_summary_resource() -> dict:
    try:
        workers = sdb.list_workers()
        return {
            "workers": [{"id": w["id"], "name": w["name"], "role": w["role"],
                         "salary": w["salary"]} for w in workers],
            "salary_status": [sdb.salary_status(w["id"]) for w in workers],
        }
    except Exception as e:
        raise MCPError(code=INTERNAL_ERROR, message=str(e)) from e


# ---------------- PROMPTS (read-only templates) ----------------

@mcp.prompt(title="Stock review")
def stock_review(
    focus: Annotated[str, Field(description="e.g. 'restock', 'valuation', or 'all'")] = "all",
) -> str:
    """Ask the AI to review stock levels and suggest restocking (never modifies data)."""
    if not isinstance(focus, str) or len(focus) > 100:
        raise MCPError(code=INVALID_PARAMS, message="focus must be <= 100 characters.")
    low = sdb.get_low_stock()
    val = sdb.stock_valuation()
    listing = "\n".join(f"- {p['name']}: {p['stock_units']} bottles (reorder at {p['reorder_level']})"
                        for p in low) or "(nothing below threshold)"
    return (
        f"Review my beverage store stock (focus: {focus.strip() or 'all'}).\n\n"
        f"Low-stock products:\n{listing}\n\n"
        f"Stock value: PKR {val['cost_value']:,.2f} (retail PKR {val['retail_value']:,.2f}).\n\n"
        "Please summarize restocking needs and suggest quantities. "
        "Do NOT modify any data — only report."
    )


@mcp.prompt(title="Monthly business report")
def business_report_prompt(
    period: Annotated[str, Field(description="today or month")] = "month",
) -> str:
    """Ask the AI to prepare a profit & loss style report (read-only)."""
    if period not in ("today", "month"):
        raise MCPError(code=INVALID_PARAMS, message="period must be 'today' or 'month'.")
    text = sdb.generate_business_report(period=period)
    return (
        f"Prepare a clear business report ({period}) using this real data:\n\n{text}\n\n"
        "Format it with short sections: Sales, Profit, Stock, Expenses, Recommendations. "
        "Do NOT modify any data."
    )


TOOL_COUNT = 25


def get_tool_definitions() -> list:
    import asyncio
    import inspect
    result = mcp.list_tools()
    tools = asyncio.run(result) if inspect.isawaitable(result) else result
    if hasattr(tools, "tools"):
        tools = tools.tools
    out = []
    for t in tools:
        ann = t.annotations
        out.append({
            "name": t.name,
            "description": (t.description or "").strip(),
            "input_schema": t.input_schema,
            "read_only": getattr(ann, "read_only_hint", None) if ann else None,
            "destructive": getattr(ann, "destructive_hint", None) if ann else None,
        })
    return out


if __name__ == "__main__":
    mcp.run(transport="stdio")
