import datetime as dt
import unittest
from unittest.mock import patch

from support import application_module
from test_tuning_schedule_assignments import _TuningScheduleFixture


class _OrderEventTasksFixture(_TuningScheduleFixture):
    """Shared checks for event tasks in tuning and both dealer order types."""

    ORDER_KIND = "tuning"
    DEALER_ORDER_TYPE = "equipment"

    def setUp(self):
        super().setUp()
        self.tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        self._wipe()
        self.addCleanup(self._wipe)
        self.event_order_id = self.order_id
        self.task_order_id = self.order_id
        self.order_url = f"/tuning/edit/{self.order_id}"
        self.order_label = f"Заказ №{self.order_id}"
        if self.ORDER_KIND == "dealer":
            with application_module.app.app_context():
                db = application_module.get_db()
                self.event_order_id = db.execute(
                    "INSERT INTO dealer_orders (client_name, order_type, status, order_date, created_at, updated_at) "
                    "VALUES ('Клиент дилерской задачи', ?, 'new_request', '2026-11-16', "
                    "'2026-11-16 09:00', '2026-11-16 09:00')", (self.DEALER_ORDER_TYPE,),
                ).lastrowid
                db.commit()
            self.addCleanup(self._delete_dealer_order)
            self.task_order_id = None
            self.order_url = f"/dealer/orders/{self.event_order_id}"
            self.order_label = f"Дилерский центр, заказ №{self.event_order_id}"

    def _delete_dealer_order(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (self.event_order_id,))
            db.commit()

    def _wipe(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM order_event_tasks")
            db.execute("DELETE FROM order_events WHERE order_id >= 0")
            db.execute("DELETE FROM tuning_task_reminders WHERE schedule_task_id IS NOT NULL")
            db.commit()

    def add_dialog(self, text="Клиент просил перезвонить в четверг"):
        self.client.post(f"/events/{self.ORDER_KIND}/{self.event_order_id}/add",
                         data={"kind": "dialog", "event_date": "2026-11-16", "text": text})
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM order_events ORDER BY id DESC LIMIT 1").fetchone()["id"]

    def task_form(self, **fields):
        data = {
            "task_title": "Перезвонить клиенту",
            "task_executor": self.EMPLOYEE_A,
            "task_date": self.tomorrow,
            "task_time": "10:00",
            "task_minutes": "30",
            "task_comment": "Уточнить про тент",
            "remind_recipient": f"employee:{self.employee_id(self.EMPLOYEE_A)}",
            "remind_at": f"{self.tomorrow}T09:30",
            "remind_message": "Не забудь про скидку",
        }
        data.update(fields)
        return data

    def db_rows(self, sql, params=()):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(sql, params).fetchall()]

    def page(self):
        return self.client.get(self.order_url).get_data(as_text=True)

    def test_task_with_reminder_is_created_from_an_event(self):
        self.login_admin()
        event_id = self.add_dialog()
        response = self.client.post(f"/events/{event_id}/task", data=self.task_form())

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"{self.order_url}#event-{event_id}"))
        (task,) = self.db_rows(
            "SELECT t.*, d.work_date, d.start_time, d.planned_hours FROM tuning_schedule_tasks t "
            "JOIN tuning_schedule_task_days d ON d.task_id = t.id")
        self.assertEqual(
            (task["employee_name"], task["title"], task["work_date"], task["start_time"], task["planned_hours"]),
            (self.EMPLOYEE_A, "Перезвонить клиенту", self.tomorrow, "10:00", 0.5),
        )
        self.assertEqual((task["rate"], task["order_id"]), (0, self.task_order_id))
        self.assertIn("Уточнить про тент", task["comment"])
        self.assertIn(self.order_label, task["comment"])
        self.assertIn("Клиент просил перезвонить в четверг", task["comment"])
        (reminder,) = self.db_rows("SELECT * FROM tuning_task_reminders WHERE schedule_task_id = ?", (task["id"],))
        self.assertEqual(
            (reminder["remind_employee_id"], reminder["remind_at"], reminder["message"], reminder["sent_at"]),
            (self.employee_id(self.EMPLOYEE_A), f"{self.tomorrow} 09:30", "Не забудь про скидку", None),
        )
        self.assertEqual(self.db_rows("SELECT event_id, schedule_task_id FROM order_event_tasks"),
                         [{"event_id": event_id, "schedule_task_id": task["id"]}])
        page = self.page()
        self.assertIn("Задача: Перезвонить клиенту", page)
        self.assertIn("Напомнит", page)
        self.assertIn("добавлена в расписание тюнинга", page)

    def test_reminder_is_required_and_nothing_is_saved_on_error(self):
        self.login_admin()
        event_id = self.add_dialog()
        for fields, error in (
            ({"remind_recipient": ""}, "Выберите, кому напомнить"),
            ({"remind_at": ""}, "Укажите дату и время напоминания"),
            ({"task_executor": ""}, "Выберите, кому поручить задачу"),
            ({"task_title": " "}, "Укажите, что нужно сделать"),
            ({"task_time": ""}, "Укажите время задачи"),
            ({"task_executor": "Посторонний"}, "Выберите сотрудника из списка тюнингмэнов"),
        ):
            with self.subTest(fields=fields):
                self.client.post(f"/events/{event_id}/task", data=self.task_form(**fields))
                self.assertIn(error, self.page())
        self.assertEqual(self.db_rows("SELECT id FROM tuning_schedule_tasks"), [])
        self.assertEqual(self.db_rows("SELECT id FROM tuning_task_reminders WHERE schedule_task_id IS NOT NULL"), [])
        self.assertEqual(self.db_rows("SELECT id FROM order_event_tasks"), [])

    def test_reminder_is_sent_to_telegram_with_the_task_time(self):
        self.login_admin()
        event_id = self.add_dialog()
        self.client.post(f"/events/{event_id}/task", data=self.task_form())
        with application_module.app.app_context(), patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as send:
            db = application_module.get_db()
            sent = application_module._send_due_task_reminders(db, f"{self.tomorrow} 09:31")
            db.commit()
        self.assertEqual(sent, 1)
        name, text = send.call_args[0][1:]
        self.assertEqual(name, self.EMPLOYEE_A)
        self.assertIn("Перезвонить клиенту", text)
        if self.ORDER_KIND == "tuning":
            self.assertIn(self.order_label, text)
        self.assertIn("10:00", text)
        self.assertIn("Не забудь про скидку", text)

    def test_deleting_the_event_removes_its_tasks_and_reminders(self):
        self.login_admin()
        event_id = self.add_dialog()
        self.client.post(f"/events/{event_id}/task", data=self.task_form())
        self.client.post(f"/events/{event_id}/task", data=self.task_form(task_title="Отправить смету"))
        self.assertEqual(len(self.db_rows("SELECT id FROM tuning_schedule_tasks")), 2)
        self.client.post(f"/events/{event_id}/delete")
        self.assertEqual(self.db_rows("SELECT id FROM tuning_schedule_tasks"), [])
        self.assertEqual(self.db_rows("SELECT id FROM tuning_task_reminders WHERE schedule_task_id IS NOT NULL"), [])
        self.assertEqual(self.db_rows("SELECT id FROM order_event_tasks"), [])

    def test_create_task_form_is_offered_on_orders(self):
        self.login_admin()
        self.add_dialog()
        page = self.page()
        self.assertIn("Создать задачу с напоминанием", page)
        self.assertIn('value="Перезвонить клиенту"', page)
        self.assertIn('name="remind_recipient"', page)


