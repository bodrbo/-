import unittest
from unittest.mock import patch

from support import application_module


class TaskNotificationRouteTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()

    def setUp(self):
        self.client = application_module.app.test_client()

    def test_task_reminder_cron_requires_secret_and_reports_counts(self):
        with patch.object(application_module, "CRON_SECRET", "task-secret"), patch.object(
            application_module,
            "send_due_task_reminders",
            return_value={"sent_3h": 2, "sent_6h": 1},
        ) as reminders:
            forbidden = self.client.get(
                "/internal/cron/send-task-reminders?token=wrong"
            )
            response = self.client.get(
                "/internal/cron/send-task-reminders?token=task-secret"
            )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 reminder(s) after 3h", response.get_data(as_text=True))
        self.assertIn("1 reminder(s) after 6h", response.get_data(as_text=True))
        reminders.assert_called_once()

    def test_existing_hourly_cron_processes_reminders_before_yclients(self):
        with patch.object(application_module, "CRON_SECRET", "task-secret"), patch.object(
            application_module,
            "send_due_task_reminders",
            return_value={"sent_3h": 1, "sent_6h": 0},
        ) as reminders, patch.object(
            application_module, "yclients_configured", return_value=False
        ):
            response = self.client.get(
                "/internal/cron/sync-fuel?token=task-secret"
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("task reminders: 1 after 3h", response.get_data(as_text=True))
        reminders.assert_called_once()


if __name__ == "__main__":
    unittest.main()
