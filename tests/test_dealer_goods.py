import json
import re
import sqlite3
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "Покупатель Морских Товаров"
ROPE = "Товарный Трос Тест"
FILM = "Товарная Плёнка Тест"
ROPE_SKU = "GOODS-TEST-ROPE"
FILM_SKU = "GOODS-TEST-FILM"
STORE = "Склад Отгрузки Тест"
STORE_2 = "Склад Запасной Тест"


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
            self.store_id = self.add_warehouse(db, STORE, {self.rope_id: 10, self.film_id: 5})
            self.store_2_id = self.add_warehouse(db, STORE_2, {self.rope_id: 1})
            db.commit()
        self.addCleanup(self.cleanup_now)

    def add_product(self, db, name, sku, unit, sale_price):
        return db.execute(
            "INSERT INTO supply_products (name, sku, cost_price, cost_unit, sale_price, created_at) "
            "VALUES (?, ?, 500, ?, ?, '2026-10-01 10:00')", (name, sku, unit, sale_price)).lastrowid

    def add_warehouse(self, db, name, stock):
        warehouse_id = db.execute(
            "INSERT INTO supply_warehouses (name, created_at) VALUES (?, '2026-10-01 10:00')", (name,)).lastrowid
        for product_id, quantity in stock.items():
            db.execute("INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)",
                       (product_id, warehouse_id, quantity))
        return warehouse_id

    def stock(self, product_id, warehouse_id):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT quantity FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
                (product_id, warehouse_id)).fetchone()
            return row["quantity"] if row else None

    def writeoffs(self, order_id):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM supply_writeoffs WHERE dealer_order_id = ? ORDER BY id", (order_id,)).fetchall()]

    def set_status(self, order_id, status, **extra):
        return self.client.post(f"/dealer/orders/{order_id}/status", data={"status": status, **extra})

    def notice(self):
        with self.client.session_transaction() as session:
            return session.get("dealer_notice") or {}

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM supply_writeoffs WHERE dealer_order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        db.execute("DELETE FROM supply_stock WHERE product_id IN (SELECT id FROM supply_products WHERE sku LIKE 'GOODS-TEST-%')")
        db.execute("DELETE FROM supply_warehouses WHERE name IN (?, ?)", (STORE, STORE_2))
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
        self.set_status(order_id, "paid")
        self.client.post(f"/dealer/orders/{order_id}/payments",
                         data={"amount": "1000", "payment_type": "CASH", "paid_date": ""})
        self.assertEqual(self.order()["status"], "paid")
        page = self.client.get(f"/dealer/goods").get_data(as_text=True)
        self.assertIn("Подтвержден и оплачен", page)
        response = self.client.post(f"/dealer/orders/{order_id}/delete")
        self.assertEqual(response.headers["Location"], f"/dealer/orders/{order_id}")   # a paid order stays

    def test_a_goods_order_is_deleted_back_to_the_goods_list(self):
        self.create()
        order_id = self.order()["id"]
        response = self.client.post(f"/dealer/orders/{order_id}/delete")
        self.assertEqual(response.headers["Location"], "/dealer/goods")
        self.assertIsNone(self.order())
        self.assertEqual(self.items(order_id), [])

    # ---- the statuses of goods orders and the shipment

    def test_goods_orders_have_their_own_statuses(self):
        self.create()
        order_id = self.order()["id"]
        goods_page = self.client.get("/dealer/goods").get_data(as_text=True)
        for label in ("Новая заявка", "Подтвержден и оплачен", "Отгружен", "Завершен"):
            self.assertIn(label, goods_page)
        self.assertNotIn("Коммерческое предложение", goods_page)
        self.assertNotIn("Подтвержден и оплачен", self.client.get("/dealer/orders").get_data(as_text=True))
        self.set_status(order_id, "ready")                      # a status of the other group
        self.assertEqual(self.order()["status"], "new_request")
        self.assertEqual(self.notice()["type"], "error")

    def test_the_other_group_cannot_use_goods_statuses(self):
        # an equipment order refuses «Отгружен»
        with application_module.app.app_context():
            db = application_module.get_db()
            equipment_id = db.execute(
                "INSERT INTO dealer_orders (client_name, status, order_date, created_at, updated_at) "
                "VALUES (?, 'new_request', '2026-10-10', '2026-10-10 10:00', '2026-10-10 10:00')", (CLIENT,)).lastrowid
            db.commit()
        self.set_status(equipment_id, "shipped", warehouse_id=self.store_id)
        self.assertEqual(self.order()["status"], "new_request")
        self.assertEqual(self.notice()["type"], "error")

    def test_shipping_needs_a_warehouse(self):
        self.create()
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped")
        self.assertEqual(self.order()["status"], "new_request")
        self.assertIn("склад", self.notice()["message"])
        self.assertEqual(self.writeoffs(order_id), [])

    def test_shipping_writes_the_goods_off_the_chosen_warehouse(self):
        self.create(items=[
            (self.rope_id, ROPE, "piece", "3", "1200"),
            (self.rope_id, ROPE, "piece", "2", "1200"),              # the same product twice
            (self.film_id, FILM, "sqm", "2,5", "800"),
            ("", "Ручной товар", "piece", "9", "100"),                # typed by hand: nothing to write off
        ])
        order_id = self.order()["id"]
        with mock.patch.object(application_module, "_maybe_create_low_stock_request") as low_stock:
            self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        self.assertEqual(self.order()["status"], "shipped")
        self.assertEqual(self.order()["ship_warehouse_id"], self.store_id)
        self.assertIn("Списано", self.notice()["message"])
        self.assertEqual(self.stock(self.rope_id, self.store_id), 5)
        self.assertEqual(self.stock(self.film_id, self.store_id), 2.5)
        self.assertEqual(self.stock(self.rope_id, self.store_2_id), 1)       # the other warehouse is untouched
        rows = {w["product_id"]: w for w in self.writeoffs(order_id)}
        self.assertEqual(set(rows), {self.rope_id, self.film_id})
        self.assertEqual((rows[self.rope_id]["quantity"], rows[self.rope_id]["warehouse_id"]), (5, self.store_id))
        self.assertEqual(rows[self.rope_id]["amount"], 2500)                  # 5 × cost price 500
        self.assertEqual(rows[self.rope_id]["reason"], "Продано в заказе")
        self.assertIn(str(order_id), rows[self.rope_id]["note"])
        self.assertEqual(sorted(call.args[1] for call in low_stock.call_args_list),
                         sorted([self.rope_id, self.film_id]))

    def test_a_warehouse_that_lacks_goods_refuses_the_whole_shipment(self):
        self.create(items=[
            (self.film_id, FILM, "sqm", "1", "800"),
            (self.rope_id, ROPE, "piece", "3", "1200"),
        ])
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped", warehouse_id=self.store_2_id)   # one rope, no film
        self.assertEqual(self.order()["status"], "new_request")
        message = self.notice()["message"]
        self.assertIn(STORE_2, message)
        self.assertIn(ROPE, message)
        self.assertIn(FILM, message)
        self.assertEqual(self.stock(self.rope_id, self.store_2_id), 1)
        self.assertEqual(self.writeoffs(order_id), [])

    def test_going_back_returns_the_goods_to_the_warehouse(self):
        self.create(items=[(self.rope_id, ROPE, "piece", "4", "1200")])
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        self.assertEqual(self.stock(self.rope_id, self.store_id), 6)
        self.set_status(order_id, "paid")
        self.assertEqual(self.stock(self.rope_id, self.store_id), 10)
        self.assertEqual(self.writeoffs(order_id), [])
        self.assertIsNone(self.order()["ship_warehouse_id"])
        # shipped again, then cancelled
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        self.set_status(order_id, "cancelled")
        self.assertEqual(self.stock(self.rope_id, self.store_id), 10)

    def test_completing_keeps_the_write_off_and_needs_the_shipment_first(self):
        self.create(items=[(self.rope_id, ROPE, "piece", "4", "1200")])
        order_id = self.order()["id"]
        self.set_status(order_id, "completed")
        self.assertEqual(self.order()["status"], "new_request")
        self.assertIn("Отгружен", self.notice()["message"])
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        self.set_status(order_id, "completed")
        order = self.order()
        self.assertEqual(order["status"], "completed")
        self.assertTrue(order["completed_at"])
        self.assertEqual(self.stock(self.rope_id, self.store_id), 6)
        self.set_status(order_id, "shipped", warehouse_id=self.store_2_id)    # already out: no second write-off
        self.assertEqual(self.stock(self.rope_id, self.store_id), 6)
        self.assertEqual(self.stock(self.rope_id, self.store_2_id), 1)
        self.assertIsNone(self.order()["completed_at"])
        self.set_status(order_id, "new_request")
        self.assertEqual(self.stock(self.rope_id, self.store_id), 10)

    def test_an_order_of_hand_typed_goods_ships_without_a_warehouse(self):
        self.create(items=[("", "Ручной товар", "piece", "2", "100")])
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped")
        self.assertEqual(self.order()["status"], "shipped")
        self.assertEqual(self.writeoffs(order_id), [])

    def test_editing_a_shipped_order_recalculates_the_write_off(self):
        self.create(items=[(self.rope_id, ROPE, "piece", "4", "1200")])
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        response = self.client.post(f"/dealer/orders/{order_id}", data=self.form(
            items=[(self.rope_id, ROPE, "piece", "7", "1200"), (self.film_id, FILM, "sqm", "1", "800")]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stock(self.rope_id, self.store_id), 3)
        self.assertEqual(self.stock(self.film_id, self.store_id), 4)
        self.assertEqual({w["product_id"]: w["quantity"] for w in self.writeoffs(order_id)},
                         {self.rope_id: 7, self.film_id: 1})
        # more than the warehouse holds: nothing changes
        response = self.client.post(f"/dealer/orders/{order_id}", data=self.form(
            items=[(self.rope_id, ROPE, "piece", "11", "1200")]))
        self.assertEqual(response.status_code, 400)
        self.assertIn(STORE, response.get_data(as_text=True))
        self.assertEqual(self.stock(self.rope_id, self.store_id), 3)
        self.assertEqual(self.stock(self.film_id, self.store_id), 4)
        self.assertEqual([i["quantity"] for i in self.items(order_id)], [7, 1])
        self.assertEqual(self.order()["total"], 9200)

    def test_deleting_a_shipped_order_returns_its_goods(self):
        self.create(items=[(self.rope_id, ROPE, "piece", "4", "1200")])
        order_id = self.order()["id"]
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        self.client.post(f"/dealer/orders/{order_id}/delete")
        self.assertIsNone(self.order())
        self.assertEqual(self.stock(self.rope_id, self.store_id), 10)
        self.assertEqual(self.writeoffs(order_id), [])

    def test_the_dialog_gets_the_warehouses_with_what_each_lacks(self):
        self.create(items=[
            (self.rope_id, ROPE, "piece", "3", "1200"),
            ("", "Ручной товар", "piece", "1", "100"),
        ])
        order_id = self.order()["id"]
        info = self.client.get(f"/dealer/orders/{order_id}/shipping").get_json()
        self.assertEqual([(l["name"], l["quantity"]) for l in info["lines"]], [(ROPE, 3)])
        warehouses = {w["name"]: w for w in info["warehouses"]}
        self.assertEqual(warehouses[STORE]["missing"], [])
        self.assertEqual(warehouses[STORE_2]["missing"], [{"name": ROPE, "need": 3, "have": 1}])
        self.assertIsNone(info["shipped_from"])
        self.set_status(order_id, "shipped", warehouse_id=self.store_id)
        info = self.client.get(f"/dealer/orders/{order_id}/shipping").get_json()
        self.assertEqual(info["shipped_from"], STORE)
        self.assertEqual(info["warehouses"], [])
        self.assertEqual(self.client.get("/dealer/orders/999999/shipping").status_code, 404)

    def test_the_dialog_is_on_the_goods_pages_only(self):
        self.create()
        order_id = self.order()["id"]
        for url in ("/dealer/goods", f"/dealer/orders/{order_id}"):
            page = self.client.get(url).get_data(as_text=True)
            self.assertIn('id="shipModal"', page, url)
            self.assertIn("status-picker:choose", page, url)
        self.assertNotIn('id="shipModal"', self.client.get("/dealer/orders").get_data(as_text=True))
        self.assertIn("status-picker:choose", self.client.get("/static/status-picker.js").get_data(as_text=True))

    def test_a_click_on_the_status_does_not_open_the_order_row(self):
        self.create()
        page = self.client.get("/dealer/goods").get_data(as_text=True)
        self.assertIn("event.target.closest('[data-status-picker-form]", page)

    def test_the_shipping_route_needs_a_login(self):
        self.create()
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.get(f"/dealer/orders/{self.order()['id']}/shipping").status_code, 302)

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

    def test_goods_orders_made_before_their_statuses_move_to_the_nearest_new_one(self):
        from modules.dealer.schema import init_schema
        conn = sqlite3.connect(":memory:")
        init_schema(conn)
        for number, (kind, status) in enumerate([
            ("goods", "estimate"), ("goods", "approved"), ("goods", "ready"), ("goods", "handed_over"),
            ("goods", "new_request"), ("equipment", "approved"),
        ], start=1):
            conn.execute("INSERT INTO dealer_orders (id, client_name, order_type, status, order_date, created_at, "
                         "updated_at) VALUES (?, 'x', ?, ?, '2026-10-10', 'n', 'n')", (number, kind, status))
        init_schema(conn)
        self.assertEqual([r[0] for r in conn.execute("SELECT status FROM dealer_orders ORDER BY id")],
                         ["new_request", "paid", "paid", "completed", "new_request", "approved"])


if __name__ == "__main__":
    unittest.main()
