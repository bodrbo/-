import unittest
from unittest import mock

from support import application_module


class SupplyLockerDropTests(unittest.TestCase):
    EMPLOYEE_NAME = "Получатель Тестовый"
    OTHER_EMPLOYEE_NAME = "Сосед Тестовый"
    USERNAME = "drop.tester"
    OTHER_USERNAME = "drop.other"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.team_account_id = self._create_team_member(db, self.EMPLOYEE_NAME, self.USERNAME)
            self.other_team_account_id = self._create_team_member(
                db, self.OTHER_EMPLOYEE_NAME, self.OTHER_USERNAME
            )
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
            "DELETE FROM supply_locker_drop_items WHERE drop_id IN "
            "(SELECT id FROM supply_locker_drops WHERE employee_name IN (?, ?))",
            (cls.EMPLOYEE_NAME, cls.OTHER_EMPLOYEE_NAME),
        )
        db.execute(
            "DELETE FROM supply_locker_drops WHERE employee_name IN (?, ?)",
            (cls.EMPLOYEE_NAME, cls.OTHER_EMPLOYEE_NAME),
        )
        for name in (cls.EMPLOYEE_NAME, cls.OTHER_EMPLOYEE_NAME):
            db.execute("DELETE FROM team_accounts WHERE employee_name = ?", (name,))
            db.execute(
                "DELETE FROM employee_positions WHERE employee_id IN "
                "(SELECT id FROM employees WHERE name = ?)",
                (name,),
            )
            db.execute("DELETE FROM employees WHERE name = ?", (name,))
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

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def login_as_team(self, account_id=None, employee_name=None):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = account_id or self.team_account_id
            session["team_employee_name"] = employee_name or self.EMPLOYEE_NAME
            session["team_username"] = self.USERNAME

    def get_drop(self):
        with application_module.app.app_context():
            row = (
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_locker_drops WHERE employee_name = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (self.EMPLOYEE_NAME,),
                )
                .fetchone()
            )
            return dict(row) if row is not None else None

    def create_drop(self, employee_name=None, items=None, comment=""):
        self.login_as_admin()
        items = items if items is not None else [("Перчатки", "3")]
        data = {
            "employee_name": employee_name if employee_name is not None else self.EMPLOYEE_NAME,
            "comment": comment,
            "item_name[]": [name for name, _ in items],
            "quantity[]": [qty for _, qty in items],
        }
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            return self.client.post(
                f"/supply/lockers/{self.locker_id}/drop", data=data
            )

    def test_requires_admin_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post(
            f"/supply/lockers/{self.locker_id}/drop",
            data={"employee_name": self.EMPLOYEE_NAME, "item_name[]": ["Перчатки"], "quantity[]": ["1"]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))
        self.assertIsNone(self.get_drop())

    def test_admin_creates_drop_and_notifies_employee(self):
        self.login_as_admin()
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notify:
            response = self.client.post(
                f"/supply/lockers/{self.locker_id}/drop",
                data={
                    "employee_name": self.EMPLOYEE_NAME,
                    "comment": "Занесите на склад после смены",
                    "item_name[]": ["Перчатки", "Трос"],
                    "quantity[]": ["3", "1"],
                },
            )
        self.assertEqual(response.status_code, 302)
        drop = self.get_drop()
        self.assertIsNotNone(drop)
        self.assertEqual(drop["locker_id"], self.locker_id)
        self.assertIsNone(drop["picked_up_at"])
        self.assertEqual(drop["comment"], "Занесите на склад после смены")

        with application_module.app.app_context():
            items = application_module.get_db().execute(
                "SELECT item_name, quantity FROM supply_locker_drop_items "
                "WHERE drop_id = ? ORDER BY id",
                (drop["id"],),
            ).fetchall()
        self.assertEqual([dict(i) for i in items], [
            {"item_name": "Перчатки", "quantity": 3.0},
            {"item_name": "Трос", "quantity": 1.0},
        ])

        notify.assert_called_once()
        called_employee, called_text = notify.call_args[0][1], notify.call_args[0][2]
        self.assertEqual(called_employee, self.EMPLOYEE_NAME)
        self.assertIn("Перчатки", called_text)
        self.assertIn("Сундук-постамат", called_text)

    def test_missing_employee_creates_nothing(self):
        response = self.create_drop(employee_name="")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.get_drop())
        with self.client.session_transaction() as session:
            self.assertIn("locker_error", session)

    def test_unknown_employee_creates_nothing(self):
        response = self.create_drop(employee_name="Кто-то Несуществующий")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.get_drop())

    def test_no_items_creates_nothing(self):
        response = self.create_drop(items=[("", "")])
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.get_drop())

    def test_locker_profile_lists_drop(self):
        self.create_drop()
        self.login_as_admin()
        page = self.client.get(f"/supply/lockers/{self.locker_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(self.EMPLOYEE_NAME.encode(), page.data)
        self.assertIn("Ожидает получения".encode(), page.data)
        self.assertIn("Передано вручную".encode(), page.data)

    def test_employee_sees_pickup_button_and_can_pick_up(self):
        self.create_drop()
        drop = self.get_drop()

        self.login_as_team()
        dashboard = self.client.get("/team/")
        pickup_marker = f"/team/locker-drops/{drop['id']}/pickup".encode()
        self.assertIn(pickup_marker, dashboard.data)
        self.assertIn("Посылки в постамате".encode(), dashboard.data)

        response = self.client.post(f"/team/locker-drops/{drop['id']}/pickup")
        self.assertEqual(response.status_code, 302)

        updated = self.get_drop()
        self.assertIsNotNone(updated["picked_up_at"])

        dashboard = self.client.get("/team/")
        self.assertNotIn(pickup_marker, dashboard.data)

    def test_other_employee_does_not_see_or_pick_up_drop(self):
        self.create_drop()
        drop = self.get_drop()

        self.login_as_team(
            account_id=self.other_team_account_id, employee_name=self.OTHER_EMPLOYEE_NAME
        )
        dashboard = self.client.get("/team/")
        self.assertNotIn("Посылки в постамате".encode(), dashboard.data)

        self.client.post(f"/team/locker-drops/{drop['id']}/pickup")
        unchanged = self.get_drop()
        self.assertIsNone(unchanged["picked_up_at"])


if __name__ == "__main__":
    unittest.main()
