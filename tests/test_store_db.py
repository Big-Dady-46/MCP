"""Tests for store_db — Beverage Store core logic (isolated temp store.db)."""

import pytest

import store_db as sdb


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sdb, "DB_PATH", str(tmp_path / "store_test.db"))
    sdb.init_db()
    yield


def product(**kw):
    defaults = dict(name="Cola 1L", brand="ColaCo", sell_price=150, unit_cost=100,
                    stock_units=20, reorder_level=5)
    defaults.update(kw)
    return sdb.create_product(**defaults)


# Money helpers
def test_to_paisa_half_up():
    assert sdb.to_paisa(10.005) == 1001
    assert sdb.to_paisa(10.004) == 1000
    assert sdb.to_rupees(1001) == 10.01


def test_create_product_and_get():
    p = product()
    assert p["stock_units"] == 20 and p["unit_cost"] == 100.0 and p["sell_price"] == 150.0
    g = sdb.get_product(p["id"])
    assert g["name"] == "Cola 1L"


def test_create_product_rejects_bad_input():
    with pytest.raises(ValueError):
        product(name="   ")
    with pytest.raises(ValueError):
        product(sell_price=-1)
    with pytest.raises(ValueError):
        product(unit_cost=-5)


def test_adjust_stock_and_no_negative():
    p = product()
    sdb.adjust_stock(p["id"], 10)
    assert sdb.get_product(p["id"])["stock_units"] == 30
    sdb.adjust_stock(p["id"], -5)
    assert sdb.get_product(p["id"])["stock_units"] == 25
    with pytest.raises(ValueError):
        sdb.adjust_stock(p["id"], -9999)


def test_low_stock():
    p = product(stock_units=3, reorder_level=5)
    low = sdb.get_low_stock()
    assert any(x["id"] == p["id"] for x in low)


def test_create_sale_math_and_stock():
    p = product(stock_units=20)
    sale = sdb.create_sale([{"product_id": p["id"], "qty": 2, "unit": "bottle"}])
    assert sale["total"] == 300.0
    cogs = sum(it["unit_cost"] * it["qty_units"] for it in sale["items"])
    assert cogs == 200.0 and sale["total"] - cogs == 100.0
    assert sale["receipt_no"].startswith("R")
    assert sdb.get_product(p["id"])["stock_units"] == 18


def test_sale_insufficient_stock_rolls_back():
    p = product(stock_units=1)
    with pytest.raises(ValueError):
        sdb.create_sale([{"product_id": p["id"], "qty": 50, "unit": "bottle"}])
    assert sdb.get_product(p["id"])["stock_units"] == 1  # unchanged


def test_sale_discount_validation():
    p = product()
    with pytest.raises(ValueError):
        sdb.create_sale([{"product_id": p["id"], "qty": 1}], discount=99999)
    with pytest.raises(ValueError):
        sdb.create_sale([{"product_id": p["id"], "qty": 1}], discount=-1)


def test_sale_requires_credit_for_partial():
    p = product()
    with pytest.raises(ValueError):
        sdb.create_sale([{"product_id": p["id"], "qty": 1}], paid=10)
    sale = sdb.create_sale([{"product_id": p["id"], "qty": 1}], paid=10, payment_method="credit")
    assert sale["total"] == 150.0


def test_crate_unit_conversion():
    p = product(units_per_crate=12, stock_units=24)
    sale = sdb.create_sale([{"product_id": p["id"], "qty": 1, "unit": "crate"}])
    assert sale["items"][0]["qty_units"] == 12
    assert sale["total"] == 150.0 * 12
    assert sdb.get_product(p["id"])["stock_units"] == 12


def test_purchase_increases_stock_and_supplier_balance():
    p = product(stock_units=10)
    sup = sdb.create_supplier("Pepsi Dist")
    pur = sdb.create_purchase(
        [{"product_id": p["id"], "qty_units": 24, "unit_cost": 90.0}],
        supplier_id=sup["id"], paid=1000.0, status="partial")
    assert pur["total"] == 2160.0
    assert sdb.get_product(p["id"])["stock_units"] == 34
    bal = sdb.supplier_balance(sup["id"])
    assert bal["balance_owed"] == 1160.0


def test_purchase_status_validation():
    p = product()
    with pytest.raises(ValueError):
        sdb.create_purchase([{"product_id": p["id"], "qty_units": 1, "unit_cost": 10}],
                            paid=0, status="paid")


def test_worker_attendance_salary():
    w = sdb.create_worker("Ali", role="Shopkeeper", salary=30000)
    sdb.record_attendance(w["id"], sdb._today(), "present")
    sdb.record_salary_payment(w["id"], 15000)
    st = sdb.salary_status(w["id"])
    assert st["expected"] == 30000.0 and st["paid"] == 15000.0
    assert st["outstanding"] == 15000.0


def test_expense_and_profit_loss():
    p = product(stock_units=10)
    sdb.create_sale([{"product_id": p["id"], "qty": 2, "unit": "bottle"}])
    sdb.record_expense("Rent", 5000, description="monthly rent")
    pl = sdb.profit_loss()
    assert pl["revenue"] == 300.0 and pl["cogs"] == 200.0
    assert pl["gross_profit"] == 100.0 and pl["expenses"] == 5000.0
    assert pl["net_operating_profit"] == 100.0 - 5000.0


def test_stock_valuation_and_dashboard():
    product(stock_units=10, unit_cost=100, sell_price=150)
    v = sdb.stock_valuation()
    assert v["units"] == 10 and v["cost_value"] == 1000.0 and v["retail_value"] == 1500.0
    d = sdb.dashboard_summary()
    assert "today_revenue" in d and "stock_value" in d and "today" in d


def test_seed_demo_idempotent_and_reset_only_demo():
    sdb.seed_demo()
    assert sdb.demo_seeded()
    r = sdb.seed_demo()
    assert r["seeded"] is False
    real = product(name="Real Cola")
    sdb.reset_demo(confirm=True)
    assert not sdb.demo_seeded()
    assert sdb.get_product(real["id"])  # real rows survive
    with pytest.raises(ValueError):
        sdb.reset_demo(confirm=False)


def test_business_report_text():
    product()
    text = sdb.generate_business_report(period="month")
    assert isinstance(text, str) and len(text) > 50


def test_deactivate_product_keeps_history():
    p = product()
    sdb.update_product(p["id"], is_active=False)
    assert sdb.get_product(p["id"])["is_active"] is False
    with pytest.raises(ValueError):
        sdb.create_sale([{"product_id": p["id"], "qty": 1}])
