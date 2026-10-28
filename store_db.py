"""Store database module — Beverage Store Management (separate store.db).

IMPORTANT: this is a NEW, independent database file. The existing
nexusai.db (tasks) and notes_server/notes.db (notes) are never touched.

Money rule: every amount is stored as INTEGER paisa (1 PKR = 100 paisa)
to avoid floating point errors. Public functions accept/return rupees
(float) and convert at the boundary via to_paisa()/to_rupees().

Stock rule: stock_units always counts BASE units (bottles). Selling by
crate/carton converts with the product's own units_per_crate.

All multi-row financial operations run inside one transaction with
rollback on failure (see get_conn()).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timezone
from decimal import Decimal, ROUND_HALF_UP

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_db_dir() -> str:
    """Local: project folder. On Vercel (VERCEL=1): /tmp (EPHEMERAL only)."""
    if os.environ.get("VERCEL"):
        return "/tmp"
    return BASE_DIR


DB_PATH = os.environ.get("NEXUSAI_STORE_DB") or os.path.join(_default_db_dir(), "store.db")

DEMO_PREFIX = "[DEMO] "


def set_db_path(path: str) -> str:
    global DB_PATH
    DB_PATH = path
    return DB_PATH


def get_db_path() -> str:
    return DB_PATH


# ---------------------------------------------------------------------------
# Money helpers (integer paisa)
# ---------------------------------------------------------------------------

def to_paisa(value) -> int:
    """Convert rupees (int/float/str) to integer paisa, half-up rounding."""
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def to_rupees(paisa: int) -> float:
    return float(Decimal(paisa) / Decimal(100))


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

_initialized_paths: set = set()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if DB_PATH not in _initialized_paths:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            _initialized_paths.add(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    opening_balance_paisa INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    brand TEXT DEFAULT '',
    size_label TEXT DEFAULT '',
    sku TEXT DEFAULT '',
    unit TEXT NOT NULL DEFAULT 'bottle',
    units_per_crate INTEGER NOT NULL DEFAULT 1 CHECK (units_per_crate >= 1),
    unit_cost_paisa INTEGER NOT NULL DEFAULT 0 CHECK (unit_cost_paisa >= 0),
    sell_price_paisa INTEGER NOT NULL DEFAULT 0 CHECK (sell_price_paisa >= 0),
    stock_units INTEGER NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    supplier_id INTEGER REFERENCES suppliers(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    change_units INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('sale','purchase','adjustment','demo')),
    ref_type TEXT DEFAULT '',
    ref_id INTEGER,
    note TEXT DEFAULT '',
    created_by TEXT DEFAULT 'system',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no TEXT NOT NULL UNIQUE,
    subtotal_paisa INTEGER NOT NULL DEFAULT 0,
    discount_paisa INTEGER NOT NULL DEFAULT 0 CHECK (discount_paisa >= 0),
    total_paisa INTEGER NOT NULL DEFAULT 0,
    paid_paisa INTEGER NOT NULL DEFAULT 0,
    change_paisa INTEGER NOT NULL DEFAULT 0,
    payment_method TEXT NOT NULL DEFAULT 'cash',
    created_by TEXT DEFAULT 'system',
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_units INTEGER NOT NULL CHECK (qty_units > 0),
    unit_price_paisa INTEGER NOT NULL,
    unit_cost_paisa INTEGER NOT NULL,
    line_total_paisa INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items(sale_id);
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER REFERENCES suppliers(id),
    ref_no TEXT DEFAULT '',
    total_paisa INTEGER NOT NULL DEFAULT 0,
    paid_paisa INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unpaid' CHECK (status IN ('paid','partial','unpaid')),
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_by TEXT DEFAULT 'system',
    purchased_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_units INTEGER NOT NULL CHECK (qty_units > 0),
    unit_cost_paisa INTEGER NOT NULL,
    line_total_paisa INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purchase_items_p ON purchase_items(purchase_id);
CREATE TABLE IF NOT EXISTS workers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    join_date TEXT NOT NULL,
    salary_paisa INTEGER NOT NULL DEFAULT 0 CHECK (salary_paisa >= 0),
    salary_frequency TEXT NOT NULL DEFAULT 'monthly'
        CHECK (salary_frequency IN ('monthly','weekly','daily')),
    is_active INTEGER NOT NULL DEFAULT 1,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    day TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('present','absent','leave')),
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (worker_id, day)
);
CREATE TABLE IF NOT EXISTS salary_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id INTEGER NOT NULL REFERENCES workers(id),
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
    note TEXT DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
    description TEXT DEFAULT '',
    payment_method TEXT DEFAULT 'cash',
    spent_at TEXT NOT NULL,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create the schema if missing. Idempotent; lazy init in get_conn()
    performs the same work automatically on first use of each DB file."""
    with get_conn() as conn:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_name(value, field="Name", max_len=120) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")
    clean = " ".join(value.split())
    if len(clean) > max_len:
        raise ValueError(f"{field} must be {max_len} characters or fewer.")
    return clean


def validate_positive_number(value, field, allow_zero=False) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number.") from None
    if v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"{field} must be {'non-negative' if allow_zero else 'greater than zero'}.")
    if v > 10_000_000_000:
        raise ValueError(f"{field} is unreasonably large.")
    return v


def validate_positive_int(value, field, allow_zero=False) -> int:
    try:
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number.") from None
    if v < 0 or (v == 0 and not allow_zero):
        raise ValueError(f"{field} must be {'non-negative' if allow_zero else 'greater than zero'}.")
    if v > 10_000_000:
        raise ValueError(f"{field} is unreasonably large.")
    return v


