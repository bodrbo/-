import unittest

from support import application_module


class TuningOrderBulkActionTests(unittest.TestCase):
    SOURCE_PREFIX = "bulk-tuning-test:"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.order_ids = []
            for index, status in ((1, "estimate"), (2, "in_progress")):
                cursor = db.execute(
                    "INSERT INTO tuning_orders "
                    "(client_name, equipment_type, boat_model, sale_channel, phone, "
                    "subtotal, total, status, order_date, created_at, updated_at, "
                    "source, source_ref) VALUES (?, 'boat', ?, 'direct', '', 1000, "
                    "1000, ?, '2026-09-10', '2026-09-10 10:00', "
                    "'2026-09-10 10:00', 'manual', ?)",
                    (
                        f"Bulk Test Client {index}",
                        f"Bulk Test Boat {index}",
                        status,
                        f"{self.SOURCE_PREFIX}regular:{index}",
                    ),
                )
                self.order_ids.append(cursor.lastrowid)
            subcontract = db.execute(
                "INSERT INTO tuning_orders "
                "(client_name, equipment_type, boat_model, sale_channel, phone, "
                "subtotal, total, status, order_date, created_at, updated_at, "
                "source, source_ref) VALUES ('Bulk Test Subcontract', 'boat', "
                "'Bulk Test Boat Subcontract', 'direct', '', 1000, 1000, "
                "'estimate', '2026-09-10', '2026-09-10 10:00', "
                "'2026-09-10 10:00', ?, ?)",
                (
                    application_module.SUBCONTRACT_REQUEST_SOURCE,
                    f"{self.SOURCE_PREFIX}subcontract",
                ),
            )
            self.subcontract_id = subcontract.lastrowid
            db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Тестовая работа', 500, 2, "
                "1000, 0, 'pending')",
                (self.order_ids[0],),
            )
            db.execute(
                "INSERT INTO tuning_order_motors "
                "(order_id, motor_model, motor_serial_number, position, created_at) "
                "VALUES (?, 'Bulk Motor', '', 0, '2026-09-10 10:00')",
                (self.order_ids[0],),
            )
            db.execute(
                "INSERT INTO tuning_payments "
                "(order_id, amount, paid_at, created_at) "
                "VALUES (?, 500, '2026-09-10 11:00', '2026-09-10 11:00')",
                (self.order_ids[0],),
            )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        order_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tuning_orders WHERE source_ref LIKE ?",
                (f"{cls.SOURCE_PREFIX}%",),
            ).fetchall()
        ]
        for order_id in order_ids:
            db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_motors WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
        db.execute(
            "DELETE FROM tuning_boat_profiles WHERE model_key LIKE 'bulk test boat %'"
        )
        db.commit()

    def test_order_list_exposes_bulk_controls(self):
        response = self.client.get(
            "/tuning",
            query_string={
                "q": "Bulk Test Client",
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
            },
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="tuning-bulk-form"', html)
        self.assertIn('id="tuning-select-all"', html)
        self.assertEqual(html.count('class="tuning-order-check"'), 2)
        self.assertIn('name="action" value="status"', html)
        self.assertIn('name="action" value="delete"', html)
        self.assertIn('name="return_q" value="Bulk Test Client"', html)
        self.assertIn('name="return_date_from" value="2026-09-01"', html)

    def test_bulk_status_update_changes_all_selected_orders(self):
        response = self.client.post(
            "/tuning/bulk",
            data={
                "order_id": [str(order_id) for order_id in self.order_ids],
                "action": "status",
                "bulk_status": "done",
                "return_q": "Bulk Test Client",
                "return_date_from": "2026-09-01",
                "return_date_to": "2026-09-30",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("q=Bulk+Test+Client", response.headers["Location"])
        self.assertIn("date_from=2026-09-01", response.headers["Location"])
        self.assertTrue(response.headers["Location"].endswith("#tuning-orders"))
        with application_module.app.app_context():
            db = application_module.get_db()
            statuses = {
                row["status"] for row in db.execute(
                    "SELECT status FROM tuning_orders WHERE id IN (?, ?)",
                    self.order_ids,
                ).fetchall()
            }
            subcontract_status = db.execute(
                "SELECT status FROM tuning_orders WHERE id = ?",
                (self.subcontract_id,),
            ).fetchone()["status"]
        self.assertEqual(statuses, {"done"})
        self.assertEqual(subcontract_status, "estimate")

    def test_bulk_action_rejects_invalid_status_and_subcontract(self):
        invalid_status = self.client.post(
            "/tuning/bulk",
            data={
                "order_id": str(self.order_ids[0]),
                "action": "status",
                "bulk_status": "forged-status",
            },
            follow_redirects=True,
        )
        self.assertIn(
            "Выберите новый статус заказа", invalid_status.get_data(as_text=True)
        )

        subcontract = self.client.post(
            "/tuning/bulk",
            data={
                "order_id": str(self.subcontract_id),
                "action": "delete",
            },
            follow_redirects=True,
        )
        self.assertIn(
            "Некоторые выбранные заказы уже недоступны",
            subcontract.get_data(as_text=True),
        )
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT id, status FROM tuning_orders WHERE id IN (?, ?)",
                (self.order_ids[0], self.subcontract_id),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "estimate")

    def test_bulk_delete_removes_selected_orders_and_core_rows(self):
        response = self.client.post(
            "/tuning/bulk",
            data={
                "order_id": [str(order_id) for order_id in self.order_ids],
                "action": "delete",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Удалено 2 заказа", response.get_data(as_text=True))
        with application_module.app.app_context():
            db = application_module.get_db()
            remaining_orders = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_orders WHERE id IN (?, ?)",
                self.order_ids,
            ).fetchone()["count"]
            remaining_items = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_order_items WHERE order_id = ?",
                (self.order_ids[0],),
            ).fetchone()["count"]
            remaining_motors = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_order_motors WHERE order_id = ?",
                (self.order_ids[0],),
            ).fetchone()["count"]
            remaining_payments = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_payments WHERE order_id = ?",
                (self.order_ids[0],),
            ).fetchone()["count"]
            subcontract = db.execute(
                "SELECT id FROM tuning_orders WHERE id = ?", (self.subcontract_id,)
            ).fetchone()
        self.assertEqual(remaining_orders, 0)
        self.assertEqual(remaining_items, 0)
        self.assertEqual(remaining_motors, 0)
        self.assertEqual(remaining_payments, 0)
        self.assertIsNotNone(subcontract)

    def test_bulk_actions_require_admin_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post(
            "/tuning/bulk",
            data={
                "order_id": str(self.order_ids[0]),
                "action": "delete",
            },
        )
        self.assertEqual(response.status_code, 302)


if __name__ == "__main__":
    unittest.main()
