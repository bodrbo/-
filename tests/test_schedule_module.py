import unittest

from support import application_module


class ScheduleModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM schedule_participants")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            db.execute(
                "DELETE FROM clients WHERE phone IN "
                "('+79998880001', '+79998880002', '+79998880003')"
            )
            self.daniil_id = self.ensure_crew_member(
                db, "Даниил Галецкий", "Гид-капитан"
            )
            self.platon_id = self.ensure_crew_member(
                db, "Платон Жмаев", "Капитан"
            )
            db.commit()

    @staticmethod
    def ensure_crew_member(db, name, position):
        row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-08-31 12:00', NULL)",
                (name,),
            )
            employee_id = cursor.lastrowid
        else:
            employee_id = row["id"]
            db.execute(
                "UPDATE employees SET deleted_at = NULL WHERE id = ?",
                (employee_id,),
            )
        db.execute(
            "INSERT OR IGNORE INTO employee_positions "
            "(employee_id, position, created_at) VALUES (?, ?, '2026-08-31 12:00')",
            (employee_id, position),
        )
        return employee_id

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def booking_data(self, **overrides):
        data = {
            "kind": "booking",
            "boat": "Бодрый Второй",
            "service_name": "Большой тур",
            "trip_date": "2026-09-05",
            "start_time": "13:00",
            "end_time": "15:30",
            "employee_id[]": [str(self.daniil_id)],
            "role[]": ["guide_captain"],
            "customer_name": "Алия",
            "customer_phone": "+79118115476",
            "revenue": "18000",
            "note": "Посадка у причала",
            "return_employee": "all",
        }
        data.update(overrides)
        return data

    def create_booking(self, **overrides):
        return self.client.post(
            "/schedule/items", data=self.booking_data(**overrides)
        )

    def test_schedule_requires_admin_login(self):
        response = self.client.get("/schedule")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_day_board_renders_crew_and_navigation(self):
        self.login()
        response = self.client.get("/schedule?date=2026-09-05")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Расписание рейсов", html)
        self.assertIn("Даниил Галецкий", html)
        self.assertIn("Платон Жмаев", html)
        self.assertIn("5 сентября, суббота", html)
        self.assertIn("Новый рейс", html)
        self.assertNotIn('name="participants_count"', html)

    def test_admin_creates_individual_booking_with_assignment(self):
        self.login()
        response = self.create_booking()
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT * FROM schedule_items").fetchone()
            assignment = db.execute(
                "SELECT * FROM schedule_assignments"
            ).fetchone()
        self.assertEqual(item["kind"], "booking")
        self.assertEqual(item["boat"], "Бодрый Второй")
        self.assertEqual(item["starts_at"], "2026-09-05 13:00")
        self.assertEqual(item["ends_at"], "2026-09-05 15:30")
        self.assertEqual(item["customer_name"], "Алия")
        self.assertEqual(item["revenue"], 18000)
        self.assertEqual(assignment["employee_name"], "Даниил Галецкий")
        self.assertEqual(assignment["role"], "guide_captain")
        self.assertIsNone(item["accounting_trip_id"])

        page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("--schedule-card-color: #673ab7", page)
        self.assertIn("--schedule-card-ink: #ffffff", page)
        self.assertIn("Запись", page)

    def test_admin_creates_group_event_with_linked_clients(self):
        self.login()
        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                boat="Бодрый Первый",
                service_name="Средний тур",
                end_time="14:30",
                **{
                    "employee_id[]": [str(self.platon_id)],
                    "role[]": ["captain"],
                    "capacity": "10",
                    "participant_client_id[]": ["", ""],
                    "participant_name[]": ["Алия", "Мария"],
                    "participant_phone[]": ["+79998880001", "+79998880002"],
                    "customer_name": "",
                    "customer_phone": "",
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT * FROM schedule_items"
            ).fetchone()
            participants = db.execute(
                "SELECT * FROM schedule_participants ORDER BY id"
            ).fetchall()
            clients = db.execute(
                "SELECT * FROM clients WHERE phone IN (?, ?) ORDER BY phone",
                ("+79998880001", "+79998880002"),
            ).fetchall()
        self.assertEqual(item["kind"], "event")
        self.assertEqual(item["capacity"], 10)
        self.assertEqual(item["participants_count"], 2)
        self.assertEqual(item["customer_name"], "")
        self.assertEqual(len(participants), 2)
        self.assertEqual(len(clients), 2)
        self.assertEqual(participants[0]["client_name"], "Алия")

    def test_group_event_reuses_existing_client_by_verified_phone(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Александр', '', '+79998880003', "
                "'schedule-existing-client', '2026-08-31 12:00')"
            )
            client_id = cursor.lastrowid
            db.commit()

        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="8",
                customer_name="",
                **{
                    "employee_id[]": [str(self.platon_id)],
                    "role[]": ["captain"],
                    "participant_client_id[]": [str(client_id)],
                    "participant_name[]": ["Александр"],
                    "participant_phone[]": ["+7 (999) 888-00-03"],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            participant = db.execute(
                "SELECT * FROM schedule_participants"
            ).fetchone()
            client_count = db.execute(
                "SELECT COUNT(*) AS count FROM clients "
                "WHERE phone = '+79998880003'"
            ).fetchone()["count"]
        self.assertEqual(participant["client_id"], client_id)
        self.assertEqual(participant["client_phone"], "+79998880003")
        self.assertEqual(client_count, 1)

    def test_group_event_rejects_more_clients_than_capacity(self):
        self.login()
        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="1",
                customer_name="",
                **{
                    "participant_client_id[]": ["", ""],
                    "participant_name[]": ["Алия", "Мария"],
                    "participant_phone[]": ["+79998880001", "+79998880002"],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item_count = db.execute(
                "SELECT COUNT(*) AS count FROM schedule_items"
            ).fetchone()["count"]
            client_count = db.execute(
                "SELECT COUNT(*) AS count FROM clients "
                "WHERE phone IN ('+79998880001', '+79998880002')"
            ).fetchone()["count"]
        self.assertEqual(item_count, 0)
        self.assertEqual(client_count, 0)

    def test_edit_group_event_updates_client_list_and_counter(self):
        self.login()
        self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": ["", ""],
                    "participant_name[]": ["Алия", "Мария"],
                    "participant_phone[]": ["+79998880001", "+79998880002"],
                },
            ),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT id FROM schedule_items").fetchone()["id"]
            client = db.execute(
                "SELECT id, client_name, phone FROM clients "
                "WHERE phone = '+79998880002'"
            ).fetchone()

        response = self.client.post(
            f"/schedule/items/{item_id}",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": [str(client["id"])],
                    "participant_name[]": [client["client_name"]],
                    "participant_phone[]": [client["phone"]],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT participants_count FROM schedule_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            participants = db.execute(
                "SELECT client_name FROM schedule_participants "
                "WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchall()
        self.assertEqual(item["participants_count"], 1)
        self.assertEqual(
            [participant["client_name"] for participant in participants], ["Мария"]
        )

    def test_edit_moves_trip_and_reassigns_employee(self):
        self.login()
        self.create_booking()
        with application_module.app.app_context():
            item_id = application_module.get_db().execute(
                "SELECT id FROM schedule_items"
            ).fetchone()["id"]

        response = self.client.post(
            f"/schedule/items/{item_id}",
            data=self.booking_data(
                boat="Ларус",
                trip_date="2026-09-06",
                start_time="16:00",
                end_time="18:30",
                **{
                    "employee_id[]": [str(self.platon_id)],
                    "role[]": ["captain"],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT * FROM schedule_items WHERE id = ?", (item_id,)
            ).fetchone()
            assignments = db.execute(
                "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchall()
        self.assertEqual(item["boat"], "Ларус")
        self.assertEqual(item["starts_at"], "2026-09-06 16:00")
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]["employee_name"], "Платон Жмаев")

    def test_employee_and_boat_overlaps_are_rejected(self):
        self.login()
        self.create_booking()
        same_employee = self.create_booking(
            boat="Ларус",
            start_time="14:00",
            end_time="15:00",
            customer_name="Второй клиент",
        )
        same_boat = self.create_booking(
            start_time="14:00",
            end_time="15:00",
            customer_name="Третий клиент",
            **{
                "employee_id[]": [str(self.platon_id)],
                "role[]": ["captain"],
            },
        )
        self.assertEqual(same_employee.status_code, 302)
        self.assertEqual(same_boat.status_code, 302)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_items"
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_delete_hides_item_but_preserves_assignment_history(self):
        self.login()
        self.create_booking()
        with application_module.app.app_context():
            item_id = application_module.get_db().execute(
                "SELECT id FROM schedule_items"
            ).fetchone()["id"]
        response = self.client.post(
            f"/schedule/items/{item_id}/delete",
            data={"return_date": "2026-09-05", "return_employee": "all"},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT * FROM schedule_items WHERE id = ?", (item_id,)
            ).fetchone()
            assignments = db.execute(
                "SELECT COUNT(*) AS count FROM schedule_assignments "
                "WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()["count"]
        self.assertIsNotNone(item["deleted_at"])
        self.assertEqual(assignments, 1)
        page = self.client.get("/schedule?date=2026-09-05").get_data(as_text=True)
        self.assertNotIn("Алия", page)


if __name__ == "__main__":
    unittest.main()
