import unittest

from support import application_module


class EmployeesModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()

    def log_in_as_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def employee(self, name):
        with application_module.app.app_context():
            return dict(
                application_module.get_db()
                .execute("SELECT * FROM employees WHERE name = ?", (name,))
                .fetchone()
            )

    def test_employee_directory_requires_admin_and_renders(self):
        response = self.client.get("/employees")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

        self.log_in_as_admin()
        response = self.client.get("/employees")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сотрудники".encode(), response.data)
        self.assertIn("Дмитрий Тарусов".encode(), response.data)

    def test_positions_are_managed_in_database_and_survive_restart(self):
        employee = self.employee("Эльмира Бектаева")
        self.log_in_as_admin()
        with application_module.app.app_context():
            db = application_module.get_db()
            position = db.execute(
                "SELECT * FROM employee_positions WHERE employee_id = ? AND position = 'Гид'",
                (employee["id"],),
            ).fetchone()
            self.assertIsNotNone(position)
            position_id = position["id"]

        response = self.client.post(
            f"/employees/{employee['id']}/positions/{position_id}/delete"
        )
        self.assertEqual(response.status_code, 302)

        # init_db models a Passenger restart. A removed bootstrap position
        # must not be silently restored from Python constants.
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            removed = db.execute(
                "SELECT 1 FROM employee_positions WHERE employee_id = ? AND position = 'Гид'",
                (employee["id"],),
            ).fetchone()
            self.assertIsNone(removed)

        response = self.client.post(
            f"/employees/{employee['id']}/positions",
            data={"position": "Гид"},
        )
        self.assertEqual(response.status_code, 302)

    def test_sync_link_test_notification_and_unlink_telegram(self):
        employee = self.employee("Дмитрий Тарусов")
        original_fetcher = application_module.fetch_recent_telegram_contacts
        original_sender = application_module.send_telegram_notification
        sent_messages = []

        application_module.fetch_recent_telegram_contacts = lambda token: [
            {
                "chat_id": "987654321",
                "username": "captain_test",
                "display_name": "Тестовый Капитан",
                "last_text": "/start",
                "last_message_at": "2026-08-18 09:00",
            }
        ]

        def fake_sender(text, chat_id=None):
            sent_messages.append((chat_id, text))
            return "sent"

        application_module.send_telegram_notification = fake_sender
        self.addCleanup(
            setattr,
            application_module,
            "fetch_recent_telegram_contacts",
            original_fetcher,
        )
        self.addCleanup(
            setattr,
            application_module,
            "send_telegram_notification",
            original_sender,
        )

        self.log_in_as_admin()
        response = self.client.post("/employees/telegram/sync")
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/employees/{employee['id']}/telegram",
            data={"chat_id": "987654321"},
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            link = db.execute(
                "SELECT * FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            legacy = db.execute(
                "SELECT telegram_chat_id FROM team_accounts WHERE employee_name = ?",
                (employee["name"],),
            ).fetchone()
            self.assertEqual(link["chat_id"], "987654321")
            self.assertEqual(link["username"], "captain_test")
            self.assertEqual(legacy["telegram_chat_id"], "987654321")

        application_module.fetch_recent_telegram_contacts = lambda token: [
            {
                "chat_id": "987654321",
                "username": "captain_renamed",
                "display_name": "Капитан после обновления",
                "last_text": "Привет",
                "last_message_at": "2026-08-18 10:00",
            }
        ]
        self.client.post("/employees/telegram/sync")
        with application_module.app.app_context():
            link = application_module.get_db().execute(
                "SELECT username, display_name FROM employee_telegram_accounts "
                "WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            self.assertEqual(link["username"], "captain_renamed")
            self.assertEqual(link["display_name"], "Капитан после обновления")

        response = self.client.post(f"/employees/{employee['id']}/telegram/test")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(sent_messages[-1][0], "987654321")

        with application_module.app.app_context():
            status = application_module.send_telegram_notification_to_employee(
                application_module.get_db(), employee["name"], "Рабочее уведомление"
            )
        self.assertEqual(status, "sent")
        self.assertEqual(sent_messages[-1], ("987654321", "Рабочее уведомление"))

        response = self.client.post(f"/employees/{employee['id']}/telegram/unlink")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM employee_telegram_accounts WHERE employee_id = ?",
                    (employee["id"],),
                ).fetchone()
            )
            legacy = db.execute(
                "SELECT telegram_chat_id FROM team_accounts WHERE employee_name = ?",
                (employee["name"],),
            ).fetchone()
            self.assertIsNone(legacy["telegram_chat_id"])

    def test_existing_team_account_telegram_link_is_migrated(self):
        employee = self.employee("Платон Жмаев")
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            )
            db.execute(
                "UPDATE team_accounts SET telegram_chat_id = 'legacy-123' "
                "WHERE employee_name = ?",
                (employee["name"],),
            )
            db.commit()

        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            migrated = db.execute(
                "SELECT chat_id FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            self.assertEqual(migrated["chat_id"], "legacy-123")
            db.execute(
                "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            )
            db.execute(
                "DELETE FROM telegram_contacts WHERE chat_id = 'legacy-123'"
            )
            db.execute(
                "UPDATE team_accounts SET telegram_chat_id = NULL WHERE employee_name = ?",
                (employee["name"],),
            )
            db.commit()


if __name__ == "__main__":
    unittest.main()
