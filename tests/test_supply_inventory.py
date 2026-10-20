import io
import json
import os
import re
import unittest

from support import application_module


class SupplyInventoryTests(unittest.TestCase):
    """Снабжение -> Инвентаризация: one card per stock position; swiping
    right saves the (editable) fields, swiping left just skips."""

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
            self.warehouse = db.execute(
                "INSERT INTO supply_warehouses (name, address, created_at) "
                "VALUES ('Склад инвентаризации', 'Причал 3', '2026-10-01 10:00')").lastrowid
            self.other_warehouse = db.execute(
                "INSERT INTO supply_warehouses (name, address, created_at) "
                "VALUES ('Склад инвентаризации 2', '', '2026-10-01 10:00')").lastrowid
            self.products = {}
            for key, name, sku in (("a", "Анод цинковый", "AN-1"), ("b", "Болт М8", "BL-8"), ("c", "Свеча", None)):
                self.products[key] = db.execute(
                    "INSERT INTO supply_products (name, sku, cost_price, cost_unit, sale_price, created_at, min_stock) "
                    "VALUES (?, ?, 100, 'piece', 150, '2026-10-01 10:00', NULL)", (name, sku)).lastrowid
            self.stock = {}
            for key, qty, zone, rack, spot, checked in (
                ("a", 10, "Б", "2", "1", None),
                ("b", 4, "А", "1", "3", None),
                ("c", 7, "А", "1", "1", "2026-10-10 09:00"),
            ):
                self.stock[key] = db.execute(
                    "INSERT INTO supply_stock (product_id, warehouse_id, quantity, zone, rack, spot, inventoried_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.products[key], self.warehouse, qty, zone, rack, spot, checked)).lastrowid
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM supply_warehouses WHERE name LIKE 'Склад инвентаризации%'").fetchall()]
        for warehouse_id in ids:
            db.execute("DELETE FROM supply_inventory_log WHERE warehouse_id = ?", (warehouse_id,))
            db.execute("DELETE FROM supply_stock WHERE warehouse_id = ?", (warehouse_id,))
            db.execute("DELETE FROM supply_warehouses WHERE id = ?", (warehouse_id,))
        db.execute("DELETE FROM supply_products WHERE name IN ('Анод цинковый', 'Болт М8', 'Свеча', 'Анод новый')")
        db.commit()

    def cards(self):
        page = self.client.get(f"/supply/inventory/{self.warehouse}").get_data(as_text=True)
        raw = re.search(r'<script id="invData" type="application/json">(.*?)</script>', page, re.S).group(1)
        return json.loads(raw), page

    def save(self, key="a", **fields):
        data = {"stock_id": str(self.stock[key]), "name": "Анод цинковый", "sku": "AN-1",
                "quantity": "10", "zone": "Б", "rack": "2", "spot": "1", "expected": "10"}
        data.update(fields)
        return self.client.post(f"/supply/inventory/{self.warehouse}/save", data=data)

    def stock_row(self, key):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM supply_stock WHERE id = ?", (self.stock[key],)).fetchone())

    def product_row(self, key):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM supply_products WHERE id = ?", (self.products[key],)).fetchone())

    # ---- the list of cards

    def test_the_picker_lists_warehouses_with_progress(self):
        page = self.client.get("/supply/inventory").get_data(as_text=True)
        self.assertIn("Склад инвентаризации", page)
        self.assertIn("Причал 3", page)
        self.assertIn(f"/supply/inventory/{self.warehouse}", page)
        self.assertIn("Не проверено", page)

    def test_the_inventory_tab_is_in_the_supply_navigation(self):
        for url in ("/supply/warehouses", "/supply/catalog", "/supply/lockers", "/supply/requests"):
            page = self.client.get(url).get_data(as_text=True)
            self.assertIn('href="/supply/inventory"', page, url)

    def test_cards_come_unchecked_first_and_along_the_shelves(self):
        cards, page = self.cards()
        self.assertEqual([c["name"] for c in cards], ["Болт М8", "Анод цинковый", "Свеча"])
        first = cards[0]
        self.assertEqual((first["sku"], first["quantity"], first["zone"], first["rack"], first["spot"]),
                         ("BL-8", 4.0, "А", "1", "3"))
        self.assertIn("Свайп влево — пропустить, вправо — сохранить", page)
        self.assertIn('data-field="quantity"', page)
        self.assertIn("Адрес на складе", page)

    def test_zero_stock_and_other_warehouses_are_not_cards(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE supply_stock SET quantity = 0 WHERE id = ?", (self.stock["b"],))
            db.execute("INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, 5)",
                       (self.products["a"], self.other_warehouse))
            db.commit()
        cards, _ = self.cards()
        self.assertEqual(sorted(c["name"] for c in cards), ["Анод цинковый", "Свеча"])

    def test_an_unknown_warehouse_goes_back_to_the_picker(self):
        response = self.client.get("/supply/inventory/999999")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/supply/inventory"))

    # ---- swipe right = save

    def test_saving_updates_the_name_article_quantity_and_address(self):
        response = self.save(name="  Анод   новый ", sku="AN-2", quantity="7,5", zone="В", rack="9", spot="")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(response.get_json()["card"]["quantity"], 7.5)
        product = self.product_row("a")
        self.assertEqual((product["name"], product["sku"]), ("Анод новый", "AN-2"))
        stock = self.stock_row("a")
        self.assertEqual((stock["quantity"], stock["zone"], stock["rack"], stock["spot"]), (7.5, "В", "9", None))
        self.assertTrue(stock["inventoried_at"])

    def test_saving_leaves_a_journal_row_with_expected_and_counted(self):
        self.save(quantity="8", zone="В", expected="10")
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM supply_inventory_log WHERE warehouse_id = ?", (self.warehouse,)).fetchone()
        self.assertEqual((row["expected_quantity"], row["counted_quantity"]), (10, 8))
        self.assertEqual((row["previous_address"], row["new_address"]), ("Б · 2 · 1", "В · 2 · 1"))
        self.assertEqual(row["admin_name"], "Администратор")

    def test_a_checked_card_goes_to_the_back_of_the_next_round(self):
        self.save(key="b", name="Болт М8", sku="BL-8", quantity="4", zone="А", rack="1", spot="3")
        cards, _ = self.cards()
        self.assertEqual([c["name"] for c in cards], ["Анод цинковый", "Свеча", "Болт М8"])

    def test_a_new_photo_replaces_the_old_one(self):
        data = {"stock_id": str(self.stock["a"]), "name": "Анод цинковый", "sku": "AN-1", "quantity": "10",
                "zone": "Б", "rack": "2", "spot": "1", "photo": (io.BytesIO(b"\x89PNG fake"), "p.png")}
        response = self.client.post(f"/supply/inventory/{self.warehouse}/save", data=data,
                                    content_type="multipart/form-data")
        self.assertTrue(response.get_json()["ok"])
        photo = self.product_row("a")["photo_filename"]
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        path = os.path.join(application_module.app.static_folder, "supply_photos", photo or "none")
        self.assertTrue(photo and photo.endswith(".png"))
        self.assertIn("supply_photos/" + photo, response.get_json()["card"]["photo"])

    def test_a_non_image_photo_is_refused(self):
        data = {"stock_id": str(self.stock["a"]), "name": "Анод цинковый", "sku": "AN-1", "quantity": "3",
                "photo": (io.BytesIO(b"x"), "evil.exe")}
        response = self.client.post(f"/supply/inventory/{self.warehouse}/save", data=data,
                                    content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stock_row("a")["quantity"], 10)

    def test_bad_values_are_refused_and_nothing_changes(self):
        for overrides in ({"name": "  "}, {"quantity": "много"}, {"quantity": "-1"}, {"quantity": "nan"},
                          {"quantity": ""}, {"name": "x" * 300}):
            response = self.save(**overrides)
            self.assertEqual(response.status_code, 400, overrides)
            self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.stock_row("a")["quantity"], 10)
        self.assertEqual(self.product_row("a")["name"], "Анод цинковый")
        self.assertIsNone(self.stock_row("a")["inventoried_at"])
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM supply_inventory_log").fetchone()[0], 0)

    def test_a_position_of_another_warehouse_cannot_be_saved(self):
        response = self.client.post(f"/supply/inventory/{self.other_warehouse}/save", data={
            "stock_id": str(self.stock["a"]), "name": "Взлом", "quantity": "1"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.product_row("a")["name"], "Анод цинковый")

    def test_counting_zero_takes_the_position_out_of_the_next_round(self):
        self.save(quantity="0")
        cards, _ = self.cards()
        self.assertNotIn("Анод цинковый", [c["name"] for c in cards])

    def test_a_shortage_can_raise_the_low_stock_request(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE supply_products SET min_stock = 8 WHERE id = ?", (self.products["a"],))
            db.commit()
            before = db.execute("SELECT COUNT(*) FROM supply_requests").fetchone()[0]
        self.save(quantity="5")
        with application_module.app.app_context():
            after = application_module.get_db().execute("SELECT COUNT(*) FROM supply_requests").fetchone()[0]
        self.assertEqual(after, before + 1)

    # ---- swipe left = skip: nothing is sent, so nothing changes

    def test_skipping_changes_nothing_on_the_server(self):
        self.cards()
        self.assertEqual(self.stock_row("a")["quantity"], 10)
        self.assertIsNone(self.stock_row("a")["inventoried_at"])

    def test_login_is_required(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.get("/supply/inventory").status_code, 302)
        self.assertEqual(anonymous.post(f"/supply/inventory/{self.warehouse}/save", data={}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
