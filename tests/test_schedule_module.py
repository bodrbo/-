import sqlite3
import unittest
from unittest.mock import Mock, patch

from support import application_module
from modules.schedule.schema import init_schema


class ScheduleModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM schedule_day_crew")
            db.execute("DELETE FROM schedule_manual_payments")
            db.execute("DELETE FROM schedule_yookassa_payments")
            db.execute("DELETE FROM schedule_modulkassa_receipts")
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

    @staticmethod
    def anonymous_client():
        return application_module.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    @staticmethod
    def create_excursion_partner(db):
        partner_id = db.execute(
            "INSERT INTO clients "
            "(client_name, boat_model, phone, token, created_at) "
            "VALUES ('Партнёр расписания', '', '+79998880003', "
            "'schedule-sales-partner', '2026-09-01 10:00')"
        ).lastrowid
        db.execute(
            "INSERT INTO client_segments "
            "(client_id, segment, relationship_type, created_at) "
            "VALUES (?, 'excursion', 'partner', '2026-09-01 10:00')",
            (partner_id,),
        )
        db.commit()
        return partner_id

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
        self.assertEqual(
            self.client.post(
                "/schedule/items/1/move",
                json={
                    "start_time": "13:30",
                    "source_employee_id": self.daniil_id,
                    "target_employee_id": self.platon_id,
                },
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
        self.assertIn("schedule-board-nav is-contained", html)
        self.assertIn('data-crew-count="2"', html)
        self.assertIn("/schedule/clients/search", html)
        self.assertIn("function startScheduleDrag", html)
        self.assertIn("перетащите её по времени и между сотрудниками", html)
        self.assertRegex(html, r"/static/style\.css\?v=\d+")

    def test_schedule_uses_fifteen_minute_grid_and_accepts_45_minute_trip(self):
        self.login()
        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)

        self.assertIn('id="scheduleStart" step="900"', html)
        self.assertIn('id="scheduleEnd" step="900"', html)
        self.assertIn("const scheduleTimeStepMinutes = 15", html)
        self.assertIn("schedule-quarter-line", html)

        response = self.create_booking(start_time="13:00", end_time="13:45")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT starts_at, ends_at FROM schedule_items"
            ).fetchone()
        self.assertEqual(item["starts_at"], "2026-09-05 13:00")
        self.assertEqual(item["ends_at"], "2026-09-05 13:45")

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
        self.assertIsNotNone(item["service_id"])
        self.assertEqual(item["starts_at"], "2026-09-05 13:00")
        self.assertEqual(item["ends_at"], "2026-09-05 15:30")
        self.assertEqual(item["customer_name"], "Алия")
        self.assertEqual(item["revenue"], 18000)
        self.assertEqual(assignment["employee_name"], "Даниил Галецкий")
        self.assertEqual(assignment["role"], "guide_captain")
        self.assertEqual(participant["client_name"], "Алия")
        self.assertEqual(participant["guests_count"], 1)
        self.assertEqual(participant["price"], 18000)
        self.assertEqual(client_segment["segment"], "excursion")
        self.assertIsNone(item["accounting_trip_id"])

        page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("--schedule-card-color: #673ab7", page)
        self.assertIn("--schedule-card-ink: #ffffff", page)
        self.assertIn("Индивидуальная экскурсия", page)
        self.assertIn(
            f'data-move-url="/schedule/items/{item["id"]}/move"', page
        )
        self.assertIn('onpointerdown="startScheduleDrag(event, this)"', page)
        self.assertIn('id="scheduleBookingPayments"', page)

    def test_individual_booking_guests_count_is_optional_and_saved(self):
        self.login()
        response = self.create_booking(guests_count="5")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT * FROM schedule_items").fetchone()
            participant = db.execute(
                "SELECT guests_count FROM schedule_participants "
                "WHERE schedule_item_id = ?",
                (item["id"],),
            ).fetchone()
        self.assertEqual(item["guests_count"], 5)
        self.assertEqual(participant["guests_count"], 5)

        page = self.client.get("/schedule?date=2026-09-05").get_data(as_text=True)
        self.assertIn('<span class="schedule-card-meta">5 гостей</span>', page)

    def test_city_excursion_is_created_without_boat(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute(
                "SELECT id FROM excursion_services WHERE name = ?",
                ("Индивидуальная прогулка по городу",),
            ).fetchone()
            if row is None:
                service_id = db.execute(
                    "INSERT INTO excursion_services "
                    "(name, service_type, activity_type, duration_hours, price, "
                    "created_at, updated_at) VALUES (?, 'individual', 'city', 2, "
                    "7000, '2026-09-01 10:00', '2026-09-01 10:00')",
                    ("Индивидуальная прогулка по городу",),
                ).lastrowid
                db.commit()
            else:
                service_id = row["id"]

        response = self.create_booking(
            service_id=str(service_id),
            boat="Бодрый Второй",
            start_time="16:00",
            end_time="18:00",
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            item = application_module.get_db().execute(
                "SELECT * FROM schedule_items WHERE service_id = ?",
                (service_id,),
            ).fetchone()
        self.assertIsNotNone(item)
        self.assertEqual(item["boat"], "")

        page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("Индивидуальная прогулка по городу", page)
        self.assertIn("Не требуется", page)
        self.assertIn('id="scheduleBoatField" hidden', page)
        self.assertIn("selected.dataset.activityType === 'boat'", page)

    def test_individual_booking_shows_unknown_guests_count_when_blank(self):
        self.login()
        response = self.create_booking(guests_count="")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT * FROM schedule_items").fetchone()
        self.assertIsNone(item["guests_count"])

        page = self.client.get("/schedule?date=2026-09-05").get_data(as_text=True)
        self.assertIn(
            '<span class="schedule-card-meta">Количество гостей неизвестно</span>', page
        )

    def test_individual_booking_rejects_invalid_guests_count(self):
        self.login()
        response = self.create_booking(guests_count="0")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item_count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_items"
            ).fetchone()["c"]
        self.assertEqual(item_count, 0)

    def test_individual_booking_keeps_manual_payment_when_edited(self):
        self.login()
        self.assertEqual(self.create_booking().status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT id FROM schedule_items").fetchone()
            participant = db.execute(
                "SELECT id FROM schedule_participants WHERE schedule_item_id = ?",
                (item["id"],),
            ).fetchone()
            item_id = item["id"]
            participant_id = participant["id"]

        payment_response = self.client.post(
            f"/schedule/items/{item_id}/participants/{participant_id}/manual-payments",
            json={"amount": "5000", "payment_method": "cashless"},
        )
        self.assertEqual(payment_response.status_code, 200)

        update_response = self.client.post(
            f"/schedule/items/{item_id}",
            data=self.booking_data(customer_price="19000", note="Детали уточнены"),
        )
        self.assertEqual(update_response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            updated_participant = db.execute(
                "SELECT id, price FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()
            payment = db.execute(
                "SELECT participant_id, amount, payment_method "
                "FROM schedule_manual_payments WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()
        self.assertEqual(updated_participant["id"], participant_id)
        self.assertEqual(updated_participant["price"], 19000)
        self.assertEqual(payment["participant_id"], participant_id)
        self.assertEqual(payment["amount"], 5000)
        self.assertEqual(payment["payment_method"], "cashless")

    def _booking_with_manual_payment(self, configured=True):
        self.login()
        self.assertEqual(self.create_booking().status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT id FROM schedule_items").fetchone()["id"]
            participant_id = db.execute(
                "SELECT id FROM schedule_participants WHERE schedule_item_id = ?", (item_id,)
            ).fetchone()["id"]
        fake_post = Mock()
        fake_post.return_value.ok = True
        fake_post.return_value.json.return_value = {"status": "QUEUED"}
        with patch.object(application_module, "_modulkassa_configured", return_value=configured), \
                patch.object(application_module.requests, "post", fake_post):
            response = self.client.post(
                f"/schedule/items/{item_id}/participants/{participant_id}/manual-payments",
                json={"amount": "5000", "payment_method": "cashless"},
            )
        self.assertEqual(response.status_code, 200)
        payment = response.get_json()["participants"][0]["manual_payments"][0]
        return item_id, participant_id, payment, fake_post

    def test_manual_payment_queues_fiscal_receipt(self):
        item_id, participant_id, payment, fake_post = self._booking_with_manual_payment()
        fake_post.assert_called_once()
        body = fake_post.call_args.kwargs["json"]
        self.assertEqual(body["moneyPositions"], [{"paymentType": "CARD", "sum": 5000.0}])
        self.assertTrue(body["inventPositions"][0]["name"].startswith("Оплата: "))
        self.assertEqual(payment["receipt"]["status"], "queued")
        self.assertFalse(payment["receipt"]["pdf_ready"])

    def test_manual_payment_without_cash_desk_is_still_recorded(self):
        _item, _participant, payment, fake_post = self._booking_with_manual_payment(configured=False)
        fake_post.assert_not_called()
        self.assertIsNone(payment["receipt"])

    def test_receipt_pdf_download_and_public_link(self):
        item_id, participant_id, payment, _post = self._booking_with_manual_payment()
        base = f"/schedule/items/{item_id}/participants/{participant_id}"
        # not yet fiscalized -> no PDF
        self.assertEqual(self.client.get(f"{base}/receipts/manual/{payment['id']}.pdf").status_code, 404)

        fake_get = Mock()
        fake_get.return_value.ok = True
        fake_get.return_value.json.return_value = {
            "status": "PRINTED",
            "fiscalInfo": {
                "qr": "t=20260907T143000&s=5000.00&fn=9999078900008998&i=571&fp=3125146288&n=1",
                "shiftNumber": 8, "checkNumber": 17, "fnNumber": "9999078900008998",
                "fnDocNumber": 571, "fnDocMark": 3125146288, "sum": 5000,
            },
        }
        with patch.object(application_module, "_modulkassa_configured", return_value=True), \
                patch.object(application_module.requests, "get", fake_get):
            response = self.client.post(f"{base}/manual-payments/{payment['id']}/receipt/check")
        updated = response.get_json()["participants"][0]["manual_payments"][0]
        self.assertTrue(updated["receipt"]["pdf_ready"])

        pdf = self.client.get(f"{base}/receipts/manual/{payment['id']}.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

        link = self.client.get(f"{base}/receipts/manual/{payment['id']}/link").get_json()
        self.assertTrue(link["ok"])
        public_path = link["url"].split("://", 1)[1].split("/", 1)[1]
        public = self.anonymous_client().get("/" + public_path)
        self.assertEqual(public.status_code, 200)
        self.assertTrue(public.data.startswith(b"%PDF"))
        wrong = self.anonymous_client().get(
            f"/client/not-the-token/schedule-receipts/manual/{payment['id']}.pdf"
        )
        self.assertEqual(wrong.status_code, 404)

        # deleting the payment drops its receipts
        self.client.post(f"{base}/manual-payments/{payment['id']}/delete")
        with application_module.app.app_context():
            left = application_module.get_db().execute(
                "SELECT COUNT(*) FROM schedule_modulkassa_receipts WHERE payment_id = ?",
                (payment["id"],),
            ).fetchone()[0]
        self.assertEqual(left, 0)

    def _booking_with_paid_link(self):
        self.login()
        self.assertEqual(self.create_booking().status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT id FROM schedule_items").fetchone()["id"]
            participant_id = db.execute(
                "SELECT id FROM schedule_participants WHERE schedule_item_id = ?", (item_id,)
            ).fetchone()["id"]
            paid_id = db.execute(
                "INSERT INTO schedule_yookassa_payments (schedule_item_id, participant_id, "
                "yookassa_payment_id, amount, status, confirmation_url, applied, created_at, updated_at) "
                "VALUES (?, ?, 'yk-paid', 4000, 'succeeded', 'https://x', 1, "
                "'2026-09-05 10:00', '2026-09-05 10:05')",
                (item_id, participant_id),
            ).lastrowid
            db.commit()
        return item_id, participant_id, paid_id

    def _check_link_receipt(self, item_id, participant_id, paid_id, receipts):
        fake_request = Mock(return_value={"items": receipts})
        with patch.object(application_module, "yookassa_configured", return_value=True), \
                patch.object(application_module, "_yookassa_request", fake_request):
            response = self.client.post(
                f"/schedule/items/{item_id}/participants/{participant_id}"
                f"/yookassa/{paid_id}/receipt/check"
            )
        return response, fake_request

    def test_paid_link_receipt_is_read_back_from_yookassa_and_rendered_with_qr(self):
        item_id, participant_id, paid_id = self._booking_with_paid_link()
        base = f"/schedule/items/{item_id}/participants/{participant_id}/receipts/online"
        # nothing registered yet -> no PDF, and no fake "confirmation"
        self.assertEqual(self.client.get(f"{base}/{paid_id}.pdf").status_code, 404)

        response, fake_request = self._check_link_receipt(
            item_id, participant_id, paid_id,
            [{"id": "r1", "type": "payment", "status": "pending"}],
        )
        self.assertEqual(fake_request.call_args.args[:2], ("GET", "/receipts"))
        self.assertEqual(fake_request.call_args.kwargs["params"], {"payment_id": "yk-paid"})
        payment = response.get_json()["participants"][0]["payments"][0]
        self.assertEqual(payment["receipt"]["status"], "pending")
        self.assertFalse(payment["receipt"]["pdf_ready"])
        self.assertNotIn("receipt_json", payment)
        self.assertEqual(self.client.get(f"{base}/{paid_id}.pdf").status_code, 404)

        response, _ = self._check_link_receipt(item_id, participant_id, paid_id, [
            {"id": "r0", "type": "refund", "status": "succeeded"},
            {
                "id": "r1", "type": "payment", "status": "succeeded",
                "registered_at": "2026-09-05T07:05:12.000Z",
                "fiscal_document_number": 571, "fiscal_storage_number": "9999078900008998",
                "fiscal_attribute": "3125146288",
            },
        ])
        payment = response.get_json()["participants"][0]["payments"][0]
        self.assertTrue(payment["receipt"]["pdf_ready"])
        pdf = self.client.get(f"{base}/{paid_id}.pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        # The download URL/name carry a fingerprint of the fiscal data so a
        # phone's PDF viewer can't keep showing an older rendering.
        version = payment["receipt"]["version"]
        self.assertEqual(len(version), 8)
        self.assertIn(version, pdf.headers["Content-Disposition"])
        link = self.client.get(
            f"/schedule/items/{item_id}/participants/{participant_id}/receipts/online/{paid_id}/link"
        ).get_json()
        self.assertIn(f"v={version}", link["url"])
        self.assertIn("no-store", pdf.headers["Cache-Control"])

        with application_module.app.app_context():
            stored = application_module.get_db().execute(
                "SELECT receipt_json FROM schedule_yookassa_payments WHERE id = ?", (paid_id,)
            ).fetchone()["receipt_json"]
            info = application_module._fiscal_info_from_yookassa_receipt(
                __import__("json").loads(stored), 4000
            )
        # registered_at is UTC; the QR carries the Moscow-time receipt moment
        self.assertEqual(
            info["qr"],
            "t=20260905T100512&s=4000.00&fn=9999078900008998&i=571&fp=3125146288&n=1",
        )

    def test_receipt_without_fiscal_attributes_is_not_treated_as_ready(self):
        item_id, participant_id, paid_id = self._booking_with_paid_link()
        response, _ = self._check_link_receipt(item_id, participant_id, paid_id, [
            {"id": "r1", "type": "payment", "status": "succeeded",
             "registered_at": "2026-09-05T07:05:12.000Z"},
        ])
        payment = response.get_json()["participants"][0]["payments"][0]
        self.assertFalse(payment["receipt"]["pdf_ready"])

    def test_cron_polls_receipts_of_recent_paid_links(self):
        item_id, participant_id, paid_id = self._booking_with_paid_link()
        with application_module.app.app_context():
            application_module.get_db().execute(
                "UPDATE schedule_yookassa_payments SET updated_at = ? WHERE id = ?",
                (application_module.dt.datetime.now().strftime("%Y-%m-%d %H:%M"), paid_id),
            )
            application_module.get_db().commit()
        fake_request = Mock(return_value={"items": [{
            "type": "payment", "status": "succeeded",
            "registered_at": "2026-09-05T07:05:12Z", "fiscal_document_number": 1,
            "fiscal_storage_number": "123", "fiscal_attribute": "456",
        }]})
        # The cron URL itself is guarded by the secret captured at startup
        # (unset in tests -> always forbidden); the polling is tested directly.
        self.assertEqual(
            self.anonymous_client().get("/internal/cron/sync-schedule-receipts?token=x").status_code,
            403,
        )
        with application_module.app.app_context():
            checked, found = application_module.schedule_services.sync_pending_receipts(
                application_module.get_db(), fake_request
            )
            row = application_module.get_db().execute(
                "SELECT receipt_status FROM schedule_yookassa_payments WHERE id = ?", (paid_id,)
            ).fetchone()
        self.assertEqual((checked, found), (1, 1))
        self.assertEqual(row["receipt_status"], "succeeded")

    def test_schedule_routes_notify_on_assignment_change_and_deletion(self):
        self.login()
        with patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.create_booking()
        self.assertEqual(response.status_code, 302)
        notifier.assert_called_once()
        self.assertEqual(notifier.call_args.args[1], "Даниил Галецкий")
        self.assertIn("Вам назначен новый рейс", notifier.call_args.args[2])

        with application_module.app.app_context():
            item_id = application_module.get_db().execute(
                "SELECT id FROM schedule_items"
            ).fetchone()["id"]

        with patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.client.post(
                f"/schedule/items/{item_id}",
                data=self.booking_data(
                    boat="Ларус",
                    trip_date="2026-09-06",
                    start_time="14:00",
                    end_time="16:30",
                ),
            )
        self.assertEqual(response.status_code, 302)
        notifier.assert_called_once()
        self.assertIn("Рейс перенесён", notifier.call_args.args[2])
        self.assertIn("Судно стало: Ларус", notifier.call_args.args[2])

        with patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.client.post(
                f"/schedule/items/{item_id}/delete",
                data={"return_date": "2026-09-06", "return_employee": "all"},
            )
        self.assertEqual(response.status_code, 302)
        notifier.assert_called_once()
        self.assertIn("Рейс отменён", notifier.call_args.args[2])

    def test_admin_can_drag_trip_in_time_and_to_another_employee(self):
        self.login()
        self.create_booking()
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT * FROM schedule_items").fetchone()
            item_id = item["id"]
            participant_before = dict(db.execute(
                "SELECT * FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone())

        invalid_step = self.client.post(
            f"/schedule/items/{item_id}/move",
            json={
                "start_time": "14:10",
                "source_employee_id": self.daniil_id,
                "target_employee_id": self.platon_id,
            },
        )
        self.assertEqual(invalid_step.status_code, 400)
        self.assertIn("шагом 15 минут", invalid_step.get_json()["message"])

        with patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.client.post(
                f"/schedule/items/{item_id}/move",
                json={
                    "start_time": "14:15",
                    "source_employee_id": self.daniil_id,
                    "target_employee_id": self.platon_id,
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["item"]["starts_at"], "2026-09-05 14:15")
        self.assertEqual(payload["item"]["ends_at"], "2026-09-05 16:45")
        self.assertEqual(
            payload["item"]["assignments"],
            [{
                "employee_id": self.platon_id,
                "employee_name": "Платон Жмаев",
                "role": "guide_captain",
            }],
        )
        notifier.assert_called_once()
        self.assertEqual(notifier.call_args.args[1], "Платон Жмаев")
        self.assertIn("Вам назначен новый рейс", notifier.call_args.args[2])

        with application_module.app.app_context():
            db = application_module.get_db()
            moved = db.execute(
                "SELECT * FROM schedule_items WHERE id = ?", (item_id,)
            ).fetchone()
            assignment = db.execute(
                "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()
            participant_after = dict(db.execute(
                "SELECT * FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone())
        self.assertEqual(moved["starts_at"], "2026-09-05 14:15")
        self.assertEqual(moved["ends_at"], "2026-09-05 16:45")
        self.assertEqual(moved["revenue"], 18000)
        self.assertEqual(assignment["employee_id"], self.platon_id)
        self.assertEqual(assignment["role"], "guide_captain")
        self.assertEqual(participant_after, participant_before)

    def test_drag_rejects_employee_or_boat_conflicts_without_partial_update(self):
        self.login()
        self.create_booking()
        with application_module.app.app_context():
            first_id = application_module.get_db().execute(
                "SELECT id FROM schedule_items"
            ).fetchone()["id"]
        self.create_booking(
            boat="Ларус",
            start_time="16:00",
            end_time="17:00",
            customer_name="Мария",
            customer_phone="+79998880003",
            **{
                "employee_id[]": [str(self.platon_id)],
                "role[]": ["captain"],
            },
        )

        response = self.client.post(
            f"/schedule/items/{first_id}/move",
            json={
                "start_time": "16:00",
                "source_employee_id": self.daniil_id,
                "target_employee_id": self.platon_id,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("Уже заняты", response.get_json()["message"])
        with application_module.app.app_context():
            db = application_module.get_db()
            original = db.execute(
                "SELECT starts_at, ends_at FROM schedule_items WHERE id = ?",
                (first_id,),
            ).fetchone()
            assignment = db.execute(
                "SELECT employee_id FROM schedule_assignments "
                "WHERE schedule_item_id = ?",
                (first_id,),
            ).fetchone()
        self.assertEqual(original["starts_at"], "2026-09-05 13:00")
        self.assertEqual(original["ends_at"], "2026-09-05 15:30")
        self.assertEqual(assignment["employee_id"], self.daniil_id)

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
                    "participant_guests[]": ["3", "2"],
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
        self.assertEqual(item["participants_count"], 5)
        self.assertEqual(item["customer_name"], "")
        self.assertEqual(len(participants), 2)
        self.assertEqual(len(clients), 2)
        self.assertEqual(excursion_segments, 2)
        self.assertEqual(participants[0]["client_name"], "Алия")
        self.assertEqual(
            [participant["guests_count"] for participant in participants],
            [3, 2],
        )
        self.assertEqual(
            [participant["price"] for participant in participants],
            [10800, 7200],
        )
        schedule_page = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn("Групповая экскурсия", schedule_page)
        self.assertIn(
            '<span class="schedule-card-event-count">5/10 мест</span>',
            schedule_page,
        )

        directory = self.client.get(
            "/admin/clients?section=excursion"
        ).get_data(as_text=True)
        tuning_directory = self.client.get("/admin/clients").get_data(as_text=True)
        self.assertIn("Алия", directory)
        self.assertIn("Мария", directory)
        self.assertNotIn("Алия", tuning_directory)

    def test_event_capacity_is_forced_by_vessel_passenger_capacity(self):
        # When Fleet has a configured passenger capacity for the boat, the
        # submitted "capacity" field is a hard-overridden formula (vessel
        # capacity minus this card's own crew), not the free-typed number —
        # here 12 total - 1 crew (Платон) = 11 guest seats, not the "99"
        # submitted in the form.
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE fleet_vessels SET capacity = 12 WHERE name = 'Бодрый Первый'"
            )
            db.commit()
        try:
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
                        "capacity": "99",
                        "participant_client_id[]": [""],
                        "participant_name[]": ["Алия"],
                        "participant_phone[]": ["+79998880003"],
                        "participant_guests[]": ["3"],
                        "customer_name": "",
                        "customer_phone": "",
                    },
                ),
            )
            self.assertEqual(response.status_code, 302)
            with application_module.app.app_context():
                db = application_module.get_db()
                item = db.execute("SELECT * FROM schedule_items").fetchone()
            self.assertEqual(item["capacity"], 11)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute(
                    "UPDATE fleet_vessels SET capacity = NULL WHERE name = 'Бодрый Первый'"
                )
                db.commit()

    def test_client_prices_override_legacy_trip_total(self):
        self.login()
        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                boat="Бодрый Первый",
                service_name="Средний тур",
                end_time="14:30",
                revenue="99999",
                **{
                    "employee_id[]": [str(self.platon_id)],
                    "role[]": ["captain"],
                    "capacity": "10",
                    "participant_client_id[]": ["", ""],
                    "participant_name[]": ["Алия", "Мария"],
                    "participant_phone[]": ["+79998880001", "+79998880002"],
                    "participant_guests[]": ["3", "2"],
                    "participant_price[]": ["7200", "4600"],
                    "customer_name": "",
                    "customer_phone": "",
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT * FROM schedule_items").fetchone()
            prices = [
                row["price"] for row in db.execute(
                    "SELECT price FROM schedule_participants ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(prices, [7200, 4600])
        self.assertEqual(item["revenue"], 11800)

    def test_schedule_page_places_price_next_to_each_client(self):
        self.login()
        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn('name="customer_price"', html)
        self.assertIn('name="participant_price[]"', html)
        self.assertIn("Итого по клиентам", html)
        self.assertNotIn("Плановая стоимость, ₽", html)
        self.assertIn("syncScheduleClientPrices", html)

    def test_tripster_sync_shows_propeller_loader(self):
        self.login()
        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)

        self.assertIn('class="page-loader hidden" id="pageLoader"', html)
        self.assertIn('class="propeller"', html)
        self.assertIn('onsubmit="showScheduleTripsterLoader(this)"', html)
        self.assertIn("Переимпорт рейсов…", html)

    def test_client_card_offers_whatsapp_and_telegram_links_by_phone(self):
        self.login()
        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)

        self.assertIn("function scheduleNormalizePhone", html)
        self.assertIn("function buildScheduleDetailClientMessengers", html)
        self.assertIn("https://wa.me/${digits}", html)
        self.assertIn("https://t.me/+${digits}", html)
        self.assertIn("button.disabled = true", html)
        self.assertIn("openScheduleDetail(itemId);", html)
        self.assertNotIn("if (item && item.kind === 'event')", html)
        self.assertIn("scheduleDetailQuickAdd').hidden = item.kind !== 'event'", html)

    def test_client_card_offers_preferred_contact_method_field(self):
        self.login()
        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)

        self.assertIn("scheduleContactMethods", html)
        self.assertIn("Канал связи", html)
        self.assertIn("is-preferred", html)
        self.assertIn('{"label": "\\u0421\\u041c\\u0421", "value": "sms"}', html)

    def test_individual_client_card_saves_contact_and_sales_channels(self):
        self.login()
        with application_module.app.app_context():
            partner_id = self.create_excursion_partner(application_module.get_db())

        response = self.create_booking(
            customer_name="Ирина Канальная",
            customer_phone="+79998880004",
            customer_sales_partner_id=str(partner_id),
            customer_preferred_contact_method="telegram",
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            participant = db.execute(
                "SELECT sales_partner_id, sales_channel, client_id "
                "FROM schedule_participants"
            ).fetchone()
            client = db.execute(
                "SELECT preferred_contact_method FROM clients WHERE id = ?",
                (participant["client_id"],),
            ).fetchone()
        self.assertEqual(participant["sales_partner_id"], partner_id)
        self.assertEqual(participant["sales_channel"], f"partner:{partner_id}")
        self.assertEqual(client["preferred_contact_method"], "telegram")

        html = self.client.get(
            "/schedule?date=2026-09-05"
        ).get_data(as_text=True)
        self.assertIn('name="customer_sales_partner_id"', html)
        self.assertIn('name="customer_preferred_contact_method"', html)
        self.assertIn('name="participant_preferred_contact_method[]"', html)

    def test_sales_channels_include_both_partner_segments_and_custom_entries(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM client_segments WHERE client_id IN "
                "(SELECT id FROM clients WHERE token = 'tuning-channel-partner')"
            )
            db.execute(
                "DELETE FROM clients WHERE token = 'tuning-channel-partner'"
            )
            tuning_partner_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES ('Тюнинг-партнёр канала', '', '', 'tuning-channel-partner', "
                "'2026-09-01 10:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments "
                "(client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'partner', '2026-09-01 10:00')",
                (tuning_partner_id,),
            )
            db.commit()

        created = self.client.post(
            "/api/sales-channels", json={"name": "Рекомендация яхт-клуба"}
        )
        self.assertEqual(created.status_code, 201)
        channel = created.get_json()["channel"]
        self.assertTrue(channel["value"].startswith("custom:"))

        html = self.client.get("/schedule?date=2026-09-05").get_data(as_text=True)
        self.assertIn("Сайт bodrbo-tuning.ru", html)
        self.assertIn("Сайт bodrbo-fort.ru", html)
        self.assertIn("Тюнинг-партнёр канала · партнёр (тюнинг)", html)
        self.assertIn("Рекомендация яхт-клуба", html)
        self.assertIn('class="sales-channel-add"', html)

    def test_individual_client_card_edit_keeps_booking_summary_in_sync(self):
        self.login()
        self.create_booking(
            customer_name="Ирина До Редактирования",
            customer_phone="+79998880004",
            guests_count="2",
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute("SELECT id FROM schedule_items").fetchone()
            participant = db.execute(
                "SELECT id FROM schedule_participants WHERE schedule_item_id = ?",
                (item["id"],),
            ).fetchone()

        response = self.client.post(
            f"/schedule/items/{item['id']}/participants/{participant['id']}",
            json={
                "client_name": "Ирина После Редактирования",
                "client_phone": "+79998880005",
                "guests_count": 4,
                "price": 22000,
                "sales_partner_id": "",
                "preferred_contact_method": "whatsapp",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        with application_module.app.app_context():
            db = application_module.get_db()
            item = db.execute(
                "SELECT customer_name, customer_phone, guests_count, "
                "participants_count, revenue FROM schedule_items"
            ).fetchone()
        self.assertEqual(item["customer_name"], "Ирина После Редактирования")
        self.assertEqual(item["customer_phone"], "+79998880005")
        self.assertEqual(item["guests_count"], 4)
        self.assertEqual(item["participants_count"], 4)
        self.assertEqual(item["revenue"], 22000)

    def test_group_client_card_saves_preferred_contact_method(self):
        self.login()
        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": [""],
                    "participant_name[]": ["Мария Канальная"],
                    "participant_phone[]": ["+79998880004"],
                    "participant_guests[]": ["2"],
                    "participant_preferred_contact_method[]": ["whatsapp"],
                },
            ),
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT clients.preferred_contact_method "
                "FROM clients JOIN schedule_participants "
                "ON schedule_participants.client_id = clients.id"
            ).fetchone()
        self.assertEqual(client["preferred_contact_method"], "whatsapp")

    def test_price_migration_preserves_historical_trip_total(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE excursion_services (id INTEGER PRIMARY KEY, name TEXT)"
        )
        init_schema(connection)
        connection.execute("DROP TABLE schedule_participants")
        connection.execute(
            "CREATE TABLE schedule_participants ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_item_id INTEGER NOT NULL, "
            "client_id INTEGER NOT NULL, client_name TEXT NOT NULL, "
            "client_phone TEXT NOT NULL, guests_count INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL, UNIQUE(schedule_item_id, client_id))"
        )
        cursor = connection.execute(
            "INSERT INTO schedule_items "
            "(kind, boat, service_name, starts_at, ends_at, revenue, created_at, updated_at) "
            "VALUES ('event', 'Ларус', 'Средний тур', '2026-08-20 12:00', "
            "'2026-08-20 13:30', 15000, '', '')"
        )
        connection.executemany(
            "INSERT INTO schedule_participants "
            "(schedule_item_id, client_id, client_name, client_phone, guests_count, created_at) "
            "VALUES (?, ?, ?, '', ?, '')",
            [
                (cursor.lastrowid, 1, "Алия", 3),
                (cursor.lastrowid, 2, "Мария", 2),
            ],
        )
        init_schema(connection)
        prices = [
            row[0] for row in connection.execute(
                "SELECT price FROM schedule_participants ORDER BY id"
            ).fetchall()
        ]
        payment_dues = [
            row[0] for row in connection.execute(
                "SELECT payment_due FROM schedule_participants ORDER BY id"
            ).fetchall()
        ]
        connection.close()
        self.assertEqual(prices, [9000, 6000])
        self.assertEqual(payment_dues, [9000, 6000])

    def test_paid_online_migration_backfills_zero_and_creates_payments_table(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE excursion_services (id INTEGER PRIMARY KEY, name TEXT)"
        )
        init_schema(connection)
        connection.execute(
            "ALTER TABLE schedule_participants RENAME TO schedule_participants_old"
        )
        connection.execute(
            "CREATE TABLE schedule_participants ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_item_id INTEGER NOT NULL, "
            "client_id INTEGER NOT NULL, client_name TEXT NOT NULL, "
            "client_phone TEXT NOT NULL, guests_count INTEGER NOT NULL DEFAULT 1, "
            "price REAL NOT NULL DEFAULT 0, prepayment REAL NOT NULL DEFAULT 0, "
            "payment_due REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
            "UNIQUE(schedule_item_id, client_id))"
        )
        connection.execute(
            "INSERT INTO schedule_participants "
            "(schedule_item_id, client_id, client_name, client_phone, guests_count, "
            "price, prepayment, payment_due, created_at) "
            "VALUES (1, 1, 'Алия', '', 2, 9000, 0, 9000, '')"
        )
        connection.execute("DROP TABLE schedule_participants_old")
        connection.execute("DROP TABLE schedule_yookassa_payments")
        init_schema(connection)
        row = connection.execute(
            "SELECT paid_online FROM schedule_participants"
        ).fetchone()
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'schedule_yookassa_payments'"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 0)
        self.assertIsNotNone(table_exists)

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
            migrated_booking = db.execute(
                "SELECT service_id FROM schedule_items WHERE id = ?",
                (booking_id,),
            ).fetchone()
            segment = db.execute(
                "SELECT segment FROM client_segments WHERE client_id = ?",
                (participant["client_id"],),
            ).fetchone()
        self.assertEqual(participant["client_name"], "Старый турист")
        self.assertEqual(participant["guests_count"], 1)
        self.assertIsNotNone(migrated_booking["service_id"])
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
                capacity="4",
                customer_name="",
                **{
                    "participant_client_id[]": ["", ""],
                    "participant_name[]": ["Алия", "Мария"],
                    "participant_phone[]": ["+79998880001", "+79998880002"],
                    "participant_guests[]": ["3", "2"],
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
                    "participant_guests[]": ["2", "3"],
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
                    "participant_guests[]": ["4"],
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
                "SELECT client_name, guests_count FROM schedule_participants "
                "WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchall()
        self.assertEqual(item["participants_count"], 4)
        self.assertEqual(
            [participant["client_name"] for participant in participants], ["Мария"]
        )
        self.assertEqual(participants[0]["guests_count"], 4)

    def test_edit_participant_updates_preferred_contact_method(self):
        self.login()
        self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": [""],
                    "participant_name[]": ["Алия"],
                    "participant_phone[]": ["+79998880001"],
                    "participant_guests[]": ["2"],
                },
            ),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT id FROM schedule_items").fetchone()["id"]
            participant = db.execute(
                "SELECT id, client_id FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()

        response = self.client.post(
            f"/schedule/items/{item_id}/participants/{participant['id']}",
            json={
                "client_name": "Алия", "client_phone": "+79998880001",
                "guests_count": 2, "price": 5000, "sales_partner_id": "",
                "preferred_contact_method": "whatsapp",
            },
        )
        data = response.get_json()
        self.assertTrue(data["ok"], data.get("message"))
        self.assertEqual(data["participants"][0]["preferred_contact_method"], "whatsapp")
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT preferred_contact_method FROM clients WHERE id = ?",
                (participant["client_id"],),
            ).fetchone()
        self.assertEqual(client["preferred_contact_method"], "whatsapp")

    def test_edit_participant_rejects_invalid_contact_method(self):
        self.login()
        self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": [""],
                    "participant_name[]": ["Алия"],
                    "participant_phone[]": ["+79998880001"],
                    "participant_guests[]": ["2"],
                },
            ),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT id FROM schedule_items").fetchone()["id"]
            participant = db.execute(
                "SELECT id FROM schedule_participants WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchone()

        response = self.client.post(
            f"/schedule/items/{item_id}/participants/{participant['id']}",
            json={
                "client_name": "Алия", "client_phone": "+79998880001",
                "guests_count": 2, "price": 5000, "sales_partner_id": "",
                "preferred_contact_method": "carrier-pigeon",
            },
        )
        data = response.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("канал связи", data["message"])

    def test_group_event_rejects_invalid_guest_count(self):
        self.login()
        response = self.client.post(
            "/schedule/items",
            data=self.booking_data(
                kind="event",
                capacity="10",
                customer_name="",
                **{
                    "participant_client_id[]": [""],
                    "participant_name[]": ["Алия"],
                    "participant_phone[]": ["+79998880001"],
                    "participant_guests[]": ["0"],
                },
            ),
            follow_redirects=True,
        )
        self.assertIn(
            "укажите количество гостей от 1 до 100",
            response.get_data(as_text=True),
        )
        with application_module.app.app_context():
            item_count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_items"
            ).fetchone()["count"]
        self.assertEqual(item_count, 0)

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
