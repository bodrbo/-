import unittest

from support import application_module


class TuningGoodsWriteoffTests(unittest.TestCase):
    MARK = "goods-writeoff-test"

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
            self.warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, created_at) VALUES (?, '2026-09-01 10:00')",
                (f"{self.MARK} склад",),
            ).lastrowid
            self.other_warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, created_at) VALUES (?, '2026-09-01 10:00')",
                (f"{self.MARK} склад 2",),
            ).lastrowid
            self.product_id = db.execute(
                "INSERT INTO supply_products (name, cost_price, cost_unit, sale_price, created_at) "
                "VALUES (?, 100, 'piece', 500, '2026-09-01 10:00')", (f"{self.MARK} товар",),
            ).lastrowid
            db.execute(
                "INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, 10)",
                (self.product_id, self.warehouse_id),
            )
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, "
                "phone, subtotal, total, status, order_date, created_at, updated_at, source, source_ref) "
                "VALUES ('x', 'boat', 'Тест', 'direct', '', 0, 0, 'estimate', '2026-09-10', "
                "'2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)", (self.MARK,),
            ).lastrowid
            db.execute(
                "INSERT INTO projects (name, tuning_order_id, created_at) VALUES ('p', ?, "
                "'2026-09-10 10:00')", (self.order_id,),
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        for row in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (self.MARK,)
        ).fetchall():
            db.execute(
                "DELETE FROM supply_writeoffs WHERE tuning_order_product_id IN "
                "(SELECT id FROM tuning_order_products WHERE order_id = ?)", (row["id"],)
            )
            db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (row["id"],))
            application_module._delete_tuning_order_records(db, [row["id"]])
        db.execute("DELETE FROM supply_stock WHERE product_id IN "
                   "(SELECT id FROM supply_products WHERE name LIKE ?)", (f"{self.MARK}%",))
        db.execute("DELETE FROM supply_products WHERE name LIKE ?", (f"{self.MARK}%",))
        db.execute("DELETE FROM supply_warehouses WHERE name LIKE ?", (f"{self.MARK}%",))
        db.commit()

    def set_status(self, status):
        self.client.post(f"/tuning/{self.order_id}/status", data={"status": status})

    def add(self, quantity="3", warehouse=None):
        data = {"product_id": str(self.product_id), "quantity": quantity}
        if warehouse is not None:
            data["warehouse_id"] = str(warehouse)
        return self.client.post(f"/tuning/{self.order_id}/products/add", data=data)

    def stock(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT quantity FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
                (self.product_id, self.warehouse_id),
            ).fetchone()["quantity"]

    def writeoffs(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM supply_writeoffs WHERE product_id = ?", (self.product_id,)
            ).fetchall()]

    def page(self):
        return self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)

    def test_no_warehouse_means_no_writeoff_even_in_work(self):
        self.set_status("in_progress")
        self.add(warehouse=None)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.writeoffs(), [])

    def test_chosen_warehouse_is_written_off_only_while_in_work(self):
        self.add(warehouse=self.warehouse_id)  # order is "На согласовании"
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.writeoffs(), [])
        self.assertIn("спишется, когда заказ будет", self.page())

        self.set_status("in_progress")
        self.assertEqual(self.stock(), 7)
        rows = self.writeoffs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["quantity"], rows[0]["warehouse_id"]), (3, self.warehouse_id))
        self.assertEqual(rows[0]["amount"], 300)
        self.assertIn("Списано со склада", self.page())

        self.set_status("in_progress")  # repeating changes nothing
        self.assertEqual(self.stock(), 7)
        self.set_status("qc")
        self.set_status("done")
        self.assertEqual(self.stock(), 7)  # later stages keep the write-off

        self.set_status("approved")  # sent back: stock returns
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.writeoffs(), [])
        self.set_status("in_progress")
        self.assertEqual(self.stock(), 7)
        self.set_status("cancelled")
        self.assertEqual(self.stock(), 10)

    def test_adding_to_an_order_already_in_work_writes_off_at_once(self):
        self.set_status("in_progress")
        self.add(warehouse=self.warehouse_id)
        self.assertEqual(self.stock(), 7)
        self.assertEqual(len(self.writeoffs()), 1)

    def test_not_enough_stock_skips_the_writeoff_and_says_so(self):
        self.add(quantity="25", warehouse=self.warehouse_id)
        self.set_status("in_progress")
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.writeoffs(), [])
        self.assertIn("не списан", self.page())
        # once the stock is topped up, the next status/total sync books it
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE supply_stock SET quantity = 30 WHERE product_id = ? AND warehouse_id = ?",
                (self.product_id, self.warehouse_id),
            )
            db.commit()
            application_module._sync_order_accounting(db, self.order_id)
        self.assertEqual(self.stock(), 5)

    def test_removing_a_line_returns_its_stock(self):
        self.set_status("in_progress")
        self.add(warehouse=self.warehouse_id)
        with application_module.app.app_context():
            line_id = application_module.get_db().execute(
                "SELECT id FROM tuning_order_products WHERE order_id = ?", (self.order_id,)
            ).fetchone()["id"]
        self.client.post(f"/tuning/{self.order_id}/products/{line_id}/remove")
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.writeoffs(), [])

    def test_bulk_status_change_triggers_the_writeoff(self):
        self.add(warehouse=self.warehouse_id)
        self.client.post(
            "/tuning/bulk",
            data={"order_id": [str(self.order_id)], "action": "status", "bulk_status": "in_progress"},
        )
        self.assertEqual(self.stock(), 7)

    def test_unknown_warehouse_is_rejected_and_stock_endpoint_lists_warehouses(self):
        self.add(warehouse=999999)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_order_products WHERE order_id = ?", (self.order_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)
        data = self.client.get(f"/tuning/products/{self.product_id}/stock").get_json()
        by_id = {w["warehouse_id"]: w["quantity"] for w in data["warehouses"]}
        self.assertEqual(by_id[self.warehouse_id], 10)
        self.assertEqual(by_id[self.other_warehouse_id], 0)
        page = self.page()
        self.assertIn('id="product_warehouse"', page)
        self.assertIn("Не списывать", page)


if __name__ == "__main__":
    unittest.main()
