import json
import re
import unittest

from support import application_module


class InvestorScheduleAccessTests(unittest.TestCase):
    MARK = "investor-schedule-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES (?, '', '+79990001122', ?, '2026-09-01 09:00')",
                (f"{self.MARK} клиент", f"{self.MARK}-token"),
            ).lastrowid
            self.item_id = db.execute(
                "INSERT INTO schedule_items (kind, boat, service_name, starts_at, ends_at, "
                "capacity, participants_count, customer_name, customer_phone, revenue, status, "
                "source, created_at, updated_at) VALUES ('booking', 'Бодрый Первый', "
                "'Малый тур', '2026-09-10 10:00', '2026-09-10 11:00', NULL, 1, "
                "'Секретный Клиент', '+79990001122', 5000, 'scheduled', 'internal', "
                "'2026-09-01 09:00', '2026-09-01 09:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO schedule_participants (schedule_item_id, client_id, client_name, "
                "client_phone, guests_count, price, prepayment, payment_due, created_at, source) "
                "VALUES (?, ?, 'Секретный Клиент', '+79990001122', 2, 5000, 0, 5000, "
                "'2026-09-01 09:00', 'internal')",
                (self.item_id, self.client_id),
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        db.execute(
            "DELETE FROM schedule_participants WHERE schedule_item_id IN "
            "(SELECT id FROM schedule_items WHERE customer_name = ?)",
            ("Секретный Клиент",),
        )
        db.execute("DELETE FROM schedule_items WHERE customer_name = 'Секретный Клиент'")
        db.execute("DELETE FROM clients WHERE token = ?", (f"{self.MARK}-token",))
        db.commit()

    def login_investor(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["investor_id"] = 1
            session["investor_name"] = f"{self.MARK} инвестор"

    def test_logged_out_visitor_is_redirected_away_from_the_schedule(self):
        response = self.client.get("/schedule?date=2026-09-10")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_investor_can_open_the_schedule_read_only(self):
        self.login_investor()
        response = self.client.get("/schedule?date=2026-09-10")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Личный кабинет инвестора", page)
        self.assertIn("режим просмотра", page)
        self.assertIn("+79990001122", page)  # ASCII, so present verbatim
        self.assertIn("scheduleCanManage = false", page)
        # the embedded schedule data (Cyrillic, escaped by |tojson) is the
        # full read-only manifest, not the empty list a plain team crew
        # member with no client-visibility capability would get
        match = re.search(r"const scheduleItems = (\[.*?\]);", page)
        self.assertIsNotNone(match)
        items = json.loads(match.group(1))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["participants"][0]["client_name"], "Секретный Клиент")
        self.assertEqual(items[0]["participants"][0]["client_phone"], "+79990001122")
        self.assertIn('class="schedule-body is-read-only can-view-clients"', page)
        # no manager-only chrome: drag handler wired to a card, admin sidebar
        self.assertNotIn('onpointerdown="startScheduleDrag', page)
        self.assertNotIn("id=\"navToggle\"", page)
        self.assertIn("openScheduleView(", page)

    def test_investor_cannot_create_or_edit_schedule_items(self):
        self.login_investor()
        with application_module.app.app_context():
            before = application_module.get_db().execute(
                "SELECT COUNT(*) FROM schedule_items"
            ).fetchone()[0]
        response = self.client.post(
            "/schedule/items",
            data={
                "kind": "booking", "boat": "Бодрый Первый", "service_name": "Взлом",
                "trip_date": "2026-09-11", "start_time": "10:00", "end_time": "11:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])
        with application_module.app.app_context():
            after = application_module.get_db().execute(
                "SELECT COUNT(*) FROM schedule_items"
            ).fetchone()[0]
        self.assertEqual(before, after)

        move = self.client.post(
            f"/schedule/items/{self.item_id}/move",
            json={"start_time": "12:00", "source_employee_id": 0, "target_employee_id": 0},
        )
        self.assertEqual(move.status_code, 302)
        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT starts_at FROM schedule_items WHERE id = ?", (self.item_id,)
            ).fetchone()
        self.assertEqual(item["starts_at"], "2026-09-10 10:00")

        crew = self.client.post(
            "/schedule/crew", data={"work_date": "2026-09-10", "employee_id": "1"}
        )
        self.assertEqual(crew.status_code, 302)
        self.assertIn("/admin/login", crew.headers["Location"])

    def test_investor_dashboard_links_to_the_schedule(self):
        self.login_investor()
        page = self.client.get("/investor/").get_data(as_text=True)
        self.assertIn('href="/schedule"', page)


if __name__ == "__main__":
    unittest.main()
