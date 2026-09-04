import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from support import application_module


class TuningNoteReminderTests(unittest.TestCase):
    SOURCE_REF = "test:employee-note-reminder"
    EMPLOYEE_NAME = "Тестовый Получатель Напоминания"

    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_fixture(db)
            now = "2026-09-04 09:00"
            self.employee_id = db.execute(
                "INSERT INTO employees (name, created_at) VALUES (?, ?)",
                (self.EMPLOYEE_NAME, now),
            ).lastrowid
            db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Мастер тюнинг-центра', ?)",
                (self.employee_id, now),
            )
            db.execute(
                "INSERT INTO employee_telegram_accounts "
                "(employee_id, chat_id, username, display_name, linked_at) "
                "VALUES (?, '987654321', 'test_reminder', ?, ?)",
                (self.employee_id, self.EMPLOYEE_NAME, now),
            )
            client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Клиент напоминания', 'Тестовая лодка', '', "
                "'test-note-reminder-client', ?)",
                (now,),
            ).lastrowid
            self.order_id = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, equipment_type, boat_model, motor_model, "
                "sale_channel, phone, discount_pct, subtotal, total, status, "
                "source_ref, created_at, updated_at) "
                "VALUES (?, 'Клиент напоминания', 'boat', 'Тестовая лодка', '', "
                "'direct', '', 0, 0, 0, 'estimate', ?, ?, ?)",
                (client_id, self.SOURCE_REF, now, now),
            ).lastrowid
            self.note_id = db.execute(
                "INSERT INTO tuning_order_notes "
                "(order_id, author_admin_id, text, created_at) "
                "VALUES (?, 1, 'Позвонить клиенту по готовности', ?)",
                (self.order_id, now),
            ).lastrowid
            self.admin_id = db.execute(
                "SELECT id FROM admin_accounts ORDER BY id LIMIT 1"
            ).fetchone()["id"]
            db.commit()
        self.addCleanup(self.cleanup_fixture)

    @classmethod
    def _clear_fixture(cls, db):
        order_rows = db.execute(
            "SELECT id, client_id FROM tuning_orders WHERE source_ref = ?",
            (cls.SOURCE_REF,),
        ).fetchall()
        for order in order_rows:
            note_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_order_notes WHERE order_id = ?",
                    (order["id"],),
                ).fetchall()
            ]
            for note_id in note_ids:
                db.execute(
                    "DELETE FROM tuning_order_note_reminders WHERE note_id = ?",
                    (note_id,),
                )
            db.execute(
                "DELETE FROM tuning_order_notes WHERE order_id = ?", (order["id"],)
            )
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order["id"],))
            if order["client_id"] is not None:
                db.execute("DELETE FROM clients WHERE id = ?", (order["client_id"],))
        employee = db.execute(
            "SELECT id FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,)
        ).fetchone()
        if employee is not None:
            db.execute(
                "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
                (employee["id"],),
            )
            db.execute(
                "DELETE FROM employee_positions WHERE employee_id = ?",
                (employee["id"],),
            )
            db.execute("DELETE FROM employees WHERE id = ?", (employee["id"],))
        db.commit()

    @classmethod
    def cleanup_fixture(cls):
        with application_module.app.app_context():
            cls._clear_fixture(application_module.get_db())

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = self.admin_id
            session["admin_name"] = "Администратор теста"

    def test_note_form_searches_all_employees_and_keeps_admins(self):
        self.login()

        response = self.client.get(f"/tuning/edit/{self.order_id}")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="remind_recipient" data-combo-value', html)
        self.assertIn('placeholder="Начните вводить имя…"', html)
        self.assertIn(f'data-value="employee:{self.employee_id}"', html)
        self.assertIn(self.EMPLOYEE_NAME, html)
        self.assertIn("Мастер тюнинг-центра", html)
        self.assertIn("@test_reminder", html)
        self.assertIn(f'data-value="admin:{self.admin_id}"', html)

    def test_admin_can_create_employee_reminder(self):
        self.login()

        response = self.client.post(
            f"/tuning/{self.order_id}/notes/{self.note_id}/remind",
            data={
                "remind_recipient": f"employee:{self.employee_id}",
                "remind_at": "2026-09-05T12:30",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("#notes"))
        with application_module.app.app_context():
            reminder = application_module.get_db().execute(
                "SELECT * FROM tuning_order_note_reminders WHERE note_id = ?",
                (self.note_id,),
            ).fetchone()
        self.assertEqual(reminder["remind_employee_id"], self.employee_id)
        self.assertEqual(reminder["remind_admin_id"], self.admin_id)
        self.assertEqual(reminder["remind_at"], "2026-09-05 12:30")

        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn(self.EMPLOYEE_NAME, page)

    def test_due_employee_and_legacy_admin_reminders_use_correct_channels(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_order_note_reminders "
                "(note_id, remind_admin_id, remind_employee_id, remind_at, created_at) "
                "VALUES (?, ?, ?, '2020-01-01 10:00', '2020-01-01 09:00')",
                (self.note_id, self.admin_id, self.employee_id),
            )
            db.execute(
                "INSERT INTO tuning_order_note_reminders "
                "(note_id, remind_admin_id, remind_employee_id, remind_at, created_at) "
                "VALUES (?, ?, NULL, '2020-01-01 10:01', '2020-01-01 09:00')",
                (self.note_id, self.admin_id),
            )
            db.commit()

        with patch.object(
            application_module,
            "CRON_SECRET",
            "note-reminder-secret",
        ), patch.object(
            application_module,
            "send_telegram_notification_to_employee",
            return_value="sent",
        ) as employee_sender, patch.object(
            application_module,
            "send_telegram_notification_to_admin",
            return_value="sent",
        ) as admin_sender:
            response = self.client.get(
                "/internal/cron/send-note-reminders?token=note-reminder-secret"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "ok: 2 reminder(s) sent")
        employee_sender.assert_called_once()
        self.assertEqual(employee_sender.call_args.args[1], self.EMPLOYEE_NAME)
        self.assertIn("Позвонить клиенту", employee_sender.call_args.args[2])
        admin_sender.assert_called_once()
        self.assertEqual(admin_sender.call_args.args[1], self.admin_id)

    def test_invalid_or_deleted_employee_is_not_saved(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE employees SET deleted_at = '2026-09-04 10:00' WHERE id = ?",
                (self.employee_id,),
            )
            db.commit()

        self.client.post(
            f"/tuning/{self.order_id}/notes/{self.note_id}/remind",
            data={
                "remind_recipient": f"employee:{self.employee_id}",
                "remind_at": "2026-09-05T12:30",
            },
        )

        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_order_note_reminders WHERE note_id = ?",
                (self.note_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_existing_reminder_schema_gains_employee_recipient_column(self):
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.remove, database_path)
        connection = sqlite3.connect(database_path)
        connection.execute(
            "CREATE TABLE tuning_order_note_reminders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, "
            "remind_admin_id INTEGER NOT NULL, remind_at TEXT NOT NULL, "
            "sent_at TEXT, created_at TEXT NOT NULL)"
        )
        connection.commit()
        connection.close()

        with patch.object(application_module, "DB_PATH", database_path):
            application_module.init_db()

        migrated = sqlite3.connect(database_path)
        column_rows = migrated.execute(
            "PRAGMA table_info(tuning_order_note_reminders)"
        ).fetchall()
        columns = {
            row[1]
            for row in column_rows
        }
        not_null = {
            row[1]: row[3]
            for row in column_rows
        }
        migrated.execute(
            "INSERT INTO tuning_order_note_reminders "
            "(note_id, remind_admin_id, remind_employee_id, remind_at, created_at) "
            "VALUES (1, 1, 2, '2026-09-05 12:00', '2026-09-04 12:00')"
        )
        migrated.commit()
        migrated.close()
        self.assertIn("remind_employee_id", columns)
        self.assertEqual(not_null["remind_admin_id"], 1)


if __name__ == "__main__":
    unittest.main()
