import unittest

from support import application_module


class EmployeeWorkdaysTests(unittest.TestCase):
    """«Рабочие дни»: many days at once, with shift hours, straight into the
    tuning or the excursion schedule."""

    NAMES = ("Рабочий Тюнинг", "Рабочий Капитан", "Рабочий Гибрид", "Рабочий Менеджер", "Рабочий Админ")
    DAYS = "2031-03-03,2031-03-04,2031-03-05"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            self.ids = {}
            for name, positions in (
                ("Рабочий Тюнинг", ["Тюнингмэн"]),
                ("Рабочий Капитан", ["Капитан"]),
                ("Рабочий Гибрид", ["Тюнингмэн", "Гид-капитан"]),
                ("Рабочий Менеджер", ["Менеджер по работе с клиентами"]),
                ("Рабочий Админ", ["Администратор"]),
            ):
                employee_id = db.execute(
                    "INSERT INTO employees (name, created_at) VALUES (?, '2026-01-01 09:00')", (name,)
                ).lastrowid
                for position in positions:
                    db.execute(
                        "INSERT INTO employee_positions (employee_id, position, created_at) "
                        "VALUES (?, ?, '2026-01-01 09:00')", (employee_id, position))
                self.ids[name] = employee_id
            db.commit()
        self.addCleanup(self._cleanup_now)
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def _cleanup_now(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        for name in self.NAMES:
            row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
            if row:
                for table in ("tuning_schedule_day_crew", "schedule_day_crew", "employee_positions",
                              "team_accounts", "employee_telegram_accounts"):
                    db.execute(f"DELETE FROM {table} WHERE employee_id = ?", (row["id"],))
                db.execute("DELETE FROM employees WHERE id = ?", (row["id"],))
        db.commit()

    def post(self, name, schedule="tuning", dates=None, start="10:00", end="19:00"):
        return self.client.post(f"/employees/{self.ids[name]}/workdays", data={
            "schedule": schedule, "dates": self.DAYS if dates is None else dates,
            "shift_start": start, "shift_end": end,
        })

    def rows(self, table, name):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                f"SELECT work_date, shift_start, shift_end FROM {table} WHERE employee_id = ? "
                "ORDER BY work_date", (self.ids[name],)).fetchall()]

    def notice(self):
        return self.client.get("/employees").get_data(as_text=True)

    def test_many_days_go_into_the_tuning_schedule_with_hours(self):
        response = self.post("Рабочий Тюнинг")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows("tuning_schedule_day_crew", "Рабочий Тюнинг"), [
            {"work_date": d, "shift_start": "10:00", "shift_end": "19:00"}
            for d in ("2031-03-03", "2031-03-04", "2031-03-05")
        ])
        self.assertEqual(self.rows("schedule_day_crew", "Рабочий Тюнинг"), [])
        page = self.notice()
        self.assertIn("добавлено дней: 3", page)
        self.assertIn("расписание «Тюнинг»", page)

    def test_many_days_go_into_the_excursion_schedule_with_hours(self):
        self.post("Рабочий Капитан", schedule="excursion", start="08:30", end="17:00")
        self.assertEqual(
            [(r["work_date"], r["shift_start"], r["shift_end"])
             for r in self.rows("schedule_day_crew", "Рабочий Капитан")],
            [(d, "08:30", "17:00") for d in ("2031-03-03", "2031-03-04", "2031-03-05")],
        )
        self.assertEqual(self.rows("tuning_schedule_day_crew", "Рабочий Капитан"), [])
        self.assertIn("расписание «Экскурсии»", self.notice())

    def test_the_chosen_schedule_decides_where_a_two_position_employee_goes(self):
        self.post("Рабочий Гибрид", schedule="excursion", dates="2031-03-03")
        self.post("Рабочий Гибрид", schedule="tuning", dates="2031-03-04")
        self.assertEqual([r["work_date"] for r in self.rows("schedule_day_crew", "Рабочий Гибрид")], ["2031-03-03"])
        self.assertEqual([r["work_date"] for r in self.rows("tuning_schedule_day_crew", "Рабочий Гибрид")], ["2031-03-04"])

    def test_days_already_on_the_roster_get_the_new_hours_instead_of_duplicates(self):
        self.post("Рабочий Тюнинг", dates="2031-03-03", start="09:00", end="18:00")
        self.post("Рабочий Тюнинг", dates="2031-03-03,2031-03-04", start="12:00", end="20:00")
        rows = self.rows("tuning_schedule_day_crew", "Рабочий Тюнинг")
        self.assertEqual([(r["work_date"], r["shift_start"], r["shift_end"]) for r in rows],
                         [("2031-03-03", "12:00", "20:00"), ("2031-03-04", "12:00", "20:00")])
        page = self.notice()
        self.assertIn("добавлено дней: 1", page)
        self.assertIn("обновлены часы в уже стоявших днях: 1", page)

    def test_an_administrator_can_work_in_both_schedules(self):
        self.post("Рабочий Админ", schedule="tuning", dates="2031-03-03")
        self.post("Рабочий Админ", schedule="excursion", dates="2031-03-04", start="11:00", end="20:00")
        self.assertEqual([(r["work_date"], r["shift_start"]) for r in self.rows("tuning_schedule_day_crew", "Рабочий Админ")],
                         [("2031-03-03", "10:00")])
        self.assertEqual([(r["work_date"], r["shift_start"]) for r in self.rows("schedule_day_crew", "Рабочий Админ")],
                         [("2031-03-04", "11:00")])
        # ...and they show up on those days' rosters
        tuning = self.client.get("/schedule/tuning?date=2031-03-03").get_data(as_text=True)
        self.assertIn("Рабочий Админ", tuning.split('id="tuningRosterModal"')[1])
        excursion = self.client.get("/schedule?date=2031-03-04").get_data(as_text=True)
        self.assertIn("Рабочий Админ", excursion.split('id="scheduleRosterModal"')[1])

    def test_an_administrator_is_offered_in_the_roster_pickers_of_both_schedules(self):
        tuning = self.client.get("/schedule/tuning?date=2031-03-10").get_data(as_text=True)
        self.assertIn("Рабочий Админ · Администратор", tuning)
        excursion = self.client.get("/schedule?date=2031-03-10").get_data(as_text=True)
        self.assertIn("Рабочий Админ · Администратор", excursion)

    def test_an_employee_without_the_right_position_is_refused(self):
        self.post("Рабочий Менеджер", schedule="tuning")
        self.assertIn("«Тюнингмэн» или «Администратор»", self.notice())
        self.post("Рабочий Тюнинг", schedule="excursion")
        self.assertIn("капитанов, гидов и администраторов", self.notice())
        self.assertEqual(self.rows("tuning_schedule_day_crew", "Рабочий Менеджер"), [])
        self.assertEqual(self.rows("schedule_day_crew", "Рабочий Тюнинг"), [])

    def test_bad_input_is_rejected_and_adds_nothing(self):
        cases = [
            dict(dates=""), dict(dates="2031-03-03,не-дата"), dict(schedule="foo"),
            dict(start="", end=""), dict(start="19:00", end="10:00"), dict(start="10:00", end="10:00"),
            dict(dates=",".join(f"2032-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 28))),
        ]
        for case in cases:
            self.post("Рабочий Тюнинг", **case)
        self.assertEqual(self.rows("tuning_schedule_day_crew", "Рабочий Тюнинг"), [])

    def test_duplicate_dates_are_collapsed(self):
        self.post("Рабочий Тюнинг", dates="2031-03-03, 2031-03-03 ,2031-03-04")
        self.assertEqual(len(self.rows("tuning_schedule_day_crew", "Рабочий Тюнинг")), 2)

    def test_a_past_day_in_the_excursion_schedule_is_accepted(self):
        self.post("Рабочий Капитан", schedule="excursion", dates="2026-01-05")
        self.assertEqual([r["work_date"] for r in self.rows("schedule_day_crew", "Рабочий Капитан")], ["2026-01-05"])

    def test_the_page_has_the_button_and_the_modal_with_eligibility_flags(self):
        page = self.client.get("/employees").get_data(as_text=True)
        self.assertIn('id="workdaysModal"', page)
        self.assertIn("Рабочие дни</button>", page)
        for name, tuning, excursion in (
            ("Рабочий Тюнинг", 1, 0), ("Рабочий Капитан", 0, 1),
            ("Рабочий Гибрид", 1, 1), ("Рабочий Менеджер", 0, 0),
            ("Рабочий Админ", 1, 1),
        ):
            button = page.split(f'data-name="{name}"')[1].split(">")[0]
            self.assertIn(f'data-tuning="{tuning}"', button, name)
            self.assertIn(f'data-excursion="{excursion}"', button, name)
            self.assertIn(f"/employees/{self.ids[name]}/workdays", page)

    def test_the_hours_show_up_in_both_schedules(self):
        self.post("Рабочий Тюнинг", dates="2031-03-03", start="10:00", end="19:00")
        self.post("Рабочий Капитан", schedule="excursion", dates="2031-03-03", start="08:30", end="17:00")
        tuning = self.client.get("/schedule/tuning?date=2031-03-03").get_data(as_text=True)
        self.assertIn('name="shift_start" value="10:00"', tuning)
        excursion = self.client.get("/schedule?date=2031-03-03").get_data(as_text=True)
        self.assertIn("смена 08:30–17:00", excursion)

    def test_an_anonymous_visitor_cannot_use_the_route(self):
        anonymous = application_module.app.test_client()
        response = anonymous.post(f"/employees/{self.ids['Рабочий Тюнинг']}/workdays", data={
            "schedule": "tuning", "dates": "2031-03-03", "shift_start": "09:00", "shift_end": "18:00"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows("tuning_schedule_day_crew", "Рабочий Тюнинг"), [])

    def test_an_unknown_employee_is_reported(self):
        response = self.client.post("/employees/999999/workdays", data={
            "schedule": "tuning", "dates": "2031-03-03", "shift_start": "09:00", "shift_end": "18:00"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("Сотрудник не найден", self.notice())


if __name__ == "__main__":
    unittest.main()
