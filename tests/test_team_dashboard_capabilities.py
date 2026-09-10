import unittest

from modules.employees.capabilities import (
    DOCUMENTS,
    FLEET,
    INCOME,
    SCHEDULE,
    SCHEDULE_CLIENTS,
    SUPPLY,
    TASKS,
    dashboard_capabilities,
)
from support import application_module


class TeamDashboardCapabilitiesTests(unittest.TestCase):
    EMPLOYEE_NAME = "Тимофей Тестовый"
    USERNAME = "team-capabilities-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_employee(db)
            employee = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-09-09 10:00', NULL)",
                (self.EMPLOYEE_NAME,),
            )
            self.employee_id = employee.lastrowid
            db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Тюнингмэн', '2026-09-09 10:00')",
                (self.employee_id,),
            )
            account = db.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, 'test-hash', '2026-09-09 10:00')",
                (self.employee_id, self.EMPLOYEE_NAME, self.USERNAME),
            )
            self.account_id = account.lastrowid
            db.commit()
        self._login_as_employee()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_employee(application_module.get_db())

    @classmethod
    def _clear_test_employee(cls, db):
        db.execute(
            "DELETE FROM team_accounts WHERE username = ? OR employee_name = ?",
            (cls.USERNAME, cls.EMPLOYEE_NAME),
        )
        db.execute(
            "DELETE FROM employee_positions WHERE employee_id IN "
            "(SELECT id FROM employees WHERE name = ?)",
            (cls.EMPLOYEE_NAME,),
        )
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.commit()

    def _login_as_employee(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.account_id
            session["team_employee_name"] = self.EMPLOYEE_NAME
            session["team_username"] = self.USERNAME

    def test_capability_union_is_composed_from_all_positions(self):
        tuningman = dashboard_capabilities(["Тюнингмэн"])
        self.assertEqual(tuningman, frozenset({INCOME, TASKS, SUPPLY}))

        captain_and_tuningman = dashboard_capabilities(["Тюнингмэн", "Капитан"])
        self.assertEqual(
            captain_and_tuningman,
            frozenset({
                INCOME, TASKS, SUPPLY, FLEET, DOCUMENTS,
                SCHEDULE, SCHEDULE_CLIENTS,
            }),
        )
        self.assertEqual(
            dashboard_capabilities(["Гид"]),
            frozenset({INCOME, SCHEDULE}),
        )
        self.assertEqual(
            dashboard_capabilities(["Гид-капитан"]),
            frozenset({INCOME, SCHEDULE, SCHEDULE_CLIENTS}),
        )

    def test_tuningman_cabinet_has_work_modules_without_fleet_or_documents(self):
        response = self.client.get("/team/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("<title>Кабинет тюнингмэна</title>", html)
        self.assertIn('id="team-income"', html)
        self.assertIn('id="team-tasks"', html)
        self.assertIn('id="team-supply"', html)
        self.assertNotIn('id="captain-fleet"', html)
        self.assertNotIn('id="team-documents"', html)
        self.assertNotIn("offline.js", html)
        self.assertIn("Снабжение</span>", html)

    def test_position_changes_update_same_cabinet_on_next_request(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            captain_position = db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Капитан', '2026-09-09 10:05')",
                (self.employee_id,),
            )
            captain_position_id = captain_position.lastrowid
            db.commit()

        captain_html = self.client.get("/team/").get_data(as_text=True)
        self.assertIn("<title>Кабинет капитана</title>", captain_html)
        self.assertIn('id="captain-fleet"', captain_html)
        self.assertIn('id="team-documents"', captain_html)
        self.assertIn("offline.js", captain_html)
        self.assertIn('id="team-schedule"', captain_html)

        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM employee_positions WHERE id = ?",
                (captain_position_id,),
            )
            db.commit()

        tuningman_html = self.client.get("/team/").get_data(as_text=True)
        self.assertIn("<title>Кабинет тюнингмэна</title>", tuningman_html)
        self.assertNotIn('id="captain-fleet"', tuningman_html)
        self.assertNotIn('id="team-documents"', tuningman_html)
        self.assertIn('id="team-tasks"', tuningman_html)
        self.assertIn('id="team-supply"', tuningman_html)

    def test_admin_can_create_missing_legacy_cabinet_and_receives_credentials(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM team_accounts WHERE id = ?", (self.account_id,))
            db.commit()

        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

        response = self.client.post(
            f"/employees/{self.employee_id}/account/reset-password"
        )
        self.assertEqual(response.status_code, 302)

        with self.client.session_transaction() as session:
            credentials = session["employee_credentials"]
            self.assertEqual(credentials["employee_name"], self.EMPLOYEE_NAME)
            self.assertTrue(credentials["username"])
            self.assertTrue(credentials["password"])

        with application_module.app.app_context():
            account = application_module.get_db().execute(
                "SELECT * FROM team_accounts WHERE employee_id = ?",
                (self.employee_id,),
            ).fetchone()
            self.assertIsNotNone(account)
            self.account_id = account["id"]


if __name__ == "__main__":
    unittest.main()
