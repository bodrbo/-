import unittest

from support import application_module


class SoftwareRequestTests(unittest.TestCase):
    ADMIN_USERNAME = "software-request-admin-test"
    EMPLOYEE_NAME = "Сотрудник Обратной Связи"
    TEAM_USERNAME = "software-request-team-test"
    DESCRIPTION_PREFIX = "[software-request-test]"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.admin_id = db.execute(
                "INSERT INTO admin_accounts "
                "(admin_name, username, password_hash, created_at) "
                "VALUES ('Тестовый администратор', ?, 'test-hash', '2026-09-04 10:00')",
                (self.ADMIN_USERNAME,),
            ).lastrowid
            self.employee_id = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-09-04 10:00', NULL)",
                (self.EMPLOYEE_NAME,),
            ).lastrowid
            self.team_id = db.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, 'test-hash', '2026-09-04 10:00')",
                (self.employee_id, self.EMPLOYEE_NAME, self.TEAM_USERNAME),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    @classmethod
    def _clear(cls, db):
        db.execute(
            "DELETE FROM software_requests WHERE description LIKE ?",
            (f"{cls.DESCRIPTION_PREFIX}%",),
        )
        db.execute("DELETE FROM team_accounts WHERE username = ?", (cls.TEAM_USERNAME,))
        db.execute("DELETE FROM employee_positions WHERE employee_id IN (SELECT id FROM employees WHERE name = ?)", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM admin_accounts WHERE username = ?", (cls.ADMIN_USERNAME,))
        db.commit()

    def login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = self.admin_id
            session["admin_name"] = "Тестовый администратор"

    def login_team(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.team_id
            session["team_employee_name"] = self.EMPLOYEE_NAME
            session["team_username"] = self.TEAM_USERNAME

    def test_widget_and_submission_require_staff_login(self):
        self.assertEqual(self.client.get("/software-requests/widget").status_code, 204)
        response = self.client.post(
            "/software-requests", json={"description": "Тест"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["ok"], False)

    def test_admin_can_submit_request_with_page_context(self):
        self.login_admin()
        widget = self.client.get("/software-requests/widget")
        self.assertEqual(widget.status_code, 200)
        self.assertIn("Заявка на доработку", widget.get_data(as_text=True))
        self.assertIn("no-store", widget.headers["Cache-Control"])

        description = f"{self.DESCRIPTION_PREFIX} Не сохраняется рейс"
        response = self.client.post(
            "/software-requests",
            json={"description": f"  {description}  ", "page_path": "/schedule"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["ok"])
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM software_requests WHERE id = ?",
                (response.get_json()["request_id"],),
            ).fetchone()
        self.assertEqual(row["author_type"], "admin")
        self.assertEqual(row["author_admin_id"], self.admin_id)
        self.assertIsNone(row["author_employee_id"])
        self.assertEqual(row["author_name"], "Тестовый администратор")
        self.assertEqual(row["description"], description)
        self.assertEqual(row["page_path"], "/schedule")
        self.assertEqual(row["status"], "new")

    def test_team_member_can_submit_request(self):
        self.login_team()
        description = f"{self.DESCRIPTION_PREFIX} Нужен новый фильтр"
        response = self.client.post(
            "/software-requests",
            json={"description": description, "page_path": "/team/"},
        )
        self.assertEqual(response.status_code, 201)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM software_requests WHERE id = ?",
                (response.get_json()["request_id"],),
            ).fetchone()
        self.assertEqual(row["author_type"], "employee")
        self.assertEqual(row["author_employee_id"], self.employee_id)
        self.assertEqual(row["author_name"], self.EMPLOYEE_NAME)

    def test_invalid_payloads_are_rejected_and_unsafe_page_is_discarded(self):
        self.login_admin()
        self.assertEqual(self.client.post("/software-requests", data="plain text").status_code, 400)
        self.assertEqual(self.client.post("/software-requests", json={"description": "  "}).status_code, 400)
        self.assertEqual(self.client.post("/software-requests", json={"description": "x" * 4001}).status_code, 400)

        description = f"{self.DESCRIPTION_PREFIX} Безопасный контекст"
        response = self.client.post(
            "/software-requests",
            json={"description": description, "page_path": "https://example.com"},
        )
        self.assertEqual(response.status_code, 201)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT page_path FROM software_requests WHERE id = ?",
                (response.get_json()["request_id"],),
            ).fetchone()
        self.assertIsNone(row["page_path"])

    def test_admin_journal_lists_requests_and_updates_status(self):
        self.login_admin()
        description = f"{self.DESCRIPTION_PREFIX} Исправить карточку"
        created = self.client.post(
            "/software-requests",
            json={"description": description, "page_path": "/fleet"},
        ).get_json()

        response = self.client.get("/settings/software-requests")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Заявки на доработку софта", html)
        self.assertIn(description, html)
        self.assertIn("Тестовый администратор", html)
        self.assertIn('<span class="nav-label">Настройки</span>', html)

        updated = self.client.post(
            f"/settings/software-requests/{created['request_id']}/status",
            data={"status": "in_progress"},
        )
        self.assertEqual(updated.status_code, 302)
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM software_requests WHERE id = ?",
                (created["request_id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "in_progress")

    def test_settings_journal_is_admin_only(self):
        response = self.client.get("/settings/software-requests")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        self.login_team()
        response = self.client.get("/settings/software-requests")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
