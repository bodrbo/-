import unittest

from support import application_module
from test_tuning_schedule_assignments import _TuningScheduleFixture


class TuningScheduleBreakTests(_TuningScheduleFixture, unittest.TestCase):
    """Breaks and days off: a stretch of a person's day closed to tasks."""

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tuning_schedule_breaks")
            db.commit()
        self.login_admin()
        self.put_on_shift(self.EMPLOYEE_A, "09:00", "18:00")
        self.put_on_shift(self.EMPLOYEE_B, "10:00", "19:00")

    def breaks(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_schedule_breaks ORDER BY employee_id, start_time").fetchall()]

    def add_break(self, name=None, **data):
        form = {"work_date": self.DAY, "employee_id": str(self.employee_id(name or self.EMPLOYEE_A)),
                "start_time": "13:00", "end_time": "14:00"}
        form.update(data)
        return self.client.post("/schedule/tuning/breaks", data=form,
                                headers={"X-Requested-With": "XMLHttpRequest"})

    def create_task(self, start, hours="1", name=None):
        return self.client.post("/schedule/tuning/tasks", data={
            "return_date": self.DAY, "work_date[]": self.DAY, "start_time[]": start,
            "planned_hours[]": hours, "employee_name": name or self.EMPLOYEE_A, "rate": "100",
            "title": "Задача"}, headers={"X-Requested-With": "XMLHttpRequest"})

    def test_a_break_is_stored_and_hatched_on_the_calendar(self):
        response = self.add_break()
        self.assertEqual(response.status_code, 200)
        saved = self.breaks()
        self.assertEqual([(b["start_time"], b["end_time"], b["kind"]) for b in saved],
                         [("13:00", "14:00", "break")])
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('class="tuning-calendar-break"', page)
        self.assertIn("Перерыв 13:00–14:00", page)
        self.assertIn('id="slotChoiceModal"', page)
        self.assertIn('id="breakModal"', page)

    def test_bad_times_are_refused(self):
        for times in ({"start_time": "14:00", "end_time": "13:00"}, {"start_time": "13:00", "end_time": "13:00"},
                      {"start_time": "", "end_time": "14:00"}):
            self.assertEqual(self.add_break(**times).status_code, 400, times)
        self.assertEqual(self.breaks(), [])

    def test_a_person_off_the_roster_gets_no_break(self):
        response = self.add_break(day_off="")  # on the roster: fine
        self.assertEqual(response.status_code, 200)
        other = self.client.post("/schedule/tuning/breaks", data={
            "work_date": "2026-10-20", "employee_id": str(self.employee_id(self.EMPLOYEE_A)),
            "start_time": "13:00", "end_time": "14:00"}, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(other.status_code, 400)

    def test_a_task_cannot_be_put_into_a_break_but_can_touch_it(self):
        self.add_break()
        for start, hours in (("13:00", "1"), ("12:30", "1"), ("13:30", "2")):
            response = self.create_task(start, hours)
            self.assertEqual(response.status_code, 400, start)
            self.assertIn("перерыв 13:00–14:00", response.get_json()["message"])
        self.assertEqual(self.create_task("12:00", "1").status_code, 200)  # ends exactly at 13:00
        self.assertEqual(self.create_task("14:00", "1").status_code, 200)  # starts exactly at 14:00
        # another person's break is theirs alone
        self.assertEqual(self.create_task("13:00", "1", name=self.EMPLOYEE_B).status_code, 200)

    def test_a_break_cannot_cover_an_existing_task_or_another_break(self):
        self.assertEqual(self.create_task("13:00", "1").status_code, 200)
        self.assertEqual(self.add_break(start_time="12:30", end_time="13:30").status_code, 400)
        self.assertEqual(self.add_break(start_time="15:00", end_time="16:00").status_code, 200)
        self.assertEqual(self.add_break(start_time="15:30", end_time="16:30").status_code, 400)
        self.assertEqual(len(self.breaks()), 1)

    def test_a_break_for_everyone_on_the_shift_skips_those_with_a_task_there(self):
        self.assertEqual(self.create_task("13:00", "1", name=self.EMPLOYEE_B).status_code, 200)
        response = self.add_break(for_all="1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([b["employee_id"] for b in self.breaks()], [self.employee_id(self.EMPLOYEE_A)])
        self.assertIn("Пропущены", response.get_json()["message"])

    def test_a_day_off_closes_the_whole_shift(self):
        self.assertEqual(self.add_break(day_off="1", start_time="", end_time="").status_code, 200)
        self.assertEqual([(b["start_time"], b["end_time"], b["kind"]) for b in self.breaks()],
                         [("09:00", "18:00", "dayoff")])
        self.assertEqual(self.create_task("12:00").status_code, 400)
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn("is-dayoff", page)
        self.assertIn("Выходной 09:00–18:00", page)

    def test_a_day_off_for_everyone_uses_each_shift(self):
        self.add_break(day_off="1", for_all="1", start_time="", end_time="")
        self.assertEqual(sorted((b["start_time"], b["end_time"]) for b in self.breaks()),
                         [("09:00", "18:00"), ("10:00", "19:00")])

    def test_a_break_can_be_changed_and_deleted(self):
        self.add_break()
        break_id = self.breaks()[0]["id"]
        response = self.client.post(f"/schedule/tuning/breaks/{break_id}/update", data={
            "return_date": self.DAY, "start_time": "13:00", "end_time": "15:00"},
            headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.breaks()[0]["end_time"], "15:00")
        bad = self.client.post(f"/schedule/tuning/breaks/{break_id}/update", data={
            "return_date": self.DAY, "start_time": "15:00", "end_time": "14:00"},
            headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(bad.status_code, 400)
        self.client.post(f"/schedule/tuning/breaks/{break_id}/delete", data={"return_date": self.DAY})
        self.assertEqual(self.breaks(), [])
        self.assertEqual(self.create_task("13:00").status_code, 200)  # free again

    def test_a_card_cannot_be_dragged_into_a_break(self):
        self.create_task("10:00", "1")
        self.add_break()
        cards = self.cards(self.EMPLOYEE_A)
        with application_module.app.app_context():
            day_id = application_module.get_db().execute(
                "SELECT id FROM tuning_schedule_task_days WHERE task_id = ?", (cards[0]["task_id"],)
            ).fetchone()["id"]
        response = self.client.post(
            f"/schedule/tuning/tasks/{cards[0]['task_id']}/days/{day_id}/move",
            json={"start_time": "13:30"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("перерыв", response.get_json()["message"])
        ok = self.client.post(
            f"/schedule/tuning/tasks/{cards[0]['task_id']}/days/{day_id}/move",
            json={"start_time": "14:00"})
        self.assertEqual(ok.status_code, 200)

    def test_an_order_task_is_placed_after_the_break_not_inside_it(self):
        self.add_break(start_time="09:00", end_time="10:30")
        self.assign([(self.EMPLOYEE_A, 100, 2)])
        self.assertEqual([c["start_time"] for c in self.cards(self.EMPLOYEE_A)], ["10:30"])

    def test_removing_someone_from_the_shift_drops_their_breaks(self):
        self.add_break()
        response = self.client.post(
            f"/schedule/tuning/crew/{self.employee_id(self.EMPLOYEE_A)}/remove", data={"work_date": self.DAY})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.breaks(), [])


if __name__ == "__main__":
    unittest.main()
