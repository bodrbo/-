import unittest

from support import application_module


class TuningSiteIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self.original_secret = application_module.TUNING_SITE_WEBHOOK_SECRET
        self.original_tilda_secret = application_module.TILDA_WEBHOOK_SECRET
        application_module.TUNING_SITE_WEBHOOK_SECRET = "integration-test-secret"
        self.addCleanup(
            setattr,
            application_module,
            "TUNING_SITE_WEBHOOK_SECRET",
            self.original_secret,
        )
        self.addCleanup(
            setattr,
            application_module,
            "TILDA_WEBHOOK_SECRET",
            self.original_tilda_secret,
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in (
                "tuning_order_note_reminders",
                "tuning_order_notes",
                "tuning_order_products",
                "tuning_order_items",
                "tuning_payments",
                "projects",
                "tuning_orders",
                "clients",
                "tilda_webhook_log",
            ):
                db.execute(f"DELETE FROM {table}")
            db.commit()

    @staticmethod
    def payload(**overrides):
        data = {
            "request_id": "lead-2026-0001",
            "name": "Иван Петров",
            "phone": "+7 921 000-00-00",
            "boat_model": "Finnmaster T8",
            "message": "Нужна установка носового подруливающего устройства",
            "source_url": "https://tuning.bodrbo.ru/services/thruster",
            "submitted_at": "2026-08-27T12:34:56+03:00",
        }
        data.update(overrides)
        return data

    @staticmethod
    def auth(secret="integration-test-secret"):
        return {"Authorization": f"Bearer {secret}"}

    def test_missing_configuration_and_invalid_tokens_return_json(self):
        application_module.TUNING_SITE_WEBHOOK_SECRET = None
        unavailable = self.client.post(
            "/api/integrations/tuning-leads", json=self.payload()
        )
        application_module.TUNING_SITE_WEBHOOK_SECRET = "integration-test-secret"
        missing = self.client.post(
            "/api/integrations/tuning-leads", json=self.payload()
        )
        wrong = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(),
            headers=self.auth("wrong-secret"),
        )

        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.get_json()["error"], "integration_not_configured")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.get_json()["error"], "unauthorized")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.headers["WWW-Authenticate"], "Bearer")
        self.assertTrue(wrong.is_json)

    def test_success_creates_client_order_project_and_note(self):
        response = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(),
            headers=self.auth(),
        )
        body = response.get_json()

        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute(
                "SELECT * FROM tuning_orders WHERE id = ?", (body["order_id"],)
            ).fetchone()
            client = db.execute(
                "SELECT * FROM clients WHERE id = ?", (order["client_id"],)
            ).fetchone()
            project = db.execute(
                "SELECT * FROM projects WHERE tuning_order_id = ?", (order["id"],)
            ).fetchone()
            note = db.execute(
                "SELECT * FROM tuning_order_notes WHERE order_id = ?", (order["id"],)
            ).fetchone()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(body, {"ok": True, "order_id": order["id"]})
        self.assertEqual(order["client_name"], "Иван Петров")
        self.assertEqual(order["phone"], "+7 921 000-00-00")
        self.assertEqual(order["boat_model"], "Finnmaster T8")
        self.assertEqual(order["status"], "new_request")
        self.assertEqual(order["source"], "tuning_site")
        self.assertEqual(order["source_ref"], "tuning_site:lead-2026-0001")
        self.assertEqual(order["sale_channel"], "direct")
        self.assertEqual(order["order_date"], "2026-08-27")
        self.assertEqual(order["subtotal"], 0)
        self.assertEqual(order["total"], 0)
        self.assertEqual(client["phone"], order["phone"])
        self.assertEqual(client["client_name"], order["client_name"])
        self.assertEqual(client["boat_model"], order["boat_model"])
        self.assertEqual(project["name"], f"Заказ №{order['id']}")
        self.assertIsNone(note["author_admin_id"])
        self.assertIn(self.payload()["message"], note["text"])
        self.assertIn(self.payload()["source_url"], note["text"])
        self.assertIn(self.payload()["submitted_at"], note["text"])

    def test_request_id_is_idempotent(self):
        first = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(),
            headers=self.auth(),
        )
        second = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(name="Другое имя", phone="+7 999 999-99-99"),
            headers=self.auth(),
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            counts = {
                table: db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                for table in ("clients", "tuning_orders", "projects", "tuning_order_notes")
            }
            order = db.execute("SELECT * FROM tuning_orders").fetchone()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.get_json(),
            {"ok": True, "duplicate": True, "order_id": first.get_json()["order_id"]},
        )
        self.assertEqual(counts, {
            "clients": 1,
            "tuning_orders": 1,
            "projects": 1,
            "tuning_order_notes": 1,
        })
        self.assertEqual(order["client_name"], "Иван Петров")

    def test_invalid_payloads_return_json_400_without_writes(self):
        cases = (
            {"data": "not json", "content_type": "text/plain"},
            {"json": {}},
            {"json": self.payload(phone=12345)},
            {"json": self.payload(request_id="x" * 129)},
            {"json": self.payload(message="x" * 5001)},
        )
        for request_kwargs in cases:
            with self.subTest(request_kwargs=request_kwargs):
                response = self.client.post(
                    "/api/integrations/tuning-leads",
                    headers=self.auth(),
                    **request_kwargs,
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.is_json)
                self.assertEqual(response.get_json()["error"], "invalid_payload")

        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM tuning_orders"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_missing_boat_model_does_not_erase_existing_client_model(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            client_id = application_module._get_or_create_client(
                db, "+7 921 000-00-00", "Старое имя", "Axopar 28"
            )
            db.commit()

        response = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(boat_model=None),
            headers=self.auth(),
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT * FROM clients WHERE id = ?", (client_id,)
            ).fetchone()
            order = db.execute("SELECT * FROM tuning_orders").fetchone()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(order["client_id"], client_id)
        self.assertEqual(order["boat_model"], "")
        self.assertEqual(client["client_name"], "Иван Петров")
        self.assertEqual(client["boat_model"], "Axopar 28")

    def test_existing_tilda_webhook_still_works(self):
        application_module.TILDA_WEBHOOK_SECRET = "tilda-test-secret"
        response = self.client.post(
            "/webhooks/tilda?token=tilda-test-secret",
            data={
                "tranid": "tilda-unchanged-1",
                "name": "Клиент Tilda",
                "phone": "+7 911 111-11-11",
                "comment": "Обратный звонок",
            },
        )

        with application_module.app.app_context():
            order = application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE source = 'tilda'"
            ).fetchone()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "ok")
        self.assertEqual(order["source_ref"], "tilda-unchanged-1")

    def test_empty_boat_catalog_is_available_from_tuning_navigation(self):
        anonymous = self.client.get("/tuning/boats")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/admin/login", anonymous.headers["Location"])

        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"
        response = self.client.get("/tuning/boats")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Каталог лодок</title>", html)
        self.assertIn(
            'href="/tuning/boats" class="active">Каталог лодок</a>', html
        )
        self.assertNotIn("Заказов всего", html)


if __name__ == "__main__":
    unittest.main()
