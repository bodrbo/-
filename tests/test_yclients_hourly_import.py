import datetime as dt
import unittest

from support import application_module


class YclientsHourlyImportTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM trip_expenses")
            db.execute("DELETE FROM trip_labor")
            db.execute("DELETE FROM trips")
            db.execute("DELETE FROM entries")
            db.execute("DELETE FROM import_candidates")
            db.execute("DELETE FROM yclients_imports")
            db.execute("DELETE FROM yclients_sync_state")
            db.execute("DELETE FROM boat_fuel_trip_events")
            db.execute("DELETE FROM boat_fuel_transactions")
            db.execute(
                "UPDATE boat_fuel_state SET activated_at = NULL, "
                "activated_by_role = NULL, activated_by_name = NULL, "
                "last_synced_at = NULL"
            )
            db.commit()

        self.original_records = application_module.yclients_get_records
        self.original_colors = application_module.yclients_get_activity_colors
        self.addCleanup(
            setattr,
            application_module,
            "yclients_get_records",
            self.original_records,
        )
        self.addCleanup(
            setattr,
            application_module,
            "yclients_get_activity_colors",
            self.original_colors,
        )

    @staticmethod
    def record(record_id, hour, *, attendance=0, deleted=False, activity_id=None):
        return {
            "id": record_id,
            "activity_id": activity_id,
            "datetime": f"2026-08-23T{hour}:00:00+03:00",
            "seance_length": 3600,
            "attendance": attendance,
            "deleted": deleted,
            "custom_color": "8bc34a",
            "staff": {"name": "Дмитрий Тарусов"},
            "services": [
                {
                    "id": 14624788,
                    "title": "Форты Кронштадта - малый тур",
                    "cost": 10000,
                }
            ],
        }

    def test_hourly_sync_imports_completed_income_once(self):
        records = [
            self.record(1, "09", activity_id=901),
            self.record(2, "14"),
            self.record(3, "08", attendance=-1),
            self.record(4, "07", deleted=True),
        ]
        requested_ranges = []
        application_module.yclients_get_records = lambda start, end: (
            requested_ranges.append((start, end)) or records
        )
        application_module.yclients_get_activity_colors = lambda ids: {901: "8bc34a"}

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 12, 0)
            )
            second = application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 12, 5)
            )
            trip = db.execute("SELECT * FROM trips").fetchone()
            trip_count = db.execute(
                "SELECT COUNT(*) AS count FROM trips"
            ).fetchone()["count"]
            payroll_total = db.execute(
                "SELECT SUM(amount) AS total FROM entries WHERE employee = ?",
                ("Дмитрий Тарусов",),
            ).fetchone()["total"]
            imported_refs = db.execute(
                "SELECT COUNT(*) AS count FROM yclients_imports"
            ).fetchone()["count"]
            cursor = db.execute(
                "SELECT last_success_at FROM yclients_sync_state "
                "WHERE sync_key = 'trip_import'"
            ).fetchone()["last_success_at"]

        self.assertEqual(first["trips"]["imported"], 1)
        self.assertEqual(second["trips"]["imported"], 0)
        self.assertEqual(first["trips"]["pending"], 0)
        self.assertEqual(trip_count, 1)
        self.assertEqual(imported_refs, 1)
        self.assertEqual(trip["revenue"], 10000)
        self.assertGreater(trip["investor_payout"], 0)
        self.assertEqual(payroll_total, 3000)
        self.assertEqual(cursor, "2026-08-23 12:05")
        self.assertEqual(
            requested_ranges,
            [("2026-08-16", "2026-08-23"), ("2026-08-21", "2026-08-23")],
        )

    def test_hourly_sync_recovers_from_last_successful_trip_cursor(self):
        requested_ranges = []
        application_module.yclients_get_records = lambda start, end: (
            requested_ranges.append((start, end)) or []
        )
        application_module.yclients_get_activity_colors = lambda ids: {}

        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO yclients_sync_state (sync_key, last_success_at) "
                "VALUES ('trip_import', '2026-08-10 11:00')"
            )
            db.commit()
            application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 12, 0)
            )

        self.assertEqual(requested_ranges, [("2026-08-09", "2026-08-23")])

    def test_failed_fetch_does_not_advance_trip_cursor(self):
        def fail_fetch(start, end):
            raise RuntimeError("temporary YCLIENTS failure")

        application_module.yclients_get_records = fail_fetch
        application_module.yclients_get_activity_colors = lambda ids: {}

        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO yclients_sync_state (sync_key, last_success_at) "
                "VALUES ('trip_import', '2026-08-10 11:00')"
            )
            db.commit()
            with self.assertRaises(RuntimeError):
                application_module._sync_hourly_yclients(
                    db, dt.datetime(2026, 8, 23, 12, 0)
                )
            cursor = db.execute(
                "SELECT last_success_at FROM yclients_sync_state "
                "WHERE sync_key = 'trip_import'"
            ).fetchone()["last_success_at"]

        self.assertEqual(cursor, "2026-08-10 11:00")


if __name__ == "__main__":
    unittest.main()
