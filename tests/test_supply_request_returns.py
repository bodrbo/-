import unittest
from unittest import mock

from support import application_module


class SupplyRequestReturnTests(unittest.TestCase):
    EMPLOYEE_NAME = "Возвратов Тестовый"
    OTHER_EMPLOYEE_NAME = "Другой Сотрудник"
    USERNAME = "returns.tester"
    OTHER_USERNAME = "returns.other"

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
            self.request_id = self._create_request(db, self.EMPLOYEE_NAME)
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        for name in (cls.EMPLOYEE_NAME, cls.OTHER_EMPLOYEE_NAME):
            db.execute(
                "DELETE FROM supply_request_returns WHERE request_id IN "
                "(SELECT id FROM supply_requests WHERE employee_name = ?)",
                (name,),
            )
            db.execute(
                "DELETE FROM supply_request_items WHERE request_id IN "
                "(SELECT id FROM supply_requests WHERE employee_name = ?)",
                (name,),
            )
            db.execute("DELETE FROM supply_requests WHERE employee_name = ?", (name,))
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

    @staticmethod
    def _create_request(db, employee_name):
        cur = db.execute(
            "INSERT INTO supply_requests (employee_name, status, created_at) "
            "VALUES (?, 'new', '2026-09-16 09:00')",
            (employee_name,),
        )
        request_id = cur.lastrowid
        db.execute(
            "INSERT INTO supply_request_items (request_id, item_name, quantity) "
            "VALUES (?, 'Шуруповёрт', 1)",
            (request_id,),
        )
        return request_id

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

    def get_request(self):
        with application_module.app.app_context():
            return dict(
                application_module.get_db()
                .execute("SELECT * FROM supply_requests WHERE id = ?", (self.request_id,))
                .fetchone()
            )

    def get_return(self):
        with application_module.app.app_context():
            row = (
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_request_returns WHERE request_id = ?",
                    (self.request_id,),
                )
                .fetchone()
            )
            return dict(row) if row is not None else None

    def set_type(self, request_type):
        self.login_as_admin()
        return self.client.post(
            f"/supply/requests/{self.request_id}/type",
            data={"request_type": request_type},
        )

    def test_admin_assigns_type_and_records_who_assigned_it(self):
        response = self.set_type("returnable")
        self.assertEqual(response.status_code, 302)
        req = self.get_request()
        self.assertEqual(req["request_type"], "returnable")
        self.assertEqual(req["type_assigned_by_admin_id"], 1)

    def test_admin_requests_page_shows_type_selector(self):
        self.set_type("returnable")
        self.login_as_admin()
        page = self.client.get("/supply/requests")
        self.assertIn(b"selected", page.data)
        self.assertIn("Возвратная".encode(), page.data)

    def _return_action_marker(self):
        return f"/team/supply-requests/{self.request_id}/return".encode()

    def test_return_button_only_shows_for_returnable_untouched_requests(self):
        self.login_as_team()
        dashboard = self.client.get("/team/")
        self.assertNotIn(self._return_action_marker(), dashboard.data)

        self.set_type("returnable")
        self.login_as_team()
        dashboard = self.client.get("/team/")
        self.assertIn(self._return_action_marker(), dashboard.data)

    def test_non_returnable_request_never_offers_return(self):
        self.set_type("non_returnable")
        self.login_as_team()
        dashboard = self.client.get("/team/")
        self.assertNotIn(self._return_action_marker(), dashboard.data)

    def test_team_member_completes_return_and_admin_is_notified(self):
        self.set_type("returnable")
        self.login_as_team()
        with mock.patch.object(
            application_module, "send_telegram_notification_to_admin"
        ) as notify:
            response = self.client.post(
                f"/team/supply-requests/{self.request_id}/return",
                data={"locker_id": self.locker_id},
            )
        self.assertEqual(response.status_code, 302)
        ret = self.get_return()
        self.assertIsNotNone(ret)
        self.assertEqual(ret["locker_id"], self.locker_id)
        self.assertIsNone(ret["collected_at"])

        notify.assert_called_once()
        called_admin_id, called_text = notify.call_args[0][1], notify.call_args[0][2]
        self.assertEqual(called_admin_id, 1)
        self.assertIn("Шуруповёрт", called_text)
        self.assertIn(self.EMPLOYEE_NAME, called_text)

    def test_cannot_return_someone_elses_request(self):
        self.set_type("returnable")
        self.login_as_team(
            account_id=self.other_team_account_id, employee_name=self.OTHER_EMPLOYEE_NAME
        )
        self.client.post(
            f"/team/supply-requests/{self.request_id}/return",
            data={"locker_id": self.locker_id},
        )
        self.assertIsNone(self.get_return())

    def test_cannot_return_without_returnable_type(self):
        self.login_as_team()
        self.client.post(
            f"/team/supply-requests/{self.request_id}/return",
            data={"locker_id": self.locker_id},
        )
        self.assertIsNone(self.get_return())

    def test_cannot_return_twice(self):
        self.set_type("returnable")
        self.login_as_team()
        with mock.patch.object(application_module, "send_telegram_notification_to_admin"):
            self.client.post(
                f"/team/supply-requests/{self.request_id}/return",
                data={"locker_id": self.locker_id},
            )
            self.client.post(
                f"/team/supply-requests/{self.request_id}/return",
                data={"locker_id": self.locker_id},
            )
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS c FROM supply_request_returns WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_admin_sees_return_in_locker_contents_and_can_mark_collected(self):
        self.set_type("returnable")
        self.login_as_team()
        with mock.patch.object(application_module, "send_telegram_notification_to_admin"):
            self.client.post(
                f"/team/supply-requests/{self.request_id}/return",
                data={"locker_id": self.locker_id},
            )
        return_id = self.get_return()["id"]

        self.login_as_admin()
        locker_page = self.client.get(f"/supply/lockers/{self.locker_id}")
        self.assertEqual(locker_page.status_code, 200)
        self.assertIn(self.EMPLOYEE_NAME.encode(), locker_page.data)
        self.assertIn("Ожидает получения".encode(), locker_page.data)

        response = self.client.post(
            f"/supply/lockers/{self.locker_id}/returns/{return_id}/collect"
        )
        self.assertEqual(response.status_code, 302)
        ret = self.get_return()
        self.assertIsNotNone(ret["collected_at"])
        self.assertEqual(ret["collected_by_admin_id"], 1)

        locker_page = self.client.get(f"/supply/lockers/{self.locker_id}")
        self.assertNotIn("Шуруповёрт".encode(), locker_page.data)

        archive_page = self.client.get(f"/supply/lockers/{self.locker_id}/archive")
        self.assertEqual(archive_page.status_code, 200)
        self.assertIn(self.EMPLOYEE_NAME.encode(), archive_page.data)
        self.assertIn("Шуруповёрт".encode(), archive_page.data)


if __name__ == "__main__":
    unittest.main()
