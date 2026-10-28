"""Store routes (Flask blueprint) — Beverage Store dashboard pages."""

from flask import Blueprint, flash, redirect, render_template, request, url_for

import store_db as sdb

bp = Blueprint("store", __name__, url_prefix="/store")


def _form_positive(name, field, allow_zero=False, default=0.0):
    raw = request.form.get(name, "").strip()
    if raw == "":
        return default
    return sdb.validate_positive_number(raw, field, allow_zero=allow_zero)


def _form_int(name, field, allow_zero=True, default=0):
    raw = request.form.get(name, "").strip()
    if raw == "":
        return default
    return sdb.validate_positive_int(raw, field, allow_zero=allow_zero)


@bp.route("/")
def dashboard():
    try:
        summary = sdb.dashboard_summary()
        recent = sdb.recent_transactions(8)
        low = sdb.get_low_stock()
        payables = sdb.business_summary()["open_supplier_payables"]
        error = None
    except Exception as e:
        summary, recent, low, payables, error = {}, [], [], 0.0, str(e)
    return render_template("store/dashboard.html", summary=summary, recent=recent,
                           low=low, payables=payables, error=error)


# ---------------- Products ----------------

@bp.route("/products", methods=["GET", "POST"])
def products():
    if request.method == "POST":
        try:
            p = sdb.create_product(
                request.form.get("name", ""),
                brand=request.form.get("brand", ""),
                size_label=request.form.get("size_label", ""),
                unit=request.form.get("unit", "bottle"),
                units_per_crate=_form_int("units_per_crate", "units_per_crate", allow_zero=False, default=12),
                unit_cost=_form_positive("unit_cost", "unit_cost", allow_zero=True),
                sell_price=_form_positive("sell_price", "sell_price", allow_zero=False),
                stock_units=_form_int("stock_units", "stock_units"),
                reorder_level=_form_int("reorder_level", "reorder_level"),
                supplier_id=_form_int("supplier_id", "supplier_id", default=0) or None,
            )
            flash(f'Product "{p["name"]}" added.', "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.products"))
    try:
        items = sdb.list_products(active_only=False)
        suppliers = sdb.list_suppliers()
        error = None
    except Exception as e:
        items, suppliers, error = [], [], str(e)
    return render_template("store/products.html", products=items, suppliers=suppliers,
                           error=error)


@bp.route("/products/<int:pid>/adjust", methods=["POST"])
def adjust(pid):
    try:
        change = int(request.form.get("change_units", "0") or 0)
        p = sdb.adjust_stock(pid, change, note=request.form.get("note", "")[:200],
                             created_by="dashboard")
        flash(f'Stock updated: "{p["name"]}" now {p["stock_units"]} bottles.', "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("store.products"))


@bp.route("/products/<int:pid>/toggle", methods=["POST"])
def toggle(pid):
    try:
        p = sdb.get_product(pid)
        sdb.update_product(pid, is_active=not p["is_active"])
        flash(f'Product {"activated" if not p["is_active"] else "deactivated"} '
              f'(history preserved).', "success")
    except Exception as e:
        flash(str(e), "error")
    return redirect(url_for("store.products"))


# ---------------- Sales / POS ----------------

@bp.route("/sales", methods=["GET", "POST"])
def sales():
    if request.method == "POST":
        try:
            items = []
            for i in range(1, 6):
                pid = request.form.get(f"p{i}_id", "").strip()
                qty = request.form.get(f"p{i}_qty", "").strip()
                if pid and qty:
                    items.append({
                        "product_id": int(pid), "qty": int(qty),
                        "unit": request.form.get(f"p{i}_unit", "bottle"),
                    })
            sale = sdb.create_sale(
                items,
                discount=_form_positive("discount", "discount", allow_zero=True),
                payment_method=request.form.get("payment_method", "cash"),
                paid=None if request.form.get("paid", "").strip() == "" else _form_positive("paid", "paid", allow_zero=True),
                created_by="dashboard",
            )
            flash(f'Sale saved — receipt {sale["receipt_no"]}, total PKR {sale["total"]:.2f}.', "success")
            return redirect(url_for("store.receipt", sale_id=sale["id"]))
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.sales"))
    try:
        products = sdb.list_products()
        history = sdb.list_sales(limit=30)
        error = None
    except Exception as e:
        products, history, error = [], [], str(e)
    return render_template("store/sales.html", products=products, history=history,
                           error=error)


@bp.route("/sales/<int:sale_id>")
def receipt(sale_id):
    try:
        sale = sdb.get_sale(sale_id)
        cogs = sum(it["unit_cost"] * it["qty_units"] for it in sale["items"])
        error = None
    except Exception as e:
        sale, cogs, error = None, 0.0, str(e)
    return render_template("store/receipt.html", sale=sale, cogs=cogs, error=error)


# ---------------- Purchases / Suppliers ----------------

@bp.route("/purchases", methods=["GET", "POST"])
def purchases():
    if request.method == "POST":
        form = request.form.get("form")
        try:
            if form == "supplier":
                s = sdb.create_supplier(request.form.get("name", ""),
                                        phone=request.form.get("phone", ""),
                                        address=request.form.get("address", ""))
                flash(f'Supplier "{s["name"]}" added.', "success")
            else:
                items = []
                for i in range(1, 5):
                    pid = request.form.get(f"p{i}_id", "").strip()
                    qty = request.form.get(f"p{i}_qty", "").strip()
                    if pid and qty:
                        items.append({
                            "product_id": int(pid), "qty_units": int(qty),
                            "unit_cost": _form_positive(f"p{i}_cost", "unit_cost", allow_zero=False),
                        })
                pur = sdb.create_purchase(
                    items,
                    supplier_id=_form_int("supplier_id", "supplier_id", default=0) or None,
                    ref_no=request.form.get("ref_no", ""),
                    paid=_form_positive("paid", "paid", allow_zero=True),
                    status=request.form.get("status", "unpaid"),
                    created_by="dashboard",
                )
                flash(f'Purchase received — total PKR {pur["total"]:.2f} ({pur["status"]}).', "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.purchases"))
    try:
        products = sdb.list_products()
        suppliers = sdb.list_suppliers()
        history = sdb.list_purchases(limit=30)
        balances = [sdb.supplier_balance(s["id"]) for s in suppliers]
        error = None
    except Exception as e:
        products, suppliers, history, balances, error = [], [], [], [], str(e)
    return render_template("store/purchases.html", products=products, suppliers=suppliers,
                           history=history, balances=balances, error=error)


