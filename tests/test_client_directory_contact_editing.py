import unittest

from support import application_module


class ClientDirectoryContactEditingTests(unittest.TestCase):
    CLIENT_TOKEN = "client-contact-edit-test"
    CONFLICT_TOKEN = "client-contact-conflict-test"
    SOURCE_REF = "client-contact-edit:order"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at, email, comment) "
                "VALUES ('Иван Петров', '', '+7 900 111-22-33', ?, 'neutral', "
                "'2026-09-08 09:00', 'old@example.ru', 'Старый комментарий')",
                (self.CLIENT_TOKEN,),
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'client', '2026-09-08 09:00')",
                (self.client_id,),
            )
            db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Другой контакт', '', '+7 999 444-55-66', ?, 'neutral', "
                "'2026-09-08 09:01')",
                (self.CONFLICT_TOKEN,),
            )
            self.order_id = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, boat_model, sale_channel, phone, subtotal, "
                "total, status, source, source_ref, created_at, updated_at) "
                "VALUES (?, 'Иван Петров', '', 'direct', '+7 900 111-22-33', 0, "
                "0, 'estimate', 'manual', ?, '2026-09-08 09:05', '2026-09-08 09:05')",
                (self.client_id, self.SOURCE_REF),
            ).lastrowid
            db.commit()
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    @classmethod
    def _clear(cls, db):
        db.execute("DELETE FROM tuning_orders WHERE source_ref = ?", (cls.SOURCE_REF,))
        client = db.execute(
            "SELECT id FROM clients WHERE token = ?", (cls.CLIENT_TOKEN,)
        ).fetchone()
        if client is not None:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (client["id"],))
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?)",
            (cls.CLIENT_TOKEN, cls.CONFLICT_TOKEN),
        )
        db.commit()

    def test_editor_is_available_for_regular_clients(self):
        html = self.http.get(
            f"/admin/clients/{self.client_id}/cabinet",
            query_string={"section": "tuning"},
        ).get_data(as_text=True)

        self.assertIn("Данные клиента", html)
        self.assertIn('name="client_name"', html)
        self.assertIn('name="phone"', html)
        self.assertIn('name="email"', html)
        self.assertIn('name="comment"', html)
        self.assertNotIn('name="partner_logo"', html)
        self.assertNotIn('name="partner_title"', html)

    def test_update_changes_client_and_linked_order(self):
        response = self.http.post(
            f"/admin/clients/{self.client_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "Иван Сидоров",
                "phone": "+7 (921) 000-11-22",
                "email": "new@example.ru",
                "comment": "Новый комментарий",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT client_name, phone, email, comment FROM clients WHERE id = ?",
                (self.client_id,),
            ).fetchone()
            order = db.execute(
                "SELECT client_name, phone FROM tuning_orders WHERE id = ?",
                (self.order_id,),
            ).fetchone()

        expected_identity = ("Иван Сидоров", "+7 (921) 000-11-22")
        self.assertEqual(
            tuple(client),
            (*expected_identity, "new@example.ru", "Новый комментарий"),
        )
        self.assertEqual(tuple(order), expected_identity)

    def test_partner_only_fields_are_ignored_for_regular_clients(self):
        response = self.http.post(
            f"/admin/clients/{self.client_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "Иван Петров",
                "phone": "+7 900 111-22-33",
                "email": "old@example.ru",
                "comment": "Старый комментарий",
                "partner_title": "Пытаюсь притвориться партнёром",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            membership = application_module.get_db().execute(
                "SELECT partner_title FROM client_segments WHERE client_id = ? AND segment = 'tuning'",
                (self.client_id,),
            ).fetchone()
        self.assertFalse(membership["partner_title"])

    def test_conflicting_phone_is_rejected(self):
        response = self.http.post(
            f"/admin/clients/{self.client_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "Новое имя",
                "phone": "8 999 444 55 66",
                "email": "draft@example.ru",
                "comment": "Черновик",
            },
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("Телефон уже принадлежит контакту", html)
        with application_module.app.app_context():
            current = application_module.get_db().execute(
                "SELECT client_name, phone FROM clients WHERE id = ?",
                (self.client_id,),
            ).fetchone()
        self.assertEqual(tuple(current), ("Иван Петров", "+7 900 111-22-33"))

    def test_invalid_fields_are_rejected(self):
        response = self.http.post(
            f"/admin/clients/{self.client_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "",
                "phone": "123",
                "email": "wrong-address",
            },
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn("Проверьте номер телефона", html)
        self.assertIn("Проверьте адрес электронной почты", html)


if __name__ == "__main__":
    unittest.main()
