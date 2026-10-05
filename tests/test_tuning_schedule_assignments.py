import unittest

from werkzeug.datastructures import MultiDict

from support import application_module
from test_tuning_task_assignments import _TuningTaskFixture


class TuningScheduleAssignmentTests(_TuningTaskFixture, unittest.TestCase):
    """A task assigned for one date becomes a card on that employee's day in
    the tuning schedule — at the start of their shift, or after the tasks
    they already have — and someone who isn't on that day's roster has to be
    added to the shift (with working hours) first."""

    DAY = "2026-10-12"

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in ("tuning_schedule_task_days", "tuning_schedule_tasks", "tuning_schedule_day_crew"):
                db.execute(f"DELETE FROM {table}")
            db.commit()

    def employee_id(self, name):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM employees WHERE name = ?", (name,)
            ).fetchone()["id"]

    def put_on_shift(self, name, start="09:00", end="18:00", day=None):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT OR REPLACE INTO tuning_schedule_day_crew "
                "(work_date, employee_id, created_at, shift_start, shift_end) "
                "VALUES (?, ?, '2026-10-01 09:00', ?, ?)",
                (day or self.DAY, self.employee_id(name), start, end),
            )
            db.commit()

    def assign(self, rows, due_from=None, due_to="", **extra):
        self.login_admin()
        data = MultiDict()
        for name, rate, hours in rows:
            data.add("employee_name[]", name)
            data.add("rate[]", str(rate))
            data.add("norm_hours[]", str(hours))
        data["comment"] = "Коммент"
        data["due_from"] = self.DAY if due_from is None else due_from
        data["due_to"] = due_to
        for key, value in extra.items():
            data[key] = value
        return self.client.post(f"/tuning/{self.order_id}/item/{self.item_id}/assign", data=data)

    def cards(self, name=None, day=None):
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT t.id AS task_id, t.assignment_id, t.employee_name, d.work_date, "
                "d.start_time, d.planned_hours FROM tuning_schedule_task_days d "
                "JOIN tuning_schedule_tasks t ON t.id = d.task_id "
                "WHERE d.work_date = ? ORDER BY d.start_time", (day or self.DAY,),
            ).fetchall()
            return [dict(r) for r in rows if name is None or r["employee_name"] == name]

    def assignments(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? ORDER BY id", (self.item_id,)
            ).fetchall()]

    def crew_hours(self, name):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT shift_start, shift_end FROM tuning_schedule_day_crew "
                "WHERE work_date = ? AND employee_id = ?", (self.DAY, self.employee_id(name)),
            ).fetchone()
            return (row["shift_start"], row["shift_end"]) if row else None

    def board(self):
        return self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)

    def test_a_task_for_one_date_lands_at_the_start_of_the_shift(self):
        self.put_on_shift(self.EMPLOYEE_A, "10:00", "19:00")
        response = self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.assertEqual(response.status_code, 302)
        cards = self.cards(self.EMPLOYEE_A)
        self.assertEqual([(c["start_time"], c["planned_hours"]) for c in cards], [("10:00", 2)])
        self.assertEqual(cards[0]["assignment_id"], self.assignments()[0]["id"])
        self.assertIn("задача добавлена в расписание на 12/10/2026, 10:00–12:00", self.board())

    def test_the_card_shows_up_in_the_schedule_calendar(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn("tuning-calendar-card", page)
        self.assertIn("Полировка корпуса", page)
        self.assertIn("09:00–11:00", page)

    def test_the_next_task_goes_after_the_one_already_there(self):
        self.put_on_shift(self.EMPLOYEE_A, "09:00", "18:00")
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        # a second work of the same order for the same person, same day
        with application_module.app.app_context():
            db = application_module.get_db()
            second_item = db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Вторая работа', 500, 2, 2000, 0, 'in_progress')",
                (self.order_id,),
            ).lastrowid
            db.commit()
        self.login_admin()
        data = MultiDict([("employee_name[]", self.EMPLOYEE_A), ("rate[]", "100"),
                          ("norm_hours[]", "1.5"), ("comment", ""),
                          ("due_from", self.DAY), ("due_to", "")])
        self.client.post(f"/tuning/{self.order_id}/item/{second_item}/assign", data=data)
        starts = [(c["start_time"], c["planned_hours"]) for c in self.cards(self.EMPLOYEE_A)]
        self.assertEqual(starts, [("09:00", 2), ("11:00", 1.5)])

    def test_another_employees_tasks_do_not_push_this_one(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        self.assign([(self.EMPLOYEE_A, 100, 3), (self.EMPLOYEE_B, 100, 1)])
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "09:00")
        self.assertEqual(self.cards(self.EMPLOYEE_B)[0]["start_time"], "09:00")

    def test_a_shift_without_stored_hours_counts_from_the_default_workday(self):
        self.put_on_shift(self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_schedule_day_crew SET shift_start = NULL, shift_end = NULL")
            db.commit()
        self.assign([(self.EMPLOYEE_A, 100, 1)])
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "09:00")

    def test_an_employee_off_the_roster_stops_the_assignment_with_a_question(self):
        response = self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assignments(), [])  # nothing created yet
        self.assertEqual(self.cards(), [])
        page = self.board()
        self.assertIn("Сотрудник не стоит в расписании", page)
        self.assertIn(self.EMPLOYEE_A, page)
        self.assertIn("Добавить в смену и поручить", page)
        self.assertIn('name="shift_start"', page)
        # shown once only
        self.assertNotIn("Сотрудник не стоит в расписании", self.board())

    def test_the_question_also_appears_on_the_order_page(self):
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Сотрудник не стоит в расписании", page)

    def test_agreeing_adds_the_employee_to_the_shift_and_places_the_task(self):
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        response = self.assign(
            [(self.EMPLOYEE_A, 100, 2)], shift_decision="add",
            shift_start="11:00", shift_end="20:00",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.crew_hours(self.EMPLOYEE_A), ("11:00", "20:00"))
        self.assertEqual(len(self.assignments()), 1)
        self.assertEqual(
            [(c["start_time"], c["planned_hours"]) for c in self.cards(self.EMPLOYEE_A)],
            [("11:00", 2)],
        )

    def test_agreeing_with_bad_hours_asks_again_and_creates_nothing(self):
        for start, end in (("", ""), ("18:00", "09:00"), ("10:00", "")):
            self.assign(
                [(self.EMPLOYEE_A, 100, 2)], shift_decision="add",
                shift_start=start, shift_end=end,
            )
            self.assertEqual(self.assignments(), [])
            self.assertIsNone(self.crew_hours(self.EMPLOYEE_A))
        self.assertIn("Сотрудник не стоит в расписании", self.board())

    def test_choosing_to_assign_without_the_schedule_keeps_the_roster_untouched(self):
        self.assign([(self.EMPLOYEE_A, 100, 2)], shift_decision="skip")
        self.assertEqual(len(self.assignments()), 1)
        self.assertEqual(self.cards(), [])
        self.assertIsNone(self.crew_hours(self.EMPLOYEE_A))

    def test_only_the_employees_off_the_roster_are_held_back_or_skipped(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)])
        self.assertEqual(self.assignments(), [])
        self.assertIn(self.EMPLOYEE_B, self.board())
        self.assign([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)], shift_decision="skip")
        self.assertEqual(len(self.assignments()), 2)
        self.assertEqual(len(self.cards(self.EMPLOYEE_A)), 1)
        self.assertEqual(self.cards(self.EMPLOYEE_B), [])

    def test_a_period_or_no_date_is_not_put_on_the_schedule(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 1)], due_from="2026-10-12", due_to="2026-10-14")
        self.assign([(self.EMPLOYEE_B, 100, 1)], due_from="")
        self.assertEqual(len(self.assignments()), 2)
        self.assertEqual(self.cards(), [])
        self.assertNotIn("Сотрудник не стоит в расписании", self.board())

    def test_hours_that_do_not_fit_a_day_assign_the_task_without_a_card(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 20)])
        self.assertEqual(len(self.assignments()), 1)
        self.assertEqual(self.cards(), [])
        self.assertIn("не помещается", self.board())

    def test_a_task_running_past_the_end_of_the_shift_is_flagged(self):
        self.put_on_shift(self.EMPLOYEE_A, "09:00", "10:00")
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.assertEqual(len(self.cards(self.EMPLOYEE_A)), 1)
        self.assertIn("Выходит за конец смены (10:00)", self.board())

    def test_the_question_belongs_to_its_own_order(self):
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        with application_module.app.app_context():
            db = application_module.get_db()
            other = db.execute(
                "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
                "sale_channel, phone, discount_pct, subtotal, total, status, order_date, created_at, "
                "updated_at, source) VALUES (?, 'Другой', 'boat', 'Лодка', 'direct', '', 0, 0, 0, "
                "'in_progress', '2026-09-01', '2026-09-01 10:00', '2026-09-01 10:00', 'manual')",
                (self.client_id,),
            ).lastrowid
            db.commit()
        page = self.client.get(f"/tuning/{other}/board").get_data(as_text=True)
        self.assertNotIn("Сотрудник не стоит в расписании", page)

    def test_a_task_scheduled_from_the_schedule_page_is_not_duplicated(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.login_admin()
        data = MultiDict([
            ("employee_name", self.EMPLOYEE_A), ("rate", "100"), ("comment", ""),
            ("order_id", str(self.order_id)), ("order_item_id", str(self.item_id)),
            ("return_date", self.DAY), ("work_date[]", self.DAY),
            ("start_time[]", "13:00"), ("planned_hours[]", "2"),
        ])
        self.client.post("/schedule/tuning/tasks", data=data)
        self.assertEqual(len(self.cards(self.EMPLOYEE_A)), 1)
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "13:00")

    def test_revoking_the_task_removes_its_card(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.login_admin()
        self.client.post(f"/tuning/assignments/{self.assignments()[0]['id']}/revoke")
        self.assertEqual(self.cards(), [])

    # --- editing on the board keeps the card in step -----------------------

    def edit_due(self, assignment_id, due_from, due_to=""):
        self.login_admin()
        return self.client.post(
            f"/tuning/assignments/{assignment_id}/dates",
            data={"due_from": due_from, "due_to": due_to, "completed_date": ""},
        )

    def test_changing_the_date_moves_the_card_to_the_new_day(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_A, "10:00", "19:00", day="2026-10-13")
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        assignment_id = self.assignments()[0]["id"]
        self.edit_due(assignment_id, "2026-10-13")
        self.assertEqual(self.cards(day=self.DAY), [])
        moved = self.cards(day="2026-10-13")
        self.assertEqual([(c["start_time"], c["planned_hours"]) for c in moved], [("10:00", 2)])
        self.assertIn("перенесена на 13/10/2026", self.board())

    def test_moving_to_a_day_the_employee_is_off_leaves_the_card_and_warns(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.edit_due(self.assignments()[0]["id"], "2026-10-14")
        self.assertEqual(len(self.cards(day=self.DAY)), 1)
        self.assertIn("осталась на прежней дате", self.board())

    def test_setting_a_date_on_a_task_that_had_none_adds_its_card(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)], due_from="")
        self.assertEqual(self.cards(), [])
        self.edit_due(self.assignments()[0]["id"], self.DAY)
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_A)], ["09:00"])

    def test_setting_a_date_for_someone_off_the_roster_just_says_so(self):
        self.assign([(self.EMPLOYEE_A, 100, 2)], due_from="")
        self.edit_due(self.assignments()[0]["id"], self.DAY)
        self.assertEqual(self.cards(), [])
        self.assertIn("не стоит в расписании на 12/10/2026", self.board())

    def test_changing_the_norm_hours_resizes_the_card(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.login_admin()
        self.client.post(
            f"/tuning/assignments/{self.assignments()[0]['id']}/rate",
            data={"rate": "100", "norm_hours": "3.5"},
        )
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["planned_hours"], 3.5)

    # --- shift hours on the schedule page ---------------------------------

    def test_adding_to_the_shift_from_the_schedule_page_stores_the_hours(self):
        self.login_admin()
        response = self.client.post("/schedule/tuning/crew", data={
            "work_date": self.DAY, "employee_id": str(self.employee_id(self.EMPLOYEE_A)),
            "shift_start": "08:30", "shift_end": "17:00",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.crew_hours(self.EMPLOYEE_A), ("08:30", "17:00"))
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('name="shift_start" value="08:30"', page)

    def test_blank_hours_fall_back_to_the_default_workday(self):
        self.login_admin()
        self.client.post("/schedule/tuning/crew", data={
            "work_date": self.DAY, "employee_id": str(self.employee_id(self.EMPLOYEE_A)),
        })
        self.assertEqual(self.crew_hours(self.EMPLOYEE_A), ("09:00", "18:00"))

    def test_invalid_shift_hours_are_rejected(self):
        self.login_admin()
        for start, end in (("18:00", "09:00"), ("zz", "10:00"), ("09:00", "")):
            self.client.post("/schedule/tuning/crew", data={
                "work_date": self.DAY, "employee_id": str(self.employee_id(self.EMPLOYEE_A)),
                "shift_start": start, "shift_end": end,
            })
            self.assertIsNone(self.crew_hours(self.EMPLOYEE_A))

    def test_shift_hours_can_be_changed_later(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.login_admin()
        self.client.post(
            f"/schedule/tuning/crew/{self.employee_id(self.EMPLOYEE_A)}/hours",
            data={"work_date": self.DAY, "shift_start": "12:00", "shift_end": "20:00"},
        )
        self.assertEqual(self.crew_hours(self.EMPLOYEE_A), ("12:00", "20:00"))
        self.client.post(
            f"/schedule/tuning/crew/{self.employee_id(self.EMPLOYEE_A)}/hours",
            data={"work_date": self.DAY, "shift_start": "20:00", "shift_end": "12:00"},
        )
        self.assertEqual(self.crew_hours(self.EMPLOYEE_A), ("12:00", "20:00"))


if __name__ == "__main__":
    unittest.main()
