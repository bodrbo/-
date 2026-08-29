import sqlite3
import unittest
from unittest.mock import Mock, call

from modules.notifications import (
    EVENT_FLEET_CHECKLIST_PROBLEM,
    EVENT_FLEET_DEFECT_CREATED,
    EVENT_FLEET_EXTRA_DEFECT,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_UNACCEPTED_3H,
    EVENT_TASK_UNACCEPTED_6H,
    EVENT_TUNING_WORK_APPROVED,
    dispatch_notification,
    dispatch_photos,
    notification_rule,
)


class NotificationModuleTestCase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE employees (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE employee_positions (
                employee_id INTEGER NOT NULL,
                position TEXT NOT NULL
            );
            CREATE TABLE employee_telegram_accounts (
                employee_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL
            );
            """
        )

    def tearDown(self):
        self.db.close()

    def add_employee(self, employee_id, name, positions, chat_id=None, deleted=False):
        self.db.execute(
            "INSERT INTO employees (id, name, deleted_at) VALUES (?, ?, ?)",
            (employee_id, name, "2026-08-29 10:00" if deleted else None),
        )
        for position in positions:
            self.db.execute(
                "INSERT INTO employee_positions (employee_id, position) VALUES (?, ?)",
                (employee_id, position),
            )
        if chat_id is not None:
            self.db.execute(
                "INSERT INTO employee_telegram_accounts (employee_id, chat_id) VALUES (?, ?)",
                (employee_id, chat_id),
            )
        self.db.commit()

    def test_rules_describe_moments_and_positions(self):
        tuning_rule = notification_rule(EVENT_TUNING_WORK_APPROVED)
        self.assertEqual(tuning_rule.delivery, "immediate")
        self.assertEqual(
            tuning_rule.positions,
            ("Тюнингмэн", "Менеджер по работе с клиентами"),
        )
        for event in (
            EVENT_FLEET_DEFECT_CREATED,
            EVENT_FLEET_CHECKLIST_PROBLEM,
            EVENT_FLEET_EXTRA_DEFECT,
        ):
            rule = notification_rule(event)
            self.assertEqual(rule.delivery, "immediate")
            self.assertEqual(rule.positions, ("Тюнингмэн",))
        self.assertEqual(notification_rule(EVENT_TASK_ASSIGNED).delay_hours, 0)
        self.assertEqual(
            notification_rule(EVENT_TASK_UNACCEPTED_3H).delay_hours, 3
        )
        self.assertEqual(
            notification_rule(EVENT_TASK_UNACCEPTED_6H).delay_hours, 6
        )
        for event in (
            EVENT_TASK_ASSIGNED,
            EVENT_TASK_UNACCEPTED_3H,
            EVENT_TASK_UNACCEPTED_6H,
        ):
            self.assertEqual(
                notification_rule(event).recipient, "assigned_employee"
            )

    def test_dispatches_only_to_active_linked_employees_in_target_positions(self):
        self.add_employee(1, "Мастер", ["Тюнингмэн"], "101")
        self.add_employee(2, "Менеджер", ["Менеджер по работе с клиентами"], "202")
        self.add_employee(3, "Капитан", ["Капитан"], "303")
        self.add_employee(4, "Без Telegram", ["Тюнингмэн"])
        self.add_employee(5, "Удалённый", ["Тюнингмэн"], "505", deleted=True)
        sender = Mock(return_value="sent")

        result = dispatch_notification(
            self.db,
            EVENT_TUNING_WORK_APPROVED,
            "Согласовано",
            sender,
            fallback_chat_id="group",
        )

        self.assertEqual(result.chat_ids, ("101", "202"))
        self.assertFalse(result.used_fallback)
        self.assertEqual(result.statuses, ("sent", "sent"))
        self.assertEqual(
            sender.call_args_list,
            [
                call("Согласовано", chat_id="101"),
                call("Согласовано", chat_id="202"),
            ],
        )

    def test_employee_with_multiple_target_positions_receives_one_message(self):
        self.add_employee(
            1,
            "Универсальный сотрудник",
            ["Тюнингмэн", "Менеджер по работе с клиентами"],
            "101",
        )
        sender = Mock(return_value="sent")

        result = dispatch_notification(
            self.db,
            EVENT_TUNING_WORK_APPROVED,
            "Согласовано",
            sender,
        )

        self.assertEqual(result.chat_ids, ("101",))
        sender.assert_called_once_with("Согласовано", chat_id="101")

    def test_uses_legacy_chat_as_fallback_when_no_personal_account_is_linked(self):
        self.add_employee(1, "Мастер", ["Тюнингмэн"])
        sender = Mock(return_value="sent")

        result = dispatch_notification(
            self.db,
            EVENT_FLEET_DEFECT_CREATED,
            "Новая неисправность",
            sender,
            fallback_chat_id="legacy-group",
        )

        self.assertTrue(result.used_fallback)
        self.assertEqual(result.chat_ids, ("legacy-group",))
        sender.assert_called_once_with(
            "Новая неисправность", chat_id="legacy-group"
        )

    def test_photos_follow_the_same_recipient_route(self):
        self.add_employee(1, "Мастер 1", ["Тюнингмэн"], "101")
        self.add_employee(2, "Мастер 2", ["Тюнингмэн"], "202")
        sender = Mock(return_value="sent")
        photo_sender = Mock(return_value="sent")
        result = dispatch_notification(
            self.db,
            EVENT_FLEET_CHECKLIST_PROBLEM,
            "Проблема",
            sender,
        )

        statuses = dispatch_photos(result, ["one.jpg", "two.jpg"], photo_sender)

        self.assertEqual(statuses, ("sent", "sent", "sent", "sent"))
        self.assertEqual(
            photo_sender.call_args_list,
            [
                call("one.jpg", chat_id="101"),
                call("two.jpg", chat_id="101"),
                call("one.jpg", chat_id="202"),
                call("two.jpg", chat_id="202"),
            ],
        )

    def test_unknown_event_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown notification event"):
            dispatch_notification(self.db, "unknown", "text", Mock())


if __name__ == "__main__":
    unittest.main()
