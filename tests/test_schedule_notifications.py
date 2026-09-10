import unittest
from unittest.mock import Mock

from modules.notifications import (
    EVENT_SCHEDULE_ASSIGNED,
    EVENT_SCHEDULE_BOAT_CHANGED,
    EVENT_SCHEDULE_CANCELLED,
    EVENT_SCHEDULE_RESCHEDULED,
)
from modules.schedule.notifications import notify_item_changes


class ScheduleNotificationTests(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.sender = Mock(return_value="sent")

    @staticmethod
    def snapshot(
        starts_at="2026-09-10 13:00",
        ends_at="2026-09-10 15:30",
        boat="Бодрый Первый",
        assignments=None,
    ):
        if assignments is None:
            assignments = (
                {
                    "employee_id": 7,
                    "employee_name": "Галина Гидова",
                    "role": "guide",
                },
            )
        return {
            "id": 101,
            "service_name": "Большой тур",
            "boat": boat,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "assignments": assignments,
        }

    def test_new_trip_notifies_every_assigned_employee(self):
        after = self.snapshot(assignments=(
            {
                "employee_id": 7,
                "employee_name": "Галина Гидова",
                "role": "guide",
            },
            {
                "employee_id": 8,
                "employee_name": "Капитон Капитанов",
                "role": "captain",
            },
        ))

        deliveries = notify_item_changes(self.db, None, after, self.sender)

        self.assertEqual(
            [delivery["event"] for delivery in deliveries],
            [EVENT_SCHEDULE_ASSIGNED, EVENT_SCHEDULE_ASSIGNED],
        )
        self.assertEqual(
            [entry.args[1] for entry in self.sender.call_args_list],
            ["Галина Гидова", "Капитон Капитанов"],
        )
        self.assertIn("Вам назначен новый рейс", self.sender.call_args.args[2])
        self.assertIn("Большой тур", self.sender.call_args.args[2])

    def test_new_employee_gets_assignment_without_duplicate_change_alert(self):
        before = self.snapshot()
        after = self.snapshot(assignments=before["assignments"] + (
            {
                "employee_id": 8,
                "employee_name": "Капитон Капитанов",
                "role": "captain",
            },
        ))

        deliveries = notify_item_changes(self.db, before, after, self.sender)

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event"], EVENT_SCHEDULE_ASSIGNED)
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "Капитон Капитанов")

    def test_time_and_boat_change_are_combined_into_one_message(self):
        before = self.snapshot()
        after = self.snapshot(
            starts_at="2026-09-11 14:00",
            ends_at="2026-09-11 16:30",
            boat="Ларус",
        )

        deliveries = notify_item_changes(self.db, before, after, self.sender)

        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["event"], EVENT_SCHEDULE_RESCHEDULED)
        text = self.sender.call_args.args[2]
        self.assertIn("Рейс перенесён", text)
        self.assertIn("Судно было: Бодрый Первый", text)
        self.assertIn("Судно стало: Ларус", text)

    def test_boat_only_change_uses_boat_event(self):
        deliveries = notify_item_changes(
            self.db,
            self.snapshot(),
            self.snapshot(boat="Ларус"),
            self.sender,
        )

        self.assertEqual(deliveries[0]["event"], EVENT_SCHEDULE_BOAT_CHANGED)
        self.assertIn("изменено судно", self.sender.call_args.args[2])

    def test_deleted_trip_notifies_previous_crew(self):
        deliveries = notify_item_changes(
            self.db, self.snapshot(), None, self.sender
        )

        self.assertEqual(deliveries[0]["event"], EVENT_SCHEDULE_CANCELLED)
        self.assertIn("Рейс отменён", self.sender.call_args.args[2])

    def test_unchanged_save_sends_nothing(self):
        before = self.snapshot()

        deliveries = notify_item_changes(
            self.db, before, dict(before), self.sender
        )

        self.assertEqual(deliveries, [])
        self.sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
