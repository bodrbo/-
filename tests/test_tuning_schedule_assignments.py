import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from support import application_module
from test_tuning_task_assignments import _TuningTaskFixture


class _TuningScheduleFixture(_TuningTaskFixture):
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
            # payouts of "free" tasks belong to no assignment, so the base
            # fixture's cleanup doesn't remove them
            db.execute(
                "DELETE FROM entries WHERE employee IN (?, ?)", (self.EMPLOYEE_A, self.EMPLOYEE_B)
            )
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


class TuningScheduleAssignmentTests(_TuningScheduleFixture, unittest.TestCase):
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

    # --- the production SQLite has no upsert -------------------------------

    def test_no_upsert_syntax_in_the_tuning_schedule_module(self):
        # this hosting's SQLite fails on upserts ("near ON: syntax error"),
        # see modules/settings/repository.py
        import pathlib
        folder = pathlib.Path(application_module.__file__).parent / "modules" / "tuning_schedule"
        for path in folder.glob("*.py"):
            self.assertNotIn("ON CONFLICT", path.read_text(encoding="utf-8"), f"{path.name} uses an upsert")

    def test_placing_a_task_on_the_same_day_twice_replaces_the_placement(self):
        from modules.tuning_schedule import repository as tuning_repo
        with application_module.app.app_context():
            db = application_module.get_db()
            task_id = tuning_repo.create_task(db, None, self.EMPLOYEE_A, "Своя", 100, "", "2026-10-01 09:00")
            tuning_repo.add_task_day(db, task_id, self.DAY, "09:00", 2)
            tuning_repo.add_task_day(db, task_id, self.DAY, "13:00", 3)
            rows = [dict(r) for r in tuning_repo.list_task_days(db, task_id)]
        self.assertEqual([(r["start_time"], r["planned_hours"]) for r in rows], [("13:00", 3)])

    def test_a_failure_while_placing_leaves_no_card_less_task_behind(self):
        from unittest import mock
        from modules.tuning_schedule import repository as tuning_repo
        self.put_on_shift(self.EMPLOYEE_A)
        with mock.patch.object(tuning_repo, "add_task_day", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.assign([(self.EMPLOYEE_A, 100, 2)])
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_schedule_tasks"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_setting_the_date_again_repairs_a_task_left_without_a_card(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)], due_from="")
        assignment_id = self.assignments()[0]["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_schedule_tasks (assignment_id, employee_name, title, rate, "
                "created_at) VALUES (?, ?, 'Работа', 100, '2026-10-05 10:00')",
                (assignment_id, self.EMPLOYEE_A),
            )
            db.commit()
        self.edit_due(assignment_id, self.DAY)
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_A)], ["09:00"])
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_schedule_tasks WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()[0]
        self.assertEqual(count, 1)  # the empty one was reused, not duplicated

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
        self.assertIn('name="shift_start" value="08:30"', page)  # editable in the roster window

    def test_the_schedule_page_uses_the_full_width_of_the_work_area(self):
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('<main class="wrap tuning-schedule-wide">', page)
        import pathlib
        css = (pathlib.Path(application_module.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".tuning-schedule-wide { max-width: none;", css)
        self.assertIn(".tuning-schedule-wide .tuning-calendar-board-scroll { max-height: max(480px, calc(100vh - 150px)); }", css)

    def test_the_who_is_on_shift_panel_is_gone_and_the_roster_button_replaces_it(self):
        self.put_on_shift(self.EMPLOYEE_A, "10:00", "19:00")
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertNotIn("Кто сегодня на смене", page)
        self.assertNotIn("tuning-shift-hours-form", page)
        self.assertIn('onclick="openTuningRosterModal()"', page)
        self.assertIn('id="tuningRosterModal"', page)
        self.assertIn("<span>Состав</span>", page)
        self.assertIn("<b>1</b>", page)  # shift size on the button
        self.assertIn(self.EMPLOYEE_A, page.split('id="tuningRosterModal"')[1])
        self.assertIn('name="shift_start" value="10:00"', page)

    def test_the_roster_window_lists_those_not_yet_on_the_shift_with_hour_fields(self):
        self.put_on_shift(self.EMPLOYEE_A)
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        add_form = page.split('class="schedule-roster-add tuning-roster-add"')[1].split("</form>")[0]
        self.assertIn(self.EMPLOYEE_B, add_form)
        self.assertNotIn(self.EMPLOYEE_A, add_form)
        self.assertIn('name="shift_start"', add_form)
        self.assertIn('name="shift_end"', add_form)

    def test_removing_from_the_roster_is_blocked_while_the_day_has_a_task(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn("Сначала перенесите или удалите задачу", page)
        self.login_admin()
        self.client.post(
            f"/schedule/tuning/crew/{self.employee_id(self.EMPLOYEE_A)}/remove",
            data={"work_date": self.DAY},
        )
        self.assertIsNotNone(self.crew_hours(self.EMPLOYEE_A))
        # without a task the same person can be removed
        self.put_on_shift(self.EMPLOYEE_B)
        self.client.post(
            f"/schedule/tuning/crew/{self.employee_id(self.EMPLOYEE_B)}/remove",
            data={"work_date": self.DAY},
        )
        self.assertIsNone(self.crew_hours(self.EMPLOYEE_B))

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


class TuningScheduleDragTests(_TuningScheduleFixture, unittest.TestCase):
    """Moving cards of the tuning calendar by drag-and-drop (the JSON
    endpoint the page script calls)."""

    def place(self, employee, hours=2, due_from=None):
        """A card on DAY for `employee`, returns (task_id, day_id)."""
        self.put_on_shift(employee)
        self.assign([(employee, 100, hours)], due_from=due_from)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT t.id AS task_id, d.id AS day_id FROM tuning_schedule_tasks t "
                "JOIN tuning_schedule_task_days d ON d.task_id = t.id "
                "JOIN tuning_item_assignments a ON a.id = t.assignment_id "
                "WHERE a.employee_name = ? ORDER BY t.id DESC LIMIT 1", (employee,)
            ).fetchone()
            return row["task_id"], row["day_id"]

    def move(self, task_id, day_id, start_time, target_employee=None):
        self.login_admin()
        payload = {"start_time": start_time}
        if target_employee is not None:
            payload["target_employee_id"] = self.employee_id(target_employee)
        return self.client.post(f"/schedule/tuning/tasks/{task_id}/days/{day_id}/move", json=payload)

    def test_a_card_moves_to_a_new_time_in_its_column(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        response = self.move(task_id, day_id, "13:15")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["card"], {"start": "13:15", "end": "15:15", "employee_name": self.EMPLOYEE_A})
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_A)], ["13:15"])

    def test_the_cards_data_for_the_script_is_on_the_page(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn(f"/schedule/tuning/tasks/{task_id}/days/{day_id}/move", page)
        self.assertIn('data-start-minutes="540"', page)
        self.assertIn('data-duration-minutes="120"', page)
        self.assertIn('onpointerdown="startTuningDrag(event, this)"', page)
        self.assertIn(f'data-employee-id="{self.employee_id(self.EMPLOYEE_A)}"', page)
        self.assertIn("var TUNING_GRID_START = ", page)

    def test_a_card_cannot_overlap_another_card_of_the_same_person(self):
        task_id, day_id = self.place(self.EMPLOYEE_A, hours=2)       # 09:00-11:00
        with application_module.app.app_context():
            db = application_module.get_db()
            second_item = db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Вторая', 1, 1, 1, 0, 'in_progress')", (self.order_id,)
            ).lastrowid
            db.commit()
        self.login_admin()
        self.client.post(f"/tuning/{self.order_id}/item/{second_item}/assign", data=MultiDict([
            ("employee_name[]", self.EMPLOYEE_A), ("rate[]", "100"), ("norm_hours[]", "1"),
            ("comment", ""), ("due_from", self.DAY), ("due_to", "")]))              # 11:00-12:00
        response = self.move(task_id, day_id, "10:30")
        self.assertEqual(response.status_code, 400)
        self.assertIn("это время занято", response.get_json()["message"])
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "09:00")
        self.assertEqual(self.move(task_id, day_id, "12:00").status_code, 200)       # right after is fine

    def test_bad_times_are_rejected(self):
        task_id, day_id = self.place(self.EMPLOYEE_A, hours=2)
        for bad in ("", "25:99", "abc", None):
            self.assertEqual(self.move(task_id, day_id, bad).status_code, 400, bad)
        self.assertEqual(self.move(task_id, day_id, "23:00").status_code, 400)       # 2 h don't fit before midnight
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "09:00")

    def test_dragging_to_another_column_hands_an_order_task_over(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        response = self.move(task_id, day_id, "10:00", target_employee=self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["card"]["employee_name"], self.EMPLOYEE_B)
        self.assertEqual(self.cards(self.EMPLOYEE_A), [])
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_B)], ["10:00"])
        assignment = self.assignments()[0]
        self.assertEqual((assignment["employee_name"], assignment["assignment_status"]), (self.EMPLOYEE_B, "pending"))

    def test_the_target_must_be_on_the_shift_that_day(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        response = self.move(task_id, day_id, "10:00", target_employee=self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 400)
        self.assertIn("не стоит в смене", response.get_json()["message"])
        self.assertEqual(self.assignments()[0]["employee_name"], self.EMPLOYEE_A)

    def test_a_finished_task_cannot_be_handed_over_by_dragging(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET assignment_status = 'done'")
            db.commit()
        response = self.move(task_id, day_id, "10:00", target_employee=self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Выполненную", response.get_json()["message"])
        self.assertEqual(self.move(task_id, day_id, "10:00").status_code, 200)       # its time can still change

    def test_a_multi_day_task_changes_time_but_not_employee(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        from modules.tuning_schedule import repository as tuning_repo
        with application_module.app.app_context():
            tuning_repo.add_task_day(application_module.get_db(), task_id, "2026-10-13", "09:00", 2)
        response = self.move(task_id, day_id, "10:00", target_employee=self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 400)
        self.assertIn("несколько дней", response.get_json()["message"])
        self.assertEqual(self.move(task_id, day_id, "10:00").status_code, 200)

    def test_a_free_task_moves_between_columns(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        self.login_admin()
        self.client.post("/schedule/tuning/tasks", data=MultiDict([
            ("employee_name", self.EMPLOYEE_A), ("rate", "100"), ("comment", ""), ("title", "Уборка"),
            ("return_date", self.DAY), ("work_date[]", self.DAY), ("start_time[]", "09:00"),
            ("planned_hours[]", "1")]))
        cards = self.cards(self.EMPLOYEE_A)
        with application_module.app.app_context():
            day_id = application_module.get_db().execute(
                "SELECT id FROM tuning_schedule_task_days WHERE task_id = ?", (cards[0]["task_id"],)
            ).fetchone()["id"]
        response = self.move(cards[0]["task_id"], day_id, "15:00", target_employee=self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_B)], ["15:00"])
        self.assertEqual(self.cards(self.EMPLOYEE_A), [])

    def test_unknown_cards_and_bad_requests_are_reported(self):
        self.login_admin()
        self.assertEqual(
            self.client.post("/schedule/tuning/tasks/999/days/999/move", json={"start_time": "10:00"}).status_code, 400)
        self.assertEqual(
            self.client.post("/schedule/tuning/tasks/1/days/1/move", data="not json").status_code, 400)

    def test_the_route_needs_an_administrator(self):
        task_id, day_id = self.place(self.EMPLOYEE_A)
        anonymous = application_module.app.test_client()
        response = anonymous.post(
            f"/schedule/tuning/tasks/{task_id}/days/{day_id}/move", json={"start_time": "14:00"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.cards(self.EMPLOYEE_A)[0]["start_time"], "09:00")


class TuningScheduleStatusTests(_TuningScheduleFixture, unittest.TestCase):
    """The status picker on a calendar card: it is the task's real status, so
    an order task changes on the order board too (and the other way round)."""

    def place_linked(self, employee=None, hours=2):
        employee = employee or self.EMPLOYEE_A
        self.put_on_shift(employee)
        self.assign([(employee, 100, hours)])
        return self.cards(employee)[0]["task_id"]

    def place_free(self, employee=None):
        employee = employee or self.EMPLOYEE_A
        self.put_on_shift(employee)
        self.login_admin()
        self.client.post("/schedule/tuning/tasks", data=MultiDict([
            ("employee_name", employee), ("rate", "100"), ("comment", ""), ("title", "Уборка"),
            ("return_date", self.DAY), ("work_date[]", self.DAY), ("start_time[]", "09:00"),
            ("planned_hours[]", "1")]))
        return self.cards(employee)[0]["task_id"]

    def set_status(self, task_id, status):
        self.login_admin()
        return self.client.post(f"/schedule/tuning/tasks/{task_id}/status", json={"status": status})

    def page(self):
        return self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)

    def test_every_card_carries_a_status_picker_with_the_current_status_selected(self):
        task_id = self.place_linked()
        page = self.page()
        select = page.split('class="tuning-card-status"')[1].split("</select>")[0]
        self.assertIn(f"/schedule/tuning/tasks/{task_id}/status", select)
        for label in ("Ожидает ответа", "Принята", "В работе", "Выполнена", "Отклонена"):
            self.assertIn(label, select)
        self.assertIn('<option value="pending" selected>', select)
        self.assertIn("tuning-calendar-card status-pending", page)
        self.assertIn("changeTuningCardStatus(this)", page)

    def test_dragging_does_not_start_from_the_status_picker(self):
        self.place_linked()
        self.assertIn("event.target.closest('select, option, button, a')", self.page())

    def test_changing_an_order_task_status_changes_the_board_too(self):
        task_id = self.place_linked()
        response = self.set_status(task_id, "in_progress")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual((body["ok"], body["status"], body["label"]), (True, "in_progress", "В работе"))
        self.assertIn(f"заказа №{self.order_id}", body["message"])
        self.assertEqual(self.assignments()[0]["assignment_status"], "in_progress")
        board = self.board()
        self.assertIn('<option value="in_progress" selected>', board)
        self.assertIn("tuning-calendar-card status-in_progress", self.page())

    def test_a_status_changed_on_the_board_shows_on_the_card(self):
        self.place_linked()
        self.login_admin()
        self.client.post(
            f"/tuning/assignments/{self.assignments()[0]['id']}/status", data={"status": "accepted"})
        page = self.page()
        self.assertIn("tuning-calendar-card status-accepted", page)
        self.assertIn('<option value="accepted" selected>', page)

    def test_finishing_a_task_from_the_card_pays_it_out_once(self):
        task_id = self.place_linked()
        self.set_status(task_id, "done")
        self.assertIsNotNone(self.assignments()[0]["entry_id"])
        self.assertIsNotNone(self.assignments()[0]["completed_at"])
        self.set_status(task_id, "done")
        self.set_status(task_id, "in_progress")
        self.set_status(task_id, "done")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_a_free_task_status_changes_and_pays_on_done(self):
        task_id = self.place_free()
        self.assertEqual(self.set_status(task_id, "accepted").status_code, 200)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(db.execute("SELECT status FROM tuning_schedule_tasks WHERE id = ?", (task_id,)).fetchone()[0], "accepted")
        response = self.set_status(task_id, "done")
        self.assertNotIn("доске задач", response.get_json()["message"])  # no order board for a free task
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT status, entry_id FROM tuning_schedule_tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["entry_id"])
        self.assertIn("tuning-calendar-card status-done", self.page())

    def test_bad_requests_are_refused_and_change_nothing(self):
        task_id = self.place_linked()
        self.assertEqual(self.set_status(task_id, "bogus").status_code, 400)
        self.assertEqual(self.set_status(task_id, "").status_code, 400)
        self.assertEqual(self.set_status(999999, "done").status_code, 400)
        self.assertEqual(self.assignments()[0]["assignment_status"], "pending")

    def test_the_old_form_still_works(self):
        task_id = self.place_linked()
        self.login_admin()
        response = self.client.post(
            f"/schedule/tuning/tasks/{task_id}/status", data={"status": "accepted", "return_date": self.DAY})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assignments()[0]["assignment_status"], "accepted")

    def test_the_status_route_needs_an_administrator(self):
        task_id = self.place_linked()
        anonymous = application_module.app.test_client()
        response = anonymous.post(f"/schedule/tuning/tasks/{task_id}/status", json={"status": "done"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.assignments()[0]["assignment_status"], "pending")


class TuningScheduleAdministratorTaskTests(_TuningScheduleFixture, unittest.TestCase):
    """«Добавить задачу» for administrators: they are listed even when they
    are not on the day's shift, need no rate (they are salaried), and are put
    on the shift automatically."""

    ADMIN = "Тестовый Администратор"
    HYBRID = "Тестовый Админ-Мастер"

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._drop_people(db)
            for name, positions in ((self.ADMIN, ["Администратор"]), (self.HYBRID, ["Администратор", "Тюнингмэн"])):
                employee_id = db.execute(
                    "INSERT INTO employees (name, created_at) VALUES (?, '2026-01-01 09:00')", (name,)
                ).lastrowid
                for position in positions:
                    db.execute(
                        "INSERT INTO employee_positions (employee_id, position, created_at) "
                        "VALUES (?, ?, '2026-01-01 09:00')", (employee_id, position))
            db.commit()
        self.addCleanup(self._cleanup_people)

    def _drop_people(self, db):
        for name in (self.ADMIN, self.HYBRID):
            row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
            if row:
                for table in ("tuning_schedule_day_crew", "employee_positions"):
                    db.execute(f"DELETE FROM {table} WHERE employee_id = ?", (row["id"],))
                db.execute("DELETE FROM employees WHERE id = ?", (row["id"],))
            db.execute("DELETE FROM entries WHERE employee = ?", (name,))
            db.execute("DELETE FROM tuning_item_assignments WHERE employee_name = ?", (name,))
        db.execute("DELETE FROM tuning_schedule_task_days")
        db.execute("DELETE FROM tuning_schedule_tasks")
        db.commit()

    def _cleanup_people(self):
        with application_module.app.app_context():
            self._drop_people(application_module.get_db())

    def add_free_task(self, employee, rate="", start="09:00", hours="2", day=None, title="Планёрка"):
        self.login_admin()
        data = MultiDict([
            ("employee_name", employee), ("rate", rate), ("comment", ""), ("title", title),
            ("return_date", self.DAY), ("work_date[]", day or self.DAY),
            ("start_time[]", start), ("planned_hours[]", hours)])
        return self.client.post("/schedule/tuning/tasks", data=data)

    def add_order_task(self, employee, rate=""):
        self.login_admin()
        data = MultiDict([
            ("employee_name", employee), ("rate", rate), ("comment", ""),
            ("order_id", str(self.order_id)), ("order_item_id", str(self.item_id)),
            ("return_date", self.DAY), ("work_date[]", self.DAY),
            ("start_time[]", "10:00"), ("planned_hours[]", "2")])
        return self.client.post("/schedule/tuning/tasks", data=data)

    def crew_row(self, name, day=None):
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute(
                "SELECT shift_start, shift_end FROM tuning_schedule_day_crew c JOIN employees e "
                "ON e.id = c.employee_id WHERE e.name = ? AND c.work_date = ?", (name, day or self.DAY)
            ).fetchone()
            return (row["shift_start"], row["shift_end"]) if row else None

    def task_rows(self, name):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT id, rate, status, entry_id, assignment_id FROM tuning_schedule_tasks WHERE employee_name = ?",
                (name,)).fetchall()]

    def entries_of(self, name):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = ?", (name,)).fetchone()[0]

    def notice(self):
        return self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)

    def test_every_administrator_is_in_the_employee_list_even_off_the_shift(self):
        self.put_on_shift(self.EMPLOYEE_A)
        page = self.notice()
        form = page.split('id="task-employee"')[1].split("</select>")[0]
        self.assertIn(f'<option value="{self.ADMIN}" data-admin="1">{self.ADMIN} · администратор (не в смене)</option>', form)
        self.assertIn(f'<option value="{self.HYBRID}" data-admin="1">', form)
        self.assertIn(f'<option value="{self.EMPLOYEE_A}" data-admin="0">{self.EMPLOYEE_A}</option>', form)
        self.assertNotIn(self.EMPLOYEE_B, form)  # an ordinary person off the shift is still not offered

    def test_an_administrator_on_the_shift_is_listed_once_and_marked(self):
        self.put_on_shift(self.ADMIN)
        form = self.notice().split('id="task-employee"')[1].split("</select>")[0]
        self.assertEqual(form.count(f'value="{self.ADMIN}"'), 1)
        self.assertIn(f'data-admin="1">{self.ADMIN} · администратор</option>', form)

    def test_the_page_hides_the_rate_field_for_an_administrator(self):
        page = self.notice()
        self.assertIn('id="tuningScheduleRateField"', page)
        self.assertIn('onchange="updateTuningScheduleRate()"', page)
        self.assertIn("function updateTuningScheduleRate()", page)
        self.assertIn("Администратор на окладе — ставка не нужна.", page)

    def test_a_task_for_an_administrator_needs_no_rate_and_puts_them_on_the_shift(self):
        response = self.add_free_task(self.ADMIN)
        self.assertEqual(response.status_code, 302)
        tasks = self.task_rows(self.ADMIN)
        self.assertEqual([t["rate"] for t in tasks], [0])
        self.assertEqual(self.crew_row(self.ADMIN), ("09:00", "18:00"))
        self.assertEqual([c["start_time"] for c in self.cards(self.ADMIN)], ["09:00"])
        page = self.notice()
        self.assertIn(f"{self.ADMIN} добавлен в смену на 12.10", page)

    def test_a_submitted_rate_is_ignored_for_an_administrator(self):
        self.add_free_task(self.ADMIN, rate="5000")
        self.add_free_task(self.HYBRID, rate="5000", day="2026-10-13")
        self.assertEqual([t["rate"] for t in self.task_rows(self.ADMIN)], [0])
        self.assertEqual([t["rate"] for t in self.task_rows(self.HYBRID)], [0])  # admin + tuningman: still salaried

    def test_an_ordinary_employee_still_needs_a_rate(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.add_free_task(self.EMPLOYEE_A, rate="")
        self.assertEqual(self.task_rows(self.EMPLOYEE_A), [])
        self.assertIn("Ставка должна быть больше нуля", self.notice())
        self.add_free_task(self.EMPLOYEE_A, rate="150")
        self.assertEqual([t["rate"] for t in self.task_rows(self.EMPLOYEE_A)], [150])

    def test_an_administrator_already_on_the_shift_is_not_added_twice_or_announced(self):
        self.put_on_shift(self.ADMIN, "10:00", "19:00")
        self.add_free_task(self.ADMIN, start="10:00")
        self.assertEqual(self.crew_row(self.ADMIN), ("10:00", "19:00"))
        self.assertNotIn("добавлен в смену", self.notice())

    def test_the_automatic_shift_is_widened_to_cover_an_early_or_late_task(self):
        self.add_free_task(self.ADMIN, start="07:00", hours="1")
        self.add_free_task(self.ADMIN, start="20:00", hours="2", day="2026-10-13")
        self.assertEqual(self.crew_row(self.ADMIN), ("07:00", "18:00"))
        self.assertEqual(self.crew_row(self.ADMIN, "2026-10-13"), ("09:00", "22:00"))

    def test_an_order_task_for_an_administrator_is_created_with_no_rate(self):
        response = self.add_order_task(self.ADMIN)
        self.assertEqual(response.status_code, 302)
        assignments = [a for a in self.assignments() if a["employee_name"] == self.ADMIN]
        self.assertEqual([(a["rate"], a["norm_hours"]) for a in assignments], [(0, 2)])
        self.assertEqual(self.crew_row(self.ADMIN), ("09:00", "18:00"))
        self.assertEqual(len(self.cards(self.ADMIN)), 1)

    def test_finishing_an_administrators_task_pays_nothing(self):
        self.add_free_task(self.ADMIN)
        task_id = self.task_rows(self.ADMIN)[0]["id"]
        self.login_admin()
        response = self.client.post(f"/schedule/tuning/tasks/{task_id}/status", json={"status": "done"})
        self.assertEqual(response.status_code, 200)
        row = self.task_rows(self.ADMIN)[0]
        self.assertEqual((row["status"], row["entry_id"]), ("done", None))
        self.assertEqual(self.entries_of(self.ADMIN), 0)

    def test_a_finished_order_task_of_an_administrator_has_no_missing_payout_flag(self):
        self.add_order_task(self.ADMIN)
        task_id = self.task_rows(self.ADMIN)[0]["id"]
        self.login_admin()
        self.client.post(f"/schedule/tuning/tasks/{task_id}/status", json={"status": "done"})
        self.assertEqual(self.entries_of(self.ADMIN), 0)
        board = self.board()
        self.assertNotIn("Нет выплаты", board)
        self.assertNotIn("repair-payouts", board)

    def test_the_board_can_edit_hours_of_a_rate_free_task_but_not_give_an_ordinary_one_zero_pay(self):
        self.add_order_task(self.ADMIN)
        admin_assignment = [a for a in self.assignments() if a["employee_name"] == self.ADMIN][0]
        self.login_admin()
        self.client.post(f"/tuning/assignments/{admin_assignment['id']}/rate", data={"rate": "0", "norm_hours": "3"})
        updated = [a for a in self.assignments() if a["employee_name"] == self.ADMIN][0]
        self.assertEqual((updated["rate"], updated["norm_hours"]), (0, 3))
        self.put_on_shift(self.EMPLOYEE_A)
        self.assign([(self.EMPLOYEE_A, 100, 1)])
        ordinary = [a for a in self.assignments() if a["employee_name"] == self.EMPLOYEE_A][0]
        self.client.post(f"/tuning/assignments/{ordinary['id']}/rate", data={"rate": "0", "norm_hours": "1"})
        self.assertEqual([a["rate"] for a in self.assignments() if a["employee_name"] == self.EMPLOYEE_A], [100])

    def test_someone_who_is_neither_staff_nor_an_administrator_is_still_refused(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO employees (name, created_at) VALUES ('Тестовый Менеджер', 'x')")
            db.commit()
        try:
            self.add_free_task("Тестовый Менеджер", rate="100")
            self.assertEqual(self.task_rows("Тестовый Менеджер"), [])
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM employees WHERE name = 'Тестовый Менеджер'")
                db.commit()


class TuningScheduleQuickTaskTests(_TuningScheduleFixture, unittest.TestCase):
    """Click on an empty place of the calendar -> a window to add a task for
    anyone, optionally tied to a project or to a work inside it."""

    def quick(self, employee=None, start="10:00", hours="1.5", rate="200", title="Проверка",
              order_id=None, item_id=None, comment=""):
        self.login_admin()
        data = MultiDict([
            ("employee_name", employee or self.EMPLOYEE_A), ("rate", rate), ("comment", comment),
            ("title", title), ("return_date", self.DAY), ("work_date[]", self.DAY),
            ("start_time[]", start), ("planned_hours[]", hours),
            ("order_id", "" if order_id is None else str(order_id)),
            ("order_item_id", "" if item_id is None else str(item_id)),
        ])
        return self.client.post("/schedule/tuning/tasks", data=data)

    def page(self):
        return self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)

    def task(self, employee=None):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM tuning_schedule_tasks WHERE employee_name = ? ORDER BY id DESC LIMIT 1",
                (employee or self.EMPLOYEE_A,)).fetchone()
            return dict(row) if row else None

    def test_the_page_has_the_window_and_the_columns_know_their_employee(self):
        self.put_on_shift(self.EMPLOYEE_A)
        page = self.page()
        self.assertIn('id="quickTaskModal"', page)
        self.assertIn(f'data-employee-name="{self.EMPLOYEE_A}"', page)
        self.assertIn("Нажмите на свободное место, чтобы добавить задачу", page)
        self.assertIn("openQuickTask(column.dataset.employeeName, minutes)", page)
        self.assertIn("if (event.target.closest('.tuning-calendar-card')", page)  # a click on a card is not an empty place
        modal = page.split('id="quickTaskModal"')[1]
        self.assertIn('name="order_id"', modal)
        self.assertIn('name="order_item_id"', modal)
        self.assertIn(self.EMPLOYEE_A, modal.split('id="quickTaskEmployee"')[1].split("</select>")[0])

    def test_the_window_is_wide_two_columns_and_has_no_scroll_area_of_its_own(self):
        self.put_on_shift(self.EMPLOYEE_A)
        page = self.page()
        form = page.split('id="quickTaskForm"')[1].split("</form>")[0]
        self.assertEqual(form.count('class="quick-task-column"'), 2)
        self.assertIn('class="quick-task-field quick-task-wide"', form)   # the comment spans both columns
        self.assertIn('class="quick-task-actions quick-task-wide"', form)
        import pathlib
        css = (pathlib.Path(application_module.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
        card = css.split(".quick-task-card {")[1].split("}")[0]
        self.assertIn("width: min(940px, 100%)", card)
        self.assertIn("max-height: none", card)
        body = css.split(".quick-task-body {")[1].split("}")[0]
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", body)
        self.assertIn("overflow: visible", body)
        self.assertNotIn("overflow-y: auto", body)  # the form itself must not scroll
        self.assertIn("#quickTaskModal { align-items: start; overflow-y: auto; }", css)  # a short screen scrolls the page behind

    def test_a_free_task_is_added_at_the_chosen_time(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(start="13:15", hours="1.5", title="Уборка")
        card = self.cards(self.EMPLOYEE_A)[0]
        self.assertEqual((card["start_time"], card["planned_hours"]), ("13:15", 1.5))
        task = self.task()
        self.assertEqual((task["title"], task["rate"], task["assignment_id"], task["order_id"]),
                         ("Уборка", 200, None, None))

    def test_a_task_can_be_tied_to_a_project_without_a_work(self):
        self.put_on_shift(self.EMPLOYEE_A)
        response = self.quick(title="Подготовка корпуса", order_id=self.order_id)
        self.assertEqual(response.status_code, 302)
        task = self.task()
        self.assertEqual((task["assignment_id"], task["order_id"]), (None, self.order_id))
        page = self.page()
        self.assertIn(f"№{self.order_id} · Salute 585 HT", page)
        self.assertIn("Подготовка корпуса", page)

    def test_a_task_can_be_tied_to_a_work_inside_a_project(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(order_id=self.order_id, item_id=self.item_id, title="не используется")
        task = self.task()
        self.assertIsNotNone(task["assignment_id"])
        self.assertEqual(task["title"], "Полировка корпуса")  # the work names the task
        assignments = [a for a in self.assignments() if a["employee_name"] == self.EMPLOYEE_A]
        self.assertEqual(len(assignments), 1)
        self.assertIn(f"№{self.order_id} · Salute 585 HT", self.page())

    def test_a_project_level_task_is_paid_against_the_project(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO projects (name, tuning_order_id, created_at) VALUES (?, ?, 'x')",
                (f"Заказ №{self.order_id}", self.order_id))
            db.commit()
        self.addCleanup(self._drop_project)
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(title="Подготовка", order_id=self.order_id, hours="2")
        task_id = self.task()["id"]
        self.login_admin()
        self.client.post(f"/schedule/tuning/tasks/{task_id}/status", json={"status": "done"})
        with application_module.app.app_context():
            db = application_module.get_db()
            entry = db.execute(
                "SELECT amount, project_id FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchone()
            project_id = db.execute(
                "SELECT id FROM projects WHERE tuning_order_id = ?", (self.order_id,)).fetchone()
        self.assertEqual(entry["amount"], 400)
        self.assertIsNotNone(project_id)
        self.assertEqual(entry["project_id"], project_id["id"])

    def test_an_unknown_or_closed_project_is_refused(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(order_id=999999)
        self.assertIsNone(self.task())
        self.assertIn("Проект не найден", self.page())
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_orders SET status = 'cancelled' WHERE id = ?", (self.order_id,))
            db.commit()
        try:
            self.quick(order_id=self.order_id)
            self.assertIsNone(self.task())
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("UPDATE tuning_orders SET status = 'in_progress' WHERE id = ?", (self.order_id,))
                db.commit()

    def _drop_project(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (self.order_id,))
            db.commit()

    def test_the_task_can_go_to_any_employee_on_the_shift_not_only_the_clicked_column(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.put_on_shift(self.EMPLOYEE_B)
        self.quick(employee=self.EMPLOYEE_B, start="11:00")
        self.assertEqual(self.cards(self.EMPLOYEE_A), [])
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_B)], ["11:00"])

    def test_hours_that_do_not_fit_the_day_are_refused(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(hours="20")
        self.assertIsNone(self.task())
        self.assertIn("Слишком много часов", self.page())

    def test_an_administrator_needs_no_rate_in_the_window_either(self):
        self.login_admin()
        with application_module.app.app_context():
            db = application_module.get_db()
            employee_id = db.execute(
                "INSERT INTO employees (name, created_at) VALUES ('Окно Админ', 'x')").lastrowid
            db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Администратор', 'x')", (employee_id,))
            db.commit()
        try:
            self.quick(employee="Окно Админ", rate="", title="Совещание")
            self.assertEqual(self.task("Окно Админ")["rate"], 0)
            self.assertIn("quickTaskAdminNote", self.page())
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM tuning_schedule_task_days")
                db.execute("DELETE FROM tuning_schedule_tasks")
                db.execute("DELETE FROM tuning_schedule_day_crew WHERE employee_id = "
                           "(SELECT id FROM employees WHERE name = 'Окно Админ')")
                db.execute("DELETE FROM employee_positions WHERE employee_id = "
                           "(SELECT id FROM employees WHERE name = 'Окно Админ')")
                db.execute("DELETE FROM employees WHERE name = 'Окно Админ'")
                db.commit()

    def test_the_existing_bottom_form_still_creates_free_and_work_tasks(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.quick(title="Просто задача")
        self.assertIsNone(self.task()["assignment_id"])
        self.quick(order_id=self.order_id, item_id=self.item_id)
        self.assertIsNotNone(self.task()["assignment_id"])


class TuningScheduleTaskEditorTests(_TuningScheduleFixture, unittest.TestCase):
    """Open a card in the tuning schedule, change any of its data in a
    window, or delete it."""

    def make_free(self, employee=None, title="Уборка цеха", rate="150", start="10:00", hours="2",
                  order_id=None):
        employee = employee or self.EMPLOYEE_A
        self.put_on_shift(employee)
        self.login_admin()
        self.client.post("/schedule/tuning/tasks", data=MultiDict([
            ("employee_name", employee), ("rate", rate), ("comment", "было"), ("title", title),
            ("return_date", self.DAY), ("work_date[]", self.DAY), ("start_time[]", start),
            ("planned_hours[]", hours), ("order_id", "" if order_id is None else str(order_id)),
        ]))
        return self.task(employee)

    def make_linked(self, employee=None, rate=100, hours=2):
        employee = employee or self.EMPLOYEE_A
        self.put_on_shift(employee)
        self.assign([(employee, rate, hours)])
        return self.task(employee)

    def task(self, employee=None):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM tuning_schedule_tasks WHERE employee_name = ? ORDER BY id DESC LIMIT 1",
                (employee or self.EMPLOYEE_A,)).fetchone()
            return dict(row) if row else None

    def days(self, task_id):
        with application_module.app.app_context():
            return [(r["work_date"], r["start_time"], r["planned_hours"]) for r in
                    application_module.get_db().execute(
                        "SELECT * FROM tuning_schedule_task_days WHERE task_id = ? ORDER BY work_date",
                        (task_id,)).fetchall()]

    def save(self, task_id, days=None, **fields):
        self.login_admin()
        data = MultiDict()
        base = {"employee_name": self.EMPLOYEE_A, "title": "Уборка цеха", "rate": "150",
                "comment": "", "status": "pending", "order_id": "", "return_date": self.DAY}
        base.update(fields)
        for key, value in base.items():
            data[key] = value
        for day in (days if days is not None else [(self.DAY, "10:00", "2")]):
            data.add("work_date[]", day[0])
            data.add("start_time[]", day[1])
            data.add("planned_hours[]", str(day[2]))
        return self.client.post(f"/schedule/tuning/tasks/{task_id}/update", data=data)

    def notice(self):
        with self.client.session_transaction() as session:
            return session.get("schedule_notice") or {}

    # ---- the page

    def test_every_card_carries_its_data_and_the_page_has_the_editor_window(self):
        task = self.make_free()
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('id="taskEditorModal"', page)
        self.assertIn("openTaskEditor(this, event)", page)
        self.assertIn("data-editor='{", page)
        self.assertIn(f'"task_id": {task["id"]}', page)
        self.assertIn('id="taskEditorDeleteForm"', page)
        for field in ("title", "employee_name", "order_id", "rate", "status", "comment"):
            self.assertIn(f'name="{field}"', page.split('id="taskEditorForm"')[1].split("</form>")[0], field)

    # ---- a task without a work item

    def test_a_free_task_can_have_everything_changed(self):
        task = self.make_free()
        self.put_on_shift(self.EMPLOYEE_B)
        response = self.save(
            task["id"], employee_name=self.EMPLOYEE_B, title="Мойка", rate="175", comment="стало",
            status="in_progress", order_id=str(self.order_id), days=[(self.DAY, "13:30", "3")],
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.notice()["type"], "success")
        row = self.task(self.EMPLOYEE_B)
        self.assertEqual((row["title"], row["rate"], row["comment"], row["status"], row["order_id"]),
                         ("Мойка", 175, "стало", "in_progress", self.order_id))
        self.assertEqual(self.days(task["id"]), [(self.DAY, "13:30", 3.0)])
        self.assertIsNone(self.task(self.EMPLOYEE_A))

    def test_days_can_be_added_moved_and_removed(self):
        task = self.make_free()
        self.put_on_shift(self.EMPLOYEE_A, day="2026-10-13")
        self.save(task["id"], days=[(self.DAY, "10:00", "2"), ("2026-10-13", "09:00", "1")])
        self.assertEqual(self.days(task["id"]), [(self.DAY, "10:00", 2.0), ("2026-10-13", "09:00", 1.0)])
        self.save(task["id"], days=[("2026-10-13", "14:00", "1")])  # the first day dropped, the other moved
        self.assertEqual(self.days(task["id"]), [("2026-10-13", "14:00", 1.0)])

    def test_a_day_off_the_roster_is_refused_and_nothing_changes(self):
        task = self.make_free()
        before = self.days(task["id"])
        self.save(task["id"], title="Другое", days=[("2026-10-20", "09:00", "1")])
        self.assertEqual(self.notice()["type"], "error")
        self.assertIn("не стоит в смене на 2026-10-20", self.notice()["message"])
        self.assertEqual(self.days(task["id"]), before)
        self.assertEqual(self.task()["title"], "Уборка цеха")

    def test_overlap_with_another_task_of_the_employee_is_refused(self):
        first = self.make_free(title="Первая", start="10:00", hours="2")
        second = self.make_free(title="Вторая", start="13:00", hours="1")
        self.save(second["id"], title="Вторая", days=[(self.DAY, "11:00", "1")])
        self.assertEqual(self.notice()["type"], "error")
        self.assertIn("занято задачей «Первая»", self.notice()["message"])
        self.assertEqual(self.days(second["id"]), [(self.DAY, "13:00", 1.0)])
        self.assertEqual(self.days(first["id"]), [(self.DAY, "10:00", 2.0)])

    def test_the_task_does_not_clash_with_itself(self):
        task = self.make_free(start="10:00", hours="2")
        self.save(task["id"], days=[(self.DAY, "11:00", "2")])
        self.assertEqual(self.notice()["type"], "success")
        self.assertEqual(self.days(task["id"]), [(self.DAY, "11:00", 2.0)])

    def test_bad_input_is_refused_with_a_message(self):
        task = self.make_free()
        for kwargs, text in (
            ({"title": ""}, "название"),
            ({"rate": "0"}, "Ставка"),
            ({"employee_name": "Никто"}, "сотрудника"),
            ({"status": "bogus"}, "статус"),
            ({"days": [(self.DAY, "23:30", "2")]}, "до конца суток"),
            ({"days": [(self.DAY, "09:00", "0")]}, "часы"),
            ({"days": [(self.DAY, "09:00", "1"), (self.DAY, "12:00", "1")]}, "дважды"),
            ({"days": []}, "день"),
            ({"order_id": "99999"}, "Проект"),
        ):
            self.save(task["id"], **kwargs)
            self.assertEqual(self.notice()["type"], "error", kwargs)
            self.assertIn(text.lower(), self.notice()["message"].lower(), kwargs)
        self.assertEqual(self.days(task["id"]), [(self.DAY, "10:00", 2.0)])

    def test_finishing_a_free_task_pays_it_once(self):
        task = self.make_free(rate="150", hours="2")
        self.save(task["id"], status="done")
        with application_module.app.app_context():
            entries = application_module.get_db().execute(
                "SELECT amount FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchall()
        self.assertEqual([e["amount"] for e in entries], [300])
        self.save(task["id"], status="done")
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchone()[0], 1)

    def test_an_administrator_task_needs_no_rate_and_is_put_on_the_shift(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO employee_positions (employee_id, position, created_at) VALUES (?, 'Администратор', 'x')",
                       (self.employee_id(self.EMPLOYEE_B),))
            db.commit()
        task = self.make_free()
        self.save(task["id"], employee_name=self.EMPLOYEE_B, rate="")
        self.assertEqual(self.notice()["type"], "success")
        self.assertEqual(self.task(self.EMPLOYEE_B)["rate"], 0)
        self.assertIsNotNone(self.crew_hours(self.EMPLOYEE_B))

    # ---- an order task

    def test_an_order_task_keeps_its_work_and_syncs_with_the_board(self):
        task = self.make_linked()
        self.save(task["id"], title="Что угодно", rate="250", comment="новый", status="in_progress",
                  days=[(self.DAY, "12:00", "3")])
        self.assertEqual(self.notice()["type"], "success")
        (assignment,) = self.assignments()
        self.assertEqual((assignment["rate"], assignment["norm_hours"], assignment["comment"],
                          assignment["assignment_status"], assignment["due_from"], assignment["due_to"]),
                         (250, 3, "новый", "in_progress", self.DAY, None))
        self.assertEqual(self.days(task["id"]), [(self.DAY, "12:00", 3.0)])
        self.assertEqual(self.task()["title"], task["title"])  # the work's name is not editable here
        page = self.board()
        self.assertIn("новый", page)

    def test_an_order_task_over_several_days_sets_the_period(self):
        task = self.make_linked()
        self.put_on_shift(self.EMPLOYEE_A, day="2026-10-14")
        self.save(task["id"], rate="100", days=[(self.DAY, "09:00", "2"), ("2026-10-14", "09:00", "1.5")])
        (assignment,) = self.assignments()
        self.assertEqual((assignment["due_from"], assignment["due_to"], assignment["norm_hours"]),
                         (self.DAY, "2026-10-14", 3.5))

    def test_an_order_task_can_be_handed_to_another_employee(self):
        task = self.make_linked()
        self.put_on_shift(self.EMPLOYEE_B)
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.save(task["id"], employee_name=self.EMPLOYEE_B, rate="100")
        self.assertEqual(self.notice()["type"], "success")
        (assignment,) = self.assignments()
        self.assertEqual(assignment["employee_name"], self.EMPLOYEE_B)
        self.assertEqual(self.task(self.EMPLOYEE_B)["id"], task["id"])

    def test_a_finished_order_task_cannot_be_handed_over(self):
        task = self.make_linked()
        self.save(task["id"], status="done", rate="100")
        self.put_on_shift(self.EMPLOYEE_B)
        self.save(task["id"], employee_name=self.EMPLOYEE_B, rate="100", status="done")
        self.assertEqual(self.notice()["type"], "error")
        self.assertEqual(self.assignments()[0]["employee_name"], self.EMPLOYEE_A)

    def test_changing_the_rate_of_a_paid_order_task_recalculates_the_payout(self):
        task = self.make_linked(rate=100, hours=2)
        self.save(task["id"], status="done", rate="100")
        self.save(task["id"], status="done", rate="300")
        with application_module.app.app_context():
            amounts = [r["amount"] for r in application_module.get_db().execute(
                "SELECT amount FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchall()]
        self.assertEqual(amounts, [600])

    # ---- delete

    def test_a_free_task_is_deleted_with_its_days_and_reminders(self):
        task = self.make_free()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO tuning_task_reminders (schedule_task_id, remind_at, created_at) "
                       "VALUES (?, '2030-01-01 10:00', 'x')", (task["id"],))
            db.commit()
        self.login_admin()
        response = self.client.post(f"/schedule/tuning/tasks/{task['id']}/delete", data={"return_date": self.DAY})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.notice()["type"], "success")
        self.assertIsNone(self.task())
        self.assertEqual(self.days(task["id"]), [])
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_task_reminders WHERE schedule_task_id = ?",
                (task["id"],)).fetchone()[0], 0)

    def test_a_paid_free_task_is_not_deleted(self):
        task = self.make_free()
        self.save(task["id"], status="done")
        self.client.post(f"/schedule/tuning/tasks/{task['id']}/delete", data={"return_date": self.DAY})
        self.assertEqual(self.notice()["type"], "error")
        self.assertIn("оплачена", self.notice()["message"])
        self.assertIsNotNone(self.task())

    def test_deleting_an_order_task_revokes_it_on_the_board_too(self):
        task = self.make_linked()
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee") as notify:
            self.client.post(f"/schedule/tuning/tasks/{task['id']}/delete", data={"return_date": self.DAY})
        self.assertEqual(self.assignments(), [])
        self.assertIsNone(self.task())
        notify.assert_called()  # the employee is told, as on the board
        self.assertIn("Пока никто не назначен", self.board())

    def test_deleting_a_paid_order_task_removes_its_payout(self):
        task = self.make_linked(rate=100, hours=2)
        self.save(task["id"], status="done", rate="100")
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.client.post(f"/schedule/tuning/tasks/{task['id']}/delete", data={"return_date": self.DAY})
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = ?", (self.EMPLOYEE_A,)).fetchone()[0], 0)

    def test_unknown_task_and_anonymous_access(self):
        self.login_admin()
        self.client.post("/schedule/tuning/tasks/999999/delete", data={"return_date": self.DAY})
        self.assertEqual(self.notice()["type"], "error")
        self.save(999999)
        self.assertEqual(self.notice()["type"], "error")
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post("/schedule/tuning/tasks/1/delete", data={})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/schedule/tuning", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
