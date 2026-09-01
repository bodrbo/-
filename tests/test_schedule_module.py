import unittest

from support import application_module


class ScheduleModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM schedule_day_crew")
            db.execute("DELETE FROM schedule_participants")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN ("
                "SELECT id FROM clients WHERE phone IN "
                "('+79998880001', '+79998880002', '+79998880003', '+79998880004')"
                ")"
            )
            db.execute(
                "DELETE FROM clients WHERE phone IN "
                "('+79998880001', '+79998880002', '+79998880003', '+79998880004')"
            )
            db.execute(
                "DELETE FROM client_segments WHERE client_id NOT IN "
                "(SELECT id FROM clients)"
            )
            self.daniil_id = self.ensure_crew_member(
                db, "Даниил Галецкий", "Гид-капитан"
            )
            self.platon_id = self.ensure_crew_member(
                db, "Платон Жмаев", "Капитан"
            )
            db.executemany(
                "INSERT INTO schedule_day_crew "
                "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
                [
                    ("2026-09-05", self.daniil_id, "2026-09-01 09:00"),
                    ("2026-09-05", self.platon_id, "2026-09-01 09:01"),
                ],
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
        search_response = self.client.get("/schedule/clients/search?q=ал")
        self.assertEqual(search_response.status_code, 302)
        self.assertEqual(
            self.client.post("/schedule/crew", data={}).status_code, 302
        )
        self.assertEqual(
            self.client.post(
                f"/schedule/crew/{self.daniil_id}/remove", data={}
            ).status_code,
            302,
        )

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
        self.assertNotIn('id="scheduleClientOptions"', html)
        self.assertIn("schedule-board-nav", html)
        self.assertIn("/schedule/clients/search", html)
        self.assertRegex(html, r"/static/style\.css\?v=\d+")

    def test_admin_can_add_and_remove_employee_from_day_schedule(self):
        self.login()
        remove_response = self.client.post(
            f"/schedule/crew/{self.platon_id}/remove",
            data={"work_date": "2026-09-05"},
        )
        self.assertEqual(remove_response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            after_remove = db.execute(
                "SELECT COUNT(*) AS count FROM schedule_day_crew "
                "WHERE work_date = '2026-09-05' AND employee_id = ?",
                (self.platon_id,),
            ).fetchone()["count"]
        self.assertEqual(after_remove, 0)

        add_response = self.client.post(
            "/schedule/crew",
            data={
                "work_date": "2026-09-05",
                "employee_id": str(self.platon_id),
            },
        )
        self.assertEqual(add_response.status_code, 302)
        with application_module.app.app_context():
            after_add = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_day_crew "
                "WHERE work_date = '2026-09-05' AND employee_id = ?",
                (self.platon_id,),
            ).fetchone()["count"]
        self.assertEqual(after_add, 1)

    def test_employee_with_trip_cannot_be_removed_from_day_schedule(self):
        self.login()
        self.create_booking()

        response = self.client.post(
            f"/schedule/crew/{self.daniil_id}/remove",
            data={"work_date": "2026-09-05"},
            follow_redirects=True,
        )

        self.assertIn(
            "Сначала переназначьте или удалите рейс",
            response.get_data(as_text=True),
        )
        with application_module.app.app_context():
            roster_count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_day_crew "
                "WHERE work_date = '2026-09-05' AND employee_id = ?",
                (self.daniil_id,),
            ).fetchone()["count"]
        self.assertEqual(roster_count, 1)

    def test_trip_assignment_automatically_adds_employee_to_day_schedule(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM schedule_day_crew "
                "WHERE work_date = '2026-09-05' AND employee_id = ?",
                (self.daniil_id,),
            )
            db.commit()

        self.create_booking()

        with application_module.app.app_context():
            roster_count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_day_crew "
                "WHERE work_date = '2026-09-05' AND employee_id = ?",
                (self.daniil_id,),
            ).fetchone()["count"]
        self.assertEqual(roster_count, 1)

    def test_empty_day_prompts_admin_to_add_crew(self):
        self.login()
        response = self.client.get("/schedule?date=2026-09-08")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Состав на этот день пока не добавлен", html)
        self.assertIn("Добавить сотрудников", html)

    def test_client_search_returns_only_ranked_excursion_clients(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            excursion_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Алия Морская', '', '+79998880001', "
                "'schedule-search-excursion', '2026-09-01 10:00')"
            ).lastrowid
            tuning_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Алия Тюнинг', '', '+79998880002', "
                "'schedule-search-tuning', '2026-09-01 10:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'excursion', '2026-09-01 10:00')",
                (excursion_id,),
            )
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'tuning', '2026-09-01 10:00')",
                (tuning_id,),
            )
            db.commit()

        response = self.client.get("/schedule/clients/search?q=алия")

        self.assertEqual(response.status_code, 200)
        clients = response.get_json()["clients"]
        names = [client["client_name"] for client in clients]
        self.assertIn("Алия Морская", names)
        self.assertNotIn("Алия Тюнинг", names)
        created_client = next(
            client for client in clients
            if client["client_name"] == "Алия Морская"
        )
        self.assertEqual(created_client["phone"], "+79998880001")
        self.assertEqual(
            self.client.get("/schedule/clients/search?q=а").get_json(),
            {"clients": []},
        )

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
            participant = db.execute(
                "SELECT * FROM schedule_participants WHERE schedule_item_id = ?",
                (item["id"],),
            ).fetchone()
            client_segment = db.execute(
                "SELECT segment FROM client_segments WHERE client_id = ?",
                (participant["client_id"],),
            ).fetchone()
        self.assertEqual(item["kind"], "booking")
        self.assertEqual(item["boat"], "Бодрый Второй")
        self.assertEqual(item["starts_at"], "2026-09-05 13:00")
        self.assertEqual(item["ends_at"], "2026-09-05 15:30")
        self.assertEqual(item["customer_name"], "Алия")
        self.assertEqual(item["revenue"], 18000)
        self.assertEqual(assignment["employee_name"], "Даниил Галецкий")
        self.assertEqual(assignment["role"], "guide_captain")
        self.assertEqual(participant["client_name"], "Алия")
        self.assertEqual(client_segment["segment"], "excursion")
        self.assertIsNone(item["accounting_trip_id"])

        page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("--schedule-card-color: #673ab7", page)
        self.assertIn("--schedule-card-ink: #ffffff", page)
        self.assertIn("Запись", page)

    def test_individual_booking_allows_client_without_phone(self):
        self.login()
        response = self.create_booking(
            customer_name="Турист без телефона", customer_phone=""
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT * FROM clients WHERE client_name = 'Турист без телефона'"
            ).fetchone()
            participant = db.execute(
                "SELECT * FROM schedule_participants WHERE client_id = ?",
                (client["id"],),
            ).fetchone()
        self.assertEqual(client["phone"], "")
        self.assertEqual(participant["client_phone"], "")

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
            excursion_segments = db.execute(
                "SELECT COUNT(*) AS count FROM client_segments "
                "WHERE segment = 'excursion' AND client_id IN (?, ?)",
                (clients[0]["id"], clients[1]["id"]),
            ).fetchone()["count"]
        self.assertEqual(item["kind"], "event")
        self.assertEqual(item["capacity"], 10)
        self.assertEqual(item["participants_count"], 2)
        self.assertEqual(item["customer_name"], "")
        self.assertEqual(len(participants), 2)
        self.assertEqual(len(clients), 2)
        self.assertEqual(excursion_segments, 2)
        self.assertEqual(participants[0]["client_name"], "Алия")

        directory = self.client.get(
            "/admin/clients?section=excursion"
        ).get_data(as_text=True)
        tuning_directory = self.client.get("/admin/clients").get_data(as_text=True)
        self.assertIn("Алия", directory)
        self.assertIn("Мария", directory)
        self.assertNotIn("Алия", tuning_directory)

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

    def test_tuning_identity_is_reused_and_promoted_to_excursion_client(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Клиент тюнинга', 'Buster XL', '+79998880003', "
                "'schedule-tuning-client', '2026-08-31 12:00')"
            )
            client_id = cursor.lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'tuning', '2026-08-31 12:00')",
                (client_id,),
            )
            db.commit()

        schedule_before = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertNotIn("Клиент тюнинга", schedule_before)

        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="8",
                customer_name="",
                **{
                    "employee_id[]": [str(self.platon_id)],
                    "role[]": ["captain"],
                    "participant_client_id[]": [""],
                    "participant_name[]": ["Клиент тюнинга"],
                    "participant_phone[]": ["+7 (999) 888-00-03"],
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            ids = db.execute(
                "SELECT id FROM clients WHERE phone = '+79998880003'"
            ).fetchall()
            segments = {
                row["segment"] for row in db.execute(
                    "SELECT segment FROM client_segments WHERE client_id = ?",
                    (client_id,),
                ).fetchall()
            }
        self.assertEqual([row["id"] for row in ids], [client_id])
        self.assertEqual(segments, {"tuning", "excursion"})

    def test_migration_links_legacy_individual_booking_to_excursion_client(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            booking_id = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, customer_name, "
                "customer_phone, created_at, updated_at) VALUES "
                "('booking', 'Ларус', 'Средний тур', '2026-09-07 12:00', "
                "'2026-09-07 13:30', 'Старый турист', '+79998880004', "
                "'2026-09-01 09:00', '2026-09-01 09:00')"
            ).lastrowid
            db.commit()

        application_module.init_db()

        with application_module.app.app_context():
            db = application_module.get_db()
            participant = db.execute(
                "SELECT * FROM schedule_participants WHERE schedule_item_id = ?",
                (booking_id,),
            ).fetchone()
            segment = db.execute(
                "SELECT segment FROM client_segments WHERE client_id = ?",
                (participant["client_id"],),
            ).fetchone()
        self.assertEqual(participant["client_name"], "Старый турист")
        self.assertEqual(segment["segment"], "excursion")

    def test_migration_adds_existing_assignments_to_day_schedule(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_name, starts_at, ends_at, customer_name, "
                "customer_phone, created_at, updated_at) VALUES "
                "('booking', 'Ларус', 'Средний тур', '2026-09-09 12:00', "
                "'2026-09-09 13:30', 'Турист', '', "
                "'2026-09-01 09:00', '2026-09-01 09:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, 'Даниил Галецкий', 'guide_captain', "
                "'2026-09-01 09:00')",
                (item_id, self.daniil_id),
            )
            db.commit()

        application_module.init_db()

        with application_module.app.app_context():
            roster = application_module.get_db().execute(
                "SELECT * FROM schedule_day_crew "
                "WHERE work_date = '2026-09-09' AND employee_id = ?",
                (self.daniil_id,),
            ).fetchone()
        self.assertIsNotNone(roster)

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
        self.assertIn("0 рейсов", page)
        self.assertIn("const scheduleItems = [];", page)


if __name__ == "__main__":
    unittest.main()
