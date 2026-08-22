import unittest

from werkzeug.security import check_password_hash

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

    def create_employee(self, name, positions=None, chat_id=""):
        self.log_in_as_admin()
        response = self.client.post(
            "/employees",
            data={
                "name": name,
                "positions": positions or ["Гид"],
                "chat_id": chat_id,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("#employee-credentials"))
        with self.client.session_transaction() as session:
            credentials = dict(session["employee_credentials"])
        return self.employee(name), credentials

    def test_employee_directory_requires_admin_and_renders(self):
        response = self.client.get("/employees")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

        self.log_in_as_admin()
        response = self.client.get("/employees")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сотрудники".encode(), response.data)
        self.assertIn("Дмитрий Тарусов".encode(), response.data)

    def test_admin_creates_employee_with_account_positions_and_telegram(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO telegram_contacts "
                "(chat_id, username, display_name, updated_at) VALUES (?, ?, ?, ?)",
                ("new-employee-chat", "ivan_test", "Иван Тестовый", "2026-08-22 12:00"),
            )
            db.commit()

        employee, credentials = self.create_employee(
            "Иван Тестовый",
            positions=["Капитан", "Гид"],
            chat_id="new-employee-chat",
        )

        self.assertEqual(credentials["employee_id"], employee["id"])
        self.assertTrue(credentials["username"].startswith("ivan."))
        self.assertGreaterEqual(len(credentials["password"]), 12)
        with application_module.app.app_context():
            db = application_module.get_db()
            account = db.execute(
                "SELECT * FROM team_accounts WHERE employee_id = ?", (employee["id"],)
            ).fetchone()
            positions = {
                row["position"]
                for row in db.execute(
                    "SELECT position FROM employee_positions WHERE employee_id = ?",
                    (employee["id"],),
                ).fetchall()
            }
            telegram = db.execute(
                "SELECT chat_id FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            self.assertEqual(account["username"], credentials["username"])
            self.assertTrue(
                check_password_hash(account["password_hash"], credentials["password"])
            )
            self.assertNotIn(credentials["password"], account["password_hash"])
            self.assertEqual(positions, {"Капитан", "Гид"})
            self.assertEqual(telegram["chat_id"], "new-employee-chat")

        page = self.client.get("/employees")
        self.assertIn("Иван Тестовый".encode(), page.data)
        self.assertIn(credentials["username"].encode(), page.data)
        self.assertIn(credentials["password"].encode(), page.data)
        self.assertNotIn(credentials["password"].encode(), self.client.get("/employees").data)
        self.assertIn("Иван Тестовый".encode(), self.client.get("/admin").data)
        self.assertIn("Иван Тестовый".encode(), self.client.get("/trips").data)

        team_client = application_module.app.test_client()
        login = team_client.post(
            "/team/login",
            data={
                "username": credentials["username"],
                "password": credentials["password"],
            },
        )
        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/team/"))

    def test_admin_can_reset_generated_employee_password(self):
        employee, original = self.create_employee("Мария Пароль", ["Гид"])

        response = self.client.post(
            f"/employees/{employee['id']}/account/reset-password"
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            replacement = dict(session["employee_credentials"])
        self.assertEqual(replacement["username"], original["username"])
        self.assertNotEqual(replacement["password"], original["password"])

        with application_module.app.app_context():
            account = application_module.get_db().execute(
                "SELECT password_hash FROM team_accounts WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            self.assertFalse(
                check_password_hash(account["password_hash"], original["password"])
            )
            self.assertTrue(
                check_password_hash(account["password_hash"], replacement["password"])
            )

    def test_password_reset_for_bootstrap_account_survives_restart(self):
        employee = self.employee("Дмитрий Тарусов")
        self.log_in_as_admin()
        response = self.client.post(
            f"/employees/{employee['id']}/account/reset-password"
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            credentials = dict(session["employee_credentials"])

        application_module.init_db()
        with application_module.app.app_context():
            account = application_module.get_db().execute(
                "SELECT password_hash FROM team_accounts WHERE employee_id = ?",
                (employee["id"],),
            ).fetchone()
            self.assertTrue(
                check_password_hash(account["password_hash"], credentials["password"])
            )

    def test_deleting_employee_revokes_access_but_preserves_history(self):
        employee, credentials = self.create_employee("Олег Архивный", ["Капитан"])
        team_client = application_module.app.test_client()
        self.assertEqual(
            team_client.post(
                "/team/login",
                data={
                    "username": credentials["username"],
                    "password": credentials["password"],
                },
            ).status_code,
            302,
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO entries "
                "(employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Историческая работа', 1000, 1, 1000, ?, ?)",
                (employee["name"], "2026-08-20", "2026-08-20 10:00"),
            )
            db.commit()

        response = self.client.post(f"/employees/{employee['id']}/delete")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            deleted = db.execute(
                "SELECT * FROM employees WHERE id = ?", (employee["id"],)
            ).fetchone()
            self.assertIsNotNone(deleted["deleted_at"])
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM team_accounts WHERE employee_id = ?", (employee["id"],)
                ).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM employee_positions WHERE employee_id = ?",
                    (employee["id"],),
                ).fetchone()
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM entries WHERE employee = ?", (employee["name"],)
                ).fetchone()
            )

        self.assertEqual(team_client.get("/team/").status_code, 302)
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertIsNotNone(
                db.execute(
                    "SELECT deleted_at FROM employees WHERE id = ?", (employee["id"],)
                ).fetchone()["deleted_at"]
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM team_accounts WHERE employee_id = ?", (employee["id"],)
                ).fetchone()
            )

    def test_employee_with_open_assignment_cannot_be_deleted(self):
        employee, _ = self.create_employee("Анна Задача", ["Тюнингмэн"])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_item_assignments "
                "(item_id, employee_name, rate, norm_hours, assignment_status, assigned_at) "
                "VALUES (999999, ?, 1000, 1, 'pending', ?)",
                (employee["name"], "2026-08-22 12:00"),
            )
            db.commit()

        response = self.client.post(f"/employees/{employee['id']}/delete")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertIsNotNone(
                db.execute(
                    "SELECT 1 FROM employees WHERE id = ? AND deleted_at IS NULL",
                    (employee["id"],),
                ).fetchone()
            )
            db.execute(
                "DELETE FROM tuning_item_assignments WHERE employee_name = ?",
                (employee["name"],),
            )
            db.commit()
        page = self.client.get(response.headers["Location"])
        self.assertIn("есть активные или ожидающие ответа".encode(), page.data)

        self.client.post(f"/employees/{employee['id']}/delete")

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
