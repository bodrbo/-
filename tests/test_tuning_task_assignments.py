import unittest

from support import application_module


class TuningTaskMultiAssignmentTests(unittest.TestCase):
    CLIENT_TOKEN = "multi-assign-test-client"
    EMPLOYEE_A = "Мастеров Первый"
    EMPLOYEE_B = "Мастеров Второй"
    USERNAME_A = "multi.assign.a"
    USERNAME_B = "multi.assign.b"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            client_cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Клиент Мультизадач', 'Salute 585 HT', "
                "'+79990003344', ?, 'neutral', '2026-09-01 10:00')",
                (self.CLIENT_TOKEN,),
            )
            self.client_id = client_cursor.lastrowid
            order_cursor = db.execute(
                "INSERT INTO tuning_orders "
                "(client_id, client_name, equipment_type, boat_model, "
                "boat_registration_number, motor_model, motor_serial_number, "
                "sale_channel, phone, discount_pct, subtotal, total, status, "
                "order_date, created_at, updated_at, source) "
                "VALUES (?, 'Клиент Мультизадач', 'boat', 'Salute 585 HT', "
                "'', '', '', 'direct', '+79990003344', 0, 2000, 2000, "
                "'in_progress', '2026-09-01', '2026-09-01 10:00', "
                "'2026-09-01 10:00', 'manual')",
                (self.client_id,),
            )
            self.order_id = order_cursor.lastrowid
            item_cursor = db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Полировка корпуса', 500, "
                "2, 2000, 0, 'in_progress')",
                (self.order_id,),
            )
            self.item_id = item_cursor.lastrowid

            for name, username in (
                (self.EMPLOYEE_A, self.USERNAME_A),
                (self.EMPLOYEE_B, self.USERNAME_B),
            ):
                employee = db.execute(
                    "INSERT INTO employees (name, created_at, deleted_at) "
                    "VALUES (?, '2026-09-01 09:00', NULL)",
                    (name,),
                )
                db.execute(
                    "INSERT INTO employee_positions (employee_id, position, created_at) "
                    "VALUES (?, 'Тюнингмэн', '2026-09-01 09:00')",
                    (employee.lastrowid,),
                )
                db.execute(
                    "INSERT INTO team_accounts "
                    "(employee_id, employee_name, username, password_hash, created_at) "
                    "VALUES (?, ?, ?, 'test-hash', '2026-09-01 09:00')",
                    (employee.lastrowid, name, username),
                )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    @classmethod
    def _clear(cls, db):
        client = db.execute(
            "SELECT id FROM clients WHERE token = ?", (cls.CLIENT_TOKEN,)
        ).fetchone()
        if client is not None:
            order_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_orders WHERE client_id = ?", (client["id"],)
                ).fetchall()
            ]
            for order_id in order_ids:
                item_ids = [
                    row["id"] for row in db.execute(
                        "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
                    ).fetchall()
                ]
                for item_id in item_ids:
                    assignment_ids = [
                        row["id"] for row in db.execute(
                            "SELECT id FROM tuning_item_assignments WHERE item_id = ?",
                            (item_id,),
                        ).fetchall()
                    ]
                    for assignment_id in assignment_ids:
                        entry = db.execute(
                            "SELECT entry_id FROM tuning_item_assignments WHERE id = ?",
                            (assignment_id,),
                        ).fetchone()
                        if entry and entry["entry_id"]:
                            db.execute("DELETE FROM entries WHERE id = ?", (entry["entry_id"],))
                    db.execute(
                        "DELETE FROM tuning_item_assignments WHERE item_id = ?", (item_id,)
                    )
                db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE client_id = ?", (client["id"],))
        db.execute("DELETE FROM clients WHERE token = ?", (cls.CLIENT_TOKEN,))
        for name in (cls.EMPLOYEE_A, cls.EMPLOYEE_B):
            db.execute("DELETE FROM team_accounts WHERE employee_name = ?", (name,))
            db.execute(
                "DELETE FROM employee_positions WHERE employee_id IN "
                "(SELECT id FROM employees WHERE name = ?)",
                (name,),
            )
            db.execute("DELETE FROM employees WHERE name = ?", (name,))
        db.commit()

    def login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def login_team(self, username, employee_name):
        with application_module.app.app_context():
            account_id = application_module.get_db().execute(
                "SELECT id FROM team_accounts WHERE username = ?", (username,)
            ).fetchone()["id"]
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = account_id
            session["team_employee_name"] = employee_name
            session["team_username"] = username

    def assign(self, employee_name, rate, hours):
        self.login_admin()
        return self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/assign",
            data={
                "employee_name": employee_name,
                "rate": str(rate),
                "norm_hours": str(hours),
                "comment": f"Задача для {employee_name}",
            },
        )

    def test_second_assignment_is_allowed_while_first_is_still_active(self):
        first = self.assign(self.EMPLOYEE_A, 1000, 2)
        self.assertEqual(first.status_code, 302)
        second = self.assign(self.EMPLOYEE_B, 1500, 1)
        self.assertEqual(second.status_code, 302)

        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT employee_name, rate, norm_hours FROM tuning_item_assignments "
                "WHERE item_id = ? ORDER BY id",
                (self.item_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["employee_name"], self.EMPLOYEE_A)
        self.assertEqual(rows[1]["employee_name"], self.EMPLOYEE_B)
        self.assertEqual(rows[0]["rate"], 1000)
        self.assertEqual(rows[1]["rate"], 1500)

    def test_admin_page_lists_both_assignments_and_still_offers_assign_button(self):
        self.assign(self.EMPLOYEE_A, 1000, 2)
        self.assign(self.EMPLOYEE_B, 1500, 1)
        self.login_admin()
        page = self.client.get(f"/tuning/edit/{self.order_id}")
        html = page.get_data(as_text=True)
        self.assertIn(self.EMPLOYEE_A, html)
        self.assertIn(self.EMPLOYEE_B, html)
        self.assertIn("Поручить задачу", html)

    def test_each_employee_is_paid_independently_at_their_own_rate(self):
        self.assign(self.EMPLOYEE_A, 1000, 2)
        self.assign(self.EMPLOYEE_B, 1500, 1)
        with application_module.app.app_context():
            db = application_module.get_db()
            assignment_a_id, assignment_b_id = (
                row["id"] for row in db.execute(
                    "SELECT id FROM tuning_item_assignments WHERE item_id = ? ORDER BY id",
                    (self.item_id,),
                ).fetchall()
            )

        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        self.client.post(
            f"/team/tuning-tasks/{assignment_a_id}/respond", data={"response": "accepted"}
        )
        self.client.post(
            f"/team/tuning-tasks/{assignment_a_id}/status", data={"status": "done"}
        )

        self.login_team(self.USERNAME_B, self.EMPLOYEE_B)
        self.client.post(
            f"/team/tuning-tasks/{assignment_b_id}/respond", data={"response": "accepted"}
        )
        self.client.post(
            f"/team/tuning-tasks/{assignment_b_id}/status", data={"status": "done"}
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            entry_a = db.execute(
                "SELECT rate, quantity, amount FROM entries WHERE employee = ? "
                "AND work_type = 'Полировка корпуса'",
                (self.EMPLOYEE_A,),
            ).fetchone()
            entry_b = db.execute(
                "SELECT rate, quantity, amount FROM entries WHERE employee = ? "
                "AND work_type = 'Полировка корпуса'",
                (self.EMPLOYEE_B,),
            ).fetchone()
        self.assertIsNotNone(entry_a)
        self.assertIsNotNone(entry_b)
        self.assertEqual(entry_a["amount"], 2000)
        self.assertEqual(entry_b["amount"], 1500)


if __name__ == "__main__":
    unittest.main()
