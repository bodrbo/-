import unittest

from support import application_module
from modules.employees import repository, services
from test_employee_data_editing import fresh_database


NO_ADDERS = {"tuning": lambda *a: ("added", ""), "excursion": lambda *a: ("added", "")}


class MainAdministratorWorkdaysServiceTests(unittest.TestCase):
    """The main administrator (admin_accounts without an employee) gets a
    schedule record on first use and is then scheduled like anyone else."""

    def setUp(self):
        self.directory, self.db = fresh_database()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.db.close)
        self.admin = services.legacy_admins(self.db)[0]

    def admin_row(self):
        return repository.get_legacy_admin(self.db, self.admin["id"])

    def test_the_first_use_creates_an_administrator_employee_without_a_cabinet(self):
        self.assertIsNone(self.admin_row()["schedule_employee_id"])
        employee_id, error = services.ensure_admin_employee(self.db, self.admin["id"])
        self.assertIsNone(error)
        employee = self.db.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()
        self.assertEqual(employee["name"], self.admin["admin_name"])
        positions = [r["position"] for r in self.db.execute(
            "SELECT position FROM employee_positions WHERE employee_id = ?", (employee_id,)).fetchall()]
        self.assertEqual(positions, ["Администратор"])
        self.assertIsNone(repository.get_team_account(self.db, employee_id))
        self.assertEqual(self.admin_row()["schedule_employee_id"], employee_id)

    def test_login_of_the_administrator_is_not_touched(self):
        services.ensure_admin_employee(self.db, self.admin["id"])
        row = self.db.execute(
            "SELECT username, employee_id FROM admin_accounts WHERE id = ?", (self.admin["id"],)).fetchone()
        self.assertEqual((row["username"], row["employee_id"]), (self.admin["username"], None))
        self.assertEqual(len(repository.list_legacy_admins(self.db)), 1)  # still the legacy login

    def test_it_is_created_once(self):
        first, _ = services.ensure_admin_employee(self.db, self.admin["id"])
        second, _ = services.ensure_admin_employee(self.db, self.admin["id"])
        self.assertEqual(first, second)
        count = self.db.execute(
            "SELECT COUNT(*) FROM employees WHERE name = ?", (self.admin["admin_name"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_he_is_not_listed_as_an_ordinary_employee(self):
        employee_id, _ = services.ensure_admin_employee(self.db, self.admin["id"])
        listed = [e["id"] for e in services.employee_directory(self.db)]
        self.assertNotIn(employee_id, listed)

    def test_workdays_go_through_the_normal_path(self):
        calls = []
        adders = {"tuning": lambda db, employee_id, day, start, end: (calls.append((employee_id, day.isoformat(), start, end)) or ("added", ""))}
        ok, message = services.add_admin_workdays(
            self.db, self.admin["id"], "tuning", "2031-03-03,2031-03-04", "10:00", "19:00", adders)
        self.assertTrue(ok, message)
        employee_id = self.admin_row()["schedule_employee_id"]
        self.assertEqual(calls, [(employee_id, "2031-03-03", "10:00", "19:00"), (employee_id, "2031-03-04", "10:00", "19:00")])
        self.assertIn("добавлено дней: 2", message)

    def test_bad_input_still_stops_before_anything_is_created(self):
        for bad in (dict(raw_dates=""), dict(raw_start="19:00", raw_end="10:00")):
            args = dict(schedule="tuning", raw_dates="2031-03-03", raw_start="09:00", raw_end="18:00")
            args.update(bad)
            ok, _message = services.add_admin_workdays(self.db, self.admin["id"], adders=NO_ADDERS, **args)
            self.assertFalse(ok)
        ok, _message = services.add_admin_workdays(
            self.db, self.admin["id"], "bogus", "2031-03-03", "09:00", "18:00", NO_ADDERS)
        self.assertFalse(ok)
        count = self.db.execute(
            "SELECT COUNT(*) FROM employees WHERE name = ?", (self.admin["admin_name"],)).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertIsNone(self.admin_row()["schedule_employee_id"])

    def test_a_name_already_taken_by_an_employee_is_reported(self):
        self.db.execute(
            "INSERT INTO employees (name, created_at) VALUES (?, 'x')", (self.admin["admin_name"],))
        self.db.commit()
        employee_id, error = services.ensure_admin_employee(self.db, self.admin["id"])
        self.assertIsNone(employee_id)
        self.assertIn("уже есть", error)
        self.assertIsNone(self.admin_row()["schedule_employee_id"])

    def test_renaming_the_administrator_renames_his_schedule_record_and_history(self):
        employee_id, _ = services.ensure_admin_employee(self.db, self.admin["id"])
        old_name = self.admin["admin_name"]
        self.db.execute(
            "INSERT INTO tuning_schedule_day_crew (work_date, employee_id, created_at) VALUES ('2031-03-03', ?, 'x')",
            (employee_id,))
        self.db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, 'Задача', 0, 1, 0, '2031-03-03', 'x')", (old_name,))
        self.db.commit()
        ok, message, _name = services.update_legacy_admin(self.db, self.admin["id"], "Главный Босс", "", "")
        self.assertTrue(ok, message)
        self.assertEqual(
            self.db.execute("SELECT name FROM employees WHERE id = ?", (employee_id,)).fetchone()[0], "Главный Босс")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM entries WHERE employee = 'Главный Босс'").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM tuning_schedule_day_crew WHERE employee_id = ?", (employee_id,)).fetchone()[0], 1)

    def test_renaming_to_a_name_of_another_employee_is_refused(self):
        services.ensure_admin_employee(self.db, self.admin["id"])
        self.db.execute("INSERT INTO employees (name, created_at) VALUES ('Занятое Имя', 'x')")
        self.db.commit()
        ok, message, _ = services.update_legacy_admin(self.db, self.admin["id"], "Занятое Имя", "", "")
        self.assertFalse(ok)
        self.assertIn("уже есть", message)
        self.assertEqual(self.admin_row()["admin_name"], self.admin["admin_name"])

    def test_an_employee_bridged_admin_account_is_not_a_main_administrator(self):
        employee_id = services.create_employee(self.db, "Мост Сотрудник", ["Администратор"], "", "")[2]["employee_id"]
        bridged = self.db.execute(
            "INSERT INTO admin_accounts (admin_name, username, password_hash, employee_id, created_at) "
            "VALUES ('Мост', '__employee_admin_q', 'h', ?, 'x')", (employee_id,)).lastrowid
        self.db.commit()
        result, error = services.ensure_admin_employee(self.db, bridged)
        self.assertIsNone(result)
        self.assertIn("не найдена", error)


