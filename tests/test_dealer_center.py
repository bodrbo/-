import datetime as dt
import json
import re
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "Дилерский Покупатель"
BOAT = "Дилерская лодка Тест"
MOTOR = "Дилерский мотор Тест"


class DealerCenterTests(unittest.TestCase):
    """Дилерский центр: orders for boats and motors taken from the catalog."""

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
            now = "2026-10-01 10:00"
            self.boat_id = db.execute(
                "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, specifications, "
                "created_at, updated_at) VALUES ('dealer-test-boat', ?, 'boat', '', ?, ?)", (BOAT, now, now)).lastrowid
            self.motor_id = db.execute(
                "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, specifications, "
                "created_at, updated_at) VALUES ('dealer-test-motor', ?, 'motor', '', ?, ?)", (MOTOR, now, now)).lastrowid
            db.execute("INSERT INTO dealer_prices (profile_id, price, updated_at) VALUES (?, 1500000, ?)",
                       (self.boat_id, now))
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        for row in db.execute("SELECT id FROM tuning_boat_profiles WHERE model_key LIKE 'dealer-test-%'").fetchall():
            db.execute("DELETE FROM dealer_prices WHERE profile_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_price_list WHERE profile_id = ?", (row["id"],))
            db.execute("DELETE FROM tuning_boat_profiles WHERE id = ?", (row["id"],))
        for row in db.execute("SELECT id FROM clients WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    # ---- helpers

    def form(self, items=None, **overrides):
        items = items if items is not None else [("boat", self.boat_id, BOAT, "HIN-1", "1", "1500000")]
        data = MultiDict([
            ("client_name", CLIENT), ("phone", "+79990001122"), ("order_date", "2026-10-10"),
            ("deadline_date", ""), ("discount_type", "percent"), ("discount_value", ""), ("comment", ""),
        ])
        for kind, profile_id, name, serial, quantity, price in items:
            data.add("item_kind[]", kind)
            data.add("item_profile_id[]", str(profile_id or ""))
            data.add("item_name[]", name)
            data.add("item_serial[]", serial)
            data.add("item_quantity[]", quantity)
            data.add("item_price[]", price)
        for key, value in overrides.items():
            data[key] = value
        return data

    def create(self, items=None, **overrides):
        return self.client.post("/dealer/orders/new", data=self.form(items, **overrides))

    def order(self, order_id=None):
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute(
                "SELECT * FROM dealer_orders WHERE client_name = ? " + ("AND id = ? " if order_id else "")
                + "ORDER BY id DESC LIMIT 1", (CLIENT, order_id) if order_id else (CLIENT,)).fetchone()
            return dict(row) if row else None

    def items(self, order_id):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM dealer_order_items WHERE order_id = ? ORDER BY position", (order_id,)).fetchall()]

    def notice(self):
        with self.client.session_transaction() as session:
            return session.get("dealer_notice") or {}

    # ---- navigation and pages

    def test_the_section_is_in_the_menu_and_the_pages_open(self):
        page = self.client.get("/dealer/orders").get_data(as_text=True)
        self.assertIn("Дилерский центр", page)
        self.assertIn('href="/dealer/orders"', page)
        self.assertEqual(self.client.get("/dealer").status_code, 302)
        for url in ("/dealer/orders/new", "/dealer/prices"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        tuning = self.client.get("/tuning").get_data(as_text=True)
        self.assertIn('href="/dealer/orders"', tuning)

    def test_login_is_required(self):
        anonymous = application_module.app.test_client()
        for url in ("/dealer/orders", "/dealer/orders/new", "/dealer/prices"):
            self.assertEqual(anonymous.get(url).status_code, 302, url)
        self.assertEqual(anonymous.post("/dealer/orders/new", data={}).status_code, 302)

    def test_the_form_offers_the_catalog_with_prices(self):
        page = self.client.get("/dealer/orders/new").get_data(as_text=True)
        raw = re.search(r'<script id="dealerCatalogData" type="application/json">(.*?)</script>', page, re.S).group(1)
        catalog = {p["name"]: p for p in json.loads(raw)}
        self.assertEqual(catalog[BOAT]["kind"], "boat")
        self.assertEqual(catalog[BOAT]["price"], 1500000)
        self.assertEqual(catalog[MOTOR]["kind"], "motor")
        self.assertIsNone(catalog[MOTOR]["price"])

    # ---- creating and editing

    def test_an_order_is_created_with_its_lines_and_totals(self):
        response = self.create(items=[
            ("boat", self.boat_id, BOAT, "HIN-1", "1", "1500000"),
            ("motor", self.motor_id, MOTOR, "SN-77", "2", "250000.50"),
            ("boat", "", "", "", "1", ""),                              # the spare empty row
        ])
        self.assertEqual(response.status_code, 302)
        order = self.order()
        self.assertEqual((order["status"], order["subtotal"], order["total"]), ("new_request", 2000001.0, 2000001.0))
        lines = self.items(order["id"])
        self.assertEqual([(l["kind"], l["profile_id"], l["name"], l["quantity"], l["price"]) for l in lines],
                         [("boat", self.boat_id, BOAT, 1, 1500000), ("motor", self.motor_id, MOTOR, 2, 250000.5)])
        self.assertEqual(lines[1]["serial_number"], "SN-77")
        self.assertEqual(response.headers["Location"], f"/dealer/orders/{order['id']}")

    def test_the_client_goes_to_the_shared_client_base(self):
        self.create()
        order = self.order()
        self.assertIsNotNone(order["client_id"])
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT client_name, phone FROM clients WHERE id = ?", (order["client_id"],)).fetchone()
        self.assertEqual((row["client_name"], row["phone"]), (CLIENT, "+79990001122"))
        self.create()
        self.assertEqual(self.order()["client_id"], order["client_id"])      # found again, not duplicated

    def test_discounts_come_off_the_total(self):
        self.create(discount_type="percent", discount_value="10")
        self.assertEqual((self.order()["subtotal"], self.order()["total"]), (1500000, 1350000))
        self.create(discount_type="amount", discount_value="100000,50")
        self.assertEqual(self.order()["total"], 1399999.5)

    def test_a_free_text_position_and_a_wrong_catalog_kind_are_kept_without_a_link(self):
        self.create(items=[
            ("boat", "", "Лодка не из каталога", "", "1", "100"),
            ("motor", self.boat_id, "Мотор с чужим id", "", "1", "50"),      # id of a boat, kind motor
        ])
        lines = self.items(self.order()["id"])
        self.assertEqual([l["profile_id"] for l in lines], [None, None])

    def test_bad_input_saves_nothing_and_keeps_what_was_typed(self):
        cases = [
            (dict(client_name=" "), "Укажите клиента"),
            (dict(items=[]), "хотя бы одну лодку или мотор"),
            (dict(items=[("boat", "", "Лодка", "", "0", "100")]), "количество"),
            (dict(items=[("boat", "", "Лодка", "", "1.5", "100")]), "количество"),
            (dict(items=[("boat", "", "Лодка", "", "1", "")]), "укажите цену"),
            (dict(items=[("boat", "", "Лодка", "", "1", "-5")]), "укажите цену"),
            (dict(items=[("yacht", "", "Лодка", "", "1", "5")]), "лодка или мотор"),
            (dict(discount_value="150"), "от 0 до 100"),
            (dict(discount_value="abc"), "числом"),
            (dict(discount_type="amount", discount_value="99999999"), "больше суммы заказа"),
            (dict(order_date="не дата"), "дата заказа"),
        ]
        for overrides, expected in cases:
            response = self.create(**overrides)
            self.assertEqual(response.status_code, 400, overrides)
            page = response.get_data(as_text=True)
            self.assertIn(expected, page, overrides)
            self.assertIsNone(self.order(), overrides)
        response = self.create(comment="Привезти до пятницы", discount_value="150")
        self.assertIn("Привезти до пятницы", response.get_data(as_text=True))     # the form is re-shown filled
        page = response.get_data(as_text=True)
        raw = re.search(r'<script id="dealerItemsData" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual([i["name"] for i in json.loads(raw)], [BOAT])

    def test_editing_replaces_the_lines_and_recalculates(self):
        self.create()
        order_id = self.order()["id"]
        response = self.client.post(f"/dealer/orders/{order_id}", data=self.form(
            items=[("motor", self.motor_id, MOTOR, "", "3", "100")], discount_type="amount", discount_value="50",
            comment="Новый комментарий"))
        self.assertEqual(response.status_code, 302)
        order = self.order(order_id)
        self.assertEqual((order["subtotal"], order["total"], order["comment"]), (300, 250, "Новый комментарий"))
        self.assertEqual([l["name"] for l in self.items(order_id)], [MOTOR])
        page = self.client.get(f"/dealer/orders/{order_id}").get_data(as_text=True)
        self.assertIn("Заказ №", page)
        self.assertEqual(self.client.get("/dealer/orders/999999").status_code, 302)

    # ---- status

    def test_the_status_flow_and_completion_date(self):
        self.create()
        order_id = self.order()["id"]
        for status in ("estimate", "approved", "awaiting_delivery", "ready"):
            self.client.post(f"/dealer/orders/{order_id}/status", data={"status": status})
            self.assertEqual(self.order(order_id)["status"], status)
            self.assertIsNone(self.order(order_id)["completed_at"])
        self.client.post(f"/dealer/orders/{order_id}/status", data={"status": "handed_over"})
        self.assertTrue(self.order(order_id)["completed_at"])
        self.client.post(f"/dealer/orders/{order_id}/status", data={"status": "ready"})
        self.assertIsNone(self.order(order_id)["completed_at"])        # taken back
        self.client.post(f"/dealer/orders/{order_id}/status", data={"status": "bogus"})
        self.assertEqual(self.order(order_id)["status"], "ready")
        self.assertEqual(self.notice()["type"], "error")

    # ---- payments

    def pay(self, order_id, **fields):
        data = {"amount": "500000", "payment_type": "CARD", "paid_date": "", "note": ""}
        data.update(fields)
        return self.client.post(f"/dealer/orders/{order_id}/payments", data=data)

    def test_payments_reduce_the_remainder(self):
        self.create()
        order_id = self.order()["id"]
        self.pay(order_id, amount="1 000 000", payment_type="CASH", note="Предоплата")
        self.pay(order_id, amount="100000,5", paid_date="2026-10-11")
        page = self.client.get(f"/dealer/orders/{order_id}").get_data(as_text=True)
        self.assertIn("Предоплата", page)
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT amount, paid_at, payment_type FROM dealer_payments WHERE order_id = ? ORDER BY id",
                (order_id,)).fetchall()
        self.assertEqual([(r["amount"], r["payment_type"]) for r in rows], [(1000000, "CASH"), (100000.5, "CARD")])
        self.assertTrue(rows[1]["paid_at"].startswith("2026-10-11"))
        listing = self.client.get("/dealer/orders").get_data(as_text=True)
        self.assertIn("1\xa0100\xa0000", listing)                        # paid so far, rounded to the ruble

    def test_bad_payments_are_refused(self):
        self.create()
        order_id = self.order()["id"]
        tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        for fields in (dict(amount="0"), dict(amount="-5"), dict(amount="abc"), dict(payment_type=""),
                       dict(payment_type="BITCOIN"), dict(paid_date="не дата"), dict(paid_date=tomorrow)):
            self.pay(order_id, **fields)
            self.assertEqual(self.notice()["type"], "error", fields)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM dealer_payments WHERE order_id = ?", (order_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_a_payment_can_be_deleted(self):
        self.create()
        order_id = self.order()["id"]
        self.pay(order_id)
        with application_module.app.app_context():
            payment_id = application_module.get_db().execute(
                "SELECT id FROM dealer_payments WHERE order_id = ?", (order_id,)).fetchone()["id"]
        self.client.post(f"/dealer/orders/{order_id}/payments/{payment_id}/delete")
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM dealer_payments WHERE order_id = ?", (order_id,)).fetchone()[0], 0)

    # ---- deleting

    def test_an_order_without_payments_can_be_deleted_one_with_payments_cannot(self):
        self.create()
        first = self.order()["id"]
        self.client.post(f"/dealer/orders/{first}/delete")
        self.assertIsNone(self.order(first))
        self.assertEqual(self.items(first), [])
        self.create()
        second = self.order()["id"]
        self.pay(second)
        response = self.client.post(f"/dealer/orders/{second}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.order(second))
        self.assertIn("удалить его нельзя", self.notice()["message"])

    # ---- the list

    def test_the_list_filters_and_leaves_cancelled_orders_out_of_the_totals(self):
        self.create()                                                      # 1 500 000
        self.create(items=[("motor", self.motor_id, MOTOR, "", "1", "300000")])
        live = self.order()["id"]
        self.create(items=[("motor", self.motor_id, MOTOR, "", "1", "70000")])
        cancelled = self.order()["id"]
        self.client.post(f"/dealer/orders/{cancelled}/status", data={"status": "cancelled"})
        page = self.client.get("/dealer/orders").get_data(as_text=True)
        self.assertIn(f"№{cancelled}", page)                               # still listed
        stats = {}
        for label, value in re.findall(r'<span class="k">([^<]+)</span>\s*<span class="v"[^>]*>([^<]*)</span>', page):
            stats.setdefault(label.strip(), value.replace("\xa0", " ").strip())
        self.assertEqual(stats["Заказов"], "2")
        self.assertEqual(stats["Сумма по заказам"], "1 800 000 ₽")
        searched = self.client.get(f"/dealer/orders?q={MOTOR}").get_data(as_text=True)
        self.assertIn(f"№{live}", searched)
        self.assertNotIn("HIN-1", searched)
        by_status = self.client.get("/dealer/orders?status=cancelled").get_data(as_text=True)
        self.assertIn(f"№{cancelled}", by_status)
        self.assertNotIn(f"№{live}", by_status)
        self.assertEqual(self.client.get("/dealer/orders?status=junk").status_code, 200)

    # ---- a tuning order from a dealer order

    def tuning_orders_of(self, dealer_order_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            return [dict(r) for r in db.execute(
                "SELECT * FROM tuning_orders WHERE dealer_order_id = ? ORDER BY id", (dealer_order_id,)).fetchall()]

    def drop_tuning_orders(self, dealer_order_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_orders WHERE dealer_order_id = ?", (dealer_order_id,)).fetchall()]
            for order_id in ids:
                db.execute("DELETE FROM tuning_order_notes WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (order_id,))
            if ids:
                application_module._delete_tuning_order_records(db, ids)
            db.commit()

    def test_a_tuning_order_is_made_from_the_dealer_order_with_the_equipment_and_the_client(self):
        self.create(items=[
            ("boat", self.boat_id, BOAT, "HIN-77", "1", "1500000"),
            ("motor", self.motor_id, MOTOR, "SN-5", "1", "250000"),
        ])
        dealer_id = self.order()["id"]
        self.addCleanup(self.drop_tuning_orders, dealer_id)
        page = self.client.get(f"/dealer/orders/{dealer_id}").get_data(as_text=True)
        self.assertIn("Создать заказ на тюнинг", page)
        response = self.client.post(f"/dealer/orders/{dealer_id}/tuning")
        (tuning,) = self.tuning_orders_of(dealer_id)
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/edit/{tuning['id']}"))
        self.assertEqual((tuning["equipment_type"], tuning["boat_model"], tuning["client_name"], tuning["phone"]),
                         ("boat", BOAT, CLIENT, "+79990001122"))
        self.assertEqual(tuning["status"], "estimate")
        with application_module.app.app_context():
            db = application_module.get_db()
            motors = [(r["motor_model"], r["motor_serial_number"]) for r in db.execute(
                "SELECT * FROM tuning_order_motors WHERE order_id = ?", (tuning["id"],)).fetchall()]
            note = db.execute("SELECT text FROM tuning_order_notes WHERE order_id = ?", (tuning["id"],)).fetchone()[0]
            project = db.execute("SELECT 1 FROM projects WHERE tuning_order_id = ?", (tuning["id"],)).fetchone()
        self.assertEqual(motors, [(MOTOR, "SN-5")])
        self.assertIn("HIN-77", note)
        self.assertIsNotNone(project)
        # the dealer order lists it, the tuning center marks it
        page = self.client.get(f"/dealer/orders/{dealer_id}").get_data(as_text=True)
        self.assertIn(f"№{tuning['id']}", page.split('id="tuning"')[1])
        for url in ("/tuning", f"/tuning/edit/{tuning['id']}"):
            html_page = self.client.get(url).get_data(as_text=True)
            self.assertIn("Куплена у Бодрого Боцмана", html_page, url)

    def test_a_motor_only_order_makes_a_motor_tuning_order(self):
        self.create(items=[("motor", self.motor_id, MOTOR, "SN-9", "1", "250000")])
        dealer_id = self.order()["id"]
        self.addCleanup(self.drop_tuning_orders, dealer_id)
        self.client.post(f"/dealer/orders/{dealer_id}/tuning")
        (tuning,) = self.tuning_orders_of(dealer_id)
        self.assertEqual((tuning["equipment_type"], tuning["motor_model"], tuning["motor_serial_number"]),
                         ("motor", MOTOR, "SN-9"))

    def test_an_ordinary_tuning_order_has_no_such_mark(self):
        page = self.client.get("/tuning").get_data(as_text=True)
        self.assertNotIn("Куплена у Бодрого Боцмана", page)

    def test_a_copy_of_such_an_order_keeps_the_mark(self):
        self.create()
        dealer_id = self.order()["id"]
        self.addCleanup(self.drop_tuning_orders, dealer_id)
        self.client.post(f"/dealer/orders/{dealer_id}/tuning")
        (tuning,) = self.tuning_orders_of(dealer_id)
        self.client.post(f"/tuning/{tuning['id']}/copy")
        self.assertEqual(len(self.tuning_orders_of(dealer_id)), 2)

    def test_the_button_is_missing_without_equipment_and_a_forced_request_creates_nothing(self):
        self.create()
        dealer_id = self.order()["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (dealer_id,))
            db.commit()
        page = self.client.get(f"/dealer/orders/{dealer_id}").get_data(as_text=True)
        self.assertNotIn("Создать заказ на тюнинг", page)
        self.client.post(f"/dealer/orders/{dealer_id}/tuning")
        self.assertEqual(self.tuning_orders_of(dealer_id), [])
        self.assertEqual(self.notice()["type"], "error")

    # ---- the price list

    def put_on_price_list(self, *profile_ids):
        with application_module.app.app_context():
            db = application_module.get_db()
            for profile_id in profile_ids:
                db.execute("INSERT OR IGNORE INTO dealer_price_list (profile_id, added_at) VALUES (?, '2026-10-01 10:00')",
                           (profile_id,))
            db.commit()

    def price_rows(self):
        """Names standing in the price list table (not in the catalog picker)."""
        page = self.client.get("/dealer/prices").get_data(as_text=True)
        table = page.split("<tbody>")[1].split("</tbody>")[0] if "<tbody>" in page else ""
        return table

    def test_the_price_list_shows_only_models_in_orders_and_added_by_hand(self):
        self.assertNotIn(BOAT, self.price_rows())      # in the catalog, but nowhere used
        self.create()                                   # an order with the boat
        self.assertIn(BOAT, self.price_rows())
        self.assertNotIn(MOTOR, self.price_rows())
        self.put_on_price_list(self.motor_id)
        self.assertIn(MOTOR, self.price_rows())

    def test_a_cancelled_orders_model_does_not_keep_a_place(self):
        self.create()
        self.assertIn(BOAT, self.price_rows())
        self.client.post(f"/dealer/orders/{self.order()['id']}/status", data={"status": "cancelled"})
        self.assertNotIn(BOAT, self.price_rows())

    def test_a_model_is_added_from_the_catalog_with_its_price(self):
        response = self.client.post("/dealer/prices/add", data={
            "mode": "catalog", "profile_id": str(self.motor_id), "price": "250 000,5"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.notice()["type"], "success")
        self.assertIn(MOTOR, self.price_rows())
        with application_module.app.app_context():
            price = application_module.get_db().execute(
                "SELECT price FROM dealer_prices WHERE profile_id = ?", (self.motor_id,)).fetchone()[0]
        self.assertEqual(price, 250000.5)
        # taken from the picker once it is in the list
        picker = self.client.get("/dealer/prices").get_data(as_text=True).split('id="price_catalog_search"')[1]
        self.assertNotIn(f'data-value="{self.motor_id}"', picker)
        self.assertIn(f'data-value="{self.boat_id}"', picker)

    def test_a_new_model_is_created_in_the_common_catalog_and_listed(self):
        name = "Дилерская новинка Тест"
        try:
            self.client.post("/dealer/prices/add", data={
                "mode": "new", "kind": "boat", "model_name": f"  {name} ", "brand": "Тестмарка", "price": "990000"})
            self.assertEqual(self.notice()["type"], "success")
            with application_module.app.app_context():
                db = application_module.get_db()
                profile = db.execute("SELECT * FROM tuning_boat_profiles WHERE model_name = ?", (name,)).fetchone()
                self.assertIsNotNone(profile)
                self.assertEqual((profile["equipment_type"], profile["brand"]), ("boat", "Тестмарка"))
                self.assertEqual(db.execute("SELECT price FROM dealer_prices WHERE profile_id = ?", (profile["id"],)).fetchone()[0], 990000)
            self.assertIn(name, self.price_rows())
            # the same name again takes the catalog entry instead of a second one
            self.client.post("/dealer/prices/add", data={"mode": "new", "kind": "boat", "model_name": name, "price": "1000000"})
            with application_module.app.app_context():
                count = application_module.get_db().execute(
                    "SELECT COUNT(*) FROM tuning_boat_profiles WHERE model_name = ?", (name,)).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                for row in db.execute("SELECT id FROM tuning_boat_profiles WHERE model_name = ?", (name,)).fetchall():
                    db.execute("DELETE FROM dealer_prices WHERE profile_id = ?", (row["id"],))
                    db.execute("DELETE FROM dealer_price_list WHERE profile_id = ?", (row["id"],))
                    db.execute("DELETE FROM tuning_boat_profiles WHERE id = ?", (row["id"],))
                db.commit()

    def test_adding_to_the_price_list_is_validated(self):
        for data in (
            {"mode": "catalog", "profile_id": str(self.boat_id), "price": ""},
            {"mode": "catalog", "profile_id": str(self.boat_id), "price": "-5"},
            {"mode": "catalog", "profile_id": "999999", "price": "10"},
            {"mode": "catalog", "profile_id": "", "price": "10"},
            {"mode": "new", "kind": "plane", "model_name": "X", "price": "10"},
            {"mode": "new", "kind": "boat", "model_name": "  ", "price": "10"},
            {"mode": "new", "kind": "boat", "model_name": "Я" * 300, "price": "10"},
            {"mode": "", "price": "10"},
        ):
            self.client.post("/dealer/prices/add", data=data)
            self.assertEqual(self.notice()["type"], "error", data)
        self.assertNotIn(BOAT, self.price_rows())

    def test_a_hand_added_model_can_be_removed_but_one_in_orders_cannot(self):
        self.put_on_price_list(self.boat_id, self.motor_id)
        self.create()    # the boat stands in an order now
        self.client.post(f"/dealer/prices/{self.motor_id}/remove")
        self.assertEqual(self.notice()["type"], "success")
        self.assertNotIn(MOTOR, self.price_rows())
        self.client.post(f"/dealer/prices/{self.boat_id}/remove")
        self.assertEqual(self.notice()["type"], "error")
        self.assertIn(BOAT, self.price_rows())

    def test_prices_are_saved_cleared_and_validated(self):
        self.put_on_price_list(self.boat_id, self.motor_id)

        def save(boat, motor):
            return self.client.post("/dealer/prices", data={
                "profile_id[]": [str(self.boat_id), str(self.motor_id)], "price[]": [boat, motor]})
        save("1 600 000", "250000,5")
        with application_module.app.app_context():
            db = application_module.get_db()
            prices = {r["profile_id"]: r["price"] for r in db.execute("SELECT * FROM dealer_prices").fetchall()}
        self.assertEqual((prices[self.boat_id], prices[self.motor_id]), (1600000, 250000.5))
        save("", "250000")
        with application_module.app.app_context():
            prices = {r["profile_id"]: r["price"] for r in application_module.get_db().execute(
                "SELECT * FROM dealer_prices").fetchall()}
        self.assertNotIn(self.boat_id, prices)
        save("abc", "1")
        self.assertEqual(self.notice()["type"], "error")
        page = self.client.get("/dealer/prices").get_data(as_text=True)
        self.assertIn(BOAT, self.price_rows())
        self.assertIn('value="250000"', page)


if __name__ == "__main__":
    unittest.main()