# ---------------- Workers ----------------

@bp.route("/workers", methods=["GET", "POST"])
def workers():
    if request.method == "POST":
        form = request.form.get("form")
        try:
            if form == "worker":
                w = sdb.create_worker(
                    request.form.get("name", ""),
                    role=request.form.get("role", ""),
                    phone=request.form.get("phone", ""),
                    salary=_form_positive("salary", "salary", allow_zero=True),
                    salary_frequency=request.form.get("salary_frequency", "monthly"),
                )
                flash(f'Worker "{w["name"]}" added.', "success")
            elif form == "attendance":
                a = sdb.record_attendance(
                    int(request.form.get("worker_id", 0)),
                    request.form.get("day", "") or sdb._today(),
                    request.form.get("status", "present"),
                )
                flash(f'Attendance saved: worker {a["worker_id"]} on {a["day"]} = {a["status"]}.', "success")
            elif form == "salary":
                pay = sdb.record_salary_payment(
                    int(request.form.get("worker_id", 0)),
                    _form_positive("amount", "amount", allow_zero=False),
                    note=request.form.get("note", ""),
                )
                flash(f'Salary payment of PKR {pay["amount"]:.2f} recorded.', "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.workers"))
    try:
        ws = sdb.list_workers(active_only=False)
        statuses = {w["id"]: sdb.salary_status(w["id"]) for w in ws if w["is_active"]}
        error = None
    except Exception as e:
        ws, statuses, error = [], {}, str(e)
    return render_template("store/workers.html", workers=ws, statuses=statuses,
                           today=sdb._today(), error=error)


# ---------------- Expenses ----------------

@bp.route("/expenses", methods=["GET", "POST"])
def expenses():
    if request.method == "POST":
        try:
            e = sdb.record_expense(
                request.form.get("category", ""),
                _form_positive("amount", "amount", allow_zero=False),
                description=request.form.get("description", ""),
                payment_method=request.form.get("payment_method", "cash"),
                spent_at=request.form.get("spent_at") or None,
            )
            flash(f'Expense of PKR {e["amount"]:.2f} recorded.', "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.expenses"))
    try:
        items = sdb.list_expenses(limit=100)
        error = None
    except Exception as e:
        items, error = [], str(e)
    return render_template("store/expenses.html", expenses=items, error=error)


# ---------------- Reports ----------------

@bp.route("/reports")
def reports():
    try:
        pl = sdb.profit_loss()
        val = sdb.stock_valuation()
        suppliers = sdb.list_suppliers()
        balances = [sdb.supplier_balance(s["id"]) for s in suppliers]
        report_text = sdb.generate_business_report("month")
        error = None
    except Exception as e:
        pl, val, balances, report_text, error = {}, {}, [], "", str(e)
    return render_template("store/reports.html", pl=pl, valuation=val,
                           balances=balances, report_text=report_text, error=error)


# ---------------- Demo data ----------------

@bp.route("/demo", methods=["GET", "POST"])
def demo():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "seed":
                r = sdb.seed_demo()
                flash("Demo data seeded." if r["seeded"] else r["reason"],
                      "success" if r["seeded"] else "warning")
            elif action == "reset":
                if request.form.get("confirm_reset") != "yes":
                    flash("Reset cancelled — tick the confirmation box first.", "error")
                else:
                    r = sdb.reset_demo(confirm=True)
                    flash(f"Demo data removed: {r['deleted']}. Real data untouched.", "success")
        except Exception as e:
            flash(str(e), "error")
        return redirect(url_for("store.demo"))
    return render_template("store/demo.html", seeded=sdb.demo_seeded())
