import unittest

from support import application_module


class CustomerManagerAccessTests(unittest.TestCase):
    MANAGER_NAME = "Мария Менеджерова"
    MANAGER_USERNAME = "customer-manager-role-test"
    GUIDE_NAME = "Георгий Ограниченный"
    GUIDE_USERNAME = "guide-role-test"
    EXCURSION_TOKEN = "manager-excursion-client-test"
    TUNING_TOKEN = "manager-tuning-client-test"

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
            "DELETE FROM team_accounts WHERE username IN (?, ?)",
            (cls.MANAGER_USERNAME, cls.GUIDE_USERNAME),
        )
        db.execute(
            "DELETE FROM employee_positions WHERE employee_id IN "
            "(SELECT id FROM employees WHERE name IN (?, ?))",
            (cls.MANAGER_NAME, cls.GUIDE_NAME),
        )
        db.execute(
            "DELETE FROM employees WHERE name IN (?, ?)",
            (cls.MANAGER_NAME, cls.GUIDE_NAME),
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
            ("/admin/clients?section=tuning", "Клиенты"),
            ("/services", "Услуги"),
        ):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Менеджер по работе с клиентами", html)
            for label in ("Расписание", "Клиенты", "Услуги"):
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

    def test_other_team_roles_cannot_open_manager_sections(self):
        self.login_as_guide()

        for path in ("/schedule", "/services", "/admin/clients"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.headers["Location"].endswith("/team/"))


if __name__ == "__main__":
    unittest.main()
