import unittest

from support import application_module


class _GoodsFixture:
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

class TuningGoodsPriceTests(_GoodsFixture, unittest.TestCase):
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


class TuningGoodsDiscountTests(_GoodsFixture, unittest.TestCase):
    """A discount on one goods line: a percent, or rubles off one unit. The
    line's unit_price is what the client pays, so every sum stays right."""

    def discount(self, order_id, line_id, value, kind="percent"):
        return self.client.post(
            f"/tuning/{order_id}/products/{line_id}/discount",
            data={"discount_value": value, "discount_type": kind})

    def line(self, order_id=None):
        return self.lines(order_id or self.order_id)[0][0]

    def test_a_percent_discount_is_set_on_adding(self):
        self.add(self.order_id, discount_value="10", discount_type="percent")
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["list_price"], lines[0]["unit_price"], lines[0]["discount_value"]), (500, 450, 10))
        self.assertEqual(total, 900)

    def test_an_amount_discount_is_taken_off_each_unit(self):
        self.add(self.order_id, discount_value="50", discount_type="amount")
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["list_price"], lines[0]["unit_price"]), (500, 450))
        self.assertEqual(total, 900)

    def test_a_discount_is_set_changed_and_cleared_on_an_existing_line(self):
        self.add(self.order_id)
        line = self.line()
        self.discount(self.order_id, line["id"], "20")
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["unit_price"], lines[0]["list_price"], total), (400, 500, 800))
        self.discount(self.order_id, line["id"], "100", "amount")      # replaces, from the original price
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["unit_price"], lines[0]["list_price"], lines[0]["discount_type"], total),
                         (400, 500, "amount", 800))
        self.discount(self.order_id, line["id"], "")                    # cleared
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["unit_price"], lines[0]["list_price"], total), (500, None, 1000))

    def test_editing_the_price_keeps_the_discount(self):
        self.add(self.order_id, discount_value="10")
        line = self.line()
        self.client.post(f"/tuning/{self.order_id}/products/{line['id']}/price", data={"unit_price": "1000"})
        lines, total = self.lines(self.order_id)
        self.assertEqual((lines[0]["list_price"], lines[0]["unit_price"], total), (1000, 900, 1800))
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn('name="unit_price" value="1000.0"', page)     # the field shows the price before the discount

    def test_the_order_discount_applies_on_top_of_the_line_discount(self):
        self.add(self.order_id, discount_value="10")
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_orders SET discount_type = 'percent', discount_value = 10 WHERE id = ?", (self.order_id,))
            db.commit()
            application_module._recompute_order_totals(db, self.order_id)
        self.assertEqual(self.lines(self.order_id)[1], 810)           # 900 less 10%

    def test_bad_discounts_are_refused_and_change_nothing(self):
        self.add(self.order_id)
        line = self.line()
        for value, kind in (("abc", "percent"), ("-5", "percent"), ("101", "percent"), ("501", "amount")):
            self.discount(self.order_id, line["id"], value, kind)
            self.assertIn("Скидка", self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True))
            lines, total = self.lines(self.order_id)
            self.assertEqual((lines[0]["unit_price"], lines[0]["list_price"], total), (500, None, 1000), value)
        self.add(self.order_id, discount_value="200", discount_type="percent")
        self.assertEqual(len(self.lines(self.order_id)[0]), 1)

    def test_a_discount_cannot_be_set_on_another_orders_line(self):
        self.add(self.order_id)
        self.add(self.other_order_id)
        line = self.line(self.other_order_id)
        self.discount(self.order_id, line["id"], "50")
        self.assertEqual(self.line(self.other_order_id)["unit_price"], 500)

    def test_the_catalog_price_is_untouched_and_a_copy_keeps_the_discount(self):
        self.add(self.order_id, discount_value="10")
        self.assertEqual(self.catalog_price(), 500)
        self.client.post(f"/tuning/{self.order_id}/copy")
        with application_module.app.app_context():
            db = application_module.get_db()
            copy = db.execute(
                "SELECT p.* FROM tuning_order_products p JOIN tuning_orders o ON o.id = p.order_id "
                "WHERE o.id != ? AND o.id != ? AND p.product_id = ?",
                (self.order_id, self.other_order_id, self.product_id)).fetchone()
            copy_order = copy["order_id"] if copy else None
        try:
            self.assertEqual((copy["list_price"], copy["unit_price"]), (500, 450))
        finally:
            if copy_order:
                with application_module.app.app_context():
                    db = application_module.get_db()
                    db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (copy_order,))
                    application_module._delete_tuning_order_records(db, [copy_order])
                    db.commit()

    def test_the_order_page_shows_the_discount_form_and_the_old_price(self):
        self.add(self.order_id, discount_value="10")
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn('name="discount_value"', page)
        self.assertIn("₽ за ед.", page)
        self.assertIn("work-price-was", page)