def validate_id(value, field="id") -> int:
    try:
        i = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number.") from None
    if i < 1:
        raise ValueError(f"{field} must be greater than zero.")
    return i


UNIT_TYPES = ("bottle", "crate", "carton")
PAYMENT_METHODS = ("cash", "card", "bank", "easypaisa", "jazzcash", "credit")
PAYMENT_STATUSES = ("paid", "partial", "unpaid")
ATTENDANCE_STATUSES = ("present", "absent", "leave")
SALARY_FREQUENCIES = ("monthly", "weekly", "daily")


def validate_choice(value, field, choices) -> str:
    if value not in choices:
        raise ValueError(f"{field} must be one of: {', '.join(choices)}.")
    return value


# ---------------------------------------------------------------------------
# Row mappers (rupees in output)
# ---------------------------------------------------------------------------

def _product(row) -> dict:
    d = dict(row)
    d["unit_cost"] = to_rupees(d.pop("unit_cost_paisa"))
    d["sell_price"] = to_rupees(d.pop("sell_price_paisa"))
    d["is_active"] = bool(d["is_active"])
    return d


def _sale(row) -> dict:
    d = dict(row)
    for k in ("subtotal_paisa", "discount_paisa", "total_paisa", "paid_paisa", "change_paisa"):
        d[k.replace("_paisa", "")] = to_rupees(d.pop(k))
    return d


def _purchase(row) -> dict:
    d = dict(row)
    for k in ("total_paisa", "paid_paisa"):
        d[k.replace("_paisa", "")] = to_rupees(d.pop(k))
    d["balance"] = round(d["total"] - d["paid"], 2)
    return d


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

def create_supplier(name, phone="", address="", is_demo=0, source="dashboard") -> dict:
    n = validate_name(name, "Supplier name")
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO suppliers (name, phone, address, is_demo, created_at) VALUES (?,?,?,?,?)",
            (n, str(phone or "")[:40], str(address or "")[:200], int(is_demo), now),
        )
        sid = cur.lastrowid
        row = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
    return dict(row)


def list_suppliers(include_inactive=False) -> list:
    q = "SELECT * FROM suppliers"
    if not include_inactive:
        q += " WHERE is_active = 1"
    q += " ORDER BY name"
    with get_conn() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


# ---------------------------------------------------------------------------
# Products (inventory)
# ---------------------------------------------------------------------------

