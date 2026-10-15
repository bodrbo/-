import unittest

from support import application_module


class ClientProductCardTests(unittest.TestCase):
    """A product of the order opens its catalog card in the client's cabinet:
    photo, description, article — and nothing internal."""

    TOKEN = "product-card-client-token"
    OTHER_TOKEN = "product-card-other-token"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.client_id = self._client(db, "Клиент Карточек", self.TOKEN)
            self.other_id = self._client(db, "Чужой Клиент", self.OTHER_TOKEN)
            category_id = db.execute(
                "INSERT INTO supply_categories (name, created_at) VALUES ('Тест-категория', 'x')").lastrowid
            self.product_id = db.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, cost_price, "
                "cost_unit, sale_price, created_at, category_id) VALUES "
                "('Тест-гребной винт', 'TP-100', 'Алюминиевый винт.\nШаг 13.', 'Секретный Поставщик', "
                "'prop.jpg', 1234, 'piece', 2500, 'x', ?)", (category_id,)).lastrowid
            self.plain_product_id = db.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, cost_price, "
                "cost_unit, sale_price, created_at, external_photo_url) VALUES "
                "('Тест-кронштейн', '', '', '', '', 10, 'piece', 20, 'x', 'https://example.test/bracket.jpg')"
            ).lastrowid
            self.unused_product_id = db.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, cost_price, "
                "cost_unit, sale_price, created_at) VALUES ('Тест-чужой товар', '', '', '', '', 1, 'piece', 2, 'x')"
            ).lastrowid
            db.execute(
                "INSERT INTO supply_product_motor_compatibility (product_id, motor_brand, power_min, power_max, "
                "created_at) VALUES (?, 'Yamaha', 15, 40, 'x')", (self.product_id,))
            db.execute(
                "INSERT INTO supply_product_motor_compatibility (product_id, motor_brand, power_min, power_max, "
                "created_at) VALUES (?, 'Suzuki', NULL, NULL, 'x')", (self.product_id,))
            order_id = self._order(db, self.client_id, "Клиент Карточек")
            for product_id, name in ((self.product_id, "Тест-гребной винт"), (self.plain_product_id, "Тест-кронштейн")):
                db.execute(
                    "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, unit_price, "
                    "cost_price, unit, created_at) VALUES (?, ?, ?, 1, 2500, 1234, 'piece', 'x')",
                    (order_id, product_id, name))
            other_order = self._order(db, self.other_id, "Чужой Клиент")
            db.execute(
                "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, unit_price, "
                "cost_price, unit, created_at) VALUES (?, ?, 'Тест-чужой товар', 1, 2, 1, 'piece', 'x')",
                (other_order, self.unused_product_id))
            db.commit()
        self.addCleanup(self._clear_now)

    def _client(self, db, name, token):
        client_id = db.execute(
            "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
            "VALUES (?, 'Salute', '', ?, 'neutral', '2026-09-01 10:00')", (name, token)).lastrowid
        db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                   "VALUES (?, 'tuning', 'client', 'x')", (client_id,))
        return client_id

    def _order(self, db, client_id, name):
        return db.execute(
            "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, sale_channel, phone, "
            "subtotal, total, status, order_date, created_at, updated_at, source) VALUES "
            "(?, ?, 'boat', 'Salute', 'direct', '', 2500, 2500, 'in_progress', '2026-09-10', 'x', 'x', 'manual')",
            (client_id, name)).lastrowid

    def _clear_now(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    def _clear(self, db):
        for token in (self.TOKEN, self.OTHER_TOKEN):
            row = db.execute("SELECT id FROM clients WHERE token = ?", (token,)).fetchone()
            if not row:
                continue
            for order in db.execute("SELECT id FROM tuning_orders WHERE client_id = ?", (row["id"],)).fetchall():
                db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order["id"],))
            db.execute("DELETE FROM tuning_orders WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        for name in ("Тест-гребной винт", "Тест-кронштейн", "Тест-чужой товар"):
            ids = [r["id"] for r in db.execute("SELECT id FROM supply_products WHERE name = ?", (name,)).fetchall()]
            for product_id in ids:
                db.execute("DELETE FROM supply_product_motor_compatibility WHERE product_id = ?", (product_id,))
                db.execute("DELETE FROM supply_products WHERE id = ?", (product_id,))
        db.execute("DELETE FROM supply_categories WHERE name = 'Тест-категория'")
        db.commit()

    def card(self, product_id, token=None):
        return self.http.get(f"/client/{token or self.TOKEN}/products/{product_id}")

    def test_the_card_has_photo_description_article_category_and_fit(self):
        response = self.card(self.product_id)
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["name"], "Тест-гребной винт")
        self.assertEqual(data["sku"], "TP-100")
        self.assertEqual(data["description"], "Алюминиевый винт.\nШаг 13.")
        self.assertTrue(data["photo_url"].endswith("/static/supply_photos/prop.jpg"))
        self.assertEqual(data["category"], "Тест-категория")
        self.assertEqual(data["compatibility"], ["Suzuki", "Yamaha 15–40 л.с."])

    def test_nothing_internal_leaves_the_card(self):
        text = self.card(self.product_id).get_data(as_text=True)
        self.assertNotIn("Секретный Поставщик", text)
        for key in ("cost_price", "supplier", "sale_price", "stock", "1234"):
            self.assertNotIn(key, text, key)

    def test_a_product_with_only_an_external_photo_uses_it(self):
        data = self.card(self.plain_product_id).get_json()
        self.assertEqual(data["photo_url"], "https://example.test/bracket.jpg")
        self.assertEqual((data["sku"], data["description"], data["compatibility"]), ("", "", []))

    def test_a_product_that_is_not_in_the_clients_orders_is_not_shown(self):
        self.assertEqual(self.card(self.unused_product_id).status_code, 404)  # in another client's order
        self.assertEqual(self.card(self.product_id, token=self.OTHER_TOKEN).status_code, 404)
        self.assertEqual(self.card(self.product_id, token="no-such-token").status_code, 404)
        self.assertEqual(self.card(99999999).status_code, 404)

    def test_the_cabinet_makes_the_goods_open_their_cards(self):
        page = self.http.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn(f'data-product-url="/client/{self.TOKEN}/products/{self.product_id}"', page)
        self.assertIn('onclick="openProductCard(this)"', page)
        self.assertIn('id="product-modal"', page)
        self.assertIn("function openProductCard", page)

    def test_a_deleted_product_stays_a_plain_name_in_the_cabinet(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_products SET product_id = 0 WHERE product_id = ?", (self.plain_product_id,))
            db.commit()
        page = self.http.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertNotIn(f"/products/{self.plain_product_id}", page)
        self.assertIn("Тест-кронштейн", page)


if __name__ == "__main__":
    unittest.main()
