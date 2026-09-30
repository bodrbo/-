import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningOrderContactMethodTests(unittest.TestCase):
    """The "канал связи" mechanic (CLIENT_CONTACT_METHODS,
    clients.preferred_contact_method) that the schedule module already has,
    brought into the tuning order form."""

    MARK = "tuning-contact-method-test"

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
            "SELECT id FROM clients WHERE phone = ?", (f"+7900{self.MARK[-6:]}",)
        ).fetchall()]
        order_ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        if order_ids:
            application_module._delete_tuning_order_records(db, order_ids)
        for client_id in client_ids:
            db.execute(
                "DELETE FROM tuning_orders WHERE client_id = ?", (client_id,)
            )
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
            ("phone", f"+7900{self.MARK[-6:]}"),
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

    def client_channel(self, phone=None):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT preferred_contact_method FROM clients WHERE phone = ?",
                (phone or f"+7900{self.MARK[-6:]}",),
            ).fetchone()
            return row["preferred_contact_method"] if row else None

    def order_id_for_phone(self):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT id, client_id FROM tuning_orders WHERE phone = ?",
                (f"+7900{self.MARK[-6:]}",),
            ).fetchone()
            return (row["id"], row["client_id"]) if row else (None, None)

    def test_creating_an_order_sets_the_clients_contact_method(self):
        response = self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="whatsapp")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client_channel(), "whatsapp")

    def test_no_selection_on_creation_leaves_it_unset(self):
        self.client.post("/tuning/add", data=self.base_form())
        self.assertEqual(self.client_channel(), "")

    def test_editing_an_order_overwrites_the_channel_even_to_blank(self):
        self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="telegram")
        )
        self.assertEqual(self.client_channel(), "telegram")
        order_id, _client_id = self.order_id_for_phone()

        edit_form = self.base_form(preferred_contact_method="sms")
        edit_form.setlist("item_id[]", [""])
        self.client.post(f"/tuning/edit/{order_id}", data=edit_form)
        self.assertEqual(self.client_channel(), "sms")

        # re-saving with nothing selected clears it — same overwrite
        # semantics as the schedule module (a blank selection is explicit
        # "не указан", not "leave whatever was there")
        edit_form2 = self.base_form(preferred_contact_method="")
        edit_form2.setlist("item_id[]", [""])
        self.client.post(f"/tuning/edit/{order_id}", data=edit_form2)
        self.assertEqual(self.client_channel(), "")

    def test_unknown_value_is_dropped_silently(self):
        response = self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="carrier-pigeon")
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client_channel(), "")

    def test_edit_page_preselects_the_clients_current_channel(self):
        self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="max")
        )
        order_id, _client_id = self.order_id_for_phone()
        page = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
        self.assertIn('<option value="max" selected>MAX</option>', page)
        self.assertIn('name="preferred_contact_method"', page)

    def test_channel_lives_on_the_shared_client_not_the_order(self):
        self.client.post(
            "/tuning/add", data=self.base_form(preferred_contact_method="email")
        )
        order_id, client_id = self.order_id_for_phone()
        # a second order for the very same client, edited to a new channel...
        second_form = self.base_form(preferred_contact_method="telegram")
        second_form["client_id"] = str(client_id)
        second_form.setlist("item_id[]", [""])
        self.client.post("/tuning/add", data=second_form)
        # ...changes what the FIRST order's card shows too, since it's the
        # same client's attribute, not something private to either order
        page = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
        self.assertIn('<option value="telegram" selected>Telegram</option>', page)

    def test_webhook_style_client_resolution_never_touches_the_channel(self):
        # simulate a caller that doesn't have the field on its form at all
        # (tuning.bodrbo.ru leads, Tilda) — the client's channel, once set
        # through the order form, must survive being resolved again there
        with application_module.app.app_context():
            db = application_module.get_db()
            client_id = application_module._get_or_create_client(
                db, f"+7900{self.MARK[-6:]}", f"{self.MARK} клиент", "Тест-катер",
                preferred_contact_method="whatsapp",
            )
            db.commit()
            self.assertEqual(self.client_channel(), "whatsapp")
            application_module._get_or_create_client(
                db, f"+7900{self.MARK[-6:]}", f"{self.MARK} клиент 2", "Тест-катер 2",
            )
            db.commit()
        self.assertEqual(self.client_channel(), "whatsapp")


if __name__ == "__main__":
    unittest.main()
