import datetime as dt
import unittest

from support import application_module
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services


class ScheduleAutoCloseTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM trip_expenses")
            db.execute("DELETE FROM trip_labor")
            db.execute("DELETE FROM trips")
            db.execute("DELETE FROM entries")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            db.execute("DELETE FROM schedule_day_crew")
            db.execute("DELETE FROM schedule_service_rates WHERE role = 'guide'")
            self.employee_id = self.ensure_employee(db, "Автозакрытие Тест", "Капитан")
            self.service_id = db.execute(
                "SELECT id FROM excursion_services WHERE name = ?", ("Малый тур",)
            ).fetchone()["id"]
            db.commit()
        self.now = dt.datetime(2026, 9, 20, 12, 0)

    @staticmethod
    def ensure_employee(db, name, position):
        row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-08-01 09:00', NULL)",
                (name,),
            )
            employee_id = cursor.lastrowid
        else:
            employee_id = row["id"]
            db.execute("UPDATE employees SET deleted_at = NULL WHERE id = ?", (employee_id,))
        db.execute(
            "INSERT OR IGNORE INTO employee_positions "
            "(employee_id, position, created_at) VALUES (?, ?, '2026-08-01 09:00')",
            (employee_id, position),
        )
        return employee_id

    def make_item(
        self, db, *, starts_at, ends_at, role="captain", employee_id=None,
        revenue=5000, deleted_at=None, assign=True, service_id=None,
    ):
        cursor = db.execute(
            "INSERT INTO schedule_items "
            "(kind, boat, service_name, starts_at, ends_at, revenue, "
            "customer_name, customer_phone, status, source, service_id, "
            "created_at, updated_at, deleted_at) "
            "VALUES ('booking', 'Бодрый Первый', 'Малый тур', ?, ?, ?, "
            "'Клиент Тест', '+79990000000', 'scheduled', 'internal', ?, "
            "'2026-08-01 09:00', '2026-08-01 09:00', ?)",
            (starts_at, ends_at, revenue, service_id or self.service_id, deleted_at),
        )
        item_id = cursor.lastrowid
        if assign:
            resolved_employee_id = employee_id or self.employee_id
            employee_name = db.execute(
                "SELECT name FROM employees WHERE id = ?", (resolved_employee_id,)
            ).fetchone()["name"]
            db.execute(
                "INSERT INTO schedule_assignments "
                "(schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, ?, ?, '2026-08-01 09:00')",
                (item_id, resolved_employee_id, employee_name, role),
            )
        db.commit()
        return item_id

    def create_trip(self, db, payload, needs_review=False):
        return application_module._create_trip_from_schedule_payload(
            db, payload, needs_review=needs_review
        )

    def test_ended_item_closes_into_trip_with_payroll_entry(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = self.make_item(
                db,
                starts_at="2026-09-20 09:00",
                ends_at="2026-09-20 10:00",
                revenue=5000,
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            item = schedule_repository.get_item(db, item_id)
            trip = db.execute(
                "SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)
            ).fetchone()
            entry = db.execute(
                "SELECT * FROM entries WHERE employee = ?", ("Автозакрытие Тест",)
            ).fetchone()

        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["needs_review"], 0)
        self.assertEqual(stats["skipped"], 0)
        self.assertIsNotNone(item["accounting_trip_id"])
        self.assertEqual(trip["source"], "schedule_auto")
        self.assertEqual(trip["needs_review"], 0)
        self.assertEqual(trip["revenue"], 5000)
        self.assertEqual(trip["boat"], "Бодрый Первый")
        self.assertEqual(entry["rate"], 1100)
        self.assertEqual(entry["quantity"], 1.0)
        self.assertEqual(entry["amount"], 1100)

    def test_rerun_is_idempotent(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
            )
            first = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            second = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            trip_count = db.execute("SELECT COUNT(*) AS c FROM trips").fetchone()["c"]

        self.assertEqual(first["closed"], 1)
        self.assertEqual(second["closed"], 0)
        self.assertEqual(trip_count, 1)

    def test_cancelled_item_is_not_closed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
                deleted_at="2026-09-19 08:00",
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            item = schedule_repository.get_item(db, item_id, include_deleted=True)
            trip_count = db.execute("SELECT COUNT(*) AS c FROM trips").fetchone()["c"]

        self.assertEqual(stats["closed"], 0)
        self.assertIsNone(item["accounting_trip_id"])
        self.assertEqual(trip_count, 0)

    def test_item_still_within_grace_buffer_is_not_closed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            # Ends 10 minutes before "now" - inside the 20-minute grace
            # buffer, so it should NOT be eligible yet.
            self.make_item(
                db, starts_at="2026-09-20 11:00", ends_at="2026-09-20 11:50",
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
        self.assertEqual(stats["closed"], 0)
        self.assertEqual(stats["skipped"], 0)

    def test_future_item_is_not_closed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.make_item(
                db, starts_at="2026-09-21 09:00", ends_at="2026-09-21 10:00",
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
        self.assertEqual(stats["closed"], 0)

    def test_unmapped_guide_role_falls_back_to_captain_rate_and_flags_review(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            guide_id = self.ensure_employee(db, "Гид Тестовый", "Гид")
            db.commit()
            item_id = self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
                role="guide", employee_id=guide_id,
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            item = schedule_repository.get_item(db, item_id)
            trip = db.execute(
                "SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)
            ).fetchone()
            entry = db.execute(
                "SELECT * FROM entries WHERE employee = ?", ("Гид Тестовый",)
            ).fetchone()

        self.assertEqual(stats["closed"], 1)
        self.assertEqual(stats["needs_review"], 1)
        self.assertEqual(trip["needs_review"], 1)
        # Falls back to the captain rate for "Малый тур" (1100), not 0.
        self.assertEqual(entry["rate"], 1100)

    def test_item_with_no_assignments_is_skipped_and_retried_later(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
                assign=False,
            )
            stats = schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            item = schedule_repository.get_item(db, item_id)

        self.assertEqual(stats["closed"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertIsNone(item["accounting_trip_id"])

    def test_delete_item_refuses_once_closed(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
            )
            schedule_services.auto_close_schedule_items(
                db, self.create_trip, now=self.now,
            )
            ok, message = schedule_services.delete_item(db, item_id)

        self.assertFalse(ok)
        self.assertIn("финансовым учётом", message)

    def test_apply_minimum_shift_invoked_for_closed_dates(self):
        calls = []

        def fake_apply_minimum_shift(db, start_date, end_date):
            calls.append((start_date, end_date))

        with application_module.app.app_context():
            db = application_module.get_db()
            self.make_item(
                db, starts_at="2026-09-20 09:00", ends_at="2026-09-20 10:00",
            )
            schedule_services.auto_close_schedule_items(
                db, self.create_trip,
                apply_minimum_shift=fake_apply_minimum_shift, now=self.now,
            )

        self.assertEqual(calls, [("2026-09-20", "2026-09-20")])

    def test_apply_minimum_shift_not_called_when_nothing_closed(self):
        calls = []

        def fake_apply_minimum_shift(db, start_date, end_date):
            calls.append((start_date, end_date))

        with application_module.app.app_context():
            db = application_module.get_db()
            schedule_services.auto_close_schedule_items(
                db, self.create_trip,
                apply_minimum_shift=fake_apply_minimum_shift, now=self.now,
            )

        self.assertEqual(calls, [])

    def test_cron_route_requires_secret(self):
        # cron_secret is captured by the blueprint factory from CRON_SECRET
        # at app.py import time (app.py:5253), not read dynamically per
        # request, so the test environment (no CRON_SECRET set) can only
        # exercise the "not configured" branch here — same limitation the
        # existing sync-tripster cron route already has, untested for its
        # 200 path for the same reason. The underlying close logic itself
        # is covered end-to-end by the other tests in this file, which call
        # auto_close_schedule_items directly with the real injected
        # create_trip callable.
        client = application_module.app.test_client()
        resp = client.get("/internal/cron/close-schedule-items")
        self.assertEqual(resp.status_code, 403)
        resp = client.get(
            "/internal/cron/close-schedule-items", query_string={"token": "wrong"},
        )
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