class TuningGoodsCreateTests(_GoodsFixture, unittest.TestCase):
    """The modal that creates a product inside an order has a box deciding
    whether the product also goes into the catalog."""

    def create(self, **extra):
        data = {"name": f"{self.MARK} новый", "cost_price": "100", "cost_unit": "piece",
                "sale_price": "300", "quantity": "2", **extra}
        return self.client.post(f"/tuning/{self.order_id}/products/create", data=data)

    def catalog_count(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT COUNT(*) FROM supply_products WHERE name = ?", (f"{self.MARK} новый",)
            ).fetchone()[0]

    def test_with_the_box_the_product_goes_into_the_catalog_too(self):
        self.create(catalog_shown="1", to_catalog="1")
        self.assertEqual(self.catalog_count(), 1)
        lines, total = self.lines(self.order_id)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["product_id"])
        self.assertEqual(total, 600)

    def test_without_the_box_it_lives_in_the_order_only(self):
        self.create(catalog_shown="1")
        self.assertEqual(self.catalog_count(), 0)
        lines, total = self.lines(self.order_id)
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["product_id"], lines[0]["unit_price"], lines[0]["quantity"]), (0, 300, 2))
        self.assertEqual(total, 600)

    def test_a_form_without_the_field_keeps_the_old_behaviour(self):
        self.create()
        self.assertEqual(self.catalog_count(), 1)

    def test_a_product_outside_the_catalog_has_no_warehouse_and_no_writeoff(self):
        self.create(catalog_shown="1")
        line_id = self.lines(self.order_id)[0][0]["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, created_at) VALUES (?, '2026-09-01 10:00')",
                (f"{self.MARK} склад",),
            ).lastrowid
            db.commit()

        def drop_warehouse():
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM supply_warehouses WHERE id = ?", (warehouse_id,))
                db.commit()
        self.addCleanup(drop_warehouse)
        self.client.post(f"/tuning/{self.order_id}/products/{line_id}/warehouse", data={"warehouse_id": str(warehouse_id)})
        self.assertIsNone(self.lines(self.order_id)[0][0]["warehouse_id"])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Только в этом заказе, в каталоге склада нет", page)
        self.assertIn('id="create-product-to-catalog"', page)
        # the order still goes "В работе" without trying to write anything off
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_products SET warehouse_id = ? WHERE id = ?", (warehouse_id, line_id))
            db.commit()
        self.client.post(f"/tuning/{self.order_id}/status", data={"status": "in_progress", "next": "/tuning"})
        with application_module.app.app_context():
            written = application_module.get_db().execute(
                "SELECT COUNT(*) FROM supply_writeoffs WHERE tuning_order_product_id = ?", (line_id,)
            ).fetchone()[0]
        self.assertEqual(written, 0)

    def test_the_priced_line_still_takes_a_discount(self):
        self.create(catalog_shown="1")
        line_id = self.lines(self.order_id)[0][0]["id"]
        self.client.post(f"/tuning/{self.order_id}/products/{line_id}/discount",
                         data={"discount_value": "10", "discount_type": "percent"})
        line = self.lines(self.order_id)[0][0]
        self.assertEqual((line["list_price"], line["unit_price"]), (300, 270))


if __name__ == "__main__":
    unittest.main()
