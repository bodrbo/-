import datetime as dt
import unittest

from support import application_module
from modules.payroll_rates import repository as payroll_rates_repository
from modules.payroll_rates.constants import DEFAULT_EXCURSION_ROLE_RATES, EXCURSION_ROLES
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services


class ClosedScheduleItemSyncTests(unittest.TestCase):
    """Editing a card that was already closed into accounting must carry the
    change over to its trip and payroll entries — only what changed, so
    values edited by hand on the trip survive."""

    DAY = "2026-09-05"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in (
                "trip_expenses", "trip_labor", "trips", "entries", "schedule_day_crew",
                "schedule_manual_payments", "schedule_yookassa_payments",
                "schedule_modulkassa_receipts", "schedule_participants",
                "schedule_assignments", "schedule_items",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute(
                "DELETE FROM payments WHERE employee IN ('Даниил Галецкий', 'Платон Жмаев')"
            )
            for role in EXCURSION_ROLES:
                db.execute(
                    "UPDATE excursion_role_rates SET rate = ? WHERE role = ?",
                    (DEFAULT_EXCURSION_ROLE_RATES.get(role, 0), role),
                )
            db.execute("UPDATE excursion_role_rates SET rate = 1000 WHERE role = 'captain'")
            db.execute("UPDATE excursion_role_rates SET rate = 1500 WHERE role = 'guide_captain'")
            self.daniil_id = self.ensure_crew_member(db, "Даниил Галецкий", "Гид-капитан")
            self.platon_id = self.ensure_crew_member(db, "Платон Жмаев", "Капитан")
            db.executemany(
                "INSERT INTO schedule_day_crew (work_date, employee_id, created_at) "
                "VALUES (?, ?, '2026-09-01 09:00')",
                [(self.DAY, self.daniil_id), (self.DAY, self.platon_id)],
            )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    @staticmethod
    def ensure_crew_member(db, name, position):
        row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
        if row is None:
            employee_id = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-08-31 12:00', NULL)", (name,),
            ).lastrowid
        else:
            employee_id = row["id"]
            db.execute("UPDATE employees SET deleted_at = NULL WHERE id = ?", (employee_id,))
        db.execute(
            "INSERT OR IGNORE INTO employee_positions (employee_id, position, created_at) "
            "VALUES (?, ?, '2026-08-31 12:00')", (employee_id, position),
        )
        return employee_id

    def form(self, **overrides):
        data = {
            "kind": "booking",
            "boat": "Бодрый Второй",
            "service_name": "Большой тур",
            "trip_date": self.DAY,
            "start_time": "13:00",
            "end_time": "15:30",
            "employee_id[]": [str(self.daniil_id)],
            "role[]": ["guide_captain"],
            "customer_name": "Алия",
            "customer_phone": "+79118115476",
            "revenue": "18000",
            "note": "",
            "return_employee": "all",
        }
        data.update(overrides)
        return data

    def create_item(self, close=True, **overrides):
        self.client.post("/schedule/items", data=self.form(**overrides))
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute("SELECT MAX(id) AS id FROM schedule_items").fetchone()["id"]
            if close:
                schedule_services.auto_close_schedule_items(
                    db, application_module._create_trip_from_schedule_payload,
                    payroll_rates_repository.get_excursion_role_rate,
                    now=dt.datetime(2026, 9, 6, 12, 0),
                )
        return item_id

    def edit(self, item_id, **overrides):
        return self.client.post(f"/schedule/items/{item_id}", data=self.form(**overrides))

    def trip_of(self, item_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            item = schedule_repository.get_item(db, item_id)
            if item["accounting_trip_id"] is None:
                return None, []
            trip = dict(db.execute(
                "SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)
            ).fetchone())
            entries = [dict(r) for r in db.execute(
                "SELECT e.* FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id "
                "WHERE tl.trip_id = ? ORDER BY e.id", (trip["id"],),
            ).fetchall()]
            return trip, entries

    def notice(self):
        with self.client.session_transaction() as session:
            return (session.get("schedule_notice") or {}).get("message", "")

    def test_the_fixture_closes_into_a_trip_paid_to_the_card_crew(self):
        item_id = self.create_item()
        trip, entries = self.trip_of(item_id)
        self.assertEqual([e["employee"] for e in entries], ["Даниил Галецкий"])
        self.assertEqual(entries[0]["quantity"], 2.5)
        self.assertEqual(entries[0]["rate"], 1500)

    def test_a_revenue_change_reaches_the_trip_and_keeps_hand_edited_costs(self):
        item_id = self.create_item()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE trips SET fuel_cost = 700")
            db.commit()
        self.edit(item_id, revenue="20000")
        trip, entries = self.trip_of(item_id)
        self.assertEqual(trip["revenue"], 20000)
        self.assertEqual(trip["fuel_cost"], 700)
        remainder = 20000 - trip["labor_cost"] - 700
        self.assertAlmostEqual(trip["investor_payout"], remainder / 2)
        self.assertEqual(len(entries), 1)
        self.assertIn("Доход рейса", self.notice())

    def test_swapping_the_captain_moves_the_pay_to_the_new_one(self):
        # the very situation of card 1936: the card says one captain, the
        # trip was paying someone else
        item_id = self.create_item()
        _trip, entries = self.trip_of(item_id)
        self.edit(
            item_id, **{"employee_id[]": [str(self.platon_id)], "role[]": ["captain"]}
        )
        trip, entries = self.trip_of(item_id)
        self.assertEqual([e["employee"] for e in entries], ["Платон Жмаев"])
        self.assertEqual((entries[0]["rate"], entries[0]["quantity"]), (1000, 2.5))
        self.assertEqual(entries[0]["amount"], 2500)
        self.assertEqual(trip["labor_cost"], 2500)
        with application_module.app.app_context():
            gone = application_module.get_db().execute(
                "SELECT COUNT(*) FROM entries WHERE employee = 'Даниил Галецкий' "
                "AND work_type = 'Большой тур'"
            ).fetchone()[0]
        self.assertEqual(gone, 0)
        message = self.notice()
        self.assertIn("Даниил Галецкий убран из экипажа", message)
        self.assertIn("Платон Жмаев добавлен в экипаж", message)

    def test_adding_a_second_crew_member_adds_an_entry_and_keeps_the_first(self):
        item_id = self.create_item()
        _trip, before = self.trip_of(item_id)
        self.edit(
            item_id,
            **{"employee_id[]": [str(self.daniil_id), str(self.platon_id)],
               "role[]": ["guide_captain", "captain"]},
        )
        trip, entries = self.trip_of(item_id)
        self.assertEqual(sorted(e["employee"] for e in entries), ["Даниил Галецкий", "Платон Жмаев"])
        kept = [e for e in entries if e["employee"] == "Даниил Галецкий"][0]
        self.assertEqual(kept["id"], before[0]["id"])
        self.assertEqual(trip["labor_cost"], 2.5 * 1500 + 2.5 * 1000)

    def test_a_longer_trip_recalculates_hours_but_not_hand_adjusted_ones(self):
        item_id = self.create_item(
            **{"employee_id[]": [str(self.daniil_id), str(self.platon_id)],
               "role[]": ["guide_captain", "captain"]},
        )
        _trip, entries = self.trip_of(item_id)
        platon = [e for e in entries if e["employee"] == "Платон Жмаев"][0]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE entries SET quantity = 4, amount = 4000 WHERE id = ?", (platon["id"],)
            )
            db.commit()
        self.edit(
            item_id, end_time="16:30",
            **{"employee_id[]": [str(self.daniil_id), str(self.platon_id)],
               "role[]": ["guide_captain", "captain"]},
        )
        _trip, entries = self.trip_of(item_id)
        by_name = {e["employee"]: e for e in entries}
        self.assertEqual(by_name["Даниил Галецкий"]["quantity"], 3.5)
        self.assertEqual(by_name["Даниил Галецкий"]["amount"], 3.5 * 1500)
        self.assertEqual(by_name["Платон Жмаев"]["quantity"], 4)  # edited by hand — untouched

    def test_a_hand_edited_rate_survives_other_changes(self):
        item_id = self.create_item()
        _trip, entries = self.trip_of(item_id)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE entries SET rate = 2000, amount = 5000 WHERE id = ?", (entries[0]["id"],)
            )
            db.commit()
        self.edit(item_id, revenue="19000")
        _trip, entries = self.trip_of(item_id)
        self.assertEqual((entries[0]["rate"], entries[0]["amount"]), (2000, 5000))

    def test_a_new_date_moves_the_trip_and_its_payroll_entries(self):
        item_id = self.create_item()
        self.edit(item_id, trip_date="2026-09-04")
        trip, entries = self.trip_of(item_id)
        self.assertEqual(trip["trip_date"], "2026-09-04")
        self.assertEqual({e["work_date"] for e in entries}, {"2026-09-04"})

    def test_a_new_boat_and_service_reach_the_trip(self):
        item_id = self.create_item()
        self.edit(item_id, boat="Бодрый Первый", service_name="Малый тур")
        trip, entries = self.trip_of(item_id)
        self.assertEqual(trip["boat"], "Бодрый Первый")
        self.assertEqual(entries[0]["work_type"], "Малый тур")
        self.assertEqual(trip["work_type"], "Малый тур")

    def test_an_edit_that_changes_nothing_financial_leaves_accounting_alone(self):
        item_id = self.create_item()
        _trip, before = self.trip_of(item_id)
        self.edit(item_id, note="Просто заметка")
        _trip, after = self.trip_of(item_id)
        self.assertEqual([e["id"] for e in before], [e["id"] for e in after])
        self.assertNotIn("Учёт рейса обновлён", self.notice())

    def test_an_open_card_is_not_touched(self):
        item_id = self.create_item(close=False)
        self.edit(item_id, revenue="21000")
        trip, _entries = self.trip_of(item_id)
        self.assertIsNone(trip)
        with application_module.app.app_context():
            count = application_module.get_db().execute("SELECT COUNT(*) FROM trips").fetchone()[0]
        self.assertEqual(count, 0)

    def test_moving_the_card_by_drag_updates_the_payroll_date_too(self):
        item_id = self.create_item()
        response = self.client.post(
            f"/schedule/items/{item_id}/move",
            json={"start_time": "09:00", "source_employee_id": self.daniil_id,
                  "target_employee_id": self.daniil_id},
        )
        self.assertEqual(response.status_code, 200)
        trip, entries = self.trip_of(item_id)
        self.assertEqual(trip["trip_time"], "09:00")

    def test_a_payroll_week_already_paid_is_flagged(self):
        item_id = self.create_item()
        with application_module.app.app_context():
            db = application_module.get_db()
            columns = [r["name"] for r in db.execute("PRAGMA table_info(payments)").fetchall()]
            values = {"employee": "Даниил Галецкий", "period_key": "2026-08-31", "amount": 100,
                      "paid_at": "2026-09-08", "created_at": "2026-09-08 10:00"}
            used = [c for c in columns if c in values]
            db.execute(
                f"INSERT INTO payments ({','.join(used)}) VALUES ({','.join('?' for _ in used)})",
                [values[c] for c in used],
            )
            db.commit()
        self.edit(
            item_id, **{"employee_id[]": [str(self.platon_id)], "role[]": ["captain"]}
        )
        self.assertIn("уже отмечена оплаченной", self.notice())

    def test_a_missing_role_rate_flags_the_trip_for_review(self):
        item_id = self.create_item()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE excursion_role_rates SET rate = 0 WHERE role = 'captain'")
            db.commit()
        self.edit(
            item_id, **{"employee_id[]": [str(self.platon_id)], "role[]": ["captain"]}
        )
        trip, entries = self.trip_of(item_id)
        self.assertEqual(trip["needs_review"], 1)
        self.assertIn("Ставка роли не задана", self.notice())

    def test_removing_the_boat_reopens_the_card_for_payroll_only_closing(self):
        item_id = self.create_item()
        trip, _entries = self.trip_of(item_id)
        with application_module.app.app_context():
            db = application_module.get_db()
            before = schedule_repository.accounting_snapshot(db, item_id)
            db.execute("UPDATE schedule_items SET boat = '' WHERE id = ?", (item_id,))
            db.commit()
            messages = application_module._sync_closed_schedule_item(
                db, item_id, before, schedule_repository.accounting_snapshot(db, item_id)
            )
            item = schedule_repository.get_item(db, item_id)
            trip_left = db.execute("SELECT COUNT(*) FROM trips WHERE id = ?", (trip["id"],)).fetchone()[0]
            entries_left = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            self.assertIsNone(item["accounting_trip_id"])
            self.assertEqual((trip_left, entries_left), (0, 0))
            self.assertIn("пересоздаётся автоматически", " ".join(messages))
            # ...and the next auto-close run pays the crew as a boat-less item
            schedule_services.auto_close_schedule_items(
                db, application_module._create_trip_from_schedule_payload,
                payroll_rates_repository.get_excursion_role_rate,
                now=dt.datetime(2026, 9, 6, 12, 0),
            )
            paid = db.execute(
                "SELECT employee, amount FROM entries WHERE schedule_item_id = ?", (item_id,)
            ).fetchall()
            self.assertEqual([(r["employee"], r["amount"]) for r in paid], [("Даниил Галецкий", 3750)])

    def test_giving_a_payroll_only_card_a_boat_reopens_it_as_a_trip(self):
        item_id = self.create_item(close=False)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE schedule_items SET boat = '' WHERE id = ?", (item_id,))
            db.commit()
            schedule_services.auto_close_schedule_items(
                db, application_module._create_trip_from_schedule_payload,
                payroll_rates_repository.get_excursion_role_rate,
                now=dt.datetime(2026, 9, 6, 12, 0),
            )
            self.assertIsNotNone(schedule_repository.get_item(db, item_id)["payroll_closed_at"])
            before = schedule_repository.accounting_snapshot(db, item_id)
            db.execute("UPDATE schedule_items SET boat = 'Бодрый Второй' WHERE id = ?", (item_id,))
            db.commit()
            messages = application_module._sync_closed_schedule_item(
                db, item_id, before, schedule_repository.accounting_snapshot(db, item_id)
            )
            item = schedule_repository.get_item(db, item_id)
            self.assertIsNone(item["payroll_closed_at"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
            self.assertIn("пересоздаётся автоматически", " ".join(messages))

    def test_a_payroll_only_card_follows_crew_and_hours_changes(self):
        item_id = self.create_item(close=False)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE schedule_items SET boat = '' WHERE id = ?", (item_id,))
            db.commit()
            schedule_services.auto_close_schedule_items(
                db, application_module._create_trip_from_schedule_payload,
                payroll_rates_repository.get_excursion_role_rate,
                now=dt.datetime(2026, 9, 6, 12, 0),
            )
            before = schedule_repository.accounting_snapshot(db, item_id)
            db.execute("DELETE FROM schedule_assignments WHERE schedule_item_id = ?", (item_id,))
            db.execute(
                "INSERT INTO schedule_assignments (schedule_item_id, employee_id, employee_name, "
                "role, created_at) VALUES (?, ?, 'Платон Жмаев', 'captain', 'x')",
                (item_id, self.platon_id),
            )
            db.execute(
                "UPDATE schedule_items SET ends_at = ? WHERE id = ?",
                (f"{self.DAY} 16:30", item_id),
            )
            db.commit()
            application_module._sync_closed_schedule_item(
                db, item_id, before, schedule_repository.accounting_snapshot(db, item_id)
            )
            paid = db.execute(
                "SELECT employee, quantity, rate, amount FROM entries WHERE schedule_item_id = ?",
                (item_id,),
            ).fetchall()
            self.assertEqual(
                [(r["employee"], r["quantity"], r["rate"], r["amount"]) for r in paid],
                [("Платон Жмаев", 3.5, 1000, 3500)],
            )

    def test_emptying_the_crew_keeps_the_trip_and_its_costs(self):
        item_id = self.create_item()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE trips SET fuel_cost = 700")
            before = schedule_repository.accounting_snapshot(db, item_id)
            db.execute("DELETE FROM schedule_assignments WHERE schedule_item_id = ?", (item_id,))
            db.commit()
            messages = application_module._sync_closed_schedule_item(
                db, item_id, before, schedule_repository.accounting_snapshot(db, item_id)
            )
            trip = db.execute("SELECT * FROM trips").fetchone()
            self.assertEqual((trip["labor_cost"], trip["fuel_cost"]), (0, 700))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)
            self.assertIn("не осталось экипажа", " ".join(messages))

if __name__ == "__main__":
    unittest.main()
