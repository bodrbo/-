import unittest

from support import application_module


class SupplyWarehousePaginationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_product_external_links")
            db.execute("DELETE FROM supply_receipts")
            db.execute("DELETE FROM supply_writeoffs")
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_warehouses")
            self.warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, address, created_at) "
                "VALUES ('Тестовый склад', 'Тестовый адрес', '2026-09-10')"
            ).lastrowid
            for index in range(55):
                product_id = db.execute(
                    "INSERT INTO supply_products "
                    "(name, sku, cost_price, cost_unit, sale_price, created_at) "
                    "VALUES (?, ?, 1, 'piece', 2, '2026-09-10')",
                    ("Товар {:03d}".format(index), "WH-{:03d}".format(index)),
                ).lastrowid
                db.execute(
                    "INSERT INTO supply_stock "
                    "(product_id, warehouse_id, quantity, zone, rack, spot) "
                    "VALUES (?, ?, 1, ?, ?, ?)",
                    (product_id, self.warehouse_id, "Зона", "Стеллаж", str(index)),
                )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_product_external_links")
            db.execute("DELETE FROM supply_receipts")
            db.execute("DELETE FROM supply_writeoffs")
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_warehouses")
            db.commit()

    def test_warehouse_stock_is_paginated_by_fifty_with_full_totals(self):
        first = self.client.get("/supply/warehouses/{}".format(self.warehouse_id))
        body = first.get_data(as_text=True)
        self.assertEqual(first.status_code, 200)
        self.assertIn("1–50 из 55", body)
        self.assertIn("<td>55</td>", body)
        self.assertIn("Товар 000", body)
        self.assertNotIn("Товар 054", body)

        second = self.client.get(
            "/supply/warehouses/{}?page=2".format(self.warehouse_id)
        ).get_data(as_text=True)
        self.assertIn("51–55 из 55", second)
        self.assertIn("Товар 054", second)
        self.assertNotIn("Товар 000", second)

    def test_search_is_server_side_and_preserved_in_pagination(self):
        response = self.client.get(
            "/supply/warehouses/{}?q=WH-054".format(self.warehouse_id)
        )
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("1–1 из 1", body)
        self.assertIn("Товар 054", body)
        self.assertNotIn("Товар 053", body)
        self.assertIn('value="WH-054"', body)


if __name__ == "__main__":
    unittest.main()
