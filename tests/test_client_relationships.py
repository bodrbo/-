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
            "(SELECT id FROM clients WHERE token IN (?, ?) "
            "OR client_name LIKE 'Ручной тест%')",
            (cls.TOKEN, cls.SECOND_TOKEN),
        )
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?) "
            "OR client_name LIKE 'Ручной тест%'",
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

    def test_admin_manually_creates_client_and_partner_in_selected_sections(self):
        client_response = self.client.post(
            "/admin/clients/create",
            data={
                "section": "excursion",
                "relationship": "client",
                "client_name": "Ручной тест Турист",
                "phone": "",
                "email": "tourist@example.ru",
                "comment": "Позвонил сам",
            },
        )
        partner_response = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "partner",
                "client_name": "Ручной тест Верфь",
                "phone": "+7 (999) 500-00-01",
                "email": "partner@example.ru",
            },
        )

        self.assertEqual(client_response.status_code, 302)
        self.assertEqual(partner_response.status_code, 302)
        partner_query = parse_qs(urlparse(partner_response.headers["Location"]).query)
        self.assertEqual(partner_query["section"], ["tuning"])
        self.assertEqual(partner_query["relationship"], ["partner"])
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT c.client_name, c.phone, c.email, c.comment, c.token, "
                "cs.segment, cs.relationship_type FROM clients c "
                "JOIN client_segments cs ON cs.client_id = c.id "
                "WHERE c.client_name LIKE 'Ручной тест%' ORDER BY c.client_name"
            ).fetchall()
        by_name = {row["client_name"]: row for row in rows}
        self.assertEqual(len(rows), 2)
        self.assertEqual(by_name["Ручной тест Турист"]["segment"], "excursion")
        self.assertEqual(by_name["Ручной тест Турист"]["relationship_type"], "client")
        self.assertEqual(by_name["Ручной тест Турист"]["comment"], "Позвонил сам")
        self.assertEqual(by_name["Ручной тест Верфь"]["segment"], "tuning")
        self.assertEqual(by_name["Ручной тест Верфь"]["relationship_type"], "partner")
        self.assertTrue(all(row["token"] for row in rows))

    def test_manual_creation_reuses_same_named_phone_across_segments(self):
        first = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "client",
                "client_name": "Ручной тест Общий",
                "phone": "+7 999 700-10-20",
            },
        )
        second = self.client.post(
            "/admin/clients/create",
            data={
                "section": "excursion",
                "relationship": "partner",
                "client_name": "  Ручной   тест Общий  ",
                "phone": "8 (999) 700-10-20",
                "email": "shared@example.ru",
            },
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            contacts = db.execute(
                "SELECT id, email FROM clients WHERE client_name = ?",
                ("Ручной тест Общий",),
            ).fetchall()
            memberships = db.execute(
                "SELECT segment, relationship_type FROM client_segments "
                "WHERE client_id = ? ORDER BY segment",
                (contacts[0]["id"],),
            ).fetchall()
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["email"], "shared@example.ru")
        self.assertEqual(
            [(row["segment"], row["relationship_type"]) for row in memberships],
            [("excursion", "partner"), ("tuning", "client")],
        )

    def test_manual_creation_rejects_conflicting_phone_and_keeps_form(self):
        first = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "client",
                "client_name": "Ручной тест Первый",
                "phone": "+7 999 800-30-40",
            },
        )
        conflict = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "partner",
                "client_name": "Ручной тест Другой",
                "phone": "8 999 800 30 40",
            },
            follow_redirects=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(conflict.status_code, 200)
        html = conflict.get_data(as_text=True)
        self.assertIn("Телефон уже принадлежит контакту", html)
        self.assertIn('value="Ручной тест Другой"', html)
        self.assertIn('data-auto-open="true"', html)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM clients "
                "WHERE client_name LIKE 'Ручной тест%'"
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_manual_creation_validates_required_name_phone_and_email(self):
        response = self.client.post(
            "/admin/clients/create",
            data={
                "section": "excursion",
                "relationship": "client",
                "client_name": "",
                "phone": "123",
                "email": "wrong-address",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn("Укажите имя клиента или название партнёра", html)
        self.assertIn("Проверьте номер телефона", html)
        self.assertIn("Проверьте адрес электронной почты", html)

    def test_customer_manager_can_only_create_excursion_contacts(self):
        with application_module.app.app_context():
            account = application_module.get_db().execute(
                "SELECT ta.id, ta.employee_id FROM team_accounts ta "
                "JOIN employees e ON e.id = ta.employee_id "
                "JOIN employee_positions ep ON ep.employee_id = e.id "
                "WHERE ep.position = ? AND e.deleted_at IS NULL LIMIT 1",
                ("Менеджер по работе с клиентами",),
            ).fetchone()
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = account["id"]
            session["team_employee_name"] = "Менеджер"
            session["team_username"] = "manual-client-manager-test"

        response = self.client.post(
            "/admin/clients/create",
            data={
                "section": "tuning",
                "relationship": "partner",
                "client_name": "Ручной тест Менеджер",
                "phone": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            membership = application_module.get_db().execute(
                "SELECT cs.segment, cs.relationship_type FROM client_segments cs "
                "JOIN clients c ON c.id = cs.client_id WHERE c.client_name = ?",
                ("Ручной тест Менеджер",),
            ).fetchone()
        self.assertEqual(membership["segment"], "excursion")
        self.assertEqual(membership["relationship_type"], "partner")


if __name__ == "__main__":
    unittest.main()
