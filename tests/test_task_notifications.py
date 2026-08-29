import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock

from modules.notifications import (
    ASSIGNMENT_DEFECT,
    ASSIGNMENT_TUNING,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_UNACCEPTED_3H,
    EVENT_TASK_UNACCEPTED_6H,
    init_schema,
    notify_task_assigned,
    send_due_task_reminders,
)


class TaskNotificationTestCase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE boat_defects (
                id INTEGER PRIMARY KEY,
                boat TEXT NOT NULL,
                description TEXT NOT NULL
            );
            CREATE TABLE defect_assignments (
                id INTEGER PRIMARY KEY,
                defect_id INTEGER NOT NULL,
                employee_name TEXT NOT NULL,
                rate REAL NOT NULL,
                norm_hours REAL NOT NULL,
                assignment_status TEXT NOT NULL,
                assigned_at TEXT NOT NULL
            );
            CREATE TABLE tuning_orders (
                id INTEGER PRIMARY KEY,
                equipment_type TEXT NOT NULL,
                boat_model TEXT,
                motor_model TEXT
            );
            CREATE TABLE tuning_order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                work_name TEXT NOT NULL
            );
            CREATE TABLE tuning_item_assignments (
                id INTEGER PRIMARY KEY,
                item_id INTEGER NOT NULL,
                employee_name TEXT NOT NULL,
                rate REAL NOT NULL,
                norm_hours REAL NOT NULL,
                assignment_status TEXT NOT NULL,
                assigned_at TEXT NOT NULL
            );
            """
        )
        init_schema(self.db)
        self.db.execute(
            "INSERT INTO boat_defects (id, boat, description) VALUES (1, 'Ларус', 'Заменить помпу')"
        )
        self.db.execute(
            "INSERT INTO tuning_orders (id, equipment_type, boat_model, motor_model) "
            "VALUES (7, 'boat', 'Салют 585', '')"
        )
        self.db.execute(
            "INSERT INTO tuning_order_items (id, order_id, work_name) "
            "VALUES (10, 7, 'Установить эхолот')"
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_defect_assignment(self, assignment_id, status, assigned_at):
        self.db.execute(
            "INSERT INTO defect_assignments "
            "(id, defect_id, employee_name, rate, norm_hours, assignment_status, assigned_at) "
            "VALUES (?, 1, 'Капитан', 1500, 2, ?, ?)",
            (assignment_id, status, assigned_at),
        )
        self.db.commit()

    def add_tuning_assignment(self, assignment_id, status, assigned_at):
        self.db.execute(
            "INSERT INTO tuning_item_assignments "
            "(id, item_id, employee_name, rate, norm_hours, assignment_status, assigned_at) "
            "VALUES (?, 10, 'Мастер', 2000, 1.5, ?, ?)",
            (assignment_id, status, assigned_at),
        )
        self.db.commit()

    def test_immediate_assignment_message_contains_task_context_and_is_logged(self):
        self.add_tuning_assignment(1, "pending", "2026-08-29 09:00")
        sender = Mock(return_value="sent")

        status = notify_task_assigned(
            self.db,
            ASSIGNMENT_TUNING,
            1,
            sender,
            now=dt.datetime(2026, 8, 29, 9, 0),
        )

        self.assertEqual(status, "sent")
        sender.assert_called_once()
        args = sender.call_args.args
        self.assertIs(args[0], self.db)
        self.assertEqual(args[1], "Мастер")
        self.assertIn("Вам поручена задача", args[2])
        self.assertIn("№7 · Салют 585", args[2])
        self.assertIn("Установить эхолот", args[2])
        self.assertIn("3 000 ₽", args[2])
        delivery = self.db.execute(
            "SELECT * FROM task_notification_deliveries"
        ).fetchone()
        self.assertEqual(delivery["notification_event"], EVENT_TASK_ASSIGNED)
        self.assertEqual(delivery["delivery_status"], "sent")

    def test_pending_tasks_receive_idempotent_3h_and_6h_reminders(self):
        self.add_defect_assignment(1, "pending", "2026-08-29 09:00")
        self.add_tuning_assignment(2, "pending", "2026-08-29 06:00")
        self.add_defect_assignment(3, "accepted", "2026-08-29 06:00")
        sender = Mock(return_value="sent")

        first = send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 12, 5)
        )

        self.assertEqual(first, {"sent_3h": 1, "sent_6h": 1})
        self.assertEqual(sender.call_count, 2)
        messages = [call.args[2] for call in sender.call_args_list]
        self.assertTrue(any("Прошло 3 ч." in text for text in messages))
        self.assertTrue(any("Прошло 6 ч." in text for text in messages))
        self.assertFalse(any(call.args[1] == "Капитан" and "6 ч." in call.args[2]
                             for call in sender.call_args_list))

        second = send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 12, 10)
        )
        self.assertEqual(second, {"sent_3h": 0, "sent_6h": 0})
        self.assertEqual(sender.call_count, 2)

        third = send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 15, 5)
        )
        self.assertEqual(third, {"sent_3h": 0, "sent_6h": 1})
        self.assertEqual(sender.call_count, 3)

    def test_first_late_run_sends_only_current_6h_reminder(self):
        self.add_defect_assignment(1, "pending", "2026-08-29 05:00")
        sender = Mock(return_value="sent")

        stats = send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 12, 0)
        )

        self.assertEqual(stats, {"sent_3h": 0, "sent_6h": 1})
        sender.assert_called_once()
        deliveries = self.db.execute(
            "SELECT notification_event, delivery_status "
            "FROM task_notification_deliveries ORDER BY notification_event"
        ).fetchall()
        self.assertEqual(
            {row["notification_event"] for row in deliveries},
            {EVENT_TASK_UNACCEPTED_3H, EVENT_TASK_UNACCEPTED_6H},
        )
        self.assertIn(
            "superseded_by_6h",
            {row["delivery_status"] for row in deliveries},
        )

    def test_task_accepted_after_3h_does_not_receive_6h_reminder(self):
        self.add_defect_assignment(1, "pending", "2026-08-29 09:00")
        sender = Mock(return_value="sent")
        send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 12, 0)
        )
        self.db.execute(
            "UPDATE defect_assignments SET assignment_status = 'accepted' WHERE id = 1"
        )
        self.db.commit()

        stats = send_due_task_reminders(
            self.db, sender, now=dt.datetime(2026, 8, 29, 15, 0)
        )

        self.assertEqual(stats, {"sent_3h": 0, "sent_6h": 0})
        self.assertEqual(sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
