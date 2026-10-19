import datetime as dt
import unittest

from support import application_module
from modules.payroll_rates import repository as rates
from modules.payroll_rates.constants import DEFAULT_EXCURSION_ROLE_RATES, EXCURSION_ROLES
from modules.schedule import repository as schedule_repository
from modules.schedule import services as schedule_services

SECOND = "Бодрый Второй"
FIRST = "Бодрый Первый"
LARUS = "Ларус"
NAME = "Лодочный Гид-Капитан"


class BoatRatesFixture(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        today = dt.date.today()
        self.recent = (today - dt.timedelta(days=2)).isoformat()
        self.old = (today - dt.timedelta(days=20)).isoformat()
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in (
                "trip_expenses", "trip_labor", "trips", "entries", "schedule_day_crew",
                "schedule_participants", "schedule_assignments", "schedule_items",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM payments WHERE employee = ?", (NAME,))
            db.execute("DELETE FROM excursion_boat_rates")
            db.execute("DELETE FROM excursion_boat_rates_applied")
            for role in EXCURSION_ROLES:
                db.execute(
                    "UPDATE excursion_role_rates SET rate = ? WHERE role = ?",
                    (DEFAULT_EXCURSION_ROLE_RATES.get(role, 0), role),
                )
            db.execute("UPDATE excursion_role_rates SET rate = 1500 WHERE role = 'guide_captain'")
            row = db.execute("SELECT id FROM employees WHERE name = ?", (NAME,)).fetchone()
            self.employee_id = row["id"] if row else db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) VALUES (?, '2026-08-01 09:00', NULL)",
                (NAME,),
            ).lastrowid
            db.execute("UPDATE employees SET deleted_at = NULL WHERE id = ?", (self.employee_id,))
            db.execute(
                "INSERT OR IGNORE INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Гид-капитан', '2026-08-01 09:00')", (self.employee_id,),
            )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            for table in (
                "trip_expenses", "trip_labor", "trips", "entries", "schedule_day_crew",
                "schedule_assignments", "schedule_items",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM payments WHERE employee = ?", (NAME,))
            db.execute("DELETE FROM excursion_boat_rates")
            db.execute("DELETE FROM excursion_boat_rates_applied")
            db.commit()

    def make_item(self, day, boat, start="10:00", end="12:00", role="guide_captain", revenue=10000):
        with application_module.app.app_context():
            db = application_module.get_db()
            item_id = db.execute(
                "INSERT INTO schedule_items (kind, boat, service_name, starts_at, ends_at, revenue, "
                "customer_name, customer_phone, status, source, created_at, updated_at) "
                "VALUES ('booking', ?, 'Малый тур', ?, ?, ?, 'Клиент', '+79990000000', 'scheduled', "
                "'internal', '2026-08-01 09:00', '2026-08-01 09:00')",
                (boat, f"{day} {start}", f"{day} {end}", revenue),
            ).lastrowid
            db.execute(
                "INSERT INTO schedule_assignments (schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, ?, ?, '2026-08-01 09:00')", (item_id, self.employee_id, NAME, role),
            )
            db.commit()
        return item_id

    def close_items(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            return schedule_services.auto_close_schedule_items(
                db, application_module._create_trip_from_schedule_payload,
                rates.get_excursion_role_rate, now=dt.datetime.now(),
            )

    def trip_of(self, item_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            item = schedule_repository.get_item(db, item_id)
            trip = dict(db.execute("SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)).fetchone())
            entries = [dict(r) for r in db.execute(
                "SELECT e.* FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id "
                "WHERE tl.trip_id = ? ORDER BY e.id", (trip["id"],)).fetchall()]
            return trip, entries

    def save_boat_rates(self, **by_boat):
        boats = [LARUS, SECOND, FIRST]
        data = {"role": "guide_captain", "boat[]": boats,
                "boat_rate[]": [str(by_boat.get(key, "")) for key in ("larus", "second", "first")]}
        self.client.post("/payroll/rates/excursions/boats", data=data)
        with self.client.session_transaction() as session:
            return (session.get("payroll_rates_notice") or {})


class BoatRateLookupTests(BoatRatesFixture):
    def test_a_boat_with_its_own_rate_overrides_the_role_rate(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            rates.set_boat_rates(db, "guide_captain", {SECOND: 1800, FIRST: 0}, "2026-10-19 10:00")
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", SECOND), 1800)
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", FIRST), 1500)  # 0 -> general
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", LARUS), 1500)  # no row
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain"), 1500)
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", ""), 1500)

    def test_other_roles_ignore_boat_rates(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            rates.set_boat_rates(db, "captain", {SECOND: 9999}, "2026-10-19 10:00")
            self.assertEqual(rates.get_excursion_role_rate(db, "captain", SECOND), 1100)


class BoatRatePageTests(BoatRatesFixture):
    def test_the_rates_page_lists_every_boat_under_guide_captain(self):
        page = self.client.get("/payroll/rates/excursions").get_data(as_text=True)
        self.assertIn("Гид-капитан — ставки по катерам", page)
        self.assertEqual(page.count('name="boat[]"'), 3)
        for boat in (LARUS, SECOND, FIRST):
            self.assertIn(f'value="{boat}"', page)
        self.assertIn("При первом сохранении", page)

    def test_saving_stores_the_rates_and_blank_means_general(self):
        notice = self.save_boat_rates(second="1800,5")
        self.assertEqual(notice["type"], "success")
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", SECOND), 1800.5)
            self.assertEqual(rates.get_excursion_role_rate(db, "guide_captain", FIRST), 1500)
        page = self.client.get("/payroll/rates/excursions").get_data(as_text=True)
        self.assertIn('value="1800.5"', page)
        self.save_boat_rates(second="1800")
        self.assertIn('value="1800"', self.client.get("/payroll/rates/excursions").get_data(as_text=True))
        self.save_boat_rates(second="1800,5")
        self.assertNotIn("При первом сохранении", page)  # the first save is spent
        self.save_boat_rates()  # clearing it again
        with application_module.app.app_context():
            self.assertEqual(
                rates.get_excursion_role_rate(application_module.get_db(), "guide_captain", SECOND), 1500)

    def test_bad_input_changes_nothing(self):
        for bad in ("abc", "-5"):
            notice = self.save_boat_rates(second=bad)
            self.assertEqual(notice["type"], "error", bad)
        response = self.client.post("/payroll/rates/excursions/boats", data={
            "role": "guide_captain", "boat[]": ["Чужой катер"], "boat_rate[]": ["100"]})
        self.assertEqual(response.status_code, 302)
        response = self.client.post("/payroll/rates/excursions/boats", data={
            "role": "captain", "boat[]": [SECOND], "boat_rate[]": ["100"]})
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(rates.list_boat_rates(db, "guide_captain"), {})
            self.assertFalse(rates.boat_rates_applied(db, "guide_captain"))


class BoatRateAccrualTests(BoatRatesFixture):
    def test_auto_close_pays_the_boat_rate_and_other_boats_the_general_one(self):
        with application_module.app.app_context():
            rates.set_boat_rates(application_module.get_db(), "guide_captain", {SECOND: 1800}, "2026-10-19 10:00")
        on_second = self.make_item(self.recent, SECOND, "09:00", "11:00")
        on_first = self.make_item(self.recent, FIRST, "12:00", "14:00")
        self.close_items()
        _, second_entries = self.trip_of(on_second)
        _, first_entries = self.trip_of(on_first)
        self.assertEqual((second_entries[0]["rate"], second_entries[0]["amount"]), (1800, 3600))
        self.assertEqual((first_entries[0]["rate"], first_entries[0]["amount"]), (1500, 3000))

    def test_the_rate_of_a_plain_captain_does_not_depend_on_the_boat(self):
        with application_module.app.app_context():
            rates.set_boat_rates(application_module.get_db(), "guide_captain", {SECOND: 1800}, "2026-10-19 10:00")
        item = self.make_item(self.recent, SECOND, role="captain")
        self.close_items()
        _, entries = self.trip_of(item)
        self.assertEqual(entries[0]["rate"], 1100)


class BoatRateRecalculationTests(BoatRatesFixture):
    def test_the_first_save_reprices_last_weeks_accruals_and_their_trips(self):
        recent = self.make_item(self.recent, SECOND, "09:00", "11:00")       # 2 h on a priced boat
        other_boat = self.make_item(self.recent, FIRST, "12:00", "14:00")    # boat without own rate
        old = self.make_item(self.old, SECOND, "09:00", "11:00")             # older than a week
        self.close_items()
        trip_before, _ = self.trip_of(recent)
        self.assertEqual(trip_before["labor_cost"], 3000)

        notice = self.save_boat_rates(second=1800)
        self.assertEqual(notice["type"], "success")
        self.assertIn("Пересчитаны начисления за последнюю неделю: 1 шт. в 1 рейсах", notice["message"])

        trip, entries = self.trip_of(recent)
        self.assertEqual((entries[0]["rate"], entries[0]["amount"]), (1800, 3600))
        self.assertEqual(trip["labor_cost"], 3600)
        self.assertEqual(trip["entry_id"], entries[0]["id"])
        # the trip's money was recalculated with the new labour cost
        self.assertAlmostEqual(trip["remainder"], trip_before["remainder"] - 600, places=2)
        self.assertAlmostEqual(trip["investor_payout"], trip_before["investor_payout"] - 300, places=2)
        self.assertEqual(trip["revenue"], trip_before["revenue"])
        self.assertEqual(self.trip_of(other_boat)[1][0]["rate"], 1500)
        self.assertEqual(self.trip_of(old)[1][0]["rate"], 1500)

    def test_only_the_first_save_recalculates(self):
        item = self.make_item(self.recent, SECOND, "09:00", "11:00")
        self.close_items()
        self.save_boat_rates(second=1800)
        self.save_boat_rates(second=2000)
        _, entries = self.trip_of(item)
        self.assertEqual(entries[0]["rate"], 1800)   # the second save left the past alone
        later = self.make_item(self.recent, SECOND, "13:00", "15:00")
        self.close_items()
        self.assertEqual(self.trip_of(later)[1][0]["rate"], 2000)  # but new accruals use it

    def test_a_first_save_without_any_boat_rate_is_not_spent(self):
        self.save_boat_rates()
        with application_module.app.app_context():
            self.assertFalse(rates.boat_rates_applied(application_module.get_db(), "guide_captain"))
        item = self.make_item(self.recent, SECOND, "09:00", "11:00")
        self.close_items()
        self.save_boat_rates(second=1800)
        self.assertEqual(self.trip_of(item)[1][0]["rate"], 1800)

    def test_a_hand_edited_rate_is_left_alone(self):
        item = self.make_item(self.recent, SECOND, "09:00", "11:00")
        self.close_items()
        _, entries = self.trip_of(item)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE entries SET rate = 1234, amount = 2468 WHERE id = ?", (entries[0]["id"],))
            db.commit()
        notice = self.save_boat_rates(second=1800)
        self.assertEqual(self.trip_of(item)[1][0]["rate"], 1234)
        self.assertIn("вручную изменённой ставкой не трогали: 1", notice["message"])
        self.assertIn("За последнюю неделю нечего пересчитывать", notice["message"])

    def test_a_trip_with_a_second_crew_member_keeps_their_pay(self):
        item = self.make_item(self.recent, SECOND, "09:00", "11:00")
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute("SELECT id FROM employees WHERE name = 'Лодочный Капитан Помощник'").fetchone()
            helper = row["id"] if row else db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES ('Лодочный Капитан Помощник', '2026-08-01 09:00', NULL)").lastrowid
            db.execute("INSERT OR IGNORE INTO employee_positions (employee_id, position, created_at) "
                       "VALUES (?, 'Капитан', '2026-08-01 09:00')", (helper,))
            db.execute(
                "INSERT INTO schedule_assignments (schedule_item_id, employee_id, employee_name, role, created_at) "
                "VALUES (?, ?, 'Лодочный Капитан Помощник', 'captain', '2026-08-01 09:00')", (item, helper))
            db.commit()
        self.close_items()
        self.save_boat_rates(second=1800)
        trip, entries = self.trip_of(item)
        by_name = {e["employee"]: e["rate"] for e in entries}
        self.assertEqual(by_name, {NAME: 1800, "Лодочный Капитан Помощник": 1100})
        self.assertEqual(trip["labor_cost"], 1800 * 2 + 1100 * 2)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM employee_positions WHERE employee_id IN "
                       "(SELECT id FROM employees WHERE name = 'Лодочный Капитан Помощник')")
            db.execute("DELETE FROM employees WHERE name = 'Лодочный Капитан Помощник'")
            db.commit()

    def test_a_settled_week_is_flagged_in_the_notice(self):
        item = self.make_item(self.recent, SECOND, "09:00", "11:00")
        self.close_items()
        day = dt.date.fromisoformat(self.recent)
        monday = (day - dt.timedelta(days=day.weekday())).isoformat()
        with application_module.app.app_context():
            db = application_module.get_db()
            cols = [r[1] for r in db.execute("PRAGMA table_info(payments)").fetchall()]
            values = {"employee": NAME, "period_key": monday, "amount": 100, "paid_at": "2026-10-19 10:00",
                      "created_at": "2026-10-19 10:00"}
            values = {k: v for k, v in values.items() if k in cols}
            db.execute(
                f"INSERT INTO payments ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                tuple(values.values()))
            db.commit()
        notice = self.save_boat_rates(second=1800)
        self.assertEqual(self.trip_of(item)[1][0]["rate"], 1800)
        self.assertIn("неделя этой выплаты уже отмечена оплаченной", notice["message"])


class BoatChangeOfAClosedCardTests(BoatRatesFixture):
    def test_moving_a_closed_card_to_another_boat_follows_the_boat_rate(self):
        with application_module.app.app_context():
            rates.set_boat_rates(application_module.get_db(), "guide_captain", {SECOND: 1800}, "2026-10-19 10:00")
        item = self.make_item(self.recent, FIRST, "09:00", "11:00")
        self.close_items()
        self.assertEqual(self.trip_of(item)[1][0]["rate"], 1500)
        with application_module.app.app_context():
            db = application_module.get_db()
            before = schedule_repository.accounting_snapshot(db, item)
            db.execute("UPDATE schedule_items SET boat = ? WHERE id = ?", (SECOND, item))
            db.commit()
            messages = application_module._sync_closed_schedule_item(
                db, item, before, schedule_repository.accounting_snapshot(db, item))
        trip, entries = self.trip_of(item)
        self.assertEqual((trip["boat"], entries[0]["rate"], entries[0]["amount"]), (SECOND, 1800, 3600))
        self.assertIn("ставка по катеру", " ".join(messages))


if __name__ == "__main__":
    unittest.main()
