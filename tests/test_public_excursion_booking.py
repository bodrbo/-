import datetime as dt
import unittest

from support import application_module


class PublicExcursionBookingTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self.original_secret = application_module.EXCURSION_SITE_BOOKING_SECRET
        application_module.EXCURSION_SITE_BOOKING_SECRET = "fort-booking-test-secret"
        self.addCleanup(
            setattr,
            application_module,
            "EXCURSION_SITE_BOOKING_SECRET",
            self.original_secret,
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in (
                "schedule_participant_addons",
                "schedule_participants",
                "schedule_assignments",
                "schedule_items",
                "client_segments",
                "clients",
            ):
                db.execute(f"DELETE FROM {table}")
            service = db.execute(
                "SELECT id FROM excursion_services WHERE name = ?",
                ("Большой тур",),
            ).fetchone()
            db.execute(
                "UPDATE excursion_services SET price = 3700 WHERE id = ?",
                (service["id"],),
            )
            db.commit()
            self.service_id = service["id"]
        self.day = dt.date.today() + dt.timedelta(days=2)

    @staticmethod
    def auth(secret="fort-booking-test-secret"):
        return {"Authorization": f"Bearer {secret}"}

    def create_event(self, *, capacity=10, status="scheduled", hour=12):
        starts_at = f"{self.day.isoformat()} {hour:02d}:00"
        ends_at = f"{self.day.isoformat()} {hour + 2:02d}:30"
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO schedule_items "
                "(kind, boat, service_id, service_name, starts_at, ends_at, capacity, "
                "status, created_at, updated_at) "
                "VALUES ('event', 'Бодрый Первый', ?, 'Большой тур', ?, ?, ?, ?, ?, ?)",
                (
                    self.service_id, starts_at, ends_at, capacity, status,
                    starts_at, starts_at,
                ),
            )
            db.commit()
            return cursor.lastrowid

    def payload(self, item_id, **overrides):
        data = {
            "request_id": "site-booking-0001",
            "schedule_item_id": item_id,
            "name": "Анастасия",
            "phone": "+7 921 000-00-00",
            "guests_count": 2,
            "consent": True,
        }
        data.update(overrides)
        return data

    def test_api_requires_configured_bearer_secret(self):
        item_id = self.create_event()
        application_module.EXCURSION_SITE_BOOKING_SECRET = None
        unavailable = self.client.get(
            "/api/integrations/excursion-booking/availability"
        )
        application_module.EXCURSION_SITE_BOOKING_SECRET = "fort-booking-test-secret"
        unauthorised = self.client.post(
            "/api/integrations/excursion-booking/bookings",
            json=self.payload(item_id),
        )

        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.get_json()["error"], "integration_not_configured"
        )
        self.assertEqual(unauthorised.status_code, 401)
        self.assertEqual(unauthorised.get_json()["error"], "unauthorized")
        self.assertEqual(unauthorised.headers["WWW-Authenticate"], "Bearer")

    def test_availability_returns_only_bookable_events_and_live_seat_count(self):
        available_id = self.create_event(capacity=5, hour=12)
        self.create_event(capacity=5, status="cancelled", hour=16)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES ('Гость', '', '+7 900 000-00-00', 'guest-token', '2026-01-01')"
            )
            db.execute(
                "INSERT INTO schedule_participants "
                "(schedule_item_id, client_id, client_name, client_phone, guests_count, "
                "price, prepayment, payment_due, created_at, source) "
                "VALUES (?, ?, 'Гость', '+7 900 000-00-00', 2, 7400, 0, 7400, "
                "'2026-01-01', 'internal')",
                (available_id, client.lastrowid),
            )
            db.commit()

        response = self.client.get(
            "/api/integrations/excursion-booking/availability",
            query_string={"from": self.day.isoformat(), "days": 3},
            headers=self.auth(),
        )
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["id"], available_id)
        self.assertEqual(body["items"][0]["available_seats"], 3)
        self.assertEqual(body["items"][0]["price_per_guest"], 3700)

    def test_booking_creates_excursion_client_and_updates_schedule_totals(self):
        item_id = self.create_event(capacity=6)
        response = self.client.post(
            "/api/integrations/excursion-booking/bookings",
            json=self.payload(item_id),
            headers=self.auth(),
        )
        body = response.get_json()

        with application_module.app.app_context():
            db = application_module.get_db()
            participant = db.execute(
                "SELECT * FROM schedule_participants WHERE id = ?",
                (body["booking_id"],),
            ).fetchone()
            item = db.execute(
                "SELECT participants_count, revenue FROM schedule_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            segment = db.execute(
                "SELECT segment FROM client_segments WHERE client_id = ?",
                (participant["client_id"],),
            ).fetchone()
            client = db.execute(
                "SELECT acquisition_channel FROM clients WHERE id = ?",
                (participant["client_id"],),
            ).fetchone()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(participant["source"], "fort_site")
        self.assertEqual(participant["source_ref"], "fort_site:site-booking-0001")
        self.assertEqual(participant["sales_channel"], "bodrbo_fort")
        self.assertEqual(client["acquisition_channel"], "bodrbo_fort")
        self.assertEqual(participant["guests_count"], 2)
        self.assertEqual(participant["price"], 7400)
        self.assertEqual(item["participants_count"], 2)
        self.assertEqual(item["revenue"], 7400)
        self.assertEqual(segment["segment"], "excursion")
        self.assertEqual(body["total"], 7400)

    def test_request_id_is_idempotent_and_last_seat_cannot_be_oversold(self):
        item_id = self.create_event(capacity=2)
        first = self.client.post(
            "/api/integrations/excursion-booking/bookings",
            json=self.payload(item_id),
            headers=self.auth(),
        )
        duplicate = self.client.post(
            "/api/integrations/excursion-booking/bookings",
            json=self.payload(item_id, name="Другое имя"),
            headers=self.auth(),
        )
        sold_out = self.client.post(
            "/api/integrations/excursion-booking/bookings",
            json=self.payload(
                item_id,
                request_id="site-booking-0002",
                phone="+7 921 999-00-00",
                guests_count=1,
            ),
            headers=self.auth(),
        )

        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_participants"
            ).fetchone()["count"]

        self.assertEqual(first.status_code, 201)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["duplicate"])
        self.assertEqual(sold_out.status_code, 409)
        self.assertEqual(sold_out.get_json()["error"], "not_enough_seats")
        self.assertEqual(count, 1)

    def test_invalid_consent_and_phone_are_rejected_without_writes(self):
        item_id = self.create_event()
        for payload in (
            self.payload(item_id, consent=False),
            self.payload(item_id, phone="123"),
        ):
            response = self.client.post(
                "/api/integrations/excursion-booking/bookings",
                json=payload,
                headers=self.auth(),
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], "invalid_payload")
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM schedule_participants"
            ).fetchone()["count"]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
