import unittest

from support import application_module


class PartnerContactEditingTests(unittest.TestCase):
    PARTNER_TOKEN = "partner-contact-edit-test"
    CONFLICT_TOKEN = "partner-contact-conflict-test"
    SOURCE_REF = "partner-contact-edit:order"
    STARTS_AT = "2026-09-19 09:10"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.partner_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at, email, comment) "
                "VALUES ('Старая верфь', '', '+7 900 100-20-30', ?, 'neutral', "
                "'2026-09-08 09:00', 'old@example.ru', 'Старый комментарий')",
                (self.PARTNER_TOKEN,),
            ).lastrowid
            for segment in ("tuning", "excursion"):
                db.execute(
                    "INSERT INTO client_segments "
                    "(client_id, segment, relationship_type, created_at) "
                    "VALUES (?, ?, 'partner', '2026-09-08 09:00')",
                    (self.partner_id, segment),
                )
            db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Другой контакт', '', '+7 999 555-44-33', ?, 'neutral', "
                "'2026-09-08 09:01')",
                (self.CONFLICT_TOKEN,),
            )
            self.order_id = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, boat_model, sale_channel, phone, subtotal, "
                "total, status, source, source_ref, created_at, updated_at) "
                "VALUES (?, 'Старая верфь', '', 'direct', '+7 900 100-20-30', 0, "
                "0, 'estimate', 'manual', ?, '2026-09-08 09:05', '2026-09-08 09:05')",
                (self.partner_id, self.SOURCE_REF),
            ).lastrowid
            self.schedule_item_id = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, customer_name, "
                "customer_phone, created_at, updated_at) VALUES "
                "('booking', 'Ларус', 'Аренда', ?, '2026-09-19 10:10', "
                "'Старая верфь', '+7 900 100-20-30', '2026-09-08 09:06', "
                "'2026-09-08 09:06')",
                (self.STARTS_AT,),
            ).lastrowid
            db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, created_at) "
                "VALUES (?, ?, 'Старая верфь', '+7 900 100-20-30', "
                "'2026-09-08 09:06')",
                (self.schedule_item_id, self.partner_id),
            )
            self.sheet_id = db.execute(
                "INSERT INTO field_diagnostic_sheets "
                "(boat_model, owner_client_id, owner_name, owner_phone, "
                "inspection_type, status, created_by_name, started_at) VALUES "
                "('Тестовая лодка', ?, 'Старая верфь', '+7 900 100-20-30', "
                "'water', 'in_progress', 'Мастер', '2026-09-08 09:07')",
                (self.partner_id,),
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
        item_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM schedule_items WHERE starts_at = ?",
                (cls.STARTS_AT,),
            ).fetchall()
        ]
        for item_id in item_ids:
            db.execute(
                "DELETE FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            )
            db.execute("DELETE FROM schedule_items WHERE id = ?", (item_id,))
        db.execute("DELETE FROM tuning_orders WHERE source_ref = ?", (cls.SOURCE_REF,))

        partner = db.execute(
            "SELECT id FROM clients WHERE token = ?", (cls.PARTNER_TOKEN,)
        ).fetchone()
        if partner is not None:
            partner_id = partner["id"]
            db.execute(
                "DELETE FROM field_diagnostic_sheets WHERE owner_client_id = ?",
                (partner_id,),
            )
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (partner_id,))
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?)",
            (cls.PARTNER_TOKEN, cls.CONFLICT_TOKEN),
        )
        db.commit()

    def test_editor_is_available_for_tuning_and_excursion_partners(self):
        tuning_html = self.http.get(
            f"/admin/clients/{self.partner_id}/cabinet",
            query_string={"section": "tuning"},
        ).get_data(as_text=True)
        excursion_html = self.http.get(
            f"/admin/clients/{self.partner_id}/cabinet",
            query_string={"section": "excursion"},
        ).get_data(as_text=True)

        self.assertIn("Данные и оформление партнёра", tuning_html)
        self.assertIn('name="client_name"', tuning_html)
        self.assertIn('name="phone"', tuning_html)
        self.assertIn('name="email"', tuning_html)
        self.assertIn('name="comment"', tuning_html)
        self.assertIn('name="partner_logo"', tuning_html)
        self.assertIn("Данные партнёра", excursion_html)
        self.assertIn('name="client_name"', excursion_html)
        self.assertNotIn('name="partner_logo"', excursion_html)

    def test_update_synchronizes_all_linked_operational_records(self):
        response = self.http.post(
            f"/admin/clients/{self.partner_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "Новая северная верфь",
                "phone": "+7 (921) 777-88-99",
                "email": "office@new-yard.ru",
                "comment": "Связываться с отделом сервиса",
                "partner_title": "Сервисный партнёр",
            },
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT client_name, phone, email, comment FROM clients WHERE id = ?",
                (self.partner_id,),
            ).fetchone()
            order = db.execute(
                "SELECT client_name, phone FROM tuning_orders WHERE id = ?",
                (self.order_id,),
            ).fetchone()
            participant = db.execute(
                "SELECT client_name, client_phone FROM schedule_participants "
                "WHERE schedule_item_id = ?",
                (self.schedule_item_id,),
            ).fetchone()
            schedule_item = db.execute(
                "SELECT customer_name, customer_phone FROM schedule_items WHERE id = ?",
                (self.schedule_item_id,),
            ).fetchone()
            sheet = db.execute(
                "SELECT owner_name, owner_phone FROM field_diagnostic_sheets WHERE id = ?",
                (self.sheet_id,),
            ).fetchone()

        expected_identity = ("Новая северная верфь", "+7 (921) 777-88-99")
        self.assertEqual(
            tuple(client),
            (*expected_identity, "office@new-yard.ru", "Связываться с отделом сервиса"),
        )
        self.assertEqual(tuple(order), expected_identity)
        self.assertEqual(tuple(participant), expected_identity)
        self.assertEqual(tuple(schedule_item), expected_identity)
        self.assertEqual(tuple(sheet), expected_identity)

    def test_conflicting_phone_is_rejected_and_form_values_are_preserved(self):
        response = self.http.post(
            f"/admin/clients/{self.partner_id}/partner-profile",
            data={
                "section": "excursion",
                "client_name": "Новое введённое название",
                "phone": "8 999 555 44 33",
                "email": "draft@example.ru",
                "comment": "Черновик",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn("Телефон уже принадлежит контакту", html)
        self.assertIn('value="Новое введённое название"', html)
        self.assertIn('value="draft@example.ru"', html)
        with application_module.app.app_context():
            current = application_module.get_db().execute(
                "SELECT client_name, phone FROM clients WHERE id = ?",
                (self.partner_id,),
            ).fetchone()
        self.assertEqual(tuple(current), ("Старая верфь", "+7 900 100-20-30"))

    def test_invalid_contact_fields_are_rejected(self):
        response = self.http.post(
            f"/admin/clients/{self.partner_id}/partner-profile",
            data={
                "section": "tuning",
                "client_name": "",
                "phone": "123",
                "email": "wrong-address",
            },
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn("Укажите название или имя партнёра", html)
        self.assertIn("Проверьте номер телефона", html)
        self.assertIn("Проверьте адрес электронной почты", html)


if __name__ == "__main__":
    unittest.main()
