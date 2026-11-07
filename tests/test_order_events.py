import datetime as dt
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module
from test_tuning_schedule_assignments import _TuningScheduleFixture


class OrderEventsTests(_TuningScheduleFixture, unittest.TestCase):
    """«События» of a tuning order: a new request, a dialog, a visit."""

    DAY = "2030-05-10"

    def setUp(self):
        super().setUp()
        self.tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
        self._wipe()
        self.addCleanup(self._wipe)

    def _wipe(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM order_events WHERE order_id >= 0")
            db.commit()

    def add(self, order_kind="tuning", order_id=None, **fields):
        data = {"kind": "dialog", "event_date": "2026-10-20", "text": "Обсудили сроки"}
        data.update(fields)
        return self.client.post(
            f"/events/{order_kind}/{order_id or self.order_id}/add", data=data, follow_redirects=False)

    def events(self, order_kind="tuning", order_id=None):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM order_events WHERE order_kind = ? AND order_id = ? ORDER BY id",
                (order_kind, order_id or self.order_id)).fetchall()]

    def tasks(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT t.*, d.work_date, d.start_time, d.planned_hours FROM tuning_schedule_tasks t "
                "JOIN tuning_schedule_task_days d ON d.task_id = t.id ORDER BY t.id").fetchall()]

    def page(self):
        return self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)

    def test_a_dialog_is_recorded_with_its_own_date(self):
        response = self.add(event_date="2026-09-15", text="Клиент просил скидку")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/edit/{self.order_id}#events"))
        (event,) = self.events()
        self.assertEqual((event["kind"], event["event_date"], event["text"], event["event_time"]),
                         ("dialog", "2026-09-15", "Клиент просил скидку", ""))
        self.assertIsNone(event["schedule_task_id"])
        page = self.page()
        self.assertIn('id="events"', page)
        self.assertIn("Клиент просил скидку", page)

    def test_a_dialog_needs_a_text_and_a_date(self):
        self.add(text="  ")
        self.assertIn("Опишите диалог", self.page())
        self.add(event_date="")
        self.assertIn("Укажите дату события", self.page())
        self.add(kind="new_request")
        self.assertIn("Выберите тип события", self.page())
        self.assertEqual(self.events(), [])

    def test_a_visit_without_a_host_creates_no_task(self):
        self.add(kind="visit", event_date=self.tomorrow, event_time="14:30", duration_minutes="90", text="")
        (event,) = self.events()
        self.assertEqual((event["kind"], event["event_time"], event["duration_minutes"], event["host_employee"]),
                         ("visit", "14:30", 90, ""))
        self.assertEqual(self.tasks(), [])
        self.assertIn("14:30–16:00", self.page())

    def test_a_visit_with_a_host_becomes_an_unpaid_task_in_the_tuning_schedule(self):
        self.put_on_shift(self.EMPLOYEE_A, day=self.tomorrow)
        self.add(kind="visit", event_date=self.tomorrow, event_time="11:00", duration_minutes="120",
                 host_employee=self.EMPLOYEE_A, text="Привезут лодку на осмотр")
        (event,) = self.events()
        (task,) = self.tasks()
        self.assertEqual(event["schedule_task_id"], task["id"])
        self.assertEqual((task["employee_name"], task["work_date"], task["start_time"], task["planned_hours"]),
                         (self.EMPLOYEE_A, self.tomorrow, "11:00", 2.0))
        self.assertEqual((task["rate"], task["order_id"], task["assignment_id"]), (0, self.order_id, None))
        self.assertEqual(task["title"], "Визит: Клиент Мультизадач")
        self.assertIn("Привезут лодку на осмотр", task["comment"])
        calendar = self.client.get(f"/schedule/tuning?date={self.tomorrow}").get_data(as_text=True)
        self.assertIn("Визит: Клиент Мультизадач", calendar)
        self.assertIn("задача в расписании", self.page())

    def test_a_host_who_is_not_on_the_shift_is_added_to_it(self):
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_B)
        self.assertEqual(len(self.tasks()), 1)
        self.assertIsNotNone(self.crew_hours_on(self.EMPLOYEE_B, self.tomorrow))
        self.assertIn("добавлен в смену", self.page())

    def crew_hours_on(self, name, day):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT shift_start, shift_end FROM tuning_schedule_day_crew WHERE work_date = ? AND employee_id = ?",
                (day, self.employee_id(name))).fetchone()

    def test_an_unpaid_visit_task_can_be_marked_done_without_a_payout(self):
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        (task,) = self.tasks()
        response = self.client.post(f"/schedule/tuning/tasks/{task['id']}/status", json={"status": "done"})
        self.assertEqual(response.status_code, 200)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_visit_input_is_validated_and_nothing_is_saved_on_error(self):
        base = dict(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        for bad in (
            {"event_time": ""}, {"event_time": "25:99"}, {"duration_minutes": "abc"},
            {"duration_minutes": "0"}, {"duration_minutes": "9999"},
            {"event_time": "23:30", "duration_minutes": "90"},   # runs past midnight
            {"host_employee": "Не существует"},
            {"event_date": "завтра"},
        ):
            self.add(**{**base, **bad})
        self.assertEqual(self.events(), [])
        self.assertEqual(self.tasks(), [])

    def test_deleting_a_visit_removes_its_task_too(self):
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        (event,) = self.events()
        self.client.post(f"/events/{event['id']}/delete")
        self.assertEqual(self.events(), [])
        self.assertEqual(self.tasks(), [])

    def test_a_paid_visit_task_stays_when_the_event_is_deleted(self):
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        (event,) = self.events()
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'x', 1, 1, 1, ?, '2026-10-01 10:00')", (self.EMPLOYEE_A, self.tomorrow)).lastrowid
            db.execute("UPDATE tuning_schedule_tasks SET entry_id = ?", (entry_id,))
            db.commit()
        self.client.post(f"/events/{event['id']}/delete")
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.tasks()), 1)

    def test_a_visit_for_a_cancelled_order_still_gets_a_task_but_without_the_project(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_orders SET status = 'cancelled' WHERE id = ?", (self.order_id,))
            db.commit()
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        (task,) = self.tasks()
        self.assertIsNone(task["order_id"])

    def test_deleting_the_order_deletes_its_events(self):
        self.add()
        self.add(kind="visit", event_date=self.tomorrow, event_time="10:00", host_employee=self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            application_module._delete_tuning_order_records(db, [self.order_id])
            db.commit()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.tasks(), [])

    def test_the_panel_and_routes_need_an_administrator_and_a_real_order(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.post(f"/events/tuning/{self.order_id}/add", data={"kind": "dialog"}).status_code, 302)
        self.assertEqual(self.events(), [])
        self.add(order_id=99999999)
        self.add(order_kind="boats")
        with application_module.app.app_context():
            count = application_module.get_db().execute("SELECT COUNT(*) FROM order_events").fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_form_switches_between_a_dialog_and_a_visit(self):
        page = self.page()
        self.assertIn('id="event_kind"', page)
        self.assertIn("data-visit-only", page)
        self.assertIn(f'<option value="{self.EMPLOYEE_A}">', page)
        self.assertIn('name="duration_minutes"', page)

    def test_events_stand_newest_day_first_with_planned_visits_on_top(self):
        self.add(event_date="2026-01-05", text="Старый диалог")
        self.add(kind="visit", event_date="2030-06-01", event_time="10:00", text="Будущий визит")
        page = self.page()
        self.assertLess(page.index("Будущий визит"), page.index("Старый диалог"))
        self.assertIn("предстоит", page)


class DealerOrderEventsTests(unittest.TestCase):
    CLIENT = "Дилерский Покупатель События"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.order_id = db.execute(
                "INSERT INTO dealer_orders (client_name, status, order_date, created_at, updated_at) "
                "VALUES (?, 'new_request', '2026-10-01', '2026-10-01 10:00', '2026-10-01 10:00')", (self.CLIENT,)
            ).lastrowid
            db.commit()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM order_events WHERE order_kind = 'dealer' AND order_id = ?", (self.order_id,))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (self.order_id,))
            db.commit()

    def test_the_dealer_order_has_the_events_panel_and_takes_a_dialog(self):
        page = self.client.get(f"/dealer/orders/{self.order_id}").get_data(as_text=True)
        self.assertIn('id="events"', page)
        response = self.client.post(f"/events/dealer/{self.order_id}/add", data={
            "kind": "dialog", "event_date": "2026-10-05", "text": "Звонил, уточнял комплектацию"})
        self.assertTrue(response.headers["Location"].endswith(f"/dealer/orders/{self.order_id}#events"))
        page = self.client.get(f"/dealer/orders/{self.order_id}").get_data(as_text=True)
        self.assertIn("Звонил, уточнял комплектацию", page)

    def test_a_dealer_visit_gives_the_host_a_task_not_tied_to_a_tuning_project(self):
        day = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        with application_module.app.app_context():
            db = application_module.get_db()
            employee = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) VALUES ('Принимающий Дилер', '2026-09-01 09:00', NULL)").lastrowid
            db.execute("INSERT INTO employee_positions (employee_id, position, created_at) "
                       "VALUES (?, 'Администратор', '2026-09-01 09:00')", (employee,))
            db.commit()
        try:
            self.client.post(f"/events/dealer/{self.order_id}/add", data={
                "kind": "visit", "event_date": day, "event_time": "12:00", "duration_minutes": "60",
                "host_employee": "Принимающий Дилер", "text": "Покажем лодку"})
            with application_module.app.app_context():
                db = application_module.get_db()
                task = db.execute("SELECT * FROM tuning_schedule_tasks WHERE employee_name = 'Принимающий Дилер'").fetchone()
                self.assertIsNotNone(task)
                self.assertIsNone(task["order_id"])
                self.assertIn("Дилерский центр, заказ №%d" % self.order_id, task["comment"])
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                for row in db.execute("SELECT id FROM tuning_schedule_tasks WHERE employee_name = 'Принимающий Дилер'").fetchall():
                    db.execute("DELETE FROM tuning_schedule_task_days WHERE task_id = ?", (row["id"],))
                    db.execute("DELETE FROM tuning_schedule_tasks WHERE id = ?", (row["id"],))
                db.execute("DELETE FROM tuning_schedule_day_crew WHERE employee_id IN (SELECT id FROM employees WHERE name = 'Принимающий Дилер')")
                db.execute("DELETE FROM employee_positions WHERE employee_id IN (SELECT id FROM employees WHERE name = 'Принимающий Дилер')")
                db.execute("DELETE FROM employees WHERE name = 'Принимающий Дилер'")
                db.commit()

    def test_deleting_a_dealer_order_deletes_its_events(self):
        self.client.post(f"/events/dealer/{self.order_id}/add", data={
            "kind": "dialog", "event_date": "2026-10-05", "text": "Разговор"})
        self.client.post(f"/dealer/orders/{self.order_id}/delete")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM order_events WHERE order_kind = 'dealer' AND order_id = ?",
                (self.order_id,)).fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
