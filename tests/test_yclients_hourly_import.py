import datetime as dt
import unittest
from unittest import mock

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
        self.original_working_days = application_module.yclients_get_working_staff_days
        application_module.yclients_get_working_staff_days = (
            lambda employee_names, start_date, end_date: {
                "staffed_days": set(),
                "checked_employees": set(),
            }
        )
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
        self.addCleanup(
            setattr,
            application_module,
            "yclients_get_working_staff_days",
            self.original_working_days,
        )

    @staticmethod
    def record(
        record_id,
        hour,
        *,
        attendance=0,
        deleted=False,
        activity_id=None,
        staff_name="Дмитрий Тарусов",
        color="8bc34a",
    ):
        return {
            "id": record_id,
            "activity_id": activity_id,
            "datetime": f"2026-08-23T{hour}:00:00+03:00",
            "seance_length": 3600,
            "attendance": attendance,
            "deleted": deleted,
            "custom_color": color,
            "staff": {"name": staff_name},
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

    def test_empty_scheduled_shift_receives_full_minimum_and_self_corrects(self):
        application_module.yclients_get_records = lambda start, end: []
        application_module.yclients_get_activity_colors = lambda ids: {}
        application_module.yclients_get_working_staff_days = (
            lambda employee_names, start_date, end_date: {
                "staffed_days": {("Платон Жмаев", "2026-08-23")},
                "checked_employees": {"Платон Жмаев"},
            }
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 12, 0)
            )
            topup = db.execute(
                "SELECT amount FROM entries WHERE employee = ? AND work_date = ? "
                "AND work_type = ?",
                (
                    "Платон Жмаев",
                    "2026-08-23",
                    application_module.MIN_SHIFT_TOPUP_WORK_TYPE,
                ),
            ).fetchone()

            application_module.yclients_get_working_staff_days = (
                lambda employee_names, start_date, end_date: {
                    "staffed_days": set(),
                    "checked_employees": {"Платон Жмаев"},
                }
            )
            second = application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 13, 0)
            )
            remaining = db.execute(
                "SELECT COUNT(*) AS count FROM entries WHERE employee = ? AND work_date = ? "
                "AND work_type = ?",
                (
                    "Платон Жмаев",
                    "2026-08-23",
                    application_module.MIN_SHIFT_TOPUP_WORK_TYPE,
                ),
            ).fetchone()["count"]

        self.assertIsNotNone(topup)
        self.assertEqual(topup["amount"], 3000)
        self.assertEqual(first["trips"]["topups_changed"], 1)
        self.assertEqual(first["schedule"]["staffed_days"], 1)
        self.assertEqual(second["trips"]["topups_changed"], 1)
        self.assertEqual(remaining, 0)

    def test_blocked_schedule_day_does_not_receive_minimum_without_real_trip(self):
        application_module.yclients_get_records = lambda start, end: [
            self.record(
                20,
                "09",
                staff_name="Платон Жмаев",
                color=application_module.BLOCKED_SHIFT_COLOR,
            )
        ]
        application_module.yclients_get_activity_colors = lambda ids: {}
        application_module.yclients_get_working_staff_days = (
            lambda employee_names, start_date, end_date: {
                "staffed_days": {("Платон Жмаев", "2026-08-23")},
                "checked_employees": {"Платон Жмаев"},
            }
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            application_module._sync_hourly_yclients(
                db, dt.datetime(2026, 8, 23, 12, 0)
            )
            topups = db.execute(
                "SELECT COUNT(*) AS count FROM entries WHERE employee = ? "
                "AND work_type = ?",
                ("Платон Жмаев", application_module.MIN_SHIFT_TOPUP_WORK_TYPE),
            ).fetchone()["count"]

        self.assertEqual(topups, 0)

    def test_one_failed_schedule_does_not_discard_other_employees(self):
        attempts = {}

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {
                    "success": True,
                    "data": [{"date": "2026-08-23", "is_working": 1, "slots": []}],
                }

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            @staticmethod
            def get(url, timeout):
                staff_id = 2 if "/schedule/979343/2/" in url else 1
                attempts[staff_id] = attempts.get(staff_id, 0) + 1
                if staff_id == 1 and attempts[staff_id] == 1:
                    raise application_module.requests.Timeout("first attempt timeout")
                if "/schedule/979343/2/" in url:
                    raise application_module.requests.Timeout("temporary timeout")
                return FakeResponse()

        staff = [
            {"id": 1, "name": "Платон Жмаев", "fired": 0},
            {"id": 2, "name": "Дмитрий Тарусов", "fired": 0},
        ]
        with mock.patch.object(application_module, "yclients_get_staff", return_value=staff), \
                mock.patch.object(application_module.requests, "Session", FakeSession):
            result = self.original_working_days(
                ["Платон Жмаев", "Дмитрий Тарусов"],
                "2026-08-23",
                "2026-08-23",
            )

        with application_module.app.app_context():
            db = application_module.get_db()
            changed = application_module.apply_minimum_shift_rate(
                db,
                [],
                scheduled_staff_days=result["staffed_days"],
                checked_schedule_employees=result["checked_employees"],
                schedule_start_date="2026-08-23",
                schedule_end_date="2026-08-23",
            )
            rows = db.execute(
                "SELECT employee, amount FROM entries WHERE work_type = ?",
                (application_module.MIN_SHIFT_TOPUP_WORK_TYPE,),
            ).fetchall()

        self.assertEqual(result["checked_employees"], {"Платон Жмаев"})
        self.assertEqual(result["failed_employees"], ["Дмитрий Тарусов"])
        self.assertEqual(attempts, {1: 2, 2: 2})
        self.assertEqual(changed, 1)
        self.assertEqual([(row["employee"], row["amount"]) for row in rows], [
            ("Платон Жмаев", 3000),
        ])

    def test_reassigned_trip_moves_payroll_without_duplicate_revenue(self):
        old_record = self.record(10, "09", staff_name="Старый капитан")
        reassigned_record = self.record(10, "09", staff_name="Платон Жмаев")

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._import_yclients_trip_records(
                db,
                [old_record],
                {},
                "2026-08-23",
                "2026-08-23",
            )
            trip = db.execute("SELECT * FROM trips").fetchone()
            review_ref = f"recheck:{trip['id']}:slot:8bc34a:2026-08-23T09:00"
            db.execute(
                "INSERT INTO import_candidates (yclients_ref, summary, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (review_ref, "Старое предупреждение", "{}", "2026-08-23 10:00"),
            )
            db.commit()

            second = application_module._import_yclients_trip_records(
                db,
                [reassigned_record],
                {},
                "2026-08-23",
                "2026-08-23",
            )

            trip_count = db.execute(
                "SELECT COUNT(*) AS count FROM trips"
            ).fetchone()["count"]
            revenue = db.execute("SELECT SUM(revenue) AS total FROM trips").fetchone()["total"]
            labor_names = {
                row["employee"]
                for row in db.execute(
                    "SELECT entries.employee FROM trip_labor "
                    "JOIN entries ON entries.id = trip_labor.entry_id"
                ).fetchall()
            }
            old_payroll = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM entries WHERE employee = ?",
                ("Старый капитан",),
            ).fetchone()["total"]
            new_payroll = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total FROM entries WHERE employee = ?",
                ("Платон Жмаев",),
            ).fetchone()["total"]
            pending = db.execute(
                "SELECT COUNT(*) AS count FROM import_candidates"
            ).fetchone()["count"]

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["payroll_updated"], 1)
        self.assertEqual(trip_count, 1)
        self.assertEqual(revenue, 10000)
        self.assertEqual(labor_names, {"Платон Жмаев"})
        self.assertEqual(old_payroll, 0)
        self.assertEqual(new_payroll, 3000)
        self.assertEqual(pending, 0)

    def test_late_plain_crew_record_joins_existing_activity_trip(self):
        daniil = self.record(
            1911819555,
            "16",
            activity_id=49006044,
            staff_name="Даниил Галецкий",
            color="",
        )
        daniil["datetime"] = "2026-08-18T16:00:00+03:00"
        daniil["seance_length"] = 9000
        daniil["services"] = [
            {
                "id": 14624702,
                "title": "Форты Кронштадта - большой тур",
                "cost": 7400,
            }
        ]
        elmira = self.record(
            1911826743,
            "16",
            activity_id=0,
            staff_name="Эльмира Бектаева",
            color="673ab7",
        )
        elmira["datetime"] = "2026-08-18T16:00:00+03:00"
        elmira["seance_length"] = 9000
        elmira["services"] = []

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._import_yclients_trip_records(
                db,
                [daniil],
                {49006044: "#673ab7"},
                "2026-08-18",
                "2026-08-18",
            )
            solo_rate = db.execute(
                "SELECT entries.rate FROM trip_labor "
                "JOIN entries ON entries.id = trip_labor.entry_id"
            ).fetchone()["rate"]
            # Simulate an administrator skipping the old invalid standalone
            # zero-revenue card before this repair existed.
            db.execute(
                "INSERT INTO yclients_imports (yclients_ref, trip_id) VALUES (?, NULL)",
                ("slot:673ab7:2026-08-18T16:00",),
            )
            db.commit()

            second = application_module._import_yclients_trip_records(
                db,
                [daniil, elmira],
                {49006044: "#673ab7"},
                "2026-08-18",
                "2026-08-18",
            )
            labor_after_second = db.execute(
                "SELECT entries.employee, entries.rate, entries.quantity "
                "FROM trip_labor JOIN entries ON entries.id = trip_labor.entry_id "
                "ORDER BY entries.employee"
            ).fetchall()
            trip = db.execute("SELECT * FROM trips").fetchone()
            trip_count = db.execute(
                "SELECT COUNT(*) AS count FROM trips"
            ).fetchone()["count"]
            imported_refs = db.execute(
                "SELECT COUNT(*) AS count FROM yclients_imports WHERE trip_id = ?",
                (trip["id"],),
            ).fetchone()["count"]
            pending = db.execute(
                "SELECT COUNT(*) AS count FROM import_candidates"
            ).fetchone()["count"]

            third = application_module._import_yclients_trip_records(
                db,
                [daniil, elmira],
                {49006044: "#673ab7"},
                "2026-08-18",
                "2026-08-18",
            )
            labor_after_third = db.execute(
                "SELECT entries.employee, entries.rate FROM trip_labor "
                "JOIN entries ON entries.id = trip_labor.entry_id "
                "ORDER BY entries.employee"
            ).fetchall()

        self.assertEqual(first["imported"], 1)
        self.assertEqual(solo_rate, 1870)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["payroll_updated"], 1)
        self.assertEqual(trip_count, 1)
        self.assertEqual(trip["revenue"], 7400)
        self.assertEqual(trip["labor_cost"], 5500)
        self.assertEqual(imported_refs, 2)
        self.assertEqual(pending, 0)
        self.assertEqual(
            [(row["employee"], row["rate"], row["quantity"]) for row in labor_after_second],
            [
                ("Даниил Галецкий", 1100, 2.5),
                ("Эльмира Бектаева", 1100, 2.5),
            ],
        )
        self.assertEqual(third["payroll_updated"], 0)
        self.assertEqual(
            [(row["employee"], row["rate"]) for row in labor_after_third],
            [("Даниил Галецкий", 1100), ("Эльмира Бектаева", 1100)],
        )

    def test_existing_trip_updates_when_service_duration_changes(self):
        primary = self.record(
            1921209114,
            "11",
            staff_name="Андрей Жаворонков",
            color="2196f3",
        )
        primary["datetime"] = "2026-08-22T11:00:00+03:00"
        primary["seance_length"] = 7200
        primary["services"] = [
            {
                "id": 14624850,
                "title": "Индивидуальная аренда 2 часа",
                "cost": 16000,
            }
        ]
        primary["comment"] = "индивидуальная обзорная"
        guide = self.record(
            1908721986,
            "11",
            staff_name="Эльмира Бектаева",
            color="2196f3",
        )
        guide["datetime"] = "2026-08-22T11:00:00+03:00"
        guide["seance_length"] = 7200
        guide["services"] = []
        guide["comment"] = "Гидом на Ларус"

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._import_yclients_trip_records(
                db,
                [primary, guide],
                {},
                "2026-08-22",
                "2026-08-22",
            )

            primary["seance_length"] = 3600
            primary["services"] = [
                {
                    "id": 14624830,
                    "title": "Индивидуальная аренда 1 час",
                    "cost": 9000,
                }
            ]
            guide["seance_length"] = 3600
            second = application_module._import_yclients_trip_records(
                db,
                [primary, guide],
                {},
                "2026-08-22",
                "2026-08-22",
            )

            trip = db.execute("SELECT * FROM trips").fetchone()
            labor = db.execute(
                "SELECT entries.employee, entries.work_type, entries.quantity, "
                "entries.rate, entries.amount FROM trip_labor "
                "JOIN entries ON entries.id = trip_labor.entry_id "
                "ORDER BY entries.employee"
            ).fetchall()
            topups = db.execute(
                "SELECT employee, amount FROM entries WHERE work_type = ? "
                "ORDER BY employee",
                (application_module.MIN_SHIFT_TOPUP_WORK_TYPE,),
            ).fetchall()

            third = application_module._import_yclients_trip_records(
                db,
                [primary, guide],
                {},
                "2026-08-22",
                "2026-08-22",
            )

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["payroll_updated"], 1)
        self.assertEqual(third["payroll_updated"], 0)
        self.assertEqual(trip["work_type"], "Индивидуальная аренда 1 час")
        self.assertEqual(trip["revenue"], 9000)
        self.assertEqual(trip["commission_amount"], 2700)
        self.assertEqual(trip["labor_cost"], 2200)
        self.assertEqual(trip["remainder"], 1999)
        self.assertEqual(trip["investor_payout"], 999.5)
        self.assertEqual(trip["my_share"], 3699.5)
        self.assertEqual(
            [
                (
                    row["employee"],
                    row["work_type"],
                    row["quantity"],
                    row["rate"],
                    row["amount"],
                )
                for row in labor
            ],
            [
                ("Андрей Жаворонков", "Индивидуальная аренда 1 час", 1, 1100, 1100),
                ("Эльмира Бектаева", "Индивидуальная аренда 1 час", 1, 1100, 1100),
            ],
        )
        self.assertEqual(
            [(row["employee"], row["amount"]) for row in topups],
            [("Андрей Жаворонков", 1900), ("Эльмира Бектаева", 1900)],
        )

    def test_activity_marked_red_after_import_removes_existing_trip(self):
        record = self.record(
            1898319087,
            "10",
            activity_id=48521964,
            staff_name="Андрей Жаворонков",
            color="",
        )
        record["datetime"] = "2026-08-19T10:00:00+03:00"
        record["seance_length"] = 9000
        record["services"] = [
            {
                "id": 14624702,
                "title": "Форты Кронштадта - большой тур",
                "cost": 14800,
            }
        ]

        with application_module.app.app_context():
            db = application_module.get_db()
            first = application_module._import_yclients_trip_records(
                db,
                [record],
                {48521964: "#8bc34a"},
                "2026-08-19",
                "2026-08-19",
            )
            cancelled = application_module._import_yclients_trip_records(
                db,
                [record],
                {48521964: "#f44336"},
                "2026-08-19",
                "2026-08-19",
            )
            trip_count_after_cancel = db.execute(
                "SELECT COUNT(*) AS count FROM trips"
            ).fetchone()["count"]
            trip_pay_after_cancel = db.execute(
                "SELECT COUNT(*) AS count FROM entries "
                "WHERE work_type = 'Большой тур'"
            ).fetchone()["count"]
            refs_after_cancel = db.execute(
                "SELECT COUNT(*) AS count FROM yclients_imports"
            ).fetchone()["count"]

            restored = application_module._import_yclients_trip_records(
                db,
                [record],
                {48521964: "#8bc34a"},
                "2026-08-19",
                "2026-08-19",
            )
            trip_count_after_restore = db.execute(
                "SELECT COUNT(*) AS count FROM trips"
            ).fetchone()["count"]

        self.assertEqual(first["imported"], 1)
        self.assertEqual(cancelled["cancelled"], 1)
        self.assertEqual(trip_count_after_cancel, 0)
        self.assertEqual(trip_pay_after_cancel, 0)
        self.assertEqual(refs_after_cancel, 0)
        self.assertEqual(restored["imported"], 1)
        self.assertEqual(trip_count_after_restore, 1)


if __name__ == "__main__":
    unittest.main()
