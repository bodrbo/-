import datetime as dt
import unittest

from support import application_module


class TuningOrderCopyTests(unittest.TestCase):
    CLIENT_TOKEN = "tuning-order-copy-client"
    SOURCE_REF = "copy-test:source-order"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            client_cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Клиент Копирования', 'Salute 585 HT', "
                "'+79990002233', ?, 'neutral', '2026-09-01 10:00')",
                (self.CLIENT_TOKEN,),
            )
            self.client_id = client_cursor.lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'tuning', '2026-09-01 10:00')",
                (self.client_id,),
            )
            order_cursor = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, equipment_type, boat_model, "
                "boat_registration_number, motor_model, motor_serial_number, "
                "sale_channel, phone, discount_pct, discount_type, discount_value, "
                "subtotal, total, status, order_date, created_at, updated_at, "
                "source, source_ref) VALUES (?, 'Клиент Копирования', 'boat', "
                "'Salute 585 HT', 'Р 12-34 ЛО', 'Yamaha F200', '', 'direct', "
                "'+79990002233', 10, 'percent', 10, 4000, 3600, 'done', "
                "'2026-09-01', '2026-09-01 10:00', '2026-09-02 12:00', "
                "'tilda', ?)",
                (self.client_id, self.SOURCE_REF),
            )
            self.source_order_id = order_cursor.lastrowid
            db.executemany(
                "INSERT INTO tuning_order_motors "
                "(order_id, motor_model, motor_serial_number, position, created_at) "
                "VALUES (?, ?, ?, ?, '2026-09-01 10:00')",
                (
                    (self.source_order_id, "Yamaha F200", "YA-001", 0),
                    (self.source_order_id, "Yamaha F200", "YA-002", 1),
                ),
            )
            active_item = db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Монтаж эхолота', 1500, 2, "
                "3000, 0, 'done')",
                (self.source_order_id,),
            )
            removed_item = db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Снятая работа', 100, 2, "
                "200, 0, 'removed')",
                (self.source_order_id,),
            )
            self.active_item_id = active_item.lastrowid
            self.removed_item_id = removed_item.lastrowid
            db.execute(
                "INSERT INTO tuning_item_assignments "
                "(item_id, employee_name, rate, norm_hours, comment, "
                "assignment_status, assigned_at) VALUES (?, 'Мастер', 1000, 2, "
                "'Только в оригинале', 'accepted', '2026-09-01 11:00')",
                (self.active_item_id,),
            )
            db.execute(
                "INSERT INTO work_item_photos (item_id, filename, comment, created_at) "
                "VALUES (?, 'copy-test.jpg', 'Фото оригинала', '2026-09-01 12:00')",
                (self.active_item_id,),
            )
            db.execute(
                "INSERT INTO tuning_order_products "
                "(order_id, product_id, product_name, quantity, unit_price, "
                "cost_price, unit, created_at) VALUES (?, 987654, 'Крепёж', 2, "
                "500, 300, 'piece', '2026-09-01 10:00')",
                (self.source_order_id,),
            )
            db.execute(
                "INSERT INTO tuning_payments "
                "(order_id, amount, paid_at, created_at) "
                "VALUES (?, 1000, '2026-09-02 10:00', '2026-09-02 10:00')",
                (self.source_order_id,),
            )
            db.execute(
                "INSERT INTO tuning_order_notes "
                "(order_id, text, created_at) VALUES (?, 'Заметка оригинала', "
                "'2026-09-02 11:00')",
                (self.source_order_id,),
            )
            db.execute(
                "INSERT INTO projects (name, tuning_order_id, created_at) "
                "VALUES (?, ?, '2026-09-01 10:00')",
                (f"Заказ №{self.source_order_id}", self.source_order_id),
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
        client = db.execute(
            "SELECT id FROM clients WHERE token = ?", (cls.CLIENT_TOKEN,)
        ).fetchone()
        if client is None:
            return
        order_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tuning_orders WHERE client_id = ?", (client["id"],)
            ).fetchall()
        ]
        for order_id in order_ids:
            item_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for item_id in item_ids:
                db.execute(
                    "DELETE FROM tuning_item_assignments WHERE item_id = ?", (item_id,)
                )
                db.execute("DELETE FROM work_item_photos WHERE item_id = ?", (item_id,))
            note_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_order_notes WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for note_id in note_ids:
                db.execute(
                    "DELETE FROM tuning_order_note_reminders WHERE note_id = ?", (note_id,)
                )
            product_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_order_products WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for product_id in product_ids:
                db.execute(
                    "DELETE FROM supply_writeoffs WHERE tuning_order_product_id = ?",
                    (product_id,),
                )
            db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_motors WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_notes WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_yookassa_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
        db.execute("DELETE FROM client_segments WHERE client_id = ?", (client["id"],))
        db.execute("DELETE FROM clients WHERE id = ?", (client["id"],))
        db.commit()

    def test_copy_creates_new_estimate_with_commercial_rows_only(self):
        response = self.client.post(f"/tuning/{self.source_order_id}/copy")

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            copied_order = db.execute(
                "SELECT * FROM tuning_orders WHERE client_id = ? AND id != ?",
                (self.client_id, self.source_order_id),
            ).fetchone()
            self.assertIsNotNone(copied_order)
            copied_order_id = copied_order["id"]
            copied_motors = db.execute(
                "SELECT motor_model, motor_serial_number, position "
                "FROM tuning_order_motors WHERE order_id = ? ORDER BY position",
                (copied_order_id,),
            ).fetchall()
            copied_items = db.execute(
                "SELECT id, work_name, cost_price, multiplier, price, "
                "price_pending, status FROM tuning_order_items WHERE order_id = ?",
                (copied_order_id,),
            ).fetchall()
            copied_products = db.execute(
                "SELECT product_name, quantity, unit_price, cost_price, unit "
                "FROM tuning_order_products WHERE order_id = ?",
                (copied_order_id,),
            ).fetchall()
            copied_project = db.execute(
                "SELECT name FROM projects WHERE tuning_order_id = ?",
                (copied_order_id,),
            ).fetchone()
            copied_assignments = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_item_assignments "
                "WHERE item_id IN (SELECT id FROM tuning_order_items WHERE order_id = ?)",
                (copied_order_id,),
            ).fetchone()["count"]
            copied_photos = db.execute(
                "SELECT COUNT(*) AS count FROM work_item_photos "
                "WHERE item_id IN (SELECT id FROM tuning_order_items WHERE order_id = ?)",
                (copied_order_id,),
            ).fetchone()["count"]
            copied_payments = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_payments WHERE order_id = ?",
                (copied_order_id,),
            ).fetchone()["count"]
            copied_notes = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_order_notes WHERE order_id = ?",
                (copied_order_id,),
            ).fetchone()["count"]

        self.assertGreater(copied_order_id, self.source_order_id)
        self.assertTrue(response.headers["Location"].endswith(
            f"/tuning/edit/{copied_order_id}"
        ))
        self.assertEqual(copied_order["status"], "estimate")
        self.assertEqual(copied_order["order_date"], dt.date.today().isoformat())
        self.assertEqual(copied_order["source"], "manual")
        self.assertIsNone(copied_order["source_ref"])
        self.assertEqual(copied_order["client_id"], self.client_id)
        self.assertEqual(copied_order["boat_model"], "Salute 585 HT")
        self.assertEqual(copied_order["boat_registration_number"], "Р 12-34 ЛО")
        self.assertEqual(copied_order["subtotal"], 4000)
        self.assertEqual(copied_order["total"], 3600)
        self.assertEqual(
            [tuple(row) for row in copied_motors],
            [("Yamaha F200", "YA-001", 0), ("Yamaha F200", "YA-002", 1)],
        )
        self.assertEqual(len(copied_items), 1)
        self.assertEqual(copied_items[0]["work_name"], "Монтаж эхолота")
        self.assertEqual(copied_items[0]["status"], "pending")
        self.assertEqual(
            tuple(copied_products[0]), ("Крепёж", 2.0, 500.0, 300.0, "piece")
        )
        self.assertEqual(copied_project["name"], f"Заказ №{copied_order_id}")
        self.assertEqual(copied_assignments, 0)
        self.assertEqual(copied_photos, 0)
        self.assertEqual(copied_payments, 0)
        self.assertEqual(copied_notes, 0)

        edit_html = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn(f"Создана копия заказа №{self.source_order_id}", edit_html)
        self.assertIn("Предварительный расчёт", edit_html)

    def test_order_list_exposes_compact_copy_action(self):
        html = self.client.get("/tuning").get_data(as_text=True)

        self.assertIn(
            f'action="/tuning/{self.source_order_id}/copy"', html
        )
        self.assertIn(
            f'aria-label="Копировать заказ №{self.source_order_id}"', html
        )

    def test_missing_order_is_not_copied(self):
        response = self.client.post("/tuning/999999999/copy")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/tuning"))
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM tuning_orders WHERE client_id = ?",
                (self.client_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
