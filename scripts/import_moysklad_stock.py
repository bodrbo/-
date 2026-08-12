"""
One-time import of the МойСклад "Остатки" stock report into the local
catalog + a single warehouse.

Source data is scripts/moysklad_import.json — parsed once out of the PDF
export the client provided (report-Stock-*.pdf, pages 1-79, before its
"Итого:" total row; pages 80+ are a separate cost/profit report and were
not used). Each entry has: sku (МойСклад's own code), name, description
(category breadcrumb), cost_unit, quantity.

No prices are in that report, so every imported product gets cost_price=0
and sale_price=0 — fill those in via the "Редактировать товар" form on the
product page as items actually get used.

Safe to re-run: a product already present (matched by sku) is left alone
and only its stock on the target warehouse is topped up if missing.

Usage (from the app's directory on the server, after `git pull`):
    python3 scripts/import_moysklad_stock.py
"""
import datetime as dt
import json
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "workhours.db")
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "moysklad_import.json")
WAREHOUSE_NAME = "Тюнинг Порзолово"


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        items = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    warehouse = conn.execute(
        "SELECT id FROM supply_warehouses WHERE name = ?", (WAREHOUSE_NAME,)
    ).fetchone()
    if warehouse is None:
        cur = conn.execute(
            "INSERT INTO supply_warehouses (name, address, created_at) VALUES (?, ?, ?)",
            (WAREHOUSE_NAME, None, now),
        )
        warehouse_id = cur.lastrowid
        print(f"Создан склад «{WAREHOUSE_NAME}» (id={warehouse_id}).")
    else:
        warehouse_id = warehouse["id"]
        print(f"Склад «{WAREHOUSE_NAME}» уже существует (id={warehouse_id}).")

    created = 0
    already_had_product = 0
    stocked = 0
    already_had_stock = 0

    for item in items:
        existing = conn.execute(
            "SELECT id FROM supply_products WHERE sku = ?", (item["sku"],)
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
                "cost_price, cost_unit, sale_price, min_stock, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["name"], item["sku"], item["description"], None, None,
                 0, item["cost_unit"], 0, None, now),
            )
            product_id = cur.lastrowid
            created += 1
        else:
            product_id = existing["id"]
            already_had_product += 1

        existing_stock = conn.execute(
            "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id),
        ).fetchone()
        if existing_stock is None:
            conn.execute(
                "INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)",
                (product_id, warehouse_id, item["quantity"]),
            )
            stocked += 1
        else:
            already_had_stock += 1

    conn.commit()
    conn.close()

    print(f"Товаров создано: {created}")
    print(f"Товаров уже было в каталоге (по артикулу): {already_had_product}")
    print(f"Остатков проставлено: {stocked}")
    print(f"Остатков уже было на этом складе (пропущено): {already_had_stock}")


if __name__ == "__main__":
    main()
