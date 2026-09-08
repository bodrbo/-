import unittest
from urllib.parse import parse_qs, urlparse

from support import application_module
from modules.schedule.repository import search_clients


class ClientRelationshipTests(unittest.TestCase):
    TOKEN = "client-relationship-shared-test"
    SECOND_TOKEN = "client-relationship-partner-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Общий Контакт', '', '+79990001122', ?, 'neutral', ?) ",
                (self.TOKEN, "2026-09-08 12:00"),
            )
            self.shared_client_id = cursor.lastrowid
            for segment in ("tuning", "excursion"):
                db.execute(
                    "INSERT INTO client_segments (client_id, segment, created_at) "
                    "VALUES (?, ?, ?)",
                    (self.shared_client_id, segment, "2026-09-08 12:00"),
                )
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Экскурсионный Партнер', '', '', ?, 'neutral', ?)",
                (self.SECOND_TOKEN, "2026-09-08 12:05"),
            )
            self.partner_client_id = cursor.lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'excursion', 'partner', ?)",
                (self.partner_client_id, "2026-09-08 12:05"),
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
        db.execute(
            "DELETE FROM client_segments WHERE client_id IN "
            "(SELECT id FROM clients WHERE token IN (?, ?))",
            (cls.TOKEN, cls.SECOND_TOKEN),
        )
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?)",
            (cls.TOKEN, cls.SECOND_TOKEN),
        )
        db.commit()

    def test_directory_has_four_subsections_and_separates_partners(self):
        client_html = self.client.get("/admin/clients").get_data(as_text=True)
        partner_html = self.client.get(
            "/admin/clients",
            query_string={"section": "excursion", "relationship": "partner"},
        ).get_data(as_text=True)

        self.assertIn("Клиенты и партнеры", client_html)
        for label in (
            "Клиенты тюнинга",
            "Партнеры тюнинга",
            "Клиенты экскурсий",
            "Партнеры экскурсий",
        ):
            self.assertIn(label, client_html)
        self.assertIn("Общий Контакт", client_html)
        self.assertNotIn("Экскурсионный Партнер", client_html)
        self.assertIn("Экскурсионный Партнер", partner_html)
        self.assertNotIn("Общий Контакт", partner_html)

    def test_relationship_change_is_scoped_to_business_segment(self):
        response = self.client.post(
            f"/admin/clients/{self.shared_client_id}/relationship",
            data={
                "section": "tuning",
                "current_relationship": "client",
                "relationship_type": "partner",
                "q": "Общий Контакт",
            },
        )

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["section"], ["tuning"])
        self.assertEqual(query["relationship"], ["partner"])
        self.assertEqual(query["q"], ["Общий Контакт"])
        with application_module.app.app_context():
            db = application_module.get_db()
            memberships = {
                row["segment"]: row["relationship_type"]
                for row in db.execute(
                    "SELECT segment, relationship_type FROM client_segments "
                    "WHERE client_id = ?",
                    (self.shared_client_id,),
                ).fetchall()
            }
            client_still_exists = db.execute(
                "SELECT 1 FROM clients WHERE id = ?", (self.shared_client_id,)
            ).fetchone()
            tuning_choice_ids = {
                row["id"] for row in application_module._tuning_client_choices(db)
            }
            excursion_choice_ids = {
                row["id"] for row in search_clients(db, "Общий Контакт")
            }
            excursion_partner_choices = search_clients(
                db, "Экскурсионный Партнер"
            )
        self.assertEqual(memberships, {"tuning": "partner", "excursion": "client"})
        self.assertIsNotNone(client_still_exists)
        self.assertNotIn(self.shared_client_id, tuning_choice_ids)
        self.assertIn(self.shared_client_id, excursion_choice_ids)
        self.assertEqual(excursion_partner_choices, [])

        tuning_partner_html = self.client.get(
            "/admin/clients",
            query_string={
                "section": "tuning",
                "relationship": "partner",
                "q": "Общий Контакт",
            },
        ).get_data(as_text=True)
        excursion_client_html = self.client.get(
            "/admin/clients",
            query_string={"section": "excursion", "q": "Общий Контакт"},
        ).get_data(as_text=True)
        self.assertIn("Общий Контакт", tuning_partner_html)
        self.assertIn("Общий Контакт", excursion_client_html)

    def test_new_segment_membership_defaults_to_client(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            relationship_type = db.execute(
                "SELECT relationship_type FROM client_segments "
                "WHERE client_id = ? AND segment = 'tuning'",
                (self.shared_client_id,),
            ).fetchone()["relationship_type"]

        self.assertEqual(relationship_type, "client")


if __name__ == "__main__":
    unittest.main()
