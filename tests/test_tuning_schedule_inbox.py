import unittest

from support import application_module
from test_tuning_schedule_assignments import _TuningScheduleFixture


class TuningScheduleInboxTests(_TuningScheduleFixture, unittest.TestCase):
    """«Входящие»: tasks noted down before anyone / any day is chosen."""

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tuning_schedule_backlog")
            db.commit()
        self.login_admin()

    def inbox(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_schedule_backlog ORDER BY sort, id").fetchall()]

    def add(self, **data):
        data.setdefault("return_date", self.DAY)
        return self.client.post("/schedule/tuning/backlog", data=data)

    def test_completed_draft_is_kept_but_hidden_from_the_inbox(self):
        self.add(titles="Готовая задача")
        item_id = self.inbox()[0]["id"]
        response = self.client.post(f"/schedule/tuning/backlog/{item_id}/complete")
        self.assertEqual(response.status_code, 200)
        with application_module.app.app_context():
            from modules.tuning_schedule import repository
            db = application_module.get_db()
            self.assertEqual(repository.get_backlog_item(db, item_id)["status"], "done")
            self.assertEqual(repository.list_backlog(db), [])
            self.assertEqual(repository.count_backlog(db), 0)

    def test_one_line_is_one_task_without_anything_else(self):
        self.assertEqual(self.add(titles="Заменить трос газа").status_code, 302)
        items = self.inbox()
        self.assertEqual([i["title"] for i in items], ["Заменить трос газа"])
        self.assertIsNone(items[0]["employee_name"])
        self.assertIsNone(items[0]["order_id"])
        self.assertIsNone(items[0]["est_hours"])

    def test_several_lines_make_several_tasks_sharing_the_optional_fields(self):
        self.add(titles="Первая\n\n  Вторая  \nТретья", order_id=str(self.order_id),
                 employee_name=self.EMPLOYEE_A, est_hours="1,5")
        items = self.inbox()
        self.assertEqual([i["title"] for i in items], ["Первая", "Вторая", "Третья"])
        self.assertTrue(all(i["order_id"] == self.order_id for i in items))
        self.assertTrue(all(i["employee_name"] == self.EMPLOYEE_A for i in items))
        self.assertTrue(all(i["est_hours"] == 1.5 for i in items))

    def test_bad_input_adds_nothing_and_json_clients_get_the_reason(self):
        for data in ({"titles": "  \n "}, {"titles": "x", "order_id": "999999"},
                     {"titles": "x", "employee_name": "Нет такого"}, {"titles": "x", "est_hours": "abc"}):
            response = self.client.post(
                "/schedule/tuning/backlog", data=dict(data, return_date=self.DAY),
                headers={"X-Requested-With": "XMLHttpRequest"})
            self.assertEqual(response.status_code, 400, data)
            self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.inbox(), [])

    def test_a_task_can_be_edited_and_deleted(self):
        self.add(titles="Старое")
        item_id = self.inbox()[0]["id"]
        self.client.post(f"/schedule/tuning/backlog/{item_id}/update", data={
            "title": "Новое", "employee_name": self.EMPLOYEE_B, "order_id": str(self.order_id),
            "est_hours": "2", "comment": "К пятнице"})
        item = self.inbox()[0]
        self.assertEqual((item["title"], item["employee_name"], item["est_hours"], item["comment"]),
                         ("Новое", self.EMPLOYEE_B, 2.0, "К пятнице"))
        self.client.post(f"/schedule/tuning/backlog/{item_id}/delete", data={"return_date": self.DAY})
        self.assertEqual(self.inbox(), [])

    def test_the_page_lists_the_inbox_behind_a_button_with_the_count(self):
        self.add(titles="Собрать консоль", order_id=str(self.order_id))
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('onclick="toggleTuningInbox()"', page)
        self.assertIn('id="tuningInboxModal"', page)
        modal = page.split('id="tuningInboxModal"')[1].split('id="quickTaskModal"')[0]
        self.assertIn("Собрать консоль", modal)
        self.assertIn("Клиент Мультизадач", modal)  # the project's label

    def test_the_inbox_is_a_docked_side_panel_not_a_modal(self):
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn('<aside class="tuning-inbox-panel" id="tuningInboxModal"', page)
        self.assertIn('onclick="toggleTuningInbox()"', page)
        self.assertNotIn("tuning-inbox-drawer", page)
        # one flat toolbar and the calendar, not cards inside cards
        self.assertIn('<section class="tuning-flat">', page)
        self.assertNotIn("Календарь на", page)

    def test_the_inbox_cards_can_be_dragged_onto_the_calendar(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.add(titles="Собрать консоль")
        page = self.client.get(f"/schedule/tuning?date={self.DAY}").get_data(as_text=True)
        self.assertIn("data-inbox-grip", page)
        for hook in ("function moveInboxDrag", "function endInboxDrag", "assignBacklogItem(state.id",
                     "tuningColumnAt(event.clientX, event.clientY)"):
            self.assertIn(hook, page)
        self.assertIn('id="quickTaskBacklogId"', page)  # a drop fills the usual task window
        import pathlib
        css = (pathlib.Path(application_module.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("body.tuning-inbox-open { padding-right: 380px; }", css)

    def test_placing_the_task_from_the_inbox_creates_it_and_empties_the_inbox(self):
        self.put_on_shift(self.EMPLOYEE_A)
        self.add(titles="Собрать консоль")
        item_id = self.inbox()[0]["id"]
        response = self.client.post("/schedule/tuning/tasks", data={
            "return_date": self.DAY, "work_date[]": self.DAY, "start_time[]": "10:00",
            "planned_hours[]": "2", "employee_name": self.EMPLOYEE_A, "rate": "100",
            "title": "Собрать консоль", "backlog_id": str(item_id)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.inbox(), [])
        self.assertEqual([(c["start_time"], c["planned_hours"]) for c in self.cards(self.EMPLOYEE_A)],
                         [("10:00", 2)])

    def test_a_failed_placement_keeps_the_task_in_the_inbox(self):
        self.add(titles="Собрать консоль")
        item_id = self.inbox()[0]["id"]
        response = self.client.post("/schedule/tuning/tasks", data={
            "return_date": self.DAY, "work_date[]": self.DAY, "start_time[]": "10:00",
            "planned_hours[]": "2", "employee_name": "", "rate": "100",
            "title": "Собрать консоль", "backlog_id": str(item_id)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.inbox()), 1)


if __name__ == "__main__":
    unittest.main()
