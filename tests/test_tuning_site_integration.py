import unittest

from support import application_module


class TuningSiteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()

    def setUp(self):
        self.client = application_module.app.test_client()
        self.original_secret = application_module.TUNING_SITE_WEBHOOK_SECRET
        application_module.TUNING_SITE_WEBHOOK_SECRET = "integration-test-secret"
        self.request_ids = []
        self.phones = []

    def tearDown(self):
        application_module.TUNING_SITE_WEBHOOK_SECRET = self.original_secret
        with application_module.app.app_context():
            db = application_module.get_db()
            if self.request_ids:
                refs = [f"tuning_site:{request_id}" for request_id in self.request_ids]
                placeholders = ",".join("?" for _ in refs)
                order_rows = db.execute(
                    f"SELECT id FROM tuning_orders WHERE source_ref IN ({placeholders})",
                    refs,
                ).fetchall()
                order_ids = [row["id"] for row in order_rows]
                if order_ids:
                    order_placeholders = ",".join("?" for _ in order_ids)
                    db.execute(
                        f"DELETE FROM tuning_order_notes WHERE order_id IN ({order_placeholders})",
                        order_ids,
                    )
                    db.execute(
                        f"DELETE FROM projects WHERE tuning_order_id IN ({order_placeholders})",
                        order_ids,
                    )
                    db.execute(
                        f"DELETE FROM tuning_orders WHERE id IN ({order_placeholders})",
                        order_ids,
                    )
            if self.phones:
                placeholders = ",".join("?" for _ in self.phones)
                db.execute(
                    f"DELETE FROM clients WHERE phone IN ({placeholders})", self.phones
                )
            db.commit()

    def headers(self, token="integration-test-secret"):
        return {"Authorization": f"Bearer {token}"}

    def payload(self, request_id="lead-test-001", phone="+7 900 100-20-30"):
        self.request_ids.append(request_id)
        self.phones.append(phone)
        return {
            "request_id": request_id,
            "name": "Александр Тестов",
            "phone": phone,
            "boat_model": "Axopar 28",
            "message": "Установить дополнительное освещение",
            "source_url": "https://tuning.bodrbo.ru/#request",
            "submitted_at": "2026-08-27T10:00:00+03:00",
        }

    def test_integration_is_closed_when_secret_is_missing(self):
        application_module.TUNING_SITE_WEBHOOK_SECRET = ""

        response = self.client.post(
            "/api/integrations/tuning-leads", json=self.payload()
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "integration_not_configured")

    def test_wrong_secret_is_rejected(self):
        response = self.client.post(
            "/api/integrations/tuning-leads",
            json=self.payload(),
            headers=self.headers("wrong-secret"),
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_valid_lead_creates_client_order_project_and_note(self):
        payload = self.payload("lead-create-001", "+7 900 100-20-31")

        response = self.client.post(
            "/api/integrations/tuning-leads",
            json=payload,
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 201)
        response_json = response.get_json()
        self.assertTrue(response_json["ok"])
        order_id = response_json["order_id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute(
                "SELECT * FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()
            client = db.execute(
                "SELECT * FROM clients WHERE id = ?", (order["client_id"],)
            ).fetchone()
            project = db.execute(
                "SELECT * FROM projects WHERE tuning_order_id = ?", (order_id,)
            ).fetchone()
            note = db.execute(
                "SELECT text FROM tuning_order_notes WHERE order_id = ?", (order_id,)
            ).fetchone()

        self.assertEqual(order["client_name"], payload["name"])
        self.assertEqual(order["phone"], payload["phone"])
        self.assertEqual(order["boat_model"], payload["boat_model"])
        self.assertEqual(order["status"], "new_request")
        self.assertEqual(order["source"], "tuning_site")
        self.assertEqual(order["source_ref"], "tuning_site:lead-create-001")
        self.assertEqual(client["boat_model"], payload["boat_model"])
        self.assertEqual(project["name"], f"Заказ №{order_id}")
        self.assertIn(payload["message"], note["text"])
        self.assertIn(payload["source_url"], note["text"])
        self.assertIn(payload["submitted_at"], note["text"])

    def test_repeated_request_id_returns_existing_order(self):
        payload = self.payload("lead-repeat-001", "+7 900 100-20-32")

        first = self.client.post(
            "/api/integrations/tuning-leads", json=payload, headers=self.headers()
        )
        repeated = self.client.post(
            "/api/integrations/tuning-leads", json=payload, headers=self.headers()
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(repeated.status_code, 200)
        repeated_json = repeated.get_json()
        self.assertTrue(repeated_json["duplicate"])
        self.assertEqual(repeated_json["order_id"], first.get_json()["order_id"])
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS n FROM tuning_orders WHERE source_ref = ?",
                ("tuning_site:lead-repeat-001",),
            ).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_invalid_payload_is_rejected_without_creating_order(self):
        response = self.client.post(
            "/api/integrations/tuning-leads",
            json={"request_id": "lead-invalid-001", "name": "", "phone": ""},
            headers=self.headers(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "invalid_payload")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS n FROM tuning_orders WHERE source_ref = ?",
                ("tuning_site:lead-invalid-001",),
            ).fetchone()["n"]
        self.assertEqual(count, 0)

    def test_empty_boat_model_does_not_erase_existing_client_model(self):
        phone = "+7 900 100-20-33"
        request_id = "lead-existing-client-001"
        self.request_ids.append(request_id)
        self.phones.append(phone)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Старый клиент", "Finnmaster T8", phone, "existing-client-token", "2026-08-27 09:00"),
            )
            db.commit()

        payload = {
            "request_id": request_id,
            "name": "Обновлённое имя",
            "phone": phone,
            "boat_model": "",
            "message": "Новая заявка",
        }
        response = self.client.post(
            "/api/integrations/tuning-leads", json=payload, headers=self.headers()
        )

        self.assertEqual(response.status_code, 201)
        with application_module.app.app_context():
            client = application_module.get_db().execute(
                "SELECT client_name, boat_model FROM clients WHERE phone = ?", (phone,)
            ).fetchone()
        self.assertEqual(client["client_name"], "Обновлённое имя")
        self.assertEqual(client["boat_model"], "Finnmaster T8")


if __name__ == "__main__":
    unittest.main()
