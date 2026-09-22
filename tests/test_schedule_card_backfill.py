import datetime as dt
import unittest

from support import application_module
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services


class ScheduleCardBackfillTests(unittest.TestCase):
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
            self.captain_id = self.ensure_employee(db, "Карточка Тест Капитан", "Капитан")
            self.guide_captain_id = self.ensure_employee(
                db, "Карточка Тест ГидКапитан", "Гид-капитан"
            )
            db.commit()

    @staticmethod
    def ensure_employee(db, name, position):
        row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-01-01 09:00', NULL)",
                (name,),
            )
            employee_id = cursor.lastrowid
        else:
            employee_id = row["id"]
            db.execute("UPDATE employees SET deleted_at = NULL WHERE id = ?", (employee_id,))
        db.execute(
            "INSERT OR IGNORE INTO employee_positions "
            "(employee_id, position, created_at) VALUES (?, ?, '2026-01-01 09:00')",
            (employee_id, position),
        )
        return employee_id

    @staticmethod
    def make_trip(
        db, *, boat="Бодрый Первый", trip_date="2026-06-10", trip_time="09:00",
        revenue=5000, source="yclients",
    ):
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO trips (boat, trip_date, trip_time, work_type, revenue, "
            "sale_channel, commission_pct, commission_is_manual, commission_amount, "
            "labor_cost, fuel_cost, mooring_cost, extra_total, remainder, "
            "investor_payout, my_share, source, needs_review, created_at) "
            "VALUES (?, ?, ?, 'Малый тур', ?, 'direct', 30, 0, 0, "
            "1100, 0, 0, 0, 0, 0, 0, ?, 0, ?)",
            (boat, trip_date, trip_time, revenue, source, now),
        )
        trip_id = cur.lastrowid
        db.commit()
        return trip_id

    @staticmethod
    def add_labor(db, trip_id, employee_name, work_type, quantity=1.0, rate=1100):
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (employee_name, work_type, rate, quantity, rate * quantity, "2026-06-10", now),
        )
        entry_id = cur.lastrowid
        db.execute(
            "INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)",
            (trip_id, entry_id),
        )
        db.commit()
        return entry_id

    def test_creates_card_with_captain_role_and_links_trip(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Карточка Тест Капитан", "Малый тур", quantity=1.5)
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item = schedule_repository.get_item(db, item_id)
            assignment = db.execute(
                "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?", (item_id,)
            ).fetchone()
            day_crew = db.execute(
                "SELECT 1 FROM schedule_day_crew WHERE work_date = ? AND employee_id = ?",
                ("2026-06-10", self.captain_id),
            ).fetchone()

        self.assertTrue(ok, message)
        self.assertEqual(item["boat"], "Бодрый Первый")
        self.assertEqual(item["accounting_trip_id"], trip_id)
        self.assertEqual(item["starts_at"], "2026-06-10 09:00")
        self.assertEqual(item["ends_at"], "2026-06-10 10:30")
        self.assertEqual(item["kind"], "event")
        self.assertEqual(item["revenue"], 5000)
        self.assertEqual(assignment["role"], "captain")
        self.assertEqual(assignment["employee_name"], "Карточка Тест Капитан")
        self.assertIsNotNone(day_crew)

    def test_creates_card_with_guide_captain_role_from_suffix(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(
                db, trip_id, "Карточка Тест ГидКапитан", "Малый тур гид/капитан",
                quantity=1.0, rate=1870,
            )
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            assignment = db.execute(
                "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?", (item_id,)
            ).fetchone()

        self.assertTrue(ok, message)
        self.assertEqual(assignment["role"], "guide_captain")

    def test_booking_kind_detected_from_rental_service_name(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Карточка Тест Капитан", "Аренда на 3 часа", quantity=3.0)
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item = schedule_repository.get_item(db, item_id)

        self.assertTrue(ok, message)
        self.assertEqual(item["kind"], "booking")
        self.assertEqual(item["service_name"], "Аренда на 3 часа")

    def test_skips_trip_with_no_labor_rows(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item_count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_items"
            ).fetchone()["c"]

        self.assertFalse(ok)
        self.assertIsNone(item_id)
        self.assertIn("сотрудник", message.lower())
        self.assertEqual(item_count, 0)

    def test_skips_trip_with_unmapped_work_type(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Карточка Тест Капитан", "Несуществующий вид рейса")
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)

        self.assertFalse(ok)
        self.assertIsNone(item_id)
        self.assertIn("не сопоставлен", message)

    def test_skips_trip_with_unknown_employee(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Совсем Неизвестный Человек", "Малый тур")
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item_count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_items"
            ).fetchone()["c"]

        self.assertFalse(ok)
        self.assertIsNone(item_id)
        self.assertIn("не найден", message)
        self.assertEqual(item_count, 0)

    def test_list_trips_without_schedule_card_filters_correctly(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            # 1. yclients trip with no card yet - should be listed.
            trip_needs_card = self.make_trip(db, trip_date="2026-06-05", source="yclients")
            # 2. yclients trip that already has a linked card - should not
            #    be listed again.
            trip_has_card = self.make_trip(db, trip_date="2026-06-06", source="yclients")
            self.add_labor(db, trip_has_card, "Карточка Тест Капитан", "Малый тур")
            trip_row = db.execute(
                "SELECT * FROM trips WHERE id = ?", (trip_has_card,)
            ).fetchone()
            schedule_services.create_item_from_trip(db, trip_row)
            # 3. manual trip - never a backfill target regardless of range.
            self.make_trip(db, trip_date="2026-06-07", source="manual")
            # 4. yclients trip outside the queried range.
            self.make_trip(db, trip_date="2026-07-01", source="yclients")

            listed_ids = {
                row["id"] for row in schedule_repository.list_trips_without_schedule_card(
                    db, "2026-06-01", "2026-06-30",
                )
            }

        self.assertEqual(listed_ids, {trip_needs_card})


if __name__ == "__main__":
    unittest.main()