def create_product(name, brand="", size_label="", sku="", unit="bottle",
                   units_per_crate=1, unit_cost=0, sell_price=0,
                   stock_units=0, reorder_level=0, supplier_id=None,
                   is_demo=0) -> dict:
    n = validate_name(name, "Product name")
    unit = validate_choice(unit, "unit", UNIT_TYPES)
    upc = validate_positive_int(units_per_crate, "units_per_crate")
    cost = to_paisa(validate_positive_number(unit_cost, "unit_cost", allow_zero=True))
    price = to_paisa(validate_positive_number(sell_price, "sell_price", allow_zero=True))
    stock = validate_positive_int(stock_units, "stock_units", allow_zero=True)
    reorder = validate_positive_int(reorder_level, "reorder_level", allow_zero=True)
    sid = validate_id(supplier_id, "supplier_id") if supplier_id else None
    now = _now()
    with get_conn() as c:
        if sid and not c.execute("SELECT 1 FROM suppliers WHERE id=?", (sid,)).fetchone():
            raise ValueError(f"No supplier found with id {sid}.")
        cur = c.execute(
            """INSERT INTO products (name, brand, size_label, sku, unit, units_per_crate,
               unit_cost_paisa, sell_price_paisa, stock_units, reorder_level, supplier_id,
               is_demo, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (n, str(brand or "")[:80], str(size_label or "")[:40], str(sku or "")[:60],
             unit, upc, cost, price, stock, reorder, sid, int(is_demo), now, now),
        )
        pid = cur.lastrowid
        if stock:
            c.execute(
                "INSERT INTO stock_movements (product_id, change_units, reason, note, created_at) "
                "VALUES (?,?,?,?,?)",
                (pid, stock, "demo" if is_demo else "adjustment",
                 "opening stock", now),
            )
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    return _product(row)


def get_product(product_id) -> dict:
    pid = validate_id(product_id, "product_id")
    with get_conn() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise ValueError(f"No product found with id {pid}.")
    return _product(row)


def list_products(active_only=True, include_demo=True) -> list:
    q = "SELECT * FROM products"
    cond = []
    if active_only:
        cond.append("is_active = 1")
    if not include_demo:
        cond.append("is_demo = 0")
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY name"
    with get_conn() as c:
        return [_product(r) for r in c.execute(q).fetchall()]


def update_product(product_id, **fields) -> dict:
    pid = validate_id(product_id, "product_id")
    allowed = {"name", "brand", "size_label", "sku", "unit", "units_per_crate",
               "unit_cost", "sell_price", "reorder_level", "supplier_id", "is_active"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    sets, vals = [], []
    if "name" in fields:
        sets.append("name = ?"); vals.append(validate_name(fields["name"], "Product name"))
    if "unit" in fields:
        sets.append("unit = ?"); vals.append(validate_choice(fields["unit"], "unit", UNIT_TYPES))
    if "units_per_crate" in fields:
        sets.append("units_per_crate = ?"); vals.append(validate_positive_int(fields["units_per_crate"], "units_per_crate"))
    if "unit_cost" in fields:
        sets.append("unit_cost_paisa = ?"); vals.append(to_paisa(validate_positive_number(fields["unit_cost"], "unit_cost", allow_zero=True)))
    if "sell_price" in fields:
        sets.append("sell_price_paisa = ?"); vals.append(to_paisa(validate_positive_number(fields["sell_price"], "sell_price", allow_zero=True)))
    if "reorder_level" in fields:
        sets.append("reorder_level = ?"); vals.append(validate_positive_int(fields["reorder_level"], "reorder_level", allow_zero=True))
    for f in ("brand", "size_label", "sku"):
        if f in fields:
            sets.append(f"{f} = ?"); vals.append(str(fields[f] or "")[:80])
    if "supplier_id" in fields:
        sid = validate_id(fields["supplier_id"], "supplier_id") if fields["supplier_id"] else None
        sets.append("supplier_id = ?"); vals.append(sid)
    if "is_active" in fields:
        sets.append("is_active = ?"); vals.append(1 if fields["is_active"] else 0)
    if not sets:
        raise ValueError("No fields to update.")
    sets.append("updated_at = ?"); vals.append(_now())
    vals.append(pid)
    with get_conn() as c:
        cur = c.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", vals)
        if cur.rowcount == 0:
            raise ValueError(f"No product found with id {pid}.")
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    return _product(row)


def adjust_stock(product_id, change_units, reason="adjustment", note="",
                 created_by="system") -> dict:
    """Change stock atomically. change_units may be negative; stock never goes below 0."""
    pid = validate_id(product_id, "product_id")
    change = validate_positive_int(abs(change_units), "change_units")
    change = change if change_units >= 0 else -change
    reason = validate_choice(reason, "reason", ("sale", "purchase", "adjustment", "demo"))
    if change == 0:
        raise ValueError("change_units cannot be zero.")
    now = _now()
    with get_conn() as c:
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError(f"No product found with id {pid}.")
        new_stock = row["stock_units"] + change
        if new_stock < 0:
            raise ValueError(
                f"Insufficient stock for '{row['name']}': available {row['stock_units']}, requested change {change}."
            )
        c.execute("UPDATE products SET stock_units = ?, updated_at = ? WHERE id = ?",
                  (new_stock, now, pid))
        c.execute(
            "INSERT INTO stock_movements (product_id, change_units, reason, note, created_by, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (pid, change, reason, str(note or "")[:200], created_by, now),
        )
        row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    return _product(row)


def stock_history(product_id, limit=50) -> list:
    pid = validate_id(product_id, "product_id")
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM stock_movements WHERE product_id = ? ORDER BY id DESC LIMIT ?",
            (pid, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def get_low_stock() -> list:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM products WHERE is_active = 1 AND stock_units <= reorder_level "
            "ORDER BY stock_units ASC"
        ).fetchall()
    return [_product(r) for r in rows]


# ---------------------------------------------------------------------------
# Sales (POS) — atomic: sale + items + stock deduction in ONE transaction
# ---------------------------------------------------------------------------

def _units_for_qty(product_row, qty: int, unit: str) -> int:
    """Convert a quantity in the chosen unit to base units (bottles)."""
    if unit == "bottle":
        return qty
    return qty * product_row["units_per_crate"]


def create_sale(items, discount=0, payment_method="cash", paid=None,
                created_by="system", is_demo=0) -> dict:
    """items: [{'product_id': int, 'qty': int, 'unit': 'bottle'|'crate'|'carton',
                'unit_price': optional rupees override}]

    Everything commits together; any failure rolls back all of it.
    """
    if not isinstance(items, list) or not items:
        raise ValueError("A sale needs at least one item.")
    if len(items) > 50:
        raise ValueError("A sale can contain at most 50 lines.")
    discount_v = validate_positive_number(discount, "discount", allow_zero=True)
    discount_p = to_paisa(discount_v)
    payment_method = validate_choice(payment_method, "payment_method", PAYMENT_METHODS)

    now = _now()
    with get_conn() as c:
        lines, subtotal_p = [], 0
        for idx, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ValueError(f"Item {idx} must be an object.")
            pid = validate_id(it.get("product_id"), f"item {idx} product_id")
            qty = validate_positive_int(it.get("qty"), f"item {idx} qty")
            unit = validate_choice(it.get("unit", "bottle"), f"item {idx} unit", UNIT_TYPES)
            prow = c.execute("SELECT * FROM products WHERE id=? AND is_active=1", (pid,)).fetchone()
            if prow is None:
                raise ValueError(f"Item {idx}: no active product with id {pid}.")
            units = _units_for_qty(prow, qty, unit)
            if "unit_price" in it and it["unit_price"] is not None:
                unit_price_p = to_paisa(validate_positive_number(it["unit_price"], f"item {idx} unit_price", allow_zero=True))
            else:
                unit_price_p = prow["sell_price_paisa"]
            line_p = unit_price_p * units
            subtotal_p += line_p
            lines.append((pid, units, unit_price_p, prow["unit_cost_paisa"], line_p, prow["name"]))

        if discount_p > subtotal_p:
            raise ValueError("Discount cannot exceed the subtotal.")
        total_p = subtotal_p - discount_p
        paid_p = to_paisa(validate_positive_number(paid, "paid", allow_zero=True)) if paid is not None else total_p
        if paid_p < total_p and payment_method != "credit":
            raise ValueError(
                f"Amount received ({to_rupees(paid_p)}) is less than the total ({to_rupees(total_p)}). "
                "Use payment_method 'credit' for credit sales."
            )
        change_p = paid_p - total_p if payment_method != "credit" else 0

        # receipt number: R + yyyymmdd + seq
        day = now[:10].replace("-", "")
        seq = c.execute("SELECT COUNT(*) FROM sales WHERE created_at LIKE ?", (now[:10] + "%",)).fetchone()[0] + 1
        receipt = f"R{day}-{seq:04d}"

        cur = c.execute(
            "INSERT INTO sales (receipt_no, subtotal_paisa, discount_paisa, total_paisa, "
            "paid_paisa, change_paisa, payment_method, created_by, is_demo, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (receipt, subtotal_p, discount_p, total_p, paid_p, change_p,
             payment_method, created_by, int(is_demo), now),
        )
        sale_id = cur.lastrowid
        for pid, units, up_p, uc_p, line_p, _name in lines:
            c.execute(
                "INSERT INTO sale_items (sale_id, product_id, qty_units, unit_price_paisa, "
                "unit_cost_paisa, line_total_paisa) VALUES (?,?,?,?,?,?)",
                (sale_id, pid, units, up_p, uc_p, line_p),
            )
            prow = c.execute("SELECT stock_units FROM products WHERE id=?", (pid,)).fetchone()
            new_stock = prow["stock_units"] - units
            if new_stock < 0:
                raise ValueError(f"Insufficient stock for product id {pid}.")
            c.execute("UPDATE products SET stock_units = ?, updated_at = ? WHERE id = ?",
                      (new_stock, now, pid))
            c.execute(
                "INSERT INTO stock_movements (product_id, change_units, reason, ref_type, ref_id, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, -units, "sale", "sale", sale_id, created_by, now),
            )
        sale = c.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        result = _sale(sale)
        result["items"] = get_sale_items_conn(c, sale_id)
        return result


def get_sale_items_conn(conn, sale_id) -> list:
    rows = conn.execute(
        "SELECT si.*, p.name AS product_name FROM sale_items si "
        "JOIN products p ON p.id = si.product_id WHERE si.sale_id = ?",
        (sale_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("unit_price_paisa", "unit_cost_paisa", "line_total_paisa"):
            d[k.replace("_paisa", "")] = to_rupees(d.pop(k))
        out.append(d)
    return out


def get_sale(sale_id) -> dict:
    sid = validate_id(sale_id, "sale_id")
    with get_conn() as c:
        row = c.execute("SELECT * FROM sales WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError(f"No sale found with id {sid}.")
        sale = _sale(row)
        sale["items"] = get_sale_items_conn(c, sid)
    return sale


def list_sales(day=None, limit=100) -> list:
    q = "SELECT * FROM sales"
    params = []
    if day:
        q += " WHERE created_at LIKE ?"
        params.append(str(day)[:10] + "%")
    q += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with get_conn() as c:
        return [_sale(r) for r in c.execute(q, params).fetchall()]


def sales_summary(day=None) -> dict:
    like = (str(day)[:10] if day else _today()) + "%"
    with get_conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(total_paisa),0) AS total, "
            "COALESCE(SUM(discount_paisa),0) AS discount FROM sales WHERE created_at LIKE ?",
            (like,),
        ).fetchone()
        cost = c.execute(
            "SELECT COALESCE(SUM(si.unit_cost_paisa * si.qty_units),0) AS cogs FROM sale_items si "
            "JOIN sales s ON s.id = si.sale_id WHERE s.created_at LIKE ?",
            (like,),
        ).fetchone()
    total = row["total"]
    return {
        "day": like[:-1],
        "transactions": row["count"],
        "revenue": to_rupees(total),
        "discounts": to_rupees(row["discount"]),
        "cogs": to_rupees(cost["cogs"]),
        "gross_profit": to_rupees(total - cost["cogs"]),
    }


# ---------------------------------------------------------------------------
# Purchases — atomic receive: items + stock increase in ONE transaction
# ---------------------------------------------------------------------------

def create_purchase(items, supplier_id=None, ref_no="", paid=0,
                    status="unpaid", purchased_at=None, created_by="system",
                    is_demo=0) -> dict:
    """items: [{'product_id': int, 'qty_units': int, 'unit_cost': rupees}]"""
    if not isinstance(items, list) or not items:
        raise ValueError("A purchase needs at least one item.")
    if len(items) > 50:
        raise ValueError("A purchase can contain at most 50 lines.")
    status = validate_choice(status, "status", PAYMENT_STATUSES)
    sid = validate_id(supplier_id, "supplier_id") if supplier_id else None
    paid_v = validate_positive_number(paid, "paid", allow_zero=True)
    paid_p = to_paisa(paid_v)
    when = str(purchased_at)[:10] if purchased_at else _today()
    now = _now()

    with get_conn() as c:
        if sid and not c.execute("SELECT 1 FROM suppliers WHERE id=?", (sid,)).fetchone():
            raise ValueError(f"No supplier found with id {sid}.")
        lines, total_p = [], 0
        for idx, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ValueError(f"Item {idx} must be an object.")
            pid = validate_id(it.get("product_id"), f"item {idx} product_id")
            qty = validate_positive_int(it.get("qty_units"), f"item {idx} qty_units")
            cost = to_paisa(validate_positive_number(it.get("unit_cost"), f"item {idx} unit_cost"))
            if not c.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
                raise ValueError(f"Item {idx}: no product with id {pid}.")
            line_p = cost * qty
            total_p += line_p
            lines.append((pid, qty, cost, line_p))
        if paid_p > total_p:
            raise ValueError("Amount paid cannot exceed the purchase total.")
        if status == "paid" and paid_p < total_p:
            raise ValueError("Status 'paid' requires the full amount paid.")
        if status == "unpaid" and paid_p != 0:
            raise ValueError("Status 'unpaid' requires paid = 0.")
        if status == "partial" and not (0 < paid_p < total_p):
            raise ValueError("Status 'partial' requires 0 < paid < total.")

        cur = c.execute(
            "INSERT INTO purchases (supplier_id, ref_no, total_paisa, paid_paisa, status, "
            "is_demo, created_by, purchased_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, str(ref_no or "")[:60], total_p, paid_p, status, int(is_demo), created_by, when, now),
        )
        purchase_id = cur.lastrowid
        for pid, qty, cost, line_p in lines:
            c.execute(
                "INSERT INTO purchase_items (purchase_id, product_id, qty_units, unit_cost_paisa, line_total_paisa) "
                "VALUES (?,?,?,?,?)",
                (purchase_id, pid, qty, cost, line_p),
            )
            c.execute("UPDATE products SET stock_units = stock_units + ?, updated_at = ? WHERE id = ?",
                      (qty, now, pid))
            c.execute(
                "INSERT INTO stock_movements (product_id, change_units, reason, ref_type, ref_id, created_by, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, qty, "purchase", "purchase", purchase_id, created_by, now),
            )
        row = c.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        p = _purchase(row)
        p["items"] = _purchase_items_conn(c, purchase_id)
        return p


def _purchase_items_conn(conn, purchase_id) -> list:
    rows = conn.execute(
        "SELECT pi.*, p.name AS product_name FROM purchase_items pi "
        "JOIN products p ON p.id = pi.product_id WHERE pi.purchase_id = ?",
        (purchase_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("unit_cost_paisa", "line_total_paisa"):
            d[k.replace("_paisa", "")] = to_rupees(d.pop(k))
        out.append(d)
    return out


def list_purchases(limit=100) -> list:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM purchases ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        out = []
        for r in rows:
            p = _purchase(r)
            p["items"] = _purchase_items_conn(c, r["id"])
            out.append(p)
    return out


def supplier_balance(supplier_id) -> dict:
    sid = validate_id(supplier_id, "supplier_id")
    with get_conn() as c:
        row = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError(f"No supplier found with id {sid}.")
        pur = c.execute(
            "SELECT COALESCE(SUM(total_paisa),0) t, COALESCE(SUM(paid_paisa),0) p "
            "FROM purchases WHERE supplier_id = ?", (sid,),
        ).fetchone()
        pay = c.execute(
            "SELECT COALESCE(SUM(amount_paisa),0) FROM salary_payments WHERE 0"
        ).fetchone()[0]  # placeholder, supplier payments tracked via purchases.paid
    owed = pur["t"] - pur["p"]
    return {
        "supplier_id": sid, "name": row["name"],
        "total_purchased": to_rupees(pur["t"]),
        "total_paid": to_rupees(pur["p"]),
        "balance_owed": to_rupees(owed),
    }


# ---------------------------------------------------------------------------
# Workers / attendance / payroll
# ---------------------------------------------------------------------------

def create_worker(name, role="", phone="", join_date=None, salary=0,
                  salary_frequency="monthly", is_demo=0) -> dict:
    n = validate_name(name, "Worker name")
    role_v = str(role or "")[:80]
    sal = to_paisa(validate_positive_number(salary, "salary", allow_zero=True))
    freq = validate_choice(salary_frequency, "salary_frequency", SALARY_FREQUENCIES)
    jd = str(join_date)[:10] if join_date else _today()
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO workers (name, role, phone, join_date, salary_paisa, salary_frequency, is_demo, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (n, role_v, str(phone or "")[:40], jd, sal, freq, int(is_demo), now),
        )
        wid = cur.lastrowid
        row = c.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
    return _worker(row)


def _worker(row) -> dict:
    d = dict(row)
    d["salary"] = to_rupees(d.pop("salary_paisa"))
    return d


def get_worker(worker_id) -> dict:
    wid = validate_id(worker_id, "worker_id")
    with get_conn() as c:
        row = c.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
    if row is None:
        raise ValueError(f"No worker found with id {wid}.")
    return _worker(row)


def list_workers(active_only=True) -> list:
    q = "SELECT * FROM workers"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY name"
    with get_conn() as c:
        return [_worker(r) for r in c.execute(q).fetchall()]


def update_worker(worker_id, **fields) -> dict:
    wid = validate_id(worker_id, "worker_id")
    allowed = {"name", "role", "phone", "salary", "salary_frequency", "is_active"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown fields: {', '.join(sorted(unknown))}.")
    sets, vals = [], []
    if "name" in fields:
        sets.append("name = ?"); vals.append(validate_name(fields["name"], "Worker name"))
    if "role" in fields:
        sets.append("role = ?"); vals.append(str(fields["role"] or "")[:80])
    if "phone" in fields:
        sets.append("phone = ?"); vals.append(str(fields["phone"] or "")[:40])
    if "salary" in fields:
        sets.append("salary_paisa = ?"); vals.append(to_paisa(validate_positive_number(fields["salary"], "salary", allow_zero=True)))
    if "salary_frequency" in fields:
        sets.append("salary_frequency = ?"); vals.append(validate_choice(fields["salary_frequency"], "salary_frequency", SALARY_FREQUENCIES))
    if "is_active" in fields:
        sets.append("is_active = ?"); vals.append(1 if fields["is_active"] else 0)
    if not sets:
        raise ValueError("No fields to update.")
    vals.append(wid)
    with get_conn() as c:
        cur = c.execute(f"UPDATE workers SET {', '.join(sets)} WHERE id = ?", vals)
        if cur.rowcount == 0:
            raise ValueError(f"No worker found with id {wid}.")
        row = c.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
    return _worker(row)


def record_attendance(worker_id, day, status, note="") -> dict:
    wid = validate_id(worker_id, "worker_id")
    status = validate_choice(status, "status", ATTENDANCE_STATUSES)
    d = str(day)[:10]
    if len(d) != 10 or d[4] != "-" or d[7] != "-":
        raise ValueError("day must be in YYYY-MM-DD format.")
    now = _now()
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM workers WHERE id=?", (wid,)).fetchone():
            raise ValueError(f"No worker found with id {wid}.")
        c.execute(
            "INSERT INTO attendance (worker_id, day, status, note, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(worker_id, day) DO UPDATE SET status=excluded.status, note=excluded.note",
            (wid, d, status, str(note or "")[:200], now),
        )
        row = c.execute("SELECT * FROM attendance WHERE worker_id=? AND day=?", (wid, d)).fetchone()
    return dict(row)


def record_salary_payment(worker_id, amount, note="", is_demo=0) -> dict:
    wid = validate_id(worker_id, "worker_id")
    amt = to_paisa(validate_positive_number(amount, "amount"))
    now = _now()
    with get_conn() as c:
        if not c.execute("SELECT 1 FROM workers WHERE id=?", (wid,)).fetchone():
            raise ValueError(f"No worker found with id {wid}.")
        cur = c.execute(
            "INSERT INTO salary_payments (worker_id, amount_paisa, note, is_demo, created_at) VALUES (?,?,?,?,?)",
            (wid, amt, str(note or "")[:200], int(is_demo), now),
        )
        row = c.execute("SELECT * FROM salary_payments WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["amount"] = to_rupees(d.pop("amount_paisa"))
    return d


def salary_status(worker_id) -> dict:
    """Expected salary vs paid, for the current month (monthly) — transparent math."""
    wid = validate_id(worker_id, "worker_id")
    month = _today()[:7]
    with get_conn() as c:
        row = c.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
        if row is None:
            raise ValueError(f"No worker found with id {wid}.")
        paid = c.execute(
            "SELECT COALESCE(SUM(amount_paisa),0) FROM salary_payments "
            "WHERE worker_id=? AND created_at LIKE ?", (wid, month + "%"),
        ).fetchone()[0]
        present = c.execute(
            "SELECT COUNT(*) FROM attendance WHERE worker_id=? AND day LIKE ? AND status='present'",
            (wid, month + "%"),
        ).fetchone()[0]
    expected = row["salary_paisa"] if row["salary_frequency"] == "monthly" else 0
    return {
        "worker_id": wid, "name": row["name"], "month": month,
        "expected": to_rupees(expected), "paid": to_rupees(paid),
        "outstanding": to_rupees(max(0, expected - paid)),
        "days_present": present,
    }


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

def record_expense(category, amount, description="", payment_method="cash",
                   spent_at=None, is_demo=0) -> dict:
    cat = validate_name(category, "Category", max_len=60)
    amt = to_paisa(validate_positive_number(amount, "amount"))
    pm = validate_choice(payment_method, "payment_method", PAYMENT_METHODS)
    when = str(spent_at)[:10] if spent_at else _today()
    now = _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO expenses (category, amount_paisa, description, payment_method, spent_at, is_demo, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cat, amt, str(description or "")[:300], pm, when, int(is_demo), now),
        )
        row = c.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["amount"] = to_rupees(d.pop("amount_paisa"))
    return d


def list_expenses(limit=100) -> list:
    with get_conn() as c:
        rows = c.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT ?",
                         (max(1, min(int(limit), 500)),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = to_rupees(d.pop("amount_paisa"))
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Reports / summaries (all derived from real rows)
# ---------------------------------------------------------------------------

def dashboard_summary() -> dict:
    today = _today()
    with get_conn() as c:
        products = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(stock_units),0) AS units FROM products WHERE is_active=1"
        ).fetchone()
        stock_val = c.execute(
            "SELECT COALESCE(SUM(stock_units * unit_cost_paisa),0) FROM products WHERE is_active=1"
        ).fetchone()[0]
        s = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_paisa),0) AS total FROM sales WHERE created_at LIKE ?",
            (today + "%",),
        ).fetchone()
        scogs = c.execute(
            "SELECT COALESCE(SUM(si.unit_cost_paisa*si.qty_units),0) FROM sale_items si "
            "JOIN sales s ON s.id=si.sale_id WHERE s.created_at LIKE ?", (today + "%",),
        ).fetchone()[0]
        p = c.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total_paisa),0) AS total FROM purchases WHERE created_at LIKE ?",
            (today + "%",),
        ).fetchone()
        e = c.execute(
            "SELECT COALESCE(SUM(amount_paisa),0) FROM expenses WHERE spent_at = ?", (today,),
        ).fetchone()[0]
        low = c.execute(
            "SELECT COUNT(*) FROM products WHERE is_active=1 AND stock_units <= reorder_level"
        ).fetchone()[0]
    return {
        "today": today,
        "products_active": products["n"],
        "stock_units_total": products["units"],
        "stock_value": to_rupees(stock_val),
        "today_sales_count": s["n"],
        "today_revenue": to_rupees(s["total"]),
        "today_gross_profit": to_rupees(s["total"] - scogs),
        "today_purchases": to_rupees(p["total"]),
        "today_purchases_count": p["n"],
        "today_expenses": to_rupees(e),
        "low_stock_count": low,
    }


def profit_loss(day_from=None, day_to=None) -> dict:
    """Gross profit = revenue - COGS. Net = gross - expenses. Purchases are
    NOT expenses (they become stock/COGS when sold) — documented behaviour."""
    d1 = str(day_from)[:10] if day_from else _today()[:7] + "-01"
    d2 = str(day_to)[:10] if day_to else _today()
    with get_conn() as c:
        rev = c.execute(
            "SELECT COALESCE(SUM(total_paisa),0) FROM sales WHERE substr(created_at,1,10) BETWEEN ? AND ?",
            (d1, d2),
        ).fetchone()[0]
        cogs = c.execute(
            "SELECT COALESCE(SUM(si.unit_cost_paisa*si.qty_units),0) FROM sale_items si "
            "JOIN sales s ON s.id=si.sale_id WHERE substr(s.created_at,1,10) BETWEEN ? AND ?",
            (d1, d2),
        ).fetchone()[0]
        exp = c.execute(
            "SELECT COALESCE(SUM(amount_paisa),0) FROM expenses WHERE spent_at BETWEEN ? AND ?",
            (d1, d2),
        ).fetchone()[0]
        pur = c.execute(
            "SELECT COALESCE(SUM(total_paisa),0) FROM purchases WHERE purchased_at BETWEEN ? AND ?",
            (d1, d2),
        ).fetchone()[0]
    gross = rev - cogs
    return {
        "from": d1, "to": d2,
        "revenue": to_rupees(rev),
        "cogs": to_rupees(cogs),
        "gross_profit": to_rupees(gross),
        "expenses": to_rupees(exp),
        "net_operating_profit": to_rupees(gross - exp),
        "purchases_in_period": to_rupees(pur),
        "note": "Purchases are inventory, not expenses; they appear as COGS when sold.",
    }


def stock_valuation() -> dict:
    with get_conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(stock_units*unit_cost_paisa),0) AS cost, "
            "COALESCE(SUM(stock_units*sell_price_paisa),0) AS retail, "
            "COALESCE(SUM(stock_units),0) AS units FROM products WHERE is_active=1"
        ).fetchone()
    return {
        "units": row["units"],
        "cost_value": to_rupees(row["cost"]),
        "retail_value": to_rupees(row["retail"]),
    }


def business_summary() -> dict:
    return {
        "dashboard": dashboard_summary(),
        "stock": stock_valuation(),
        "profit_loss_this_month": profit_loss(),
        "open_supplier_payables": to_rupees(_supplier_payables()),
    }


def _supplier_payables() -> int:
    with get_conn() as c:
        return c.execute(
            "SELECT COALESCE(SUM(total_paisa - paid_paisa),0) FROM purchases WHERE status != 'paid'"
        ).fetchone()[0]


def recent_transactions(limit=10) -> list:
    out = []
    for s in list_sales(limit=limit):
        out.append({"type": "sale", "id": s["id"], "ref": s["receipt_no"],
                    "amount": s["total"], "at": s["created_at"]})
    for p in list_purchases(limit=limit):
        out.append({"type": "purchase", "id": p["id"], "ref": p["ref_no"] or f"P{p['id']}",
                    "amount": p["total"], "at": p["created_at"]})
    for e in list_expenses(limit=limit):
        out.append({"type": "expense", "id": e["id"], "ref": e["category"],
                    "amount": e["amount"], "at": e["spent_at"]})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:limit]


# ---------------------------------------------------------------------------
# Demo data — idempotent seed, safe reset (only is_demo = 1 rows)
# ---------------------------------------------------------------------------

def demo_seeded() -> bool:
    with get_conn() as c:
        n = c.execute("SELECT COUNT(*) FROM products WHERE is_demo=1").fetchone()[0]
    return n > 0


def seed_demo(force=False) -> dict:
    """Create demo data only if none exists (unless force=True). Idempotent."""
    if demo_seeded() and not force:
        return {"seeded": False, "reason": "Demo data already present. Use reset first."}
    now = _now()
    with get_conn() as c:
        sup = c.execute(
            "INSERT INTO suppliers (name, phone, address, is_demo, created_at) VALUES (?,?,?,?,?)",
            ("[DEMO] PK Beverages Distributor", "0300-0000000", "Karachi", 1, now),
        ).lastrowid
        catalog = [
            ("[DEMO] Coca-Cola", "Coca-Cola", "1.5L", 12, 180, 250, 240, 24),
            ("[DEMO] Sprite", "Sprite", "1.5L", 12, 175, 245, 200, 24),
            ("[DEMO] Fanta", "Fanta", "1.5L", 12, 175, 245, 180, 24),
            ("[DEMO] Pepsi", "Pepsi", "1.5L", 12, 170, 240, 260, 24),
            ("[DEMO] 7UP", "7UP", "1.5L", 12, 170, 240, 150, 24),
            ("[DEMO] Mineral Water", "AquaPure", "1.5L", 12, 60, 90, 400, 20),
        ]
        pids = []
        for name, brand, size, upc, cost, price, stock, reorder in catalog:
            pid = c.execute(
                "INSERT INTO products (name, brand, size_label, unit, units_per_crate, "
                "unit_cost_paisa, sell_price_paisa, stock_units, reorder_level, supplier_id, is_demo, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, brand, size, "bottle", upc, to_paisa(cost), to_paisa(price),
                 stock, reorder, sup, 1, now, now),
            ).lastrowid
            pids.append(pid)
            c.execute(
                "INSERT INTO stock_movements (product_id, change_units, reason, note, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (pid, stock, "demo", "demo opening stock", "demo", now),
            )
        workers = [
            ("[DEMO] Ali", "Salesman", 45000),
            ("[DEMO] Ahmed", "Stock Manager", 50000),
            ("[DEMO] Bilal", "Cashier", 40000),
        ]
        wids = []
        for wname, wrole, wsal in workers:
            wid = c.execute(
                "INSERT INTO workers (name, role, join_date, salary_paisa, salary_frequency, is_demo, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (wname, wrole, _today(), to_paisa(wsal), "monthly", 1, now),
            ).lastrowid
            wids.append(wid)
        c.execute(
            "INSERT INTO attendance (worker_id, day, status, created_at) VALUES (?,?,?,?)",
            (wids[0], _today(), "present", now),
        )
        c.execute(
            "INSERT INTO expenses (category, amount_paisa, description, spent_at, is_demo, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("Rent", to_paisa(1000), "demo shop rent", _today(), 1, now),
        )
    # sales/purchases outside the insert loop for clarity (own transactions)
    create_sale(
        [{"product_id": pids[0], "qty": 2, "unit": "bottle"},
         {"product_id": pids[1], "qty": 1, "unit": "bottle"}],
        created_by="demo", is_demo=1,
    )
    create_purchase(
        [{"product_id": pids[0], "qty_units": 24, "unit_cost": 180}],
        supplier_id=sup, ref_no="DEMO-PO-1", paid=0,
        status="unpaid", created_by="demo", is_demo=1,
    )
    return {"seeded": True, "demo_products": len(catalog), "demo_workers": len(workers)}


def reset_demo(confirm=False) -> dict:
    """Delete ONLY rows with is_demo = 1. Real data is never touched."""
    if not confirm:
        raise ValueError("Reset requires confirm=true. This deletes demo rows only.")
    with get_conn() as c:
        demo_pids = [r[0] for r in c.execute("SELECT id FROM products WHERE is_demo=1").fetchall()]
        counts = {}
        for table in ("sale_items", "sales"):
            counts[table] = c.execute(
                f"DELETE FROM {table} WHERE sale_id IN (SELECT id FROM sales WHERE is_demo=1)"
                if table == "sale_items" else "DELETE FROM sales WHERE is_demo=1"
            ).rowcount
        counts["purchase_items"] = c.execute(
            "DELETE FROM purchase_items WHERE purchase_id IN (SELECT id FROM purchases WHERE is_demo=1)"
        ).rowcount
        counts["purchases"] = c.execute("DELETE FROM purchases WHERE is_demo=1").rowcount
        counts["attendance"] = c.execute(
            "DELETE FROM attendance WHERE worker_id IN (SELECT id FROM workers WHERE is_demo=1)"
        ).rowcount
        counts["salary_payments"] = c.execute(
            "DELETE FROM salary_payments WHERE worker_id IN (SELECT id FROM workers WHERE is_demo=1)"
        ).rowcount
        counts["workers"] = c.execute("DELETE FROM workers WHERE is_demo=1").rowcount
        counts["expenses"] = c.execute("DELETE FROM expenses WHERE is_demo=1").rowcount
        counts["stock_movements"] = c.execute(
            "DELETE FROM stock_movements WHERE product_id IN "
            "(SELECT id FROM products WHERE is_demo=1) OR ref_type='sale' AND ref_id IN "
            "(SELECT id FROM sales WHERE is_demo=1)"
        ).rowcount
        counts["products"] = c.execute("DELETE FROM products WHERE is_demo=1").rowcount
        counts["suppliers"] = c.execute("DELETE FROM suppliers WHERE is_demo=1").rowcount
    return {"reset": True, "deleted": counts}


def generate_business_report(period="month") -> str:
    """Human-readable report from real data (used by MCP prompt/tool)."""
    if period == "today":
        pl = profit_loss(_today(), _today())
    elif period == "week":
        pl = profit_loss(_today()[:8] + "01", _today())  # month-to-date fallback
    else:
        pl = profit_loss()
    dash = dashboard_summary()
    low = get_low_stock()
    lines = [
        f"BEVERAGE STORE REPORT ({period}) — {pl['from']} to {pl['to']}",
        f"Revenue: PKR {pl['revenue']:,.2f} | COGS: PKR {pl['cogs']:,.2f}",
        f"Gross profit: PKR {pl['gross_profit']:,.2f} | Expenses: PKR {pl['expenses']:,.2f}",
        f"Net operating profit: PKR {pl['net_operating_profit']:,.2f}",
        f"Purchases in period (inventory, not expense): PKR {pl['purchases_in_period']:,.2f}",
        f"Active products: {dash['products_active']} | Stock units: {dash['stock_units_total']} "
        f"(value PKR {dash['stock_value']:,.2f})",
        f"Low-stock products: {len(low)}" + (" — " + ", ".join(p['name'] for p in low[:5]) if low else ""),
        pl["note"],
    ]
    return "\n".join(lines)


# NOTE: no database writes happen at import time. Schema creation is lazy
# (first get_conn() use per file), safe on read-only filesystems (Vercel).
# Call init_db() explicitly to create the schema up front.
