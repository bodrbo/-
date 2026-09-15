import unittest
from unittest import mock

from support import application_module


class SupplyRequestDeliveryTests(unittest.TestCase):
    EMPLOYEE_NAME = "Доставкин Тестовый"
    OTHER_EMPLOYEE_NAME = "Другой Получатель"
    USERNAME = "delivery.tester"
    OTHER_USERNAME = "delivery.other"

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
                "DELETE FROM supply_request_deliveries WHERE request_id IN "
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
            "VALUES (?, 'shipping', '2026-09-16 09:00')",
            (employee_name,),
        )
        request_id = cur.lastrowid
        db.execute(
            "INSERT INTO supply_request_items (request_id, item_name, quantity) "
            "VALUES (?, 'Перчатки', 2)",
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

    def get_delivery(self):
        with application_module.app.app_context():
            row = (
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_request_deliveries WHERE request_id = ?",
                    (self.request_id,),
                )
                .fetchone()
            )
            return dict(row) if row is not None else None

    def set_locker(self, locker_id):
        self.login_as_admin()
        return self.client.post(
            f"/supply/requests/{self.request_id}/locker",
            data={"delivery_locker_id": locker_id},
        )

    def set_status(self, status, comment=""):
        self.login_as_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            return self.client.post(
                f"/supply/requests/{self.request_id}/status",
                data={"status": status, "comment": comment},
            )

    def test_admin_assigns_delivery_locker(self):
        response = self.set_locker(self.locker_id)
        self.assertEqual(response.status_code, 302)
        req = self.get_request()
        self.assertEqual(req["delivery_locker_id"], self.locker_id)

    def test_admin_requests_page_shows_locker_selector(self):
        self.set_locker(self.locker_id)
        self.login_as_admin()
        page = self.client.get("/supply/requests")
        self.assertIn(b"selected", page.data)
        self.assertIn("Сундук-постамат".encode(), page.data)

    def test_delivered_status_without_locker_creates_no_delivery(self):
        self.set_status("delivered")
        self.assertIsNone(self.get_delivery())

    def test_delivered_status_with_locker_creates_delivery(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")
        delivery = self.get_delivery()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery["locker_id"], self.locker_id)
        self.assertIsNone(delivery["picked_up_at"])

    def test_delivered_status_twice_does_not_duplicate_delivery(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")
        self.set_status("delivered")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS c FROM supply_request_deliveries WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_delivered_status_sends_photo_when_asset_exists(self):
        self.set_locker(self.locker_id)
        self.login_as_admin()
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ), mock.patch.object(
            application_module, "telegram_chat_id_for_employee", return_value="12345"
        ), mock.patch.object(
            application_module.os.path, "exists", return_value=True
        ), mock.patch.object(
            application_module, "send_telegram_photo"
        ) as send_photo:
            self.client.post(
                f"/supply/requests/{self.request_id}/status",
                data={"status": "delivered", "comment": ""},
            )
        send_photo.assert_called_once()
        self.assertEqual(send_photo.call_args.kwargs.get("chat_id"), "12345")

    def test_admin_sees_delivery_in_locker_contents(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")
        self.login_as_admin()
        locker_page = self.client.get(f"/supply/lockers/{self.locker_id}")
        self.assertEqual(locker_page.status_code, 200)
        self.assertIn(self.EMPLOYEE_NAME.encode(), locker_page.data)
        self.assertIn("Ожидает получения".encode(), locker_page.data)

    def test_employee_sees_pickup_button_and_can_pick_up(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")

        self.login_as_team()
        dashboard = self.client.get("/team/")
        pickup_marker = f"/team/supply-requests/{self.request_id}/pickup".encode()
        self.assertIn(pickup_marker, dashboard.data)

        response = self.client.post(f"/team/supply-requests/{self.request_id}/pickup")
        self.assertEqual(response.status_code, 302)

        delivery = self.get_delivery()
        self.assertIsNotNone(delivery["picked_up_at"])

        dashboard = self.client.get("/team/")
        self.assertNotIn(pickup_marker, dashboard.data)

    def test_cannot_pick_up_someone_elses_delivery(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")

        self.login_as_team(
            account_id=self.other_team_account_id, employee_name=self.OTHER_EMPLOYEE_NAME
        )
        self.client.post(f"/team/supply-requests/{self.request_id}/pickup")
        delivery = self.get_delivery()
        self.assertIsNone(delivery["picked_up_at"])

    def test_admin_locker_page_shows_picked_up_status(self):
        self.set_locker(self.locker_id)
        self.set_status("delivered")
        self.login_as_team()
        self.client.post(f"/team/supply-requests/{self.request_id}/pickup")

        self.login_as_admin()
        locker_page = self.client.get(f"/supply/lockers/{self.locker_id}")
        self.assertIn("Забрано".encode(), locker_page.data)


if __name__ == "__main__":
    unittest.main()
