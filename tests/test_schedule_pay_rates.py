import unittest

from support import application_module
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services
from modules.schedule.constants import DEFAULT_SERVICE_RATES
from modules.schedule.schema import init_schema


class SchedulePayRatesTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            # None of DEFAULT_SERVICE_RATES seed a "guide" row (that's the
            # deliberate gap under test) — safe to always clear it so tests
            # that add one don't leak into each other regardless of order.
            db.execute("DELETE FROM schedule_service_rates WHERE role = 'guide'")
            db.commit()
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def test_seeding_matches_default_service_rates_with_no_guide_row(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            rows = db.execute(
                "SELECT excursion_services.name AS service_name, "
                "schedule_service_rates.role, schedule_service_rates.rate "
                "FROM schedule_service_rates "
                "JOIN excursion_services "
                "ON excursion_services.id = schedule_service_rates.service_id"
            ).fetchall()
        seeded = {(row["service_name"], row["role"]): row["rate"] for row in rows}
        expected = {
            (name, role): rate for name, role, rate in DEFAULT_SERVICE_RATES
        }
        self.assertEqual(seeded, expected)
        self.assertFalse(any(role == "guide" for _name, role in seeded))

    def test_reinit_schema_does_not_duplicate_seeded_rates(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            before = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_service_rates"
            ).fetchone()["c"]
            init_schema(db)
            init_schema(db)
            after = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_service_rates"
            ).fetchone()["c"]
        self.assertEqual(before, after)
        self.assertEqual(before, len(DEFAULT_SERVICE_RATES))

    def test_set_service_rate_validates_and_upserts(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            service = db.execute(
                "SELECT id FROM excursion_services WHERE name = ?", ("Малый тур",)
            ).fetchone()
            service_id = service["id"]

            ok, message = schedule_services.set_service_rate(
                db, service_id, "guide", "950"
            )
            self.assertTrue(ok, message)
            self.assertEqual(
                schedule_repository.get_service_rate(db, service_id, "guide"), 950.0
            )

            # Upsert: same (service, role) updates in place, no duplicate row.
            ok, message = schedule_services.set_service_rate(
                db, service_id, "guide", "1000"
            )
            self.assertTrue(ok, message)
            count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_service_rates "
                "WHERE service_id = ? AND role = ?",
                (service_id, "guide"),
            ).fetchone()["c"]
            self.assertEqual(count, 1)
            self.assertEqual(
                schedule_repository.get_service_rate(db, service_id, "guide"), 1000.0
            )

            ok, message = schedule_services.set_service_rate(
                db, service_id, "not-a-role", "500"
            )
            self.assertFalse(ok)

            ok, message = schedule_services.set_service_rate(
                db, service_id, "captain", "not-a-number"
            )
            self.assertFalse(ok)

            ok, message = schedule_services.set_service_rate(
                db, service_id, "captain", "-5"
            )
            self.assertFalse(ok)

            ok, message = schedule_services.set_service_rate(
                db, 999999, "captain", "1000"
            )
            self.assertFalse(ok)

    def test_rates_page_and_update_route_require_admin_and_work(self):
        anon = application_module.app.test_client()
        resp = anon.get("/schedule/rates")
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get("/schedule/rates")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Малый тур".encode(), resp.data)

        with application_module.app.app_context():
            db = application_module.get_db()
            service = db.execute(
                "SELECT id FROM excursion_services WHERE name = ?", ("Средний тур",)
            ).fetchone()
            service_id = service["id"]

        resp = self.client.post(
            "/schedule/rates",
            data={"service_id": str(service_id), "role": "guide", "rate": "1234"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with application_module.app.app_context():
            db = application_module.get_db()
            rate = schedule_repository.get_service_rate(db, service_id, "guide")
        self.assertEqual(rate, 1234.0)


if __name__ == "__main__":
    unittest.main()
