import os
import sqlite3
import tempfile
import unittest

from werkzeug.security import check_password_hash

from support import application_module
from modules.employees import repository, services
from modules.employees.constants import EMPLOYEES


def fresh_database():
    """A brand-new, fully migrated database in its own file."""
    directory = tempfile.TemporaryDirectory()
    path = os.path.join(directory.name, "employees.db")
    application_module.init_db(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return directory, connection


class EmployeeDataEditingDatabaseTests(unittest.TestCase):
    """Rules of editing ФИО / логин / пароль, on an isolated database."""

    def setUp(self):
        self.directory, self.db = fresh_database()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.db.close)
        success, _message, credentials = services.create_employee(
            self.db, "Тест Переименов", ["Капитан"], "", ""
        )
        self.assertTrue(success)
        self.employee_id = credentials["employee_id"]
        self.old_login = credentials["username"]

    def employee(self, employee_id=None):
        return self.db.execute(
            "SELECT * FROM employees WHERE id = ?", (employee_id or self.employee_id,)
        ).fetchone()

    def account(self):
        return repository.get_team_account(self.db, self.employee_id)

    def test_renaming_rewrites_the_name_everywhere_it_is_stored(self):
        db = self.db
        old = "Тест Переименов"
        db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, 'Рейс', 100, 1, 100, '2026-09-01', 'x')", (old,))
        db.execute("INSERT INTO payments (employee, period_key, paid_at) VALUES (?, '2026-08-31', 'x')", (old,))
        db.execute(
            "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, phone, "
            "subtotal, total, status, order_date, created_at, updated_at, source) VALUES "
            "('К','boat','Л','direct','',0,0,'in_progress','2026-09-01','x','x','manual')")
        order_id = db.execute("SELECT MAX(id) FROM tuning_orders").fetchone()[0]
        db.execute(
            "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
            "price_pending, status) VALUES (?, 'Работа', 1, 1, 1, 0, 'pending')", (order_id,))
        item_id = db.execute("SELECT MAX(id) FROM tuning_order_items").fetchone()[0]
        db.execute(
            "INSERT INTO tuning_item_assignments (item_id, employee_name, rate, norm_hours, "
            "assignment_status, assigned_at) VALUES (?, ?, 1, 1, 'pending', 'x')", (item_id, old))
        assignment_id = db.execute("SELECT MAX(id) FROM tuning_item_assignments").fetchone()[0]
        db.execute(
            "INSERT INTO tuning_schedule_tasks (assignment_id, employee_name, title, rate, created_at) "
            "VALUES (?, ?, 'Работа', 1, 'x')", (assignment_id, old))
        db.execute(
            "INSERT INTO schedule_assignments (schedule_item_id, employee_id, employee_name, role, created_at) "
            "VALUES (1, ?, ?, 'captain', 'x')", (self.employee_id, old))
        db.execute(
            "INSERT INTO admin_accounts (admin_name, username, password_hash, employee_id, created_at) "
            "VALUES (?, '__employee_admin_x', 'h', ?, 'x')", (old, self.employee_id))
        db.commit()

        success, message = services.update_employee(db, self.employee_id, "Новое Имя-Фамилия", "", "")
        self.assertTrue(success, message)
        new = "Новое Имя-Фамилия"
        self.assertEqual(self.employee()["name"], new)
        for table, column in (
            ("entries", "employee"), ("payments", "employee"),
            ("tuning_item_assignments", "employee_name"), ("tuning_schedule_tasks", "employee_name"),
            ("schedule_assignments", "employee_name"), ("team_accounts", "employee_name"),
            ("admin_accounts", "admin_name"),
        ):
            names = {r[0] for r in db.execute(
                f"SELECT {column} FROM {table} WHERE {column} IN (?, ?)", (old, new)).fetchall()}
            self.assertEqual(names, {new}, f"{table}.{column}")

    def test_a_name_must_be_valid_and_unique(self):
        other = services.create_employee(self.db, "Другой Человек", ["Гид"], "", "")
        self.assertTrue(other[0])
        for bad in ("Один", "", "Иван 123 Петров"):
            self.assertFalse(services.update_employee(self.db, self.employee_id, bad, "", "")[0], bad)
        taken = services.update_employee(self.db, self.employee_id, "другой человек", "", "")
        self.assertFalse(taken[0])
        self.assertIn("уже есть", taken[1])
        self.assertEqual(self.employee()["name"], "Тест Переименов")

    def test_changing_only_the_case_or_spacing_of_ones_own_name_is_allowed(self):
        success, _message = services.update_employee(self.db, self.employee_id, "тест  переименов", "", "")
        self.assertTrue(success)
        self.assertEqual(self.employee()["name"], "тест переименов")

    def test_login_can_be_changed_and_must_be_valid_and_unique(self):
        ok, message = services.update_employee(self.db, self.employee_id, "Тест Переименов", "new.login", "")
        self.assertTrue(ok, message)
        self.assertEqual(self.account()["username"], "new.login")
        for bad in ("ab", "с пробелом", "кириллица", "x" * 51, "a/b"):
            self.assertFalse(
                services.update_employee(self.db, self.employee_id, "Тест Переименов", bad, "")[0], bad)
        other = services.create_employee(self.db, "Второй Сотрудник", ["Гид"], "", "")[2]
        taken = services.update_employee(self.db, other["employee_id"], "Второй Сотрудник", "NEW.login", "")
        self.assertFalse(taken[0])
        self.assertIn("занят", taken[1])
        # the main administrator's login is taken as well
        admin_login = repository.list_legacy_admins(self.db)[0]["username"]
        self.assertFalse(
            services.update_employee(self.db, other["employee_id"], "Второй Сотрудник", admin_login, "")[0])

    def test_password_is_set_hashed_and_blank_keeps_the_current_one(self):
        before = self.account()["password_hash"]
        self.assertTrue(services.update_employee(self.db, self.employee_id, "Тест Переименов", "", "")[0])
        self.assertEqual(self.account()["password_hash"], before)
        self.assertFalse(services.update_employee(self.db, self.employee_id, "Тест Переименов", "", "short")[0])
        self.assertEqual(self.account()["password_hash"], before)
        self.assertTrue(services.update_employee(
            self.db, self.employee_id, "Тест Переименов", "", "Хорош0-пароль")[0])  # any characters allowed
        ok, message = services.update_employee(self.db, self.employee_id, "Тест Переименов", "", "new-secret-1")
        self.assertTrue(ok, message)
        stored = self.account()["password_hash"]
        self.assertNotIn("new-secret-1", stored)
        self.assertTrue(check_password_hash(stored, "new-secret-1"))

    def test_an_employee_without_a_cabinet_gets_one_only_with_login_and_password(self):
        self.db.execute("DELETE FROM team_accounts WHERE employee_id = ?", (self.employee_id,))
        self.db.commit()
        self.assertIsNone(self.account())
        self.assertFalse(services.update_employee(self.db, self.employee_id, "Тест Переименов", "only.login", "")[0])
        self.assertFalse(services.update_employee(self.db, self.employee_id, "Тест Переименов", "", "only-password-1")[0])
        self.assertIsNone(self.account())
        ok, message = services.update_employee(
            self.db, self.employee_id, "Тест Переименов", "fresh.login", "fresh-pass-1")
        self.assertTrue(ok, message)
        self.assertEqual(self.account()["username"], "fresh.login")
        self.assertTrue(check_password_hash(self.account()["password_hash"], "fresh-pass-1"))

    def test_a_password_change_reaches_a_bridged_administrator_account(self):
        self.db.execute(
            "INSERT INTO admin_accounts (admin_name, username, password_hash, employee_id, created_at) "
            "VALUES ('Тест Переименов', '__employee_admin_y', 'old-hash', ?, 'x')", (self.employee_id,))
        self.db.commit()
        services.update_employee(self.db, self.employee_id, "Тест Переименов", "", "brand-new-pass-1")
        row = self.db.execute(
            "SELECT password_hash FROM admin_accounts WHERE employee_id = ?", (self.employee_id,)).fetchone()
        self.assertTrue(check_password_hash(row["password_hash"], "brand-new-pass-1"))

    def test_nothing_changed_says_so(self):
        ok, message = services.update_employee(self.db, self.employee_id, "Тест Переименов", self.old_login, "")
        self.assertTrue(ok)
        self.assertEqual(message, "Изменений нет.")

    # --- the main administrator ------------------------------------------

    def test_the_main_administrator_is_listed_and_editable(self):
        admins = services.legacy_admins(self.db)
        self.assertEqual(len(admins), 1)
        admin = admins[0]
        ok, message, name = services.update_legacy_admin(
            self.db, admin["id"], "Главный  Босс", "boss", "boss-pass-123")
        self.assertTrue(ok, message)
        self.assertEqual(name, "Главный Босс")
        row = repository.get_legacy_admin(self.db, admin["id"])
        self.assertEqual((row["admin_name"], row["username"]), ("Главный Босс", "boss"))
        stored = self.db.execute("SELECT password_hash FROM admin_accounts WHERE id = ?", (admin["id"],)).fetchone()[0]
        self.assertTrue(check_password_hash(stored, "boss-pass-123"))

    def test_the_administrator_edit_is_validated(self):
        admin_id = services.legacy_admins(self.db)[0]["id"]
        taken = services.update_legacy_admin(self.db, admin_id, "Администратор", self.old_login, "")
        self.assertFalse(taken[0])
        self.assertFalse(services.update_legacy_admin(self.db, admin_id, "", "admin", "")[0])
        self.assertFalse(services.update_legacy_admin(self.db, admin_id, "Админ", "admin", "short")[0])
        self.assertFalse(services.update_legacy_admin(self.db, admin_id, "Админ", "с пробелом", "")[0])
        self.assertEqual(services.update_legacy_admin(self.db, admin_id, "Администратор", "admin", "")[1:2],
                         ("Изменений нет.",))

    def test_employee_bridged_admin_accounts_are_not_listed_as_main_administrators(self):
        self.db.execute(
            "INSERT INTO admin_accounts (admin_name, username, password_hash, employee_id, created_at) "
            "VALUES ('Мост', '__employee_admin_z', 'h', ?, 'x')", (self.employee_id,))
        self.db.commit()
        self.assertEqual([a["admin_name"] for a in services.legacy_admins(self.db)], ["Администратор"])

    # --- start-up must not undo the edits --------------------------------

    def test_a_renamed_seeded_employee_is_not_recreated_on_the_next_start(self):
        seeded = EMPLOYEES[0]
        row = self.db.execute("SELECT id FROM employees WHERE name = ?", (seeded,)).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(services.update_employee(self.db, row["id"], "Совсем Другое", "", "")[0])
        self.db.close()
        application_module.init_db(os.path.join(self.directory.name, "employees.db"))
        self.db = sqlite3.connect(os.path.join(self.directory.name, "employees.db"))
        self.db.row_factory = sqlite3.Row
        self.assertIsNone(self.db.execute("SELECT 1 FROM employees WHERE name = ?", (seeded,)).fetchone())
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM employees WHERE name = 'Совсем Другое'").fetchone()[0], 1)

    def test_a_changed_administrator_login_does_not_bring_the_original_back(self):
        admin_id = services.legacy_admins(self.db)[0]["id"]
        self.assertTrue(services.update_legacy_admin(self.db, admin_id, "Администратор", "boss", "")[0])
        self.db.close()
        path = os.path.join(self.directory.name, "employees.db")
        application_module.init_db(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        usernames = [r[0] for r in self.db.execute(
            "SELECT username FROM admin_accounts WHERE employee_id IS NULL").fetchall()]
        self.assertEqual(usernames, ["boss"])

    def test_a_changed_employee_login_does_not_bring_the_original_back(self):
        constants = application_module.TEAM_ACCOUNTS
        employee_name, original_login, _hash = next(
            entry for entry in constants
            if self.db.execute("SELECT 1 FROM team_accounts WHERE username = ?", (entry[1],)).fetchone())
        employee_id = self.db.execute("SELECT id FROM employees WHERE name = ?", (employee_name,)).fetchone()[0]
        self.assertTrue(services.update_employee(self.db, employee_id, employee_name, "changed.login", "")[0])
        self.db.close()
        path = os.path.join(self.directory.name, "employees.db")
        application_module.init_db(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        usernames = [r[0] for r in self.db.execute(
            "SELECT username FROM team_accounts WHERE employee_id = ?", (employee_id,)).fetchall()]
        self.assertEqual(usernames, ["changed.login"])
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM team_accounts WHERE username = ?", (original_login,)).fetchone())


class EmployeeDataEditingHttpTests(unittest.TestCase):
    """The same through the pages: forms, login with the new data, sessions."""

    NAME = "Сайтовый Тестер"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            success, _m, credentials = services.create_employee(db, self.NAME, ["Тюнингмэн"], "", "")
            self.assertTrue(success)
            self.employee_id = credentials["employee_id"]
            self.login = credentials["username"]
        self.addCleanup(self._cleanup_now)
        self.login_admin()

    def _cleanup_now(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        for name in (self.NAME, "Сайтовый Переименованный"):
            row = db.execute("SELECT id FROM employees WHERE name = ?", (name,)).fetchone()
            if row:
                db.execute("DELETE FROM team_accounts WHERE employee_id = ?", (row["id"],))
                db.execute("DELETE FROM employee_positions WHERE employee_id = ?", (row["id"],))
                db.execute("DELETE FROM employees WHERE id = ?", (row["id"],))
        db.commit()

    def login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def update(self, **fields):
        data = {"name": self.NAME, "username": "", "password": ""}
        data.update(fields)
        return self.client.post(f"/employees/{self.employee_id}/update", data=data)

    def team_login(self, username, password):
        client = application_module.app.test_client()
        return client, client.post("/team/login", data={"username": username, "password": password})

    def test_the_page_lists_the_main_administrator_and_an_edit_form_per_employee(self):
        page = self.client.get("/employees").get_data(as_text=True)
        self.assertIn("Основной администратор", page)
        self.assertIn("/employees/admins/1/update", page)
        self.assertIn("Это вы", page)
        self.assertIn(f"/employees/{self.employee_id}/update", page)
        self.assertIn("Изменить ФИО, логин и пароль", page)

    def test_a_new_login_and_password_work_and_the_old_ones_stop(self):
        response = self.update(username="sitetester", password="Site-pass-123")
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/employees").get_data(as_text=True)
        self.assertIn("Данные сотрудника", page)
        _client, ok = self.team_login("sitetester", "Site-pass-123")
        self.assertEqual(ok.status_code, 302)
        _client, old = self.team_login(self.login, "anything")
        self.assertEqual(old.status_code, 401)

    def test_an_error_is_shown_and_nothing_changes(self):
        self.update(username="x")
        page = self.client.get("/employees").get_data(as_text=True)
        self.assertIn("Логин должен быть", page)
        _client, still = self.team_login(self.login, "wrong")
        self.assertEqual(still.status_code, 401)

    def test_a_logged_in_employee_keeps_their_data_after_being_renamed(self):
        self.update(username="sitetester", password="Site-pass-123")
        client, _ok = self.team_login("sitetester", "Site-pass-123")
        with client.session_transaction() as session:
            self.assertEqual(session["team_employee_name"], self.NAME)
        self.update(name="Сайтовый Переименованный")
        self.assertEqual(client.get("/team/").status_code, 200)
        with client.session_transaction() as session:
            self.assertEqual(session["team_employee_name"], "Сайтовый Переименованный")

    def test_editing_the_main_administrator_through_the_page(self):
        with application_module.app.app_context():
            original = repository.get_legacy_admin(application_module.get_db(), 1)
            original_name, original_login = original["admin_name"], original["username"]
            original_hash = application_module.get_db().execute(
                "SELECT password_hash FROM admin_accounts WHERE id = 1").fetchone()[0]
        self.addCleanup(self._restore_admin, original_name, original_login, original_hash)
        response = self.client.post("/employees/admins/1/update", data={
            "name": "Босс Системы", "username": "boss.sys", "password": "Boss-pass-123"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertEqual(session["admin_name"], "Босс Системы")  # the current admin's own name follows
        fresh = application_module.app.test_client()
        ok = fresh.post("/admin/login", data={"username": "boss.sys", "password": "Boss-pass-123"})
        self.assertEqual(ok.status_code, 302)
        old = application_module.app.test_client().post(
            "/admin/login", data={"username": original_login, "password": "whatever"})
        self.assertEqual(old.status_code, 401)

    def _restore_admin(self, name, login, password_hash):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE admin_accounts SET admin_name = ?, username = ?, password_hash = ? WHERE id = 1",
                (name, login, password_hash))
            db.commit()

    def test_the_edit_routes_require_an_administrator(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.post(f"/employees/{self.employee_id}/update", data={}).status_code, 302)
        self.assertEqual(anonymous.post("/employees/admins/1/update", data={}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