class MainAdministratorWorkdaysHttpTests(unittest.TestCase):
    DAYS = "2031-04-07,2031-04-08"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self.addCleanup(self._cleanup)
        self._cleanup()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def _cleanup(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute("SELECT schedule_employee_id FROM admin_accounts WHERE id = 1").fetchone()
            if row and row["schedule_employee_id"]:
                employee_id = row["schedule_employee_id"]
                for table in ("tuning_schedule_day_crew", "schedule_day_crew", "employee_positions"):
                    db.execute(f"DELETE FROM {table} WHERE employee_id = ?", (employee_id,))
                db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
                db.execute("UPDATE admin_accounts SET schedule_employee_id = NULL WHERE id = 1")
                db.commit()

    def post(self, schedule="tuning", dates=None, start="10:00", end="19:00", admin_id=1):
        return self.client.post(f"/employees/admins/{admin_id}/workdays", data={
            "schedule": schedule, "dates": dates or self.DAYS, "shift_start": start, "shift_end": end})

    def employee(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute(
                "SELECT e.id, e.name FROM admin_accounts a JOIN employees e ON e.id = a.schedule_employee_id "
                "WHERE a.id = 1").fetchone()
            return dict(row) if row else None

    def crew(self, table):
        with application_module.app.app_context():
            employee = self.employee()
            if not employee:
                return []
            return [dict(r) for r in application_module.get_db().execute(
                f"SELECT work_date, shift_start, shift_end FROM {table} WHERE employee_id = ? ORDER BY work_date",
                (employee["id"],)).fetchall()]

    def test_the_administrator_card_has_the_workdays_button(self):
        page = self.client.get("/employees").get_data(as_text=True)
        card = page.split('id="admin-1"')[1].split("</article>")[0]
        self.assertIn("/employees/admins/1/workdays", card)
        self.assertIn("Рабочие дни</button>", card)
        self.assertIn('data-tuning="1"', card)
        self.assertIn('data-excursion="1"', card)

    def test_days_go_into_the_tuning_schedule(self):
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(
            self.crew("tuning_schedule_day_crew"),
            [{"work_date": d, "shift_start": "10:00", "shift_end": "19:00"} for d in ("2031-04-07", "2031-04-08")])
        self.assertEqual(self.crew("schedule_day_crew"), [])
        self.assertIn("добавлено дней: 2", self.client.get("/employees").get_data(as_text=True))

    def test_days_go_into_the_excursion_schedule(self):
        self.post(schedule="excursion", start="08:00", end="17:00")
        self.assertEqual(
            [(r["work_date"], r["shift_start"]) for r in self.crew("schedule_day_crew")],
            [("2031-04-07", "08:00"), ("2031-04-08", "08:00")])

    def test_the_second_request_reuses_the_same_record(self):
        self.post()
        first = self.employee()
        self.post(schedule="excursion")
        self.assertEqual(self.employee(), first)

    def test_he_is_in_the_schedules_but_not_in_the_employee_list(self):
        self.post()
        employee = self.employee()
        page = self.client.get("/employees").get_data(as_text=True)
        self.assertNotIn(f'id="employee-{employee["id"]}"', page)
        tuning = self.client.get("/schedule/tuning?date=2031-04-07").get_data(as_text=True)
        self.assertIn(employee["name"], tuning.split('id="tuningRosterModal"')[1])

    def test_the_route_needs_an_administrator_and_a_real_account(self):
        anonymous = application_module.app.test_client()
        response = anonymous.post("/employees/admins/1/workdays", data={
            "schedule": "tuning", "dates": self.DAYS, "shift_start": "09:00", "shift_end": "18:00"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.employee())
        self.post(admin_id=999999)
        self.assertIn("не найдена", self.client.get("/employees").get_data(as_text=True))
        self.assertIsNone(self.employee())


if __name__ == "__main__":
    unittest.main()
