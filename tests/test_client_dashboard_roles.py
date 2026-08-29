import unittest

from support import application_module


class ClientDashboardRoleTests(unittest.TestCase):
    CLIENT_TOKEN = "client-role-test-token"
    CLIENT_PHONE = "+7 921 555-12-34"
    INTERNAL_NOTE = "Скрытая служебная заметка для администратора"
    SOURCE_REF = "tuning_site:private-request-reference"
    PAYMENT_URL = "https://payments.example.test/private-checkout"

    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            client_cursor = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "Иван Петров",
                    "Finnmaster T8",
                    self.CLIENT_PHONE,
                    self.CLIENT_TOKEN,
                    "2026-08-29 09:00",
                ),
            )
            self.client_id = client_cursor.lastrowid
            order_cursor = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, equipment_type, boat_model, "
                "boat_registration_number, motor_model, motor_serial_number, "
                "sale_channel, phone, discount_pct, discount_type, discount_value, "
                "subtotal, total, status, source, source_ref, created_at, updated_at) "
                "VALUES (?, 'Иван Петров', 'boat', 'Finnmaster T8', '', "
                "'Yamaha F200', '', 'direct', ?, 0, 'percent', 0, 18000, "
                "18000, 'estimate', 'tuning_site', ?, ?, ?)",
                (
                    self.client_id,
                    self.CLIENT_PHONE,
                    self.SOURCE_REF,
                    "2026-08-29 09:10",
                    "2026-08-29 10:30",
                ),
            )
            self.order_id = order_cursor.lastrowid
            item_cursor = db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, status) "
                "VALUES (?, 'Установка эхолота', 10000, 1.5, 15000, 'pending')",
                (self.order_id,),
            )
            self.item_id = item_cursor.lastrowid
            db.execute(
                "INSERT INTO tuning_order_products "
                "(order_id, product_id, product_name, quantity, unit_price, "
                "cost_price, unit, created_at) "
                "VALUES (?, 1, 'Крепёж', 2, 1500, 800, 'piece', ?)",
                (self.order_id, "2026-08-29 09:20"),
            )
            db.execute(
                "INSERT INTO tuning_order_notes "
                "(order_id, author_admin_id, text, created_at) VALUES (?, NULL, ?, ?)",
                (self.order_id, self.INTERNAL_NOTE, "2026-08-29 09:30"),
            )
            db.execute(
                "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) "
                "VALUES (?, 5000, ?, ?)",
                (
                    self.order_id,
                    "2026-08-29 10:00",
                    "2026-08-29 10:00",
                ),
            )
            db.execute(
                "INSERT INTO tuning_yookassa_payments "
                "(order_id, yookassa_payment_id, amount, status, confirmation_url, "
                "created_at, updated_at) VALUES (?, ?, 13000, 'pending', ?, ?, ?)",
                (
                    self.order_id,
                    "dashboard-role-payment",
                    self.PAYMENT_URL,
                    "2026-08-29 10:05",
                    "2026-08-29 10:05",
                ),
            )
            db.commit()
        self.addCleanup(self.cleanup_database)

    @classmethod
    def _clear_test_data(cls, db):
        order_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM tuning_orders WHERE source_ref = ?", (cls.SOURCE_REF,)
            ).fetchall()
        ]
        for order_id in order_ids:
            payment_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM tuning_payments WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for payment_id in payment_ids:
                db.execute(
                    "DELETE FROM modulkassa_receipts WHERE payment_id = ?", (payment_id,)
                )
            note_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM tuning_order_notes WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for note_id in note_ids:
                db.execute(
                    "DELETE FROM tuning_order_note_reminders WHERE note_id = ?", (note_id,)
                )
            item_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
                ).fetchall()
            ]
            for item_id in item_ids:
                db.execute("DELETE FROM work_item_photos WHERE item_id = ?", (item_id,))
            db.execute("DELETE FROM tuning_yookassa_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_notes WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
        db.execute("DELETE FROM clients WHERE token = ?", (cls.CLIENT_TOKEN,))
        db.commit()

    @classmethod
    def cleanup_database(cls):
        with application_module.app.app_context():
            cls._clear_test_data(application_module.get_db())

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def test_admin_cabinet_requires_admin_login(self):
        response = self.client.get(f"/admin/clients/{self.client_id}/cabinet")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_clients_directory_requires_admin_login(self):
        response = self.client.get("/admin/clients")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_client_status_update_requires_admin_login(self):
        response = self.client.post(
            f"/admin/clients/{self.client_id}/status",
            data={"status": "dissatisfied"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM clients WHERE id = ?", (self.client_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "neutral")

    def test_public_cabinet_does_not_expose_internal_data(self):
        response = self.client.get(f"/client/{self.CLIENT_TOKEN}")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Личный кабинет клиента", html)
        self.assertIn("Установка эхолота", html)
        self.assertIn(
            f'/client/{self.CLIENT_TOKEN}/item/{self.item_id}/approve', html
        )
        self.assertIn(self.PAYMENT_URL, html)
        for private_value in (
            "Служебный режим",
            "Внутренняя себестоимость",
            "Себестоимость, ₽",
            self.CLIENT_PHONE,
            self.INTERNAL_NOTE,
            self.SOURCE_REF,
            f"/tuning/edit/{self.order_id}",
        ):
            self.assertNotIn(private_value, html)

    def test_admin_cabinet_shows_internal_data_without_client_actions(self):
        self.login()
        response = self.client.get(f"/admin/clients/{self.client_id}/cabinet")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("ЛК клиента — режим администратора", html)
        self.assertIn("Вы смотрите кабинет как администратор", html)
        self.assertIn("Администратор теста", html)
        self.assertIn(self.CLIENT_PHONE, html)
        self.assertIn(self.INTERNAL_NOTE, html)
        self.assertIn(self.SOURCE_REF, html)
        self.assertIn("11 600,00 ₽", html)
        self.assertIn("6 400,00 ₽", html)
        self.assertIn(f'/tuning/edit/{self.order_id}', html)
        self.assertIn('href="/admin/clients"', html)
        self.assertIn(f'/client/{self.CLIENT_TOKEN}', html)
        self.assertNotIn(
            f'/client/{self.CLIENT_TOKEN}/item/{self.item_id}/approve', html
        )
        self.assertNotIn(self.PAYMENT_URL, html)

    def test_tuning_order_list_uses_protected_admin_cabinet_link(self):
        self.login()
        response = self.client.get("/tuning")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/admin/clients"', html)
        self.assertIn(f'/admin/clients/{self.client_id}/cabinet', html)
        self.assertNotIn(f'/client/{self.CLIENT_TOKEN}', html)

    def test_clients_directory_aggregates_orders_and_payments(self):
        self.login()
        response = self.client.get("/admin/clients")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Клиенты тюнинга", html)
        self.assertIn("Иван Петров", html)
        self.assertIn(self.CLIENT_PHONE, html)
        self.assertIn("Finnmaster T8", html)
        self.assertIn("18 000,00", html)
        self.assertIn("5 000,00", html)
        self.assertIn("13 000,00", html)
        self.assertIn(f'/admin/clients/{self.client_id}/cabinet', html)
        self.assertIn(
            'href="/admin/clients" class="active"', html
        )
        self.assertIn("Статус", html)
        self.assertIn("tuning-client-status-neutral", html)
        self.assertIn('<option value="neutral" selected>Нейтральный</option>', html)

    def test_admin_can_update_client_status_and_invalid_value_is_ignored(self):
        self.login()
        updated = self.client.post(
            f"/admin/clients/{self.client_id}/status",
            data={"status": "satisfied"},
        )

        self.assertEqual(updated.status_code, 302)
        self.assertTrue(updated.headers["Location"].endswith("/admin/clients"))
        with application_module.app.app_context():
            db = application_module.get_db()
            status = db.execute(
                "SELECT status FROM clients WHERE id = ?", (self.client_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "satisfied")

        directory = self.client.get("/admin/clients").get_data(as_text=True)
        cabinet = self.client.get(
            f"/admin/clients/{self.client_id}/cabinet"
        ).get_data(as_text=True)
        self.assertIn("tuning-client-status-satisfied", directory)
        self.assertIn('<option value="satisfied" selected>Довольный</option>', directory)
        self.assertIn("client-status-pill-satisfied", cabinet)
        self.assertIn("Довольный", cabinet)

        invalid = self.client.post(
            f"/admin/clients/{self.client_id}/status",
            data={"status": "unknown-status"},
        )
        self.assertEqual(invalid.status_code, 302)
        with application_module.app.app_context():
            status_after_invalid = application_module.get_db().execute(
                "SELECT status FROM clients WHERE id = ?", (self.client_id,)
            ).fetchone()["status"]
        self.assertEqual(status_after_invalid, "satisfied")


if __name__ == "__main__":
    unittest.main()
