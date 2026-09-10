import json
import unittest

from support import application_module


class CustomerManagerAccessTests(unittest.TestCase):
    MANAGER_NAME = "Мария Менеджерова"
    MANAGER_USERNAME = "customer-manager-role-test"
    GUIDE_NAME = "Георгий Ограниченный"
    GUIDE_USERNAME = "guide-role-test"
    CAPTAIN_NAME = "Кирилл Капитанов"
    CAPTAIN_USERNAME = "captain-role-test"
    EXCURSION_TOKEN = "manager-excursion-client-test"
    TUNING_TOKEN = "manager-tuning-client-test"
    TRIPSTER_SOURCE_REF = "manager-access-tripster-unassigned"
    CAPTAIN_TRIP_SOURCE_REF = "captain-manifest-access-trip"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.manager_id, self.manager_account_id = self._create_team_member(
                db,
                self.MANAGER_NAME,
                self.MANAGER_USERNAME,
                "Менеджер по работе с клиентами",
            )
            self.guide_id, self.guide_account_id = self._create_team_member(
                db, self.GUIDE_NAME, self.GUIDE_USERNAME, "Гид"
            )
            self.captain_id, self.captain_account_id = self._create_team_member(
                db, self.CAPTAIN_NAME, self.CAPTAIN_USERNAME, "Капитан"
            )
            self.excursion_client_id = self._create_client(
                db,
                "Экскурсионный Клиент",
                self.EXCURSION_TOKEN,
                "excursion",
            )
            self.tuning_client_id = self._create_client(
                db,
                "Тюнинговый Клиент",
                self.TUNING_TOKEN,
                "tuning",
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        test_refs = (cls.TRIPSTER_SOURCE_REF, cls.CAPTAIN_TRIP_SOURCE_REF)
        db.execute(
            "DELETE FROM schedule_participants WHERE schedule_item_id IN "
            "(SELECT id FROM schedule_items WHERE source_ref IN (?, ?))",
            test_refs,
        )
        db.execute(
            "DELETE FROM schedule_assignments WHERE schedule_item_id IN "
            "(SELECT id FROM schedule_items WHERE source_ref IN (?, ?))",
            test_refs,
        )
        db.execute(
            "DELETE FROM schedule_items WHERE source_ref IN (?, ?)",
            test_refs,
        )
        db.execute(
            "DELETE FROM schedule_day_crew WHERE employee_id IN "
            "(SELECT id FROM employees WHERE name IN (?, ?, ?))",
            (cls.MANAGER_NAME, cls.GUIDE_NAME, cls.CAPTAIN_NAME),
        )
        db.execute(
            "DELETE FROM client_segments WHERE client_id IN "
            "(SELECT id FROM clients WHERE token IN (?, ?))",
            (cls.EXCURSION_TOKEN, cls.TUNING_TOKEN),
        )
        db.execute(
            "DELETE FROM clients WHERE token IN (?, ?)",
            (cls.EXCURSION_TOKEN, cls.TUNING_TOKEN),
        )
        db.execute(
            "DELETE FROM team_accounts WHERE username IN (?, ?, ?)",
            (cls.MANAGER_USERNAME, cls.GUIDE_USERNAME, cls.CAPTAIN_USERNAME),
        )
        db.execute(
            "DELETE FROM employee_positions WHERE employee_id IN "
            "(SELECT id FROM employees WHERE name IN (?, ?, ?))",
            (cls.MANAGER_NAME, cls.GUIDE_NAME, cls.CAPTAIN_NAME),
        )
        db.execute(
            "DELETE FROM employees WHERE name IN (?, ?, ?)",
            (cls.MANAGER_NAME, cls.GUIDE_NAME, cls.CAPTAIN_NAME),
        )
        db.commit()

    @staticmethod
    def _create_team_member(db, name, username, position):
        employee = db.execute(
            "INSERT INTO employees (name, created_at, deleted_at) "
            "VALUES (?, '2026-09-01 15:00', NULL)",
            (name,),
        )
        employee_id = employee.lastrowid
        db.execute(
            "INSERT INTO employee_positions (employee_id, position, created_at) "
            "VALUES (?, ?, '2026-09-01 15:00')",
            (employee_id, position),
        )
        account = db.execute(
            "INSERT INTO team_accounts "
            "(employee_id, employee_name, username, password_hash, created_at) "
            "VALUES (?, ?, ?, 'test-hash', '2026-09-01 15:00')",
            (employee_id, name, username),
        )
        return employee_id, account.lastrowid

    @staticmethod
    def _create_client(db, name, token, segment):
        client = db.execute(
            "INSERT INTO clients "
            "(client_name, boat_model, phone, token, status, created_at) "
            "VALUES (?, '', '', ?, 'neutral', '2026-09-01 15:00')",
            (name, token),
        )
        db.execute(
            "INSERT INTO client_segments (client_id, segment, created_at) "
            "VALUES (?, ?, '2026-09-01 15:00')",
            (client.lastrowid, segment),
        )
        return client.lastrowid

    def login_as_manager(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.manager_account_id
            session["team_employee_name"] = self.MANAGER_NAME
            session["team_username"] = self.MANAGER_USERNAME

    def login_as_guide(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.guide_account_id
            session["team_employee_name"] = self.GUIDE_NAME
            session["team_username"] = self.GUIDE_USERNAME

    def login_as_captain(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.captain_account_id
            session["team_employee_name"] = self.CAPTAIN_NAME
            session["team_username"] = self.CAPTAIN_USERNAME

    def test_position_is_available_in_employee_creation_list(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

        html = self.client.get("/employees").get_data(as_text=True)

        self.assertIn(
            'value="Менеджер по работе с клиентами"', html
        )

    def test_manager_cabinet_redirects_to_schedule_with_limited_navigation(self):
        self.login_as_manager()

        cabinet = self.client.get("/team/")
        self.assertEqual(cabinet.status_code, 302)
        self.assertTrue(cabinet.headers["Location"].endswith("/schedule"))

        for path, active_label in (
            ("/schedule?date=2026-09-05", "Расписание"),
            ("/admin/clients?section=tuning", "Клиенты и партнеры"),
            ("/services", "Услуги"),
        ):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Менеджер по работе с клиентами", html)
            self.assertIn('<aside class="desktop-sidebar"', html)
            self.assertIn('id="desktopSidebarToggle"', html)
            self.assertEqual(html.count('id="mainNav"'), 1)
            for label in ("Расписание", "Клиенты и партнеры", "Услуги"):
                self.assertIn(f'<span class="nav-label">{label}</span>', html)
            self.assertIn(
                f'<span class="nav-label">{active_label}</span>', html
            )
            self.assertNotIn('<span class="nav-label">Зарплаты</span>', html)
            self.assertNotIn('<span class="nav-label">Тюнинг-центр</span>', html)
            self.assertNotIn('<span class="nav-label">Флот</span>', html)
            self.assertIn('action="/team/logout"', html)

        clients_html = self.client.get(
            "/admin/clients?section=tuning"
        ).get_data(as_text=True)
        self.assertIn("Клиенты экскурсий", clients_html)
        self.assertIn("Экскурсионный Клиент", clients_html)
        self.assertNotIn("Тюнинговый Клиент", clients_html)
        self.assertNotIn("Клиенты тюнинга", clients_html)

    def test_manager_can_use_excursion_tools_but_not_other_admin_modules(self):
        self.login_as_manager()

        crew_response = self.client.post(
            "/schedule/crew",
            data={"work_date": "2026-09-05", "employee_id": "0"},
        )
        self.assertEqual(crew_response.status_code, 302)
        self.assertIn("/schedule", crew_response.headers["Location"])

        for path in ("/tuning", "/employees", "/fleet"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers["Location"])

    def test_manager_is_restricted_to_excursion_clients(self):
        self.login_as_manager()

        excursion_card = self.client.get(
            f"/admin/clients/{self.excursion_client_id}/cabinet"
        )
        excursion_html = excursion_card.get_data(as_text=True)
        self.assertEqual(excursion_card.status_code, 200)
        self.assertIn("Карточка экскурсионного клиента", excursion_html)
        self.assertIn(self.MANAGER_NAME, excursion_html)
        self.assertIn("К расписанию", excursion_html)
        self.assertNotIn("К заказам", excursion_html)

        tuning_card = self.client.get(
            f"/admin/clients/{self.tuning_client_id}/cabinet"
        )
        self.assertEqual(tuning_card.status_code, 302)
        self.assertIn("section=excursion", tuning_card.headers["Location"])

        self.client.post(
            f"/admin/clients/{self.tuning_client_id}/status",
            data={"status": "blacklisted", "section": "excursion"},
        )
        self.client.post(
            f"/admin/clients/{self.excursion_client_id}/status",
            data={"status": "satisfied", "section": "excursion"},
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            tuning_status = db.execute(
                "SELECT status FROM clients WHERE id = ?",
                (self.tuning_client_id,),
            ).fetchone()["status"]
            excursion_status = db.execute(
                "SELECT status FROM clients WHERE id = ?",
                (self.excursion_client_id,),
            ).fetchone()["status"]
        self.assertEqual(tuning_status, "neutral")
        self.assertEqual(excursion_status, "satisfied")

        self.client.post(
            f"/admin/clients/{self.tuning_client_id}/relationship",
            data={
                "section": "tuning",
                "current_relationship": "client",
                "relationship_type": "partner",
            },
        )
        self.client.post(
            f"/admin/clients/{self.excursion_client_id}/relationship",
            data={
                "section": "tuning",
                "current_relationship": "client",
                "relationship_type": "partner",
            },
        )
        with application_module.app.app_context():
            relationships = {
                row["client_id"]: row["relationship_type"]
                for row in application_module.get_db().execute(
                    "SELECT client_id, relationship_type FROM client_segments "
                    "WHERE client_id IN (?, ?)",
                    (self.tuning_client_id, self.excursion_client_id),
                ).fetchall()
            }
        self.assertEqual(relationships[self.tuning_client_id], "client")
        self.assertEqual(relationships[self.excursion_client_id], "partner")
        partner_directory = self.client.get(
            "/admin/clients?relationship=partner"
        ).get_data(as_text=True)
        self.assertIn("Экскурсионный Клиент", partner_directory)
        self.assertNotIn("Тюнинговый Клиент", partner_directory)

    def test_manager_can_set_sales_channel_only_for_excursion_client(self):
        self.login_as_manager()

        card = self.client.get(
            f"/admin/clients/{self.excursion_client_id}/cabinet"
        )
        html = card.get_data(as_text=True)
        self.assertIn("Канал продаж", html)
        self.assertIn('<option value="tripster"', html)
        self.assertIn('<option value="sputnik"', html)
        self.assertIn('<option value="bodrbo_fort"', html)
        self.assertIn("Сайт bodrbo-fort.ru", html)

        response = self.client.post(
            f"/admin/clients/{self.excursion_client_id}/acquisition-channel",
            data={"acquisition_channel": "sputnik"},
        )
        self.assertEqual(response.status_code, 302)
        self.client.post(
            f"/admin/clients/{self.tuning_client_id}/acquisition-channel",
            data={"acquisition_channel": "tripster"},
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            excursion_channel = db.execute(
                "SELECT acquisition_channel FROM clients WHERE id = ?",
                (self.excursion_client_id,),
            ).fetchone()["acquisition_channel"]
            tuning_channel = db.execute(
                "SELECT acquisition_channel FROM clients WHERE id = ?",
                (self.tuning_client_id,),
            ).fetchone()["acquisition_channel"]
        self.assertEqual(excursion_channel, "sputnik")
        self.assertEqual(tuning_channel, "")

    def test_guide_can_view_schedule_but_cannot_manage_it(self):
        self.login_as_guide()

        schedule = self.client.get("/schedule?date=2026-09-05")
        schedule_html = schedule.get_data(as_text=True)
        self.assertEqual(schedule.status_code, 200)
        self.assertIn("Расписание рейсов", schedule_html)
        self.assertIn("Режим просмотра", schedule_html)
        self.assertNotIn("schedule-create-button", schedule_html)
        self.assertNotIn("schedule-tripster-button", schedule_html)
        self.assertNotIn("schedule-day-total", schedule_html)
        self.assertNotIn("const scheduleItems = [{", schedule_html)
        self.assertNotIn("startScheduleDrag(event, this)", schedule_html)

        cabinet_html = self.client.get("/team/").get_data(as_text=True)
        self.assertIn('id="team-schedule"', cabinet_html)
        self.assertIn('href="/schedule"', cabinet_html)

        for path in ("/services", "/admin/clients"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/team/"))

        for path, method in (
            ("/schedule/clients/search?q=ал", "get"),
            ("/schedule/crew", "post"),
            ("/schedule/items", "post"),
            ("/schedule/tripster/sync", "post"),
        ):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/team/"))

    def test_captain_can_open_readonly_trip_manifest(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, capacity, "
                "participants_count, customer_name, customer_phone, revenue, "
                "note, status, source, source_ref, created_at, updated_at) "
                "VALUES ('event', 'Бодрый Первый', 'Большой тур', "
                "'2026-09-05 13:00', '2026-09-05 15:30', 10, 3, '', '', "
                "33300, 'Служебная заметка', 'scheduled', 'internal', ?, "
                "'2026-09-01 15:00', '2026-09-01 15:00')",
                (self.CAPTAIN_TRIP_SOURCE_REF,),
            )
            item_id = item.lastrowid
            db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, ?, 'captain', '2026-09-01 15:00')",
                (item_id, self.captain_id, self.CAPTAIN_NAME),
            )
            db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, "
                "guests_count, price, prepayment, payment_due, created_at) "
                "VALUES (?, ?, 'Экскурсионный Клиент', '+79990001122', "
                "3, 33300, 3000, 30300, '2026-09-01 15:00')",
                (item_id, self.excursion_client_id),
            )
            db.execute(
                "INSERT INTO schedule_day_crew "
                "(work_date, employee_id, created_at) VALUES "
                "('2026-09-05', ?, '2026-09-01 15:00')",
                (self.captain_id,),
            )
            db.commit()

        self.login_as_captain()
        response = self.client.get("/schedule?date=2026-09-05")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="scheduleViewModal"', html)
        self.assertIn(f"openScheduleView({item_id})", html)
        payload = html.split("const scheduleItems = ", 1)[1].split(";", 1)[0]
        items = json.loads(payload)
        manifest = next(item for item in items if item["id"] == item_id)
        self.assertEqual(manifest["participants_count"], 3)
        self.assertEqual(manifest["participants"], [{
            "client_name": "Экскурсионный Клиент",
            "client_phone": "+79990001122",
            "guests_count": 3,
        }])
        self.assertEqual(manifest["assignments"], [{
            "employee_name": self.CAPTAIN_NAME,
            "role_label": "Капитан",
        }])
        for private_field in ("revenue", "note", "price", "prepayment", "payment_due"):
            self.assertNotIn(private_field, json.dumps(manifest, ensure_ascii=False))

        self.login_as_guide()
        guide_html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertNotIn('id="scheduleViewModal"', guide_html)
        self.assertNotIn(
            'onclick="event.stopPropagation(); openScheduleView', guide_html
        )
        guide_payload = guide_html.split(
            "const scheduleItems = ", 1
        )[1].split(";", 1)[0]
        self.assertEqual(json.loads(guide_payload), [])

    def test_unassigned_tripster_orders_are_hidden_from_crew_only(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, capacity, "
                "participants_count, customer_name, customer_phone, revenue, "
                "note, status, source, source_ref, created_at, updated_at) "
                "VALUES ('event', 'Не назначен', 'Скрытая заявка Tripster', "
                "'2026-09-05 13:00', '2026-09-05 14:00', 10, 2, '', '', 0, "
                "'', 'scheduled', 'tripster', ?, "
                "'2026-09-01 15:00', '2026-09-01 15:00')",
                (self.TRIPSTER_SOURCE_REF,),
            )
            db.commit()

        self.login_as_guide()
        crew_html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertNotIn("Скрытая заявка Tripster", crew_html)
        self.assertNotIn("Не назначено", crew_html)

        self.login_as_manager()
        manager_html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("Скрытая заявка Tripster", manager_html)
        self.assertIn("Не назначено", manager_html)


if __name__ == "__main__":
    unittest.main()
