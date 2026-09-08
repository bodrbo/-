import io
import os
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningPartnerDashboardTests(unittest.TestCase):
    PARTNER_TOKEN = "tuning-partner-dashboard-test"
    CLIENT_TOKEN = "regular-tuning-client-dashboard-test"
    BOAT_MODEL = "Тестовая партнёрская лодка 908"
    MOTOR_MODEL = "Тестовый партнёрский мотор 908"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Верфь Север', '', '+79991112233', ?, 'neutral', "
                "'2026-09-08 10:00')",
                (self.PARTNER_TOKEN,),
            )
            self.partner_id = cursor.lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, relationship_type, partner_title, created_at) "
                "VALUES (?, 'tuning', 'partner', 'Партнёрский тюнинг-центр', "
                "'2026-09-08 10:00')",
                (self.partner_id,),
            )
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Обычный клиент', '', '', ?, 'neutral', "
                "'2026-09-08 10:05')",
                (self.CLIENT_TOKEN,),
            )
            self.regular_client_id = cursor.lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'client', '2026-09-08 10:05')",
                (self.regular_client_id,),
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        client_rows = db.execute(
            "SELECT id FROM clients WHERE token IN (?, ?)",
            (cls.PARTNER_TOKEN, cls.CLIENT_TOKEN),
        ).fetchall()
        client_ids = [row["id"] for row in client_rows]
        for client_id in client_ids:
            order_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_orders WHERE client_id = ?", (client_id,)
                ).fetchall()
            ]
            for order_id in order_ids:
                db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM tuning_order_motors WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (order_id,))
                db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (client_id,))
            db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        for equipment_type, model_name in (
            ("boat", cls.BOAT_MODEL),
            ("motor", cls.MOTOR_MODEL),
        ):
            key = application_module._tuning_equipment_profile_key(
                equipment_type, model_name
            )
            db.execute(
                "DELETE FROM tuning_boat_profiles WHERE model_key = ?", (key,)
            )
        db.commit()

    def _login_admin(self):
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def _valid_request(self):
        return MultiDict([
            ("equipment_type", "boat"),
            ("boat_model", self.BOAT_MODEL),
            ("boat_registration_number", "Р 90-08 ЛО"),
            ("boat_motor_model[]", self.MOTOR_MODEL),
            ("boat_motor_serial_number[]", "MOTOR-908"),
            ("work_name[]", "Установить картплоттер"),
            ("work_name[]", "Смонтировать ходовые огни"),
        ])

    def test_partner_gets_branded_cabinet_and_regular_client_does_not(self):
        partner_html = self.http.get(
            f"/client/{self.PARTNER_TOKEN}"
        ).get_data(as_text=True)
        regular_html = self.http.get(
            f"/client/{self.CLIENT_TOKEN}"
        ).get_data(as_text=True)

        self.assertIn("Личный кабинет партнёра", partner_html)
        self.assertIn("Партнёрский тюнинг-центр", partner_html)
        self.assertIn("Заявка на расчёт", partner_html)
        self.assertIn(
            f'/client/{self.PARTNER_TOKEN}/estimate-request', partner_html
        )
        self.assertNotIn("Заявка на расчёт", regular_html)

    def test_admin_can_edit_title_and_upload_logo(self):
        self._login_admin()
        with tempfile.TemporaryDirectory() as static_dir:
            with patch.object(application_module.app, "_static_folder", static_dir):
                response = self.http.post(
                    f"/admin/clients/{self.partner_id}/partner-profile",
                    data={
                        "partner_title": "Сертифицированный партнёр",
                        "partner_logo": (
                            io.BytesIO(b"fake-png-content"), "logo.png", "image/png"
                        ),
                    },
                    content_type="multipart/form-data",
                )
                with application_module.app.app_context():
                    row = application_module.get_db().execute(
                        "SELECT partner_title, partner_logo_filename "
                        "FROM client_segments WHERE client_id = ? AND segment = 'tuning'",
                        (self.partner_id,),
                    ).fetchone()
                saved_path = os.path.join(
                    static_dir, "partner_logos", row["partner_logo_filename"]
                )
                self.assertTrue(os.path.isfile(saved_path))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(row["partner_title"], "Сертифицированный партнёр")
        self.assertTrue(row["partner_logo_filename"].endswith(".png"))

    def test_partner_request_forces_identity_prices_status_and_no_products(self):
        form = self._valid_request()
        # Values that are legal on the administrator form must remain inert
        # on the public partner endpoint.
        form.add("client_id", str(self.regular_client_id))
        form.add("client_name", "Подменённый клиент")
        form.add("phone", "+70000000000")
        form.add("status", "done")
        form.add("cost_price[]", "900000")
        form.add("multiplier[]", "5")
        form.add("product_id", "1")
        form.add("quantity", "10")

        response = self.http.post(
            f"/client/{self.PARTNER_TOKEN}/estimate-request", data=form
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(
            f"/client/{self.PARTNER_TOKEN}#orders"
        ))
        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute(
                "SELECT * FROM tuning_orders WHERE client_id = ?",
                (self.partner_id,),
            ).fetchone()
            items = db.execute(
                "SELECT id, work_name, cost_price, multiplier, price, price_pending, status "
                "FROM tuning_order_items WHERE order_id = ? ORDER BY id",
                (order["id"],),
            ).fetchall()
            motors = db.execute(
                "SELECT motor_model, motor_serial_number FROM tuning_order_motors "
                "WHERE order_id = ?",
                (order["id"],),
            ).fetchall()
            product_count = db.execute(
                "SELECT COUNT(*) AS count FROM tuning_order_products WHERE order_id = ?",
                (order["id"],),
            ).fetchone()["count"]
            project = db.execute(
                "SELECT name FROM projects WHERE tuning_order_id = ?", (order["id"],)
            ).fetchone()

        self.assertEqual(order["client_id"], self.partner_id)
        self.assertEqual(order["client_name"], "Верфь Север")
        self.assertEqual(order["phone"], "+79991112233")
        self.assertEqual(order["status"], "estimate")
        self.assertEqual(order["source"], "partner_request")
        self.assertEqual(order["sale_channel"], "direct")
        self.assertEqual(order["subtotal"], 0)
        self.assertEqual(order["total"], 0)
        self.assertEqual(product_count, 0)
        self.assertEqual(project["name"], f"Заказ №{order['id']}")
        self.assertEqual(
            [tuple(item)[1:] for item in items],
            [
                ("Установить картплоттер", 0.0, 0.0, 0.0, 1, "pending"),
                ("Смонтировать ходовые огни", 0.0, 0.0, 0.0, 1, "pending"),
            ],
        )
        self.assertEqual(
            [tuple(motor) for motor in motors],
            [(self.MOTOR_MODEL, "MOTOR-908")],
        )

        cabinet_html = self.http.get(
            f"/client/{self.PARTNER_TOKEN}"
        ).get_data(as_text=True)
        self.assertIn("Заявка №", cabinet_html)
        self.assertIn("Ждёт расчёта", cabinet_html)
        self.assertNotIn("Согласовать работу", cabinet_html)

        # Hiding the control is not the security boundary: a forged approval
        # POST must also leave an unpriced line untouched.
        self.http.post(
            f"/client/{self.PARTNER_TOKEN}/item/{items[0]['id']}/approve"
        )
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM tuning_order_items WHERE id = ?",
                (items[0]["id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "pending")

    def test_regular_client_cannot_submit_partner_request(self):
        response = self.http.post(
            f"/client/{self.CLIENT_TOKEN}/estimate-request",
            data=self._valid_request(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(
            f"/client/{self.CLIENT_TOKEN}"
        ))
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM tuning_orders WHERE client_id = ?",
                (self.regular_client_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_invalid_partner_request_creates_nothing(self):
        response = self.http.post(
            f"/client/{self.PARTNER_TOKEN}/estimate-request",
            data={"equipment_type": "boat", "boat_model": ""},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Укажите модель лодки", response.get_data(as_text=True))
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM tuning_orders WHERE client_id = ?",
                (self.partner_id,),
            ).fetchone()["count"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
