import json
import re
import sqlite3
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "Покупатель Морских Товаров"
ROPE = "Товарный Трос Тест"
FILM = "Товарная Плёнка Тест"
ROPE_SKU = "GOODS-TEST-ROPE"
FILM_SKU = "GOODS-TEST-FILM"


class DealerGoodsTests(unittest.TestCase):
    """Дилерский центр → «Морские товары»: orders for goods of the warehouse catalog."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            self.rope_id = self.add_product(db, ROPE, ROPE_SKU, "piece", 1200)
            self.film_id = self.add_product(db, FILM, FILM_SKU, "sqm", 800)
            db.commit()
        self.addCleanup(self.cleanup_now)

    def add_product(self, db, name, sku, unit, sale_price):
        return db.execute(
            "INSERT INTO supply_products (name, sku, cost_price, cost_unit, sale_price, created_at) "
            "VALUES (?, ?, 500, ?, ?, '2026-10-01 10:00')", (name, sku, unit, sale_price)).lastrowid

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        db.execute("DELETE FROM supply_products WHERE sku LIKE 'GOODS-TEST-%'")
        for row in db.execute("SELECT id FROM clients WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    # ---- helpers

    def form(self, items=None, **overrides):
        items = items if items is not None else [(self.rope_id, ROPE, "piece", "3", "1200")]
        data = MultiDict([
            ("client_name", CLIENT), ("phone", "+79990003344"), ("order_date", "2026-10-10"),
            ("deadline_date", ""), ("discount_type", "percent"), ("discount_value", ""), ("comment", ""),
        ])
        for product_id, name, unit, quantity, price in items:
            data.add("item_product_id[]", str(product_id or ""))
            data.add("item_name[]", name)
            data.add("item_unit[]", unit)
            data.add("item_quantity[]", quantity)
            data.add("item_price[]", price)
        for key, value in overrides.items():
            data[key] = value
        return data

    def create(self, items=None, **overrides):
        return self.client.post("/dealer/goods/new", data=self.form(items, **overrides))

    def order(self):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM dealer_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1", (CLIENT,)).fetchone()
            return dict(row) if row else None

    def items(self, order_id):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM dealer_order_items WHERE order_id = ? ORDER BY position", (order_id,)).fetchall()]

    # ---- navigation

    def test_the_subsection_is_in_the_tabs_and_the_pages_open(self):
        page = self.client.get("/dealer/goods").get_data(as_text=True)
        self.assertIn("Морские товары", page)
        self.assertIn('href="/dealer/goods"', page)
        self.assertIn("Лодки и моторы", page)
        self.assertEqual(self.client.get("/dealer/goods/new").status_code, 200)

    def test_login_is_required(self):
        anonymous = application_module.app.test_client()
        for url in ("/dealer/goods", "/dealer/goods/new"):
            self.assertEqual(anonymous.get(url).status_code, 302, url)
        self.assertEqual(anonymous.post("/dealer/goods/new", data={}).status_code, 302)

    def test_the_form_offers_the_warehouse_catalog(self):
        page = self.client.get("/dealer/goods/new").get_data(as_text=True)
        raw = re.search(r'<script id="dealerGoodsData" type="application/json">(.*?)</script>', page, re.S).group(1)
        catalog = {p["name"]: p for p in json.loads(raw)}
        self.assertEqual(catalog[ROPE]["price"], 1200)
        self.assertEqual(catalog[ROPE]["sku"], ROPE_SKU)
        self.assertEqual(catalog[FILM]["unit"], "sqm")
        self.assertNotIn("dealerCatalogData", page)
        self.assertNotIn("Заказ на тюнинг", page)

    # ---- creating and editing

    def test_an_order_is_created_with_goods_lines_and_totals(self):
        response = self.create(items=[
            (self.rope_id, ROPE, "piece", "3", "1200"),
            (self.film_id, FILM, "sqm", "2,5", "800"),
            ("", "", "piece", "1", ""),                                   # the spare empty row
        ], discount_type="amount", discount_value="100")
        self.assertEqual(response.status_code, 302)
        order = self.order()
        self.assertEqual(response.headers["Location"], f"/dealer/orders/{order['id']}")
        self.assertEqual(order["order_type"], "goods")
        self.assertEqual(order["status"], "new_request")
        self.assertEqual(order["subtotal"], 5600)            # 3 × 1200 + 2.5 × 800
        self.assertEqual(order["total"], 5500)
        rope, film = self.items(order["id"])
        self.assertEqual((rope["kind"], rope["product_id"], rope["unit"], rope["quantity"], rope["price"]),
                         ("goods", self.rope_id, "piece", 3, 1200))
        self.assertIsNone(rope["profile_id"])
        self.assertEqual((film["product_id"], film["unit"], film["quantity"]), (self.film_id, "sqm", 2.5))

    def test_the_unit_of_a_catalog_product_comes_from_the_catalog(self):
        self.create(items=[(self.film_id, FILM, "piece", "2", "800")])
        self.assertEqual(self.items(self.order()["id"])[0]["unit"], "sqm")

    def test_a_good_missing_from_the_catalog_can_be_typed_by_hand(self):
        self.create(items=[("", "Ручной товар", "linear_m", "4,5", "300")])
        item = self.items(self.order()["id"])[0]
        self.assertIsNone(item["product_id"])
        self.assertEqual((item["unit"], item["quantity"], item["name"]), ("linear_m", 4.5, "Ручной товар"))

    def test_a_wrong_form_is_refused_and_the_input_kept(self):
        for items, text in [
            ([], "хотя бы один товар"),
            ([(self.rope_id, ROPE, "piece", "2,5", "1200")], "штуки — целое число"),
            ([(self.rope_id, ROPE, "piece", "0", "1200")], "количество"),
            ([(self.rope_id, ROPE, "piece", "1", "")], "укажите цену"),
        ]:
            response = self.create(items=items)
            self.assertEqual(response.status_code, 400, text)
            self.assertIn(text, response.get_data(as_text=True))
            self.assertIsNone(self.order())
        # what was typed stays in the form
        page = self.create(items=[(self.rope_id, ROPE, "piece", "1", "")]).get_data(as_text=True)
        self.assertIn(ROPE, page)
        self.assertIn(CLIENT, page)

    def test_an_order_is_edited_and_stays_a_goods_order(self):
        self.create()
        order = self.order()
        page = self.client.get(f"/dealer/orders/{order['id']}").get_data(as_text=True)
        self.assertIn("Все заказы на товары", page)
        self.assertNotIn("Заказ на тюнинг", page)
        raw = re.search(r'<script id="dealerItemsData" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(raw)[0]["product_id"], self.rope_id)
        response = self.client.post(
            f"/dealer/orders/{order['id']}", data=self.form(items=[(self.film_id, FILM, "sqm", "4", "750")]))
        self.assertEqual(response.status_code, 302)
        order = self.order()
        self.assertEqual((order["order_type"], order["total"]), ("goods", 3000))
        self.assertEqual([i["product_id"] for i in self.items(order["id"])], [self.film_id])

    # ---- the two kinds of orders do not mix

    def test_each_list_shows_only_its_own_orders(self):
        self.create()
        goods_page = self.client.get(f"/dealer/goods?q={CLIENT}").get_data(as_text=True)
        self.assertIn(CLIENT, goods_page)
        self.assertIn(ROPE, goods_page)
        equipment_page = self.client.get(f"/dealer/orders?q={CLIENT}").get_data(as_text=True)
        self.assertNotIn(ROPE, equipment_page)
        self.assertNotIn(ROPE, self.client.get("/dealer/orders?status=new_request").get_data(as_text=True))

    def test_no_tuning_order_is_made_from_a_goods_order(self):
        self.create()
        order = self.order()
        response = self.client.post(f"/dealer/orders/{order['id']}/tuning")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/dealer/orders")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_orders WHERE dealer_order_id = ?", (order["id"],)).fetchone()[0]
        self.assertEqual(count, 0)

    # ---- payments, status, deleting

    def test_status_and_payments_work_as_in_the_other_orders(self):
        self.create()
        order_id = self.order()["id"]
        self.client.post(f"/dealer/orders/{order_id}/status", data={"status": "ready"})
        self.client.post(f"/dealer/orders/{order_id}/payments",
                         data={"amount": "1000", "payment_type": "CASH", "paid_date": ""})
        self.assertEqual(self.order()["status"], "ready")
        page = self.client.get(f"/dealer/goods").get_data(as_text=True)
        self.assertIn("Готов к выдаче", page)
        response = self.client.post(f"/dealer/orders/{order_id}/delete")
        self.assertEqual(response.headers["Location"], f"/dealer/orders/{order_id}")   # a paid order stays

    def test_a_goods_order_is_deleted_back_to_the_goods_list(self):
        self.create()
        order_id = self.order()["id"]
        response = self.client.post(f"/dealer/orders/{order_id}/delete")
        self.assertEqual(response.headers["Location"], "/dealer/goods")
        self.assertIsNone(self.order())
        self.assertEqual(self.items(order_id), [])

    # ---- the client cabinet

    def test_the_cabinet_shows_the_goods_order_with_units(self):
        self.create(items=[(self.film_id, FILM, "sqm", "2,5", "800")])
        order = self.order()
        with application_module.app.app_context():
            db = application_module.get_db()
            client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', '', 'goods-cabinet-token', 'neutral', '2026-09-01 10:00')", (CLIENT,)).lastrowid
            db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                       "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (client_id,))
            db.execute("UPDATE dealer_orders SET client_id = ? WHERE id = ?", (client_id, order["id"]))
            db.commit()
        html = self.client.get("/client/goods-cabinet-token").get_data(as_text=True)
        self.assertIn("Морские товары", html)
        self.assertNotIn("Лодки и моторы", html)
        self.assertIn(FILM, html)
        self.assertIn("2.5 м²", html)
        self.assertIn(">Товар<", html)

    # ---- old databases

    def test_old_dealer_tables_get_the_new_columns_and_keep_their_orders(self):
        from modules.dealer.schema import init_schema
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE dealer_orders (id INTEGER PRIMARY KEY, client_name TEXT, status TEXT, "
                     "order_date TEXT, discount_type TEXT, discount_value REAL, subtotal REAL, total REAL, "
                     "comment TEXT, completed_at TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("CREATE TABLE dealer_order_items (id INTEGER PRIMARY KEY, order_id INTEGER, kind TEXT, "
                     "profile_id INTEGER, name TEXT, serial_number TEXT, quantity INTEGER, price REAL, position INTEGER)")
        conn.execute("INSERT INTO dealer_orders (id, client_name) VALUES (1, 'Старый')")
        conn.execute("INSERT INTO dealer_order_items (order_id, kind, name) VALUES (1, 'boat', 'Лодка')")
        init_schema(conn)
        init_schema(conn)           # idempotent
        self.assertEqual(conn.execute("SELECT order_type FROM dealer_orders").fetchone()[0], "equipment")
        self.assertEqual(conn.execute("SELECT product_id, unit FROM dealer_order_items").fetchone(), (None, "piece"))


if __name__ == "__main__":
    unittest.main()
