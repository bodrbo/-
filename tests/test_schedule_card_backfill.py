import datetime as dt
import unittest

from support import application_module
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services


class EventCapacityForCrewTests(unittest.TestCase):
    def test_subtracts_crew_from_vessel_capacity(self):
        self.assertEqual(schedule_services.event_capacity_for_crew(12, 2), 10)

    def test_returns_none_when_vessel_capacity_not_set(self):
        self.assertIsNone(schedule_services.event_capacity_for_crew(None, 2))
        self.assertIsNone(schedule_services.event_capacity_for_crew(0, 2))

    def test_never_below_one(self):
        self.assertEqual(schedule_services.event_capacity_for_crew(2, 5), 1)

    def test_never_below_already_booked_guests(self):
        self.assertEqual(schedule_services.event_capacity_for_crew(10, 2, participants_count=9), 9)


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

    def test_capacity_computed_from_vessel_capacity_minus_crew(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE fleet_vessels SET capacity = 12 WHERE name = 'Бодрый Первый'"
            )
            db.commit()
            try:
                trip_id = self.make_trip(db)
                self.add_labor(db, trip_id, "Карточка Тест Капитан", "Малый тур", quantity=1.5)
                self.add_labor(
                    db, trip_id, "Карточка Тест ГидКапитан", "Малый тур гид/капитан",
                    quantity=1.5,
                )
                trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
                ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
                item = schedule_repository.get_item(db, item_id)
            finally:
                db.execute(
                    "UPDATE fleet_vessels SET capacity = NULL WHERE name = 'Бодрый Первый'"
                )
                db.commit()

        self.assertTrue(ok, message)
        self.assertEqual(item["capacity"], 10)

    def test_capacity_left_none_when_vessel_capacity_not_configured(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Карточка Тест Капитан", "Малый тур", quantity=1.5)
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item = schedule_repository.get_item(db, item_id)

        self.assertTrue(ok, message)
        self.assertIsNone(item["capacity"])

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

    def test_skips_trip_when_another_card_already_occupies_the_slot(self):
        # Reproduces the real production case (schedule card №1438 vs
        # №1931): a Tripster-sourced card already sits on this boat/time
        # (boat/crew assigned by hand, so it carries no accounting_trip_id
        # for list_trips_without_schedule_card to notice), and the backfill
        # tries to reconstruct the same physical trip from its own YCLIENTS
        # trips row — must skip instead of double-booking the boat.
        with application_module.app.app_context():
            db = application_module.get_db()
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            db.execute(
                "INSERT INTO schedule_items (kind, boat, service_name, starts_at, "
                "ends_at, capacity, participants_count, customer_name, "
                "customer_phone, revenue, note, status, source, created_at, "
                "updated_at) "
                "VALUES ('event', 'Бодрый Первый', 'Малый тур', "
                "'2026-06-10 09:00', '2026-06-10 10:30', 10, 8, '', '', 29600, "
                "'', 'scheduled', 'tripster', ?, ?)",
                (now, now),
            )
            db.commit()

            trip_id = self.make_trip(db)
            self.add_labor(db, trip_id, "Карточка Тест Капитан", "Малый тур", quantity=1.5)
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

            ok, message, item_id = schedule_services.create_item_from_trip(db, trip)
            item_count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_items"
            ).fetchone()["c"]

        self.assertFalse(ok)
        self.assertIsNone(item_id)
        self.assertIn("уже занят", message)
        self.assertIn("Малый тур", message)
        self.assertEqual(item_count, 1)

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


class ListItemsNeedingParticipantsTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM schedule_participants")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            db.execute("DELETE FROM trip_expenses")
            db.execute("DELETE FROM trip_labor")
            db.execute("DELETE FROM trips")
            db.execute("DELETE FROM entries")
            db.commit()

    @staticmethod
    def make_trip(db, *, trip_date="2026-06-10", source="yclients"):
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO trips (boat, trip_date, trip_time, work_type, revenue, "
            "sale_channel, commission_pct, commission_is_manual, commission_amount, "
            "labor_cost, fuel_cost, mooring_cost, extra_total, remainder, "
            "investor_payout, my_share, source, needs_review, created_at) "
            "VALUES ('Бодрый Первый', ?, '09:00', 'Малый тур', 5000, 'direct', "
            "30, 0, 0, 1100, 0, 0, 0, 0, 0, 0, ?, 0, ?)",
            (trip_date, source, now),
        )
        return cur.lastrowid

    @staticmethod
    def make_item(db, *, trip_id=None, with_participant=False, starts_at="2026-06-10 09:00"):
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO schedule_items (kind, boat, service_name, starts_at, "
            "ends_at, capacity, participants_count, customer_name, "
            "customer_phone, revenue, note, accounting_trip_id, created_at, "
            "updated_at) "
            "VALUES ('event', 'Бодрый Первый', 'Малый тур', ?, "
            "'2026-06-10 10:30', NULL, 0, '', '', 0, '', ?, ?, ?)",
            (starts_at, trip_id, now, now),
        )
        item_id = cur.lastrowid
        if with_participant:
            cur2 = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
                "VALUES ('Тест Клиент', '', '79110000000', 'tok-' || ?, ?)",
                (item_id, now),
            )
            client_id = cur2.lastrowid
            db.execute(
                "INSERT INTO schedule_participants (schedule_item_id, client_id, "
                "client_name, client_phone, guests_count, price, prepayment, "
                "payment_due, created_at) "
                "VALUES (?, ?, 'Тест Клиент', '79110000000', 1, 1000, 0, 1000, ?)",
                (item_id, client_id, now),
            )
        return item_id

    def test_lists_only_yclients_linked_items_with_no_participants(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_yclients = self.make_trip(db, source="yclients")
            trip_manual = self.make_trip(db, source="manual")
            needs = self.make_item(db, trip_id=trip_yclients)
            has_participant = self.make_item(db, trip_id=self.make_trip(db, source="yclients"), with_participant=True)
            manual_item = self.make_item(db, trip_id=trip_manual)
            unlinked_item = self.make_item(db, trip_id=None)
            db.commit()

            listed_ids = {
                row["id"] for row in schedule_repository.list_items_needing_participants(
                    db, "2026-06-01", "2026-06-30",
                )
            }

        self.assertIn(needs, listed_ids)
        self.assertNotIn(has_participant, listed_ids)
        self.assertNotIn(manual_item, listed_ids)
        self.assertNotIn(unlinked_item, listed_ids)

    def test_respects_date_range(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            trip_id = self.make_trip(db, trip_date="2026-07-01", source="yclients")
            outside = self.make_item(db, trip_id=trip_id, starts_at="2026-07-01 09:00")
            db.commit()

            listed_ids = {
                row["id"] for row in schedule_repository.list_items_needing_participants(
                    db, "2026-06-01", "2026-06-30",
                )
            }

        self.assertNotIn(outside, listed_ids)


class AttachParticipantFromRecordTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM schedule_participants")
            db.execute("DELETE FROM schedule_assignments")
            db.execute("DELETE FROM schedule_items")
            db.execute("DELETE FROM clients WHERE yclients_client_id IS NOT NULL")
            db.commit()
            self.item_id = self._make_item(db)
            db.commit()

    @staticmethod
    def _make_item(db):
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO schedule_items (kind, boat, service_name, starts_at, "
            "ends_at, capacity, participants_count, customer_name, "
            "customer_phone, revenue, note, created_at, updated_at) "
            "VALUES ('event', 'Бодрый Первый', 'Малый тур', '2026-06-10 09:00', "
            "'2026-06-10 10:30', NULL, 0, '', '', 0, '', ?, ?)",
            (now, now),
        )
        return cur.lastrowid

    @staticmethod
    def make_record(*, record_id=1001, client_id=555, name="Александра",
                     phone="79113810382", clients_count=4, cost=9000, comment=""):
        return {
            "id": record_id,
            "client": {
                "id": client_id,
                "name": name,
                "display_name": name,
                "phone": phone,
            },
            "clients_count": clients_count,
            "services": [{"cost": cost}],
            "comment": comment,
        }

    def test_attaches_client_and_fills_customer_fields(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            record = self.make_record()
            ok, message = schedule_services.attach_participant_from_record(
                db, self.item_id, record, "2026-06-01 12:00",
            )
            item = schedule_repository.get_item(db, self.item_id)
            participant = db.execute(
                "SELECT * FROM schedule_participants WHERE schedule_item_id = ?",
                (self.item_id,),
            ).fetchone()
            client = db.execute(
                "SELECT * FROM clients WHERE yclients_client_id = 555"
            ).fetchone()

        self.assertTrue(ok, message)
        self.assertIsNotNone(participant)
        self.assertEqual(participant["client_id"], client["id"])
        self.assertEqual(participant["guests_count"], 4)
        self.assertEqual(participant["price"], 9000)
        self.assertEqual(participant["source"], "yclients")
        self.assertEqual(participant["source_ref"], "record:1001")
        self.assertEqual(item["customer_name"], "Александра")
        self.assertEqual(item["customer_phone"], "79113810382")
        self.assertEqual(item["revenue"], 9000)
        self.assertEqual(item["participants_count"], 4)

    def test_rerun_does_not_duplicate_participant(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            record = self.make_record()
            schedule_services.attach_participant_from_record(
                db, self.item_id, record, "2026-06-01 12:00",
            )
            ok, message = schedule_services.attach_participant_from_record(
                db, self.item_id, record, "2026-06-01 12:05",
            )
            count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_participants "
                "WHERE schedule_item_id = ?",
                (self.item_id,),
            ).fetchone()["c"]

        self.assertTrue(ok, message)
        self.assertEqual(count, 1)

    def test_group_event_multiple_attendees_sum_to_group_revenue(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            record_a = self.make_record(record_id=2001, client_id=601, name="Гость Раз",
                                         phone="79110000001", clients_count=2, cost=4000)
            record_b = self.make_record(record_id=2002, client_id=602, name="Гость Два",
                                         phone="79110000002", clients_count=3, cost=6000)
            schedule_services.attach_participant_from_record(
                db, self.item_id, record_a, "2026-06-01 12:00",
            )
            schedule_services.attach_participant_from_record(
                db, self.item_id, record_b, "2026-06-01 12:00",
            )
            item = schedule_repository.get_item(db, self.item_id)

        self.assertEqual(item["revenue"], 10000)
        self.assertEqual(item["participants_count"], 5)

    def test_skips_record_with_no_client(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            record = {"id": 3001, "client": {}, "clients_count": 1, "services": []}
            ok, message = schedule_services.attach_participant_from_record(
                db, self.item_id, record, "2026-06-01 12:00",
            )
            count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_participants"
            ).fetchone()["c"]

        self.assertFalse(ok)
        self.assertEqual(count, 0)

    def test_same_client_on_two_records_collapses_to_one_participant(self):
        # schedule_participants has UNIQUE(schedule_item_id, client_id) —
        # if the same real person turns up on two raw YCLIENTS records for
        # one card (e.g. a duplicate booking), the second attach must not
        # error out, but also can't create a second row.
        with application_module.app.app_context():
            db = application_module.get_db()
            first = self.make_record(record_id=4001, client_id=701)
            second = self.make_record(record_id=4002, client_id=701, cost=9500)
            ok1, _ = schedule_services.attach_participant_from_record(
                db, self.item_id, first, "2026-06-01 12:00",
            )
            ok2, _ = schedule_services.attach_participant_from_record(
                db, self.item_id, second, "2026-06-01 12:05",
            )
            client_count = db.execute(
                "SELECT COUNT(*) AS c FROM clients WHERE yclients_client_id = 701"
            ).fetchone()["c"]
            participant_count = db.execute(
                "SELECT COUNT(*) AS c FROM schedule_participants "
                "WHERE schedule_item_id = ?", (self.item_id,),
            ).fetchone()["c"]

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(client_count, 1)
        self.assertEqual(participant_count, 1)


if __name__ == "__main__":
    unittest.main()
