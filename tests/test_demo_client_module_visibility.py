import unittest

from support import application_module


class DemoClientModuleVisibilityTests(unittest.TestCase):
    CONTACT_NAME = "Демо скрытый контакт модулей"

    @classmethod
    def setUpClass(cls):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)

    def setUp(self):
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN "
                "(SELECT id FROM clients WHERE client_name = ?)",
                (self.CONTACT_NAME,),
            )
            db.execute(
                "DELETE FROM clients WHERE client_name = ?",
                (self.CONTACT_NAME,),
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN "
                "(SELECT id FROM clients WHERE client_name = ?)",
                (self.CONTACT_NAME,),
            )
            db.execute(
                "DELETE FROM clients WHERE client_name = ?",
                (self.CONTACT_NAME,),
            )
            db.commit()

    def log_in_demo(self, modules):
        with self.client.session_transaction() as demo_session:
            demo_session.clear()
            demo_session["demo_tenant_id"] = 901
            demo_session["demo_tenant_name"] = "Демо модулей"
            demo_session["demo_tenant_db_path"] = application_module.DB_PATH
            demo_session["demo_tenant_modules"] = ",".join(modules)

    def test_tuning_only_demo_hides_excursion_directories(self):
        self.log_in_demo(["tuning"])

        response = self.client.get("/admin/clients?section=excursion")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Клиенты тюнинга", html)
        self.assertIn("Партнеры тюнинга", html)
        self.assertNotIn("Клиенты экскурсий", html)
        self.assertNotIn("Партнеры экскурсий", html)
        self.assertIn("Тюнинг-центр · отдельная база", html)

    def test_excursions_only_demo_hides_tuning_directories(self):
        self.log_in_demo(["excursions"])

        response = self.client.get("/admin/clients?section=tuning")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Клиенты тюнинга", html)
        self.assertNotIn("Партнеры тюнинга", html)
        self.assertIn("Клиенты экскурсий", html)
        self.assertIn("Партнеры экскурсий", html)
        self.assertIn("Экскурсионные рейсы · отдельная база", html)
        self.assertIn('href="/admin/clients?section=excursion"', html)

    def test_demo_with_both_modules_keeps_all_directories(self):
        self.log_in_demo(["tuning", "excursions"])

        html = self.client.get("/admin/clients").get_data(as_text=True)

        for label in (
            "Клиенты тюнинга",
            "Партнеры тюнинга",
            "Клиенты экскурсий",
            "Партнеры экскурсий",
        ):
            self.assertIn(label, html)

    def test_demo_without_business_modules_has_no_client_directory(self):
        self.log_in_demo(["fleet"])

        self.assertEqual(self.client.get("/admin/clients").status_code, 404)
        home = self.client.get("/admin").get_data(as_text=True)
        self.assertNotIn('href="/admin/clients', home)

    def test_hidden_segment_cannot_be_created_through_direct_post(self):
        self.log_in_demo(["excursions"])

        response = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "client",
                "client_name": self.CONTACT_NAME,
            },
        )

        self.assertEqual(response.status_code, 404)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT id FROM clients WHERE client_name = ?",
                (self.CONTACT_NAME,),
            ).fetchone()
        self.assertIsNone(row)

    def test_excursion_only_fields_are_blocked_without_excursion_module(self):
        self.log_in_demo(["tuning"])

        acquisition_response = self.client.post(
            "/admin/clients/1/acquisition-channel",
            data={"acquisition_channel": "tripster"},
        )
        contact_response = self.client.post(
            "/admin/clients/1/contact-method",
            data={"preferred_contact_method": "telegram"},
        )

        self.assertEqual(acquisition_response.status_code, 404)
        self.assertEqual(contact_response.status_code, 404)

    def test_excursion_demo_hides_and_rejects_tripster_sales_channel(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, "
                "acquisition_channel, created_at) "
                "VALUES (?, '', '', 'demo-tripster-hidden-token', 'neutral', "
                "'', '2026-09-23 12:00')",
                (self.CONTACT_NAME,),
            )
            client_id = client.lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, created_at) VALUES (?, 'excursion', ?)",
                (client_id, "2026-09-23 12:00"),
            )
            db.commit()
        self.log_in_demo(["excursions"])

        response = self.client.get(
            f"/admin/clients/{client_id}/cabinet?section=excursion"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<option value="tripster"', response.get_data(as_text=True))

        update = self.client.post(
            f"/admin/clients/{client_id}/acquisition-channel",
            data={"acquisition_channel": "tripster"},
        )
        self.assertEqual(update.status_code, 302)
        with application_module.app.app_context():
            channel = application_module.get_db().execute(
                "SELECT acquisition_channel FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()["acquisition_channel"]
        self.assertEqual(channel, "")


if __name__ == "__main__":
    unittest.main()
