import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningOrderMessengerButtonsTests(unittest.TestCase):
    """Quick wa.me/t.me buttons in the order summary's "Клиент" block,
    same idea as the schedule module's buildScheduleDetailClientMessengers,
    but server-rendered since tuning_form.html has edit_order server-side."""

    MARK = "tuning-messenger-buttons-test"
    PHONE = "+79991234567"

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
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        client_ids = [r["id"] for r in db.execute(
            "SELECT id FROM clients WHERE phone = ?", (self.PHONE,)
        ).fetchall()]
        order_ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE client_name LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        if order_ids:
            application_module._delete_tuning_order_records(db, order_ids)
        for client_id in client_ids:
            db.execute("DELETE FROM tuning_orders WHERE client_id = ?", (client_id,))
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (client_id,))
            db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        db.commit()

    def base_form(self, **overrides):
        data = MultiDict([
            ("client_name", f"{self.MARK} клиент"),
            ("equipment_type", "boat"),
            ("boat_model", "Тест-катер"),
            ("boat_registration_number", ""),
            ("motor_model", ""),
            ("motor_serial_number", ""),
            ("phone", self.PHONE),
            ("order_date", "2026-09-10"),
            ("sale_channel", "direct"),
            ("discount_type", "percent"),
            ("discount_value", "0"),
            ("work_name[]", "Диагностика"),
            ("cost_price[]", "1000"),
            ("multiplier[]", "2"),
            ("item_id[]", ""),
        ])
        for key, value in overrides.items():
            data[key] = value
        return data

    def order_id(self):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT id FROM tuning_orders WHERE client_name = ?",
                (f"{self.MARK} клиент",),
            ).fetchone()
            return row["id"] if row else None

    def test_buttons_render_with_normalized_wa_me_and_t_me_links(self):
        self.client.post("/tuning/add", data=self.base_form())
        page = self.client.get(f"/tuning/edit/{self.order_id()}").get_data(as_text=True)
        self.assertIn('href="https://wa.me/79991234567"', page)
        self.assertIn('href="https://t.me/+79991234567"', page)
        self.assertIn("order-summary-client-messengers", page)

    def test_preferred_channel_gets_the_highlight_class(self):
        self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="telegram")
        )
        page = self.client.get(f"/tuning/edit/{self.order_id()}").get_data(as_text=True)
        self.assertIn('class="icon-btn telegram is-preferred"', page)
        self.assertIn('class="icon-btn whatsapp"', page)

    def test_no_phone_means_no_buttons(self):
        self.client.post("/tuning/add", data=self.base_form(phone=""))
        page = self.client.get(f"/tuning/edit/{self.order_id()}").get_data(as_text=True)
        self.assertNotIn("order-summary-client-messengers", page)

    def test_phone_normalization_ten_and_eleven_digit_forms(self):
        self.assertEqual(
            application_module.messenger_phone_digits("+7 (999) 123-45-67"), "79991234567"
        )
        self.assertEqual(
            application_module.messenger_phone_digits("89991234567"), "79991234567"
        )
        self.assertEqual(
            application_module.messenger_phone_digits("9991234567"), "79991234567"
        )
        self.assertEqual(application_module.messenger_phone_digits(""), "")
        self.assertEqual(application_module.messenger_phone_digits(None), "")


if __name__ == "__main__":
    unittest.main()
