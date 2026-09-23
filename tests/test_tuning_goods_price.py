import unittest

from support import application_module


class TuningGoodsPriceTests(unittest.TestCase):
    MARK = "goods-price-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            self.product_id = db.execute(
                "INSERT INTO supply_products (name, cost_price, cost_unit, sale_price, created_at) "
                "VALUES (?, 100, 'piece', 500, '2026-09-01 10:00')",
                (f"{self.MARK} товар",),
            ).lastrowid
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, "
                "phone, subtotal, total, status, order_date, created_at, updated_at, source, source_ref) "
                "VALUES ('x', 'boat', 'Тест', 'direct', '', 0, 0, 'estimate', '2026-09-10', "
                "'2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
                (self.MARK,),
            ).lastrowid
            self.other_order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, "
                "phone, subtotal, total, status, order_date, created_at, updated_at, source, source_ref) "
                "VALUES ('y', 'boat', 'Тест', 'direct', '', 0, 0, 'estimate', '2026-09-10', "
                "'2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
                (self.MARK + "-2",),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        for order_id in ids:
            db.execute("DELETE FROM supply_writeoffs WHERE tuning_order_product_id IN "
                       "(SELECT id FROM tuning_order_products WHERE order_id = ?)", (order_id,))
            db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.execute("DELETE FROM supply_stock WHERE product_id IN (SELECT id FROM supply_products WHERE name LIKE ?)", (f"{self.MARK}%",))
        db.execute("DELETE FROM supply_products WHERE name LIKE ?", (f"{self.MARK}%",))
        db.commit()

    def add(self, order_id, **extra):
        data = {"product_id": str(self.product_id), "quantity": "2", **extra}
        return self.client.post(f"/tuning/{order_id}/products/add", data=data)

    def lines(self, order_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            return [dict(r) for r in db.execute(
                "SELECT * FROM tuning_order_products WHERE order_id = ?", (order_id,)
            ).fetchall()], db.execute(
                "SELECT total FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()["total"]

    def catalog_price(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT sale_price FROM supply_products WHERE id = ?", (self.product_id,)
            ).fetchone()["sale_price"]

    def test_default_price_comes_from_catalog(self):
        self.add(self.order_id)
        lines, total = self.lines(self.order_id)
        self.assertEqual(lines[0]["unit_price"], 500)
        self.assertEqual(total, 1000)

    def test_price_can_be_overridden_when_adding(self):
        self.add(self.order_id, unit_price="350,5")
        lines, total = self.lines(self.order_id)
        self.assertEqual(lines[0]["unit_price"], 350.5)
        self.assertEqual(total, 701)
        self.assertEqual(self.catalog_price(), 500)

    def test_price_can_be_edited_in_order_only(self):
        self.add(self.order_id)
        self.add(self.other_order_id)
        line = self.lines(self.order_id)[0][0]
        response = self.client.post(
            f"/tuning/{self.order_id}/products/{line['id']}/price", data={"unit_price": "420"}
        )
        self.assertEqual(response.status_code, 302)
        lines, total = self.lines(self.order_id)
        self.assertEqual(lines[0]["unit_price"], 420)
        self.assertEqual(total, 840)
        # other order and the catalog keep the catalog price
        other_lines, other_total = self.lines(self.other_order_id)
        self.assertEqual(other_lines[0]["unit_price"], 500)
        self.assertEqual(other_total, 1000)
        self.assertEqual(self.catalog_price(), 500)

    def test_invalid_prices_are_rejected(self):
        self.add(self.order_id)
        line = self.lines(self.order_id)[0][0]
        for bad in ("abc", "-5", ""):
            self.client.post(
                f"/tuning/{self.order_id}/products/{line['id']}/price", data={"unit_price": bad}
            )
        self.assertEqual(self.lines(self.order_id)[0][0]["unit_price"], 500)
        self.add(self.order_id, unit_price="-1")
        self.assertEqual(len(self.lines(self.order_id)[0]), 1)

    def test_price_of_foreign_order_line_is_not_editable(self):
        self.add(self.other_order_id)
        line = self.lines(self.other_order_id)[0][0]
        self.client.post(
            f"/tuning/{self.order_id}/products/{line['id']}/price", data={"unit_price": "1"}
        )
        self.assertEqual(self.lines(self.other_order_id)[0][0]["unit_price"], 500)

    def test_order_page_renders_editable_price(self):
        self.add(self.order_id)
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn('name="unit_price"', page)
        self.assertIn("goods-price-input", page)


if __name__ == "__main__":
    unittest.main()