class OrderEventTasksTests(_OrderEventTasksFixture, unittest.TestCase):
    pass


class DealerOrderEventTasksTests(_OrderEventTasksFixture, unittest.TestCase):
    ORDER_KIND = "dealer"

    def test_deleting_the_order_removes_event_tasks_and_reminders(self):
        self.login_admin()
        event_id = self.add_dialog()
        self.client.post(f"/events/{event_id}/task", data=self.task_form())
        self.assertEqual(len(self.db_rows("SELECT id FROM tuning_schedule_tasks")), 1)
        self.client.post(f"/dealer/orders/{self.event_order_id}/delete")
        self.assertEqual(self.db_rows("SELECT id FROM dealer_orders WHERE id = ?", (self.event_order_id,)), [])
        self.assertEqual(self.db_rows("SELECT id FROM order_events WHERE id = ?", (event_id,)), [])
        self.assertEqual(self.db_rows("SELECT id FROM tuning_schedule_tasks"), [])
        self.assertEqual(self.db_rows("SELECT id FROM tuning_task_reminders WHERE schedule_task_id IS NOT NULL"), [])
        self.assertEqual(self.db_rows("SELECT id FROM order_event_tasks"), [])

    def test_task_creation_requires_administrator_access(self):
        self.login_admin()
        event_id = self.add_dialog()
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        response = self.client.post(f"/events/{event_id}/task", data=self.task_form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db_rows("SELECT id FROM tuning_schedule_tasks"), [])
        self.assertEqual(self.db_rows("SELECT id FROM tuning_task_reminders WHERE schedule_task_id IS NOT NULL"), [])


class DealerGoodsEventTasksTests(DealerOrderEventTasksTests):
    DEALER_ORDER_TYPE = "goods"


if __name__ == "__main__":
    unittest.main()
