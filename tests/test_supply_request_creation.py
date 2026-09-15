import unittest

from support import application_module


class SupplyRequestCreationTests(unittest.TestCase):
    EMPLOYEE_NAME = "Создателев Тестовый"
    USERNAME = "creation.tester"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.team_account_id = self._create_team_member(db, self.EMPLOYEE_NAME, self.USERNAME)
            self.locker_id = db.execute(
                "SELECT id FROM supply_lockers WHERE name = 'Сундук-постамат №1'"
            ).fetchone()["id"]
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        db.execute(
            "DELETE FROM supply_request_items WHERE request_id IN "
            "(SELECT id FROM supply_requests WHERE employee_name = ?)",
            (cls.EMPLOYEE_NAME,),
        )
        db.execute("DELETE FROM supply_requests WHERE employee_name = ?", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM team_accounts WHERE employee_name = ?", (cls.EMPLOYEE_NAME,))
        db.execute(
            "DELETE FROM employee_positions WHERE employee_id IN "
            "(SELECT id FROM employees WHERE name = ?)",
            (cls.EMPLOYEE_NAME,),
        )
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.commit()

    @staticmethod
    def _create_team_member(db, name, username):
        employee = db.execute(
            "INSERT INTO employees (name, created_at, deleted_at) "
            "VALUES (?, '2026-09-16 09:00', NULL)",
            (name,),
        )
        db.execute(
            "INSERT INTO employee_positions (employee_id, position, created_at) "
            "VALUES (?, 'Тюнингмэн', '2026-09-16 09:00')",
            (employee.lastrowid,),
        )
        account = db.execute(
            "INSERT INTO team_accounts "
            "(employee_id, employee_name, username, password_hash, created_at) "
            "VALUES (?, ?, ?, 'test-hash', '2026-09-16 09:00')",
            (employee.lastrowid, name, username),
        )
        return account.lastrowid

    def login_as_team(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.team_account_id
            session["team_employee_name"] = self.EMPLOYEE_NAME
            session["team_username"] = self.USERNAME

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def get_latest_request(self):
        with application_module.app.app_context():
            row = (
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_requests WHERE employee_name = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (self.EMPLOYEE_NAME,),
                )
                .fetchone()
            )
            return dict(row) if row is not None else None

    def create_request(self, locker_id=None, item_name="Перчатки", quantity="1"):
        self.login_as_team()
        data = {"item_name[]": [item_name], "quantity[]": [quantity]}
        if locker_id is not None:
            data["locker_id"] = str(locker_id)
        return self.client.post("/team/supply-requests/create", data=data)

    def test_create_without_locker_leaves_delivery_locker_unset(self):
        response = self.create_request()
        self.assertEqual(response.status_code, 302)
        req = self.get_latest_request()
        self.assertIsNotNone(req)
        self.assertIsNone(req["delivery_locker_id"])

    def test_create_with_locker_sets_delivery_locker_id(self):
        response = self.create_request(locker_id=self.locker_id)
        self.assertEqual(response.status_code, 302)
        req = self.get_latest_request()
        self.assertEqual(req["delivery_locker_id"], self.locker_id)

    def test_create_with_garbage_locker_id_ignored(self):
        response = self.create_request(locker_id="not-a-number")
        self.assertEqual(response.status_code, 302)
        req = self.get_latest_request()
        self.assertIsNotNone(req)
        self.assertIsNone(req["delivery_locker_id"])

    def test_create_with_nonexistent_locker_id_ignored(self):
        response = self.create_request(locker_id=999999)
        self.assertEqual(response.status_code, 302)
        req = self.get_latest_request()
        self.assertIsNotNone(req)
        self.assertIsNone(req["delivery_locker_id"])

    def test_dashboard_shows_chosen_locker_form_option(self):
        self.login_as_team()
        dashboard = self.client.get("/team/")
        self.assertIn("Сундук-постамат №1".encode(), dashboard.data)
        self.assertIn("Не важно, куда доставят".encode(), dashboard.data)

    def test_dashboard_shows_chosen_locker_on_request_card(self):
        self.create_request(locker_id=self.locker_id)
        self.login_as_team()
        dashboard = self.client.get("/team/")
        self.assertIn("Постамат для доставки".encode(), dashboard.data)

    def test_admin_requests_page_prefills_locker_from_employee_choice(self):
        self.create_request(locker_id=self.locker_id)
        self.login_as_admin()
        page = self.client.get("/supply/requests")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"selected", page.data)
        self.assertIn("Сундук-постамат №1".encode(), page.data)


if __name__ == "__main__":
    unittest.main()
