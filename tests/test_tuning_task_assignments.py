import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

from support import application_module


class _TuningTaskFixture:
    """Shared order/item/employee fixture — not a TestCase itself, so
    subclassing it for a second test class doesn't re-run these methods'
    tests twice under unittest's discovery."""

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
        return self.assign_many([(employee_name, rate, hours)])

    def assign_many(self, rows, comment=""):
        """rows: list of (employee_name, rate, hours) — one form row each,
        mirroring the sequential "Добавить сотрудника" rows in the modal."""
        self.login_admin()
        data = MultiDict()
        for name, rate, hours in rows:
            data.add("employee_name[]", name)
            data.add("rate[]", str(rate))
            data.add("norm_hours[]", str(hours))
        data["comment"] = comment
        return self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/assign", data=data
        )


class TuningTaskMultiAssignmentTests(_TuningTaskFixture, unittest.TestCase):
    def test_assigning_several_employees_at_once_creates_one_row_each(self):
        response = self.assign_many(
            [(self.EMPLOYEE_A, 1200, 3), (self.EMPLOYEE_B, 1200, 3)],
            "Общая формулировка задачи",
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT employee_name, rate, norm_hours, comment, assignment_status "
                "FROM tuning_item_assignments WHERE item_id = ? ORDER BY id",
                (self.item_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["employee_name"], self.EMPLOYEE_A)
        self.assertEqual(rows[1]["employee_name"], self.EMPLOYEE_B)
        for row in rows:
            self.assertEqual(row["rate"], 1200)
            self.assertEqual(row["norm_hours"], 3)
            self.assertEqual(row["comment"], "Общая формулировка задачи")
            self.assertEqual(row["assignment_status"], "pending")

    def test_assigning_several_employees_with_different_rates(self):
        """An experienced hand and a trainee on the same task, priced
        differently — the whole point of per-row tarification."""
        response = self.assign_many([
            (self.EMPLOYEE_A, 2000, 2),
            (self.EMPLOYEE_B, 800, 2),
        ])
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT employee_name, rate, norm_hours FROM tuning_item_assignments "
                "WHERE item_id = ? ORDER BY id",
                (self.item_id,),
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["employee_name"], rows[0]["rate"]), (self.EMPLOYEE_A, 2000))
        self.assertEqual((rows[1]["employee_name"], rows[1]["rate"]), (self.EMPLOYEE_B, 800))
        self.assertEqual(rows[0]["norm_hours"], 2)
        self.assertEqual(rows[1]["norm_hours"], 2)

    def test_assigning_several_employees_notifies_each_of_them(self):
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notify:
            self.assign_many([(self.EMPLOYEE_A, 1200, 3), (self.EMPLOYEE_B, 900, 2)])
        self.assertEqual(notify.call_count, 2)
        notified_names = {call.args[1] for call in notify.call_args_list}
        self.assertEqual(notified_names, {self.EMPLOYEE_A, self.EMPLOYEE_B})

    def test_row_with_invalid_rate_is_skipped_but_others_still_created(self):
        response = self.assign_many([
            (self.EMPLOYEE_A, "not-a-number", 3),
            (self.EMPLOYEE_B, 900, 2),
        ])
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT employee_name FROM tuning_item_assignments WHERE item_id = ?",
                (self.item_id,),
            ).fetchall()
        self.assertEqual([r["employee_name"] for r in rows], [self.EMPLOYEE_B])

    def test_duplicate_and_invalid_names_in_selection_are_ignored(self):
        response = self.assign_many([
            (self.EMPLOYEE_A, 1200, 3),
            (self.EMPLOYEE_A, 1200, 3),
            ("Кто-то Несуществующий", 1200, 3),
        ])
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT employee_name FROM tuning_item_assignments WHERE item_id = ?",
                (self.item_id,),
            ).fetchall()
        self.assertEqual([r["employee_name"] for r in rows], [self.EMPLOYEE_A])

    def test_empty_selection_creates_nothing(self):
        response = self.assign_many([])
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS c FROM tuning_item_assignments WHERE item_id = ?",
                (self.item_id,),
            ).fetchone()["c"]
        self.assertEqual(count, 0)

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

    def test_board_shows_work_block_and_task_branches(self):
        self.assign(self.EMPLOYEE_A, 1000, 2)
        self.assign(self.EMPLOYEE_B, 1500, 1)
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Полировка корпуса", html)
        self.assertIn(self.EMPLOYEE_A, html)
        self.assertIn(self.EMPLOYEE_B, html)
        self.assertIn("Ожидает ответа", html)
        self.assertIn("+ Поручить задачу", html)

    def test_board_requires_admin_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get(f"/tuning/{self.order_id}/board")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_board_shows_empty_hint_when_item_has_no_assignments(self):
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board")
        self.assertIn("Пока никто не назначен".encode(), page.data)

    def test_assign_from_board_returns_to_board(self):
        self.login_admin()
        response = self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/assign",
            data={
                "employee_name": self.EMPLOYEE_A,
                "rate": "1000",
                "norm_hours": "2",
                "comment": "",
                "next": "board",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith(f"/tuning/{self.order_id}/board")
        )

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


class TuningLaborBudgetTests(_TuningTaskFixture, unittest.TestCase):
    """cost_price on the fixture item is 500 — reused here as the labor
    budget under test, so tasks totalling under/over that trip the
    different branches of the budget strip and the overrun warning."""

    def get_assignment_id(self, employee_name):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM tuning_item_assignments WHERE item_id = ? "
                "AND employee_name = ?",
                (self.item_id, employee_name),
            ).fetchone()["id"]

    def test_board_shows_remaining_budget_when_within_budget(self):
        self.assign(self.EMPLOYEE_A, 100, 2)  # 200 of 500
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board")
        html = page.get_data(as_text=True)
        self.assertIn("Остаток бюджета на труд", html)
        self.assertNotIn("превышен", html)

    def test_order_page_also_shows_budget_strip(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        self.login_admin()
        page = self.client.get(f"/tuning/edit/{self.order_id}")
        self.assertIn("Остаток бюджета на труд".encode(), page.data)

    def test_assigning_over_budget_flags_warning_and_board_shows_overrun(self):
        response = self.assign(self.EMPLOYEE_A, 400, 2)  # 800 > 500 budget
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn("tuning_budget_warning", session)
            self.assertIn("Полировка корпуса", session["tuning_budget_warning"])

        page = self.client.get(f"/tuning/{self.order_id}/board")
        html = page.get_data(as_text=True)
        self.assertIn("Бюджет на труд превышен", html)
        # the flash is one-shot
        with self.client.session_transaction() as session:
            self.assertNotIn("tuning_budget_warning", session)

    def test_assigning_within_budget_sets_no_warning(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        with self.client.session_transaction() as session:
            self.assertNotIn("tuning_budget_warning", session)

    def test_update_assignment_terms_changes_rate_and_can_trigger_overrun(self):
        self.assign(self.EMPLOYEE_A, 100, 2)  # 200 of 500, within budget
        assignment_id = self.get_assignment_id(self.EMPLOYEE_A)
        self.login_admin()
        response = self.client.post(
            f"/tuning/assignments/{assignment_id}/rate",
            data={"rate": "400", "norm_hours": "2"},  # 800 > 500
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT rate, norm_hours FROM tuning_item_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
        self.assertEqual(row["rate"], 400)
        self.assertEqual(row["norm_hours"], 2)
        with self.client.session_transaction() as session:
            self.assertIn("tuning_budget_warning", session)

    def test_update_assignment_terms_of_a_paid_task_recalculates_its_payout(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        assignment_id = self.get_assignment_id(self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            entry = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, "
                "work_date, created_at) VALUES (?, 'Полировка корпуса', 100, 2, 200, "
                "'2026-09-01', '2026-09-01 12:00')",
                (self.EMPLOYEE_A,),
            )
            entry_id = entry.lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ? WHERE id = ?",
                (entry_id, assignment_id),
            )
            db.commit()

        self.login_admin()
        response = self.client.post(
            f"/tuning/assignments/{assignment_id}/rate",
            data={"rate": "150", "norm_hours": "3"},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute(
                "SELECT rate, norm_hours FROM tuning_item_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
            entry = db.execute(
                "SELECT rate, quantity, amount FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        self.assertEqual((row["rate"], row["norm_hours"]), (150, 3))
        self.assertEqual((entry["rate"], entry["quantity"], entry["amount"]), (150, 3, 450))
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Выплата пересчитана", page)

    def test_invalid_terms_are_rejected_with_a_message(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        assignment_id = self.get_assignment_id(self.EMPLOYEE_A)
        self.login_admin()
        self.client.post(
            f"/tuning/assignments/{assignment_id}/rate", data={"rate": "abc", "norm_hours": "2"}
        )
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT rate FROM tuning_item_assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        self.assertEqual(row["rate"], 100)
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("должны быть числами больше нуля", page)

    def test_rejected_assignments_do_not_count_toward_budget(self):
        self.assign(self.EMPLOYEE_A, 400, 2)  # would be over budget
        assignment_id = self.get_assignment_id(self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE tuning_item_assignments SET assignment_status = 'rejected' "
                "WHERE id = ?",
                (assignment_id,),
            )
            db.commit()
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board")
        html = page.get_data(as_text=True)
        self.assertNotIn("Бюджет на труд превышен", html)
        self.assertIn("Остаток бюджета на труд", html)


class TuningTaskRevokeTests(_TuningTaskFixture, unittest.TestCase):
    def assignment_ids(self):
        with application_module.app.app_context():
            return {
                row["employee_name"]: row["id"] for row in application_module.get_db().execute(
                    "SELECT id, employee_name FROM tuning_item_assignments WHERE item_id = ?",
                    (self.item_id,),
                ).fetchall()
            }

    def revoke(self, assignment_id):
        self.login_admin()
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.client.post(f"/tuning/assignments/{assignment_id}/revoke")
        return response, notifier

    def test_revoking_removes_only_that_employees_task_and_tells_them(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 2), (self.EMPLOYEE_B, 150, 1)])
        ids = self.assignment_ids()
        response, notifier = self.revoke(ids[self.EMPLOYEE_A])
        self.assertEqual(response.status_code, 302)
        self.assertIn("/board", response.headers["Location"])
        self.assertEqual(list(self.assignment_ids()), [self.EMPLOYEE_B])
        notifier.assert_called_once()
        self.assertEqual(notifier.call_args.args[1], self.EMPLOYEE_A)
        self.assertIn("Задача отозвана", notifier.call_args.args[2])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn(f"отозвана у {self.EMPLOYEE_A}", page)
        # the tuningman no longer sees it
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        team = self.client.get("/team/").get_data(as_text=True)
        self.assertNotIn("Полировка корпуса", team)

    def test_the_board_offers_the_revoke_button_for_every_task(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 2), (self.EMPLOYEE_B, 100, 1)])
        ids = self.assignment_ids()
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Полировка корпуса', 100, 1, 100, '2026-09-01', '2026-09-01 12:00')",
                (self.EMPLOYEE_B,),
            ).lastrowid
            db.execute("UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done' "
                       "WHERE id = ?", (entry_id, ids[self.EMPLOYEE_B]))
            db.commit()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        for assignment_id in ids.values():
            self.assertIn(f"/tuning/assignments/{assignment_id}/revoke", page)
        self.assertIn("Уже начисленная выплата", page)  # only the paid task warns

    def paid_task(self, work_date="2026-09-01"):
        self.assign(self.EMPLOYEE_A, 100, 2)
        assignment_id = self.assignment_ids()[self.EMPLOYEE_A]
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Полировка корпуса', 100, 2, 200, ?, '2026-09-01 12:00')",
                (self.EMPLOYEE_A, work_date),
            ).lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done' WHERE id = ?",
                (entry_id, assignment_id),
            )
            db.commit()
        return assignment_id, entry_id

    def entry_exists(self, entry_id):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT 1 FROM entries WHERE id = ?", (entry_id,)
            ).fetchone() is not None

    def test_a_paid_task_can_be_revoked_and_its_payout_is_deleted(self):
        assignment_id, entry_id = self.paid_task()
        response, notifier = self.revoke(assignment_id)
        self.assertNotIn(self.EMPLOYEE_A, self.assignment_ids())
        self.assertFalse(self.entry_exists(entry_id))
        notifier.assert_called_once()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Выплата 200 ₽ удалена", page)
        self.assertNotIn("неделя этой выплаты уже отмечена оплаченной", page)

    def test_revoking_a_payout_from_a_settled_week_warns(self):
        assignment_id, entry_id = self.paid_task(work_date="2026-09-02")  # week of Mon 2026-08-31
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO payments (employee, period_key, paid_at) VALUES (?, '2026-08-31', "
                "'2026-09-08 10:00')", (self.EMPLOYEE_A,),
            )
            db.commit()
        try:
            self.revoke(assignment_id)
            self.assertFalse(self.entry_exists(entry_id))
            page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
            self.assertIn("неделя этой выплаты уже отмечена оплаченной", page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM payments WHERE employee = ? AND period_key = '2026-08-31'",
                           (self.EMPLOYEE_A,))
                db.commit()

    def test_tasks_with_written_off_materials_cannot_be_revoked(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        assignment_id = self.assignment_ids()[self.EMPLOYEE_A]
        with application_module.app.app_context():
            db = application_module.get_db()
            warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, created_at) VALUES ('revoke-test', '2026-09-01 10:00')"
            ).lastrowid
            product_id = db.execute(
                "INSERT INTO supply_products (name, cost_price, cost_unit, sale_price, created_at) "
                "VALUES ('revoke-test товар', 10, 'piece', 20, '2026-09-01 10:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO supply_writeoffs (product_id, warehouse_id, quantity, reason, created_at, "
                "cost_price, amount, tuning_item_assignment_id) VALUES (?, ?, 1, 'Использовано в работе', "
                "'2026-09-01 12:00', 10, 10, ?)", (product_id, warehouse_id, assignment_id),
            )
            db.commit()
        try:
            self.revoke(assignment_id)
            self.assertIn(self.EMPLOYEE_A, self.assignment_ids())
            page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
            self.assertIn("списаны материалы", page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM supply_writeoffs WHERE tuning_item_assignment_id = ?", (assignment_id,))
                db.execute("DELETE FROM supply_products WHERE name = 'revoke-test товар'")
                db.execute("DELETE FROM supply_warehouses WHERE name = 'revoke-test'")
                db.commit()

    def test_revoking_also_clears_its_placement_on_the_tuning_schedule(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        assignment_id = self.assignment_ids()[self.EMPLOYEE_A]
        with application_module.app.app_context():
            db = application_module.get_db()
            task_id = db.execute(
                "INSERT INTO tuning_schedule_tasks (assignment_id, employee_name, title, rate, created_at) "
                "VALUES (?, ?, 'Полировка корпуса', 100, '2026-09-01 10:00')",
                (assignment_id, self.EMPLOYEE_A),
            ).lastrowid
            db.execute(
                "INSERT INTO tuning_schedule_task_days (task_id, work_date, start_time, planned_hours) "
                "VALUES (?, '2026-09-02', '09:00', 2)", (task_id,),
            )
            db.commit()
        self.revoke(assignment_id)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tuning_schedule_tasks WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tuning_schedule_task_days WHERE task_id = ?", (task_id,)
            ).fetchone()[0], 0)

    def test_revoking_an_unknown_task_is_harmless(self):
        response, notifier = self.revoke(999999)
        self.assertEqual(response.status_code, 302)
        notifier.assert_not_called()


class TuningTaskDatesTests(_TuningTaskFixture, unittest.TestCase):
    def assignment(self, employee):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? AND employee_name = ?",
                (self.item_id, employee),
            ).fetchone())

    def set_status(self, assignment_id, status):
        self.login_admin()
        self.client.post(f"/tuning/assignments/{assignment_id}/status", data={"status": status})

    def test_the_completion_date_is_stamped_automatically_and_creation_date_is_not_shown(self):
        today = application_module.dt.date.today().isoformat()
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        self.assertIsNone(task["completed_at"])
        self.assertIsNone(task["due_from"])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertNotIn("Поручена", page)
        self.assertNotIn('name="assigned_date"', page)
        self.assertIn('name="due_from"', page)
        self.assertIn('name="due_to"', page)
        self.assertIn('name="completed_date"', page)

        self.set_status(task["id"], "accepted")
        self.assertIsNone(self.assignment(self.EMPLOYEE_A)["completed_at"])
        self.set_status(task["id"], "done")
        done = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(done["completed_at"][:10], today)
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn(f'name="completed_date" value="{today}"', page)

    def test_completion_date_is_kept_while_done_and_cleared_when_an_unpaid_task_is_reopened(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET assignment_status = 'done', "
                       "completed_at = '2026-01-05 10:00' WHERE id = ?", (task_id,))
            db.commit()
        self.assertEqual(self.assignment(self.EMPLOYEE_A)["completed_at"], "2026-01-05 10:00")
        # re-saving the same status does not restamp
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET assignment_status = 'done' WHERE id = ?", (task_id,))
            db.commit()
        self.assertEqual(self.assignment(self.EMPLOYEE_A)["completed_at"], "2026-01-05 10:00")
        # reopened (and not paid) -> cleared
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET assignment_status = 'in_progress' WHERE id = ?", (task_id,))
            db.commit()
        self.assertIsNone(self.assignment(self.EMPLOYEE_A)["completed_at"])

    def test_a_paid_task_keeps_its_completion_date_when_moved_back(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.set_status(task_id, "done")  # pays it out, stamps the date
        stamped = self.assignment(self.EMPLOYEE_A)
        self.assertIsNotNone(stamped["entry_id"])
        self.set_status(task_id, "in_progress")
        self.assertEqual(self.assignment(self.EMPLOYEE_A)["completed_at"], stamped["completed_at"])

    def edit_dates(self, task_id, due_from="", completed="", due_to=""):
        self.login_admin()
        return self.client.post(
            f"/tuning/assignments/{task_id}/dates",
            data={"due_from": due_from, "due_to": due_to, "completed_date": completed},
        )

    def test_dates_can_be_edited_keeping_the_time_of_day(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE tuning_item_assignments SET assigned_at = '2026-09-01 10:30', "
                "assignment_status = 'done', completed_at = '2026-09-03 17:45' WHERE id = ?", (task_id,)
            )
            db.commit()
        response = self.edit_dates(task_id, "2026-08-28", "2026-09-05", due_to="2026-08-31")
        self.assertEqual(response.status_code, 302)
        task = self.assignment(self.EMPLOYEE_A)
        self.assertEqual((task["due_from"], task["due_to"]), ("2026-08-28", "2026-08-31"))
        self.assertEqual(task["assigned_at"], "2026-09-01 10:30")  # creation stamp is never edited
        self.assertEqual(task["completed_at"], "2026-09-05 17:45")
        self.assertIn("Даты задачи обновлены", self.client.get(
            f"/tuning/{self.order_id}/board").get_data(as_text=True))
        # the completion date can be cleared on a done task
        self.edit_dates(task_id, "2026-08-28", "")
        self.assertIsNone(self.assignment(self.EMPLOYEE_A)["completed_at"])

    def test_editing_the_completion_date_moves_the_payout_date(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Полировка корпуса', 100, 2, 200, '2026-09-08', '2026-09-08 12:00')",
                (self.EMPLOYEE_A,),
            ).lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done', "
                "assigned_at = '2026-08-01 09:00', completed_at = '2026-09-08 18:00' WHERE id = ?",
                (entry_id, task_id),
            )
            db.commit()

        def entry_date():
            with application_module.app.app_context():
                return application_module.get_db().execute(
                    "SELECT work_date FROM entries WHERE id = ?", (entry_id,)
                ).fetchone()["work_date"]

        self.edit_dates(task_id, "2026-08-01", "2026-09-02")
        self.assertEqual(entry_date(), "2026-09-02")
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Дата выплаты изменена", page)
        self.assertNotIn("уже отмечена оплаченной", page)

        # unchanged completion date: the payout is left alone
        self.edit_dates(task_id, "2026-08-05", "2026-09-02")
        self.assertEqual(entry_date(), "2026-09-02")
        # clearing the completion date keeps the payout where it is
        self.edit_dates(task_id, "2026-08-05", "")
        self.assertEqual(entry_date(), "2026-09-02")

        # moving it out of / into a week that is already settled warns
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO payments (employee, period_key, paid_at) VALUES (?, '2026-08-31', "
                       "'2026-09-09 10:00')", (self.EMPLOYEE_A,))
            db.execute("UPDATE tuning_item_assignments SET completed_at = '2026-09-02 18:00' WHERE id = ?",
                       (task_id,))
            db.commit()
        try:
            self.edit_dates(task_id, "2026-08-05", "2026-09-10")  # week of 08-31 -> week of 09-07
            self.assertEqual(entry_date(), "2026-09-10")
            page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
            self.assertIn("уже отмечена оплаченной", page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM payments WHERE employee = ? AND period_key = '2026-08-31'",
                           (self.EMPLOYEE_A,))
                db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
                db.commit()

    def test_a_paid_task_moved_back_from_done_still_moves_its_payout(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Полировка корпуса', 100, 2, 200, '2026-09-08', '2026-09-08 12:00')",
                (self.EMPLOYEE_A,),
            ).lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'in_progress', "
                "assigned_at = '2026-08-01 09:00', completed_at = '2026-09-08 18:00' WHERE id = ?",
                (entry_id, task_id),
            )
            db.commit()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertNotIn("Дата появится, когда задача", page)
        self.edit_dates(task_id, "2026-08-01", "2026-09-03")
        with application_module.app.app_context():
            db = application_module.get_db()
            work_date = db.execute("SELECT work_date FROM entries WHERE id = ?", (entry_id,)).fetchone()[0]
            db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            db.commit()
        self.assertEqual(work_date, "2026-09-03")
        self.assertEqual(self.assignment(self.EMPLOYEE_A)["completed_at"][:10], "2026-09-03")

    def done_task_without_payout(self, employee=None, dangling_entry=False):
        self.assign(employee or self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(employee or self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE tuning_item_assignments SET assignment_status = 'done', "
                "assigned_at = '2026-08-01 09:00', completed_at = '2026-08-20 15:00', entry_id = ? WHERE id = ?",
                (999999 if dangling_entry else None, task_id),
            )
            db.commit()
        return task_id

    def payouts_of(self, employee):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM entries WHERE employee = ? AND work_type = 'Полировка корпуса'", (employee,)
            ).fetchall()]

    def test_saving_dates_creates_the_missing_payout_of_a_done_task(self):
        task_id = self.done_task_without_payout()
        self.assertEqual(self.payouts_of(self.EMPLOYEE_A), [])
        self.edit_dates(task_id, "2026-08-01", "2026-08-18")
        rows = self.payouts_of(self.EMPLOYEE_A)
        self.assertEqual([(r["work_date"], r["amount"]) for r in rows], [("2026-08-18", 200.0)])
        self.assertIsNotNone(self.assignment(self.EMPLOYEE_A)["entry_id"])
        self.assertIn("У задачи не было выплаты", self.client.get(
            f"/tuning/{self.order_id}/board").get_data(as_text=True))
        # saving again does not pay twice
        self.edit_dates(task_id, "2026-08-01", "2026-08-19")
        self.assertEqual(len(self.payouts_of(self.EMPLOYEE_A)), 1)
        self.assertEqual(self.payouts_of(self.EMPLOYEE_A)[0]["work_date"], "2026-08-19")
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM entries WHERE employee = ?", (self.EMPLOYEE_A,))
            db.commit()

    def test_board_flags_and_repairs_done_tasks_without_a_payout(self):
        self.done_task_without_payout(self.EMPLOYEE_A)
        self.done_task_without_payout(self.EMPLOYEE_B, dangling_entry=True)
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("У выполненных задач нет выплаты: 2", page)
        self.assertEqual(page.count("Нет выплаты"), 2)
        response = self.client.post(f"/tuning/{self.order_id}/board/repair-payouts")
        self.assertEqual(response.status_code, 302)
        for employee, day in ((self.EMPLOYEE_A, "2026-08-20"), (self.EMPLOYEE_B, "2026-08-20")):
            rows = self.payouts_of(employee)
            self.assertEqual([(r["work_date"], r["amount"]) for r in rows], [(day, 200.0)])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Создано выплат: 2", page)
        self.assertNotIn("У выполненных задач нет выплаты", page)
        # repeating changes nothing
        self.client.post(f"/tuning/{self.order_id}/board/repair-payouts")
        self.assertEqual(len(self.payouts_of(self.EMPLOYEE_A)), 1)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM entries WHERE employee IN (?, ?)", (self.EMPLOYEE_A, self.EMPLOYEE_B))
            db.commit()

    def test_deleting_a_payout_on_the_payroll_page_unlinks_the_task_and_it_can_be_repaid(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.set_status(task_id, "done")  # pays it out
        entry_id = self.assignment(self.EMPLOYEE_A)["entry_id"]
        self.assertIsNotNone(entry_id)
        response = self.client.post(f"/delete/{entry_id}")
        self.assertIsNone(self.assignment(self.EMPLOYEE_A)["entry_id"])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Нет выплаты", page)
        # marking it done again pays it again now that the link is clean
        self.set_status(task_id, "in_progress")
        self.set_status(task_id, "done")
        self.assertEqual(len(self.payouts_of(self.EMPLOYEE_A)), 1)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM entries WHERE employee = ?", (self.EMPLOYEE_A,))
            db.commit()

    def test_invalid_or_inconsistent_dates_are_rejected(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET assigned_at = '2026-09-01 10:30', "
                       "assignment_status = 'done', completed_at = '2026-09-03 17:45' WHERE id = ?", (task_id,))
            db.commit()
        before = self.assignment(self.EMPLOYEE_A)
        self.edit_dates(task_id, "not-a-date", "2026-09-05")
        self.edit_dates(task_id, "2026-09-10", "garbage", due_to="2026-09-12")
        after = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(
            (after["due_from"], after["due_to"], after["completed_at"]),
            (before["due_from"], before["due_to"], before["completed_at"]),
        )
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Укажите корректные даты", page)

    def test_a_task_that_is_not_done_has_no_completion_date_to_edit(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.edit_dates(task_id, "2026-08-20", "2026-09-05")
        task = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(task["due_from"], "2026-08-20")
        self.assertIsNone(task["completed_at"])

    def test_team_dashboard_shows_the_due_date_or_period_instead_of_the_creation_date(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        page = self.client.get("/team/").get_data(as_text=True)
        self.assertNotIn("Поручена:", page)
        self.assertNotIn("Срок:", page)  # none set -> nothing shown
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.edit_dates(task_id, "2026-10-12")
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        self.assertIn("Срок: 12/10/2026", self.client.get("/team/").get_data(as_text=True))
        self.edit_dates(task_id, "2026-10-12", due_to="2026-10-15")
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        self.assertIn("Срок: 12/10/2026 – 15/10/2026", self.client.get("/team/").get_data(as_text=True))


class TuningTaskDueDateTests(_TuningTaskFixture, unittest.TestCase):
    """The planned execution (one date or a period) chosen when a task is
    created, replacing the shown creation date."""

    def assign_with_due(self, due_from="", due_to="", rows=None):
        self.login_admin()
        data = MultiDict()
        for name, rate, hours in rows or [(self.EMPLOYEE_A, 100, 2)]:
            data.add("employee_name[]", name)
            data.add("rate[]", str(rate))
            data.add("norm_hours[]", str(hours))
        data["comment"] = ""
        data["due_from"] = due_from
        data["due_to"] = due_to
        # these tests are about the stored due date, not the tuning schedule
        # (covered in test_tuning_schedule_assignments.py)
        data["shift_decision"] = "skip"
        return self.client.post(f"/tuning/{self.order_id}/item/{self.item_id}/assign", data=data)

    def due(self, employee):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT due_from, due_to FROM tuning_item_assignments "
                "WHERE item_id = ? AND employee_name = ?", (self.item_id, employee),
            ).fetchone()
            return (row["due_from"], row["due_to"])

    def test_a_single_date_is_stored_as_due_from_only(self):
        self.assign_with_due("2026-10-12")
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", None))

    def test_a_period_stores_both_ends(self):
        self.assign_with_due("2026-10-12", "2026-10-15")
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", "2026-10-15"))

    def test_no_date_is_allowed(self):
        self.assign_with_due()
        self.assertEqual(self.due(self.EMPLOYEE_A), (None, None))

    def test_equal_ends_collapse_to_one_date_and_reversed_ends_are_swapped(self):
        self.assign_with_due("2026-10-12", "2026-10-12")
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", None))
        self.assign_with_due("2026-10-20", "2026-10-15", rows=[(self.EMPLOYEE_B, 100, 1)])
        self.assertEqual(self.due(self.EMPLOYEE_B), ("2026-10-15", "2026-10-20"))

    def test_a_lone_end_date_counts_as_the_single_date_and_garbage_is_ignored(self):
        self.assign_with_due("", "2026-10-14")
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-14", None))
        self.assign_with_due("not-a-date", "", rows=[(self.EMPLOYEE_B, 100, 1)])
        self.assertEqual(self.due(self.EMPLOYEE_B), (None, None))

    def test_everyone_assigned_in_one_submission_shares_the_due_date(self):
        self.assign_with_due("2026-10-12", "2026-10-15",
                             rows=[(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 150, 1)])
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", "2026-10-15"))
        self.assertEqual(self.due(self.EMPLOYEE_B), ("2026-10-12", "2026-10-15"))

    def test_the_board_shows_the_due_date_in_the_edit_form(self):
        self.assign_with_due("2026-10-12", "2026-10-15")
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn('name="due_from" value="2026-10-12"', page)
        self.assertIn('name="due_to" value="2026-10-15"', page)

    def test_the_assign_modals_offer_the_due_fields(self):
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn('id="assign-due-from"', page)
        order_page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn('id="assign-due-from"', order_page)

    def schedule_task(self, days):
        self.login_admin()
        data = MultiDict([
            ("employee_name", self.EMPLOYEE_A), ("rate", "100"), ("comment", ""),
            ("order_id", str(self.order_id)), ("order_item_id", str(self.item_id)),
            ("return_date", "2026-10-12"),
        ])
        for day in days:
            data.add("work_date[]", day)
            data.add("start_time[]", "10:00")
            data.add("planned_hours[]", "2")
        return self.client.post("/schedule/tuning/tasks", data=data)

    def test_a_task_scheduled_on_one_day_gets_that_day_as_its_due_date(self):
        self.schedule_task(["2026-10-12"])
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", None))

    def test_a_task_scheduled_over_several_days_gets_the_period_as_its_due_date(self):
        self.schedule_task(["2026-10-14", "2026-10-12", "2026-10-13"])
        self.assertEqual(self.due(self.EMPLOYEE_A), ("2026-10-12", "2026-10-14"))

    def test_the_assignment_message_to_the_employee_includes_the_due_date(self):
        from unittest import mock
        with mock.patch.object(application_module, "send_telegram_notification_to_employee") as sender:
            self.assign_with_due("2026-10-12", "2026-10-15")
        texts = [call.args[2] for call in sender.call_args_list]
        self.assertTrue(any("Срок: 12.10.2026 – 15.10.2026" in text for text in texts), texts)
        with mock.patch.object(application_module, "send_telegram_notification_to_employee") as sender:
            self.assign_with_due("2026-10-14", rows=[(self.EMPLOYEE_B, 100, 1)])
        texts = [call.args[2] for call in sender.call_args_list]
        self.assertTrue(any("Срок: 14.10.2026" in text and "–" not in text.split("Срок:")[1].split("\n")[0]
                            for text in texts), texts)


class TuningTaskCommentEditTests(_TuningTaskFixture, unittest.TestCase):
    def assignment(self, employee):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? AND employee_name = ?",
                (self.item_id, employee),
            ).fetchone())

    def comment_of(self, employee):
        return self.assignment(employee)["comment"]

    def save_comment(self, assignment_id, comment):
        self.login_admin()
        return self.client.post(
            f"/tuning/assignments/{assignment_id}/comment", data={"comment": comment}
        )

    def test_a_comment_can_be_added_to_a_task_created_without_one(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.assertFalse(self.comment_of(self.EMPLOYEE_A))
        response = self.save_comment(task_id, "  Не забыть про прокладку  ")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/board", response.headers["Location"])
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "Не забыть про прокладку")
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Комментарий сохранён", page)
        self.assertIn("Не забыть про прокладку", page)

    def test_an_existing_comment_can_be_edited_and_cleared(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.save_comment(task_id, "Старый текст")
        self.save_comment(task_id, "Новый текст")
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "Новый текст")
        self.save_comment(task_id, "")
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "")
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Комментарий удалён", page)

    def test_the_comment_only_changes_that_employees_task(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)])
        self.save_comment(self.assignment(self.EMPLOYEE_A)["id"], "Только для А")
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "Только для А")
        self.assertFalse(self.comment_of(self.EMPLOYEE_B))

    def test_too_long_comment_is_rejected_and_old_one_kept(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.save_comment(task_id, "Короткий")
        self.save_comment(task_id, "я" * 2001)
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "Короткий")
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("слишком длинный", page)

    def test_a_paid_task_comment_is_editable_too(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, "
                "work_date, created_at) VALUES (?, 'Полировка корпуса', 100, 2, 200, "
                "'2026-09-01', '2026-09-01 12:00')",
                (self.EMPLOYEE_A,),
            ).lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done' "
                "WHERE id = ?", (entry_id, task_id),
            )
            db.commit()
        self.save_comment(task_id, "Сделано, принято клиентом")
        self.assertEqual(self.comment_of(self.EMPLOYEE_A), "Сделано, принято клиентом")

    def test_the_board_offers_comment_editing_for_every_task(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        for employee in (self.EMPLOYEE_A, self.EMPLOYEE_B):
            self.assertIn(
                f"/tuning/assignments/{self.assignment(employee)['id']}/comment", page
            )
        self.assertIn("+ Добавить комментарий", page)


class TuningTaskEmployeeChangeTests(_TuningTaskFixture, unittest.TestCase):
    EMPLOYEE_C = "Мастеров Третий"

    def assignment(self, employee):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? AND employee_name = ?",
                (self.item_id, employee),
            ).fetchone()
            return dict(row) if row else None

    def change(self, assignment_id, new_name):
        self.login_admin()
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.client.post(
                f"/tuning/assignments/{assignment_id}/employee",
                data={"employee_name": new_name},
            )
        return response, notifier

    def _drop_settled_week(self, period_key):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM payments WHERE employee = ? AND period_key = ?",
                (self.EMPLOYEE_A, period_key),
            )
            db.commit()

    def make_paid(self, assignment_id, employee, work_date="2026-09-01"):
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, "
                "work_date, created_at) VALUES (?, 'Полировка корпуса', 100, 2, 200, "
                "?, '2026-09-01 12:00')",
                (employee, work_date),
            ).lastrowid
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done' "
                "WHERE id = ?", (entry_id, assignment_id),
            )
            db.commit()
        return entry_id

    def test_an_unfinished_task_moves_and_goes_back_to_pending(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE tuning_item_assignments SET assignment_status = 'in_progress', "
                "comment = 'Заметка' WHERE id = ?", (task["id"],)
            )
            db.commit()
        response, notifier = self.change(task["id"], self.EMPLOYEE_B)
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.assignment(self.EMPLOYEE_A))
        moved = self.assignment(self.EMPLOYEE_B)
        self.assertEqual(moved["id"], task["id"])
        self.assertEqual(moved["assignment_status"], "pending")
        self.assertEqual((moved["rate"], moved["norm_hours"], moved["comment"]), (100, 2, "Заметка"))
        recipients = [call.args[1] for call in notifier.call_args_list]
        self.assertIn(self.EMPLOYEE_A, recipients)  # told it was taken away
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn(f"{self.EMPLOYEE_A} → {self.EMPLOYEE_B}", page)

    def test_the_new_assignee_sees_the_task_and_the_old_one_does_not(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        self.change(self.assignment(self.EMPLOYEE_A)["id"], self.EMPLOYEE_B)
        self.login_team(self.USERNAME_B, self.EMPLOYEE_B)
        self.assertIn("Полировка корпуса", self.client.get("/team/").get_data(as_text=True))
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        self.assertNotIn("Полировка корпуса", self.client.get("/team/").get_data(as_text=True))

    def test_a_paid_task_keeps_its_status_and_the_payout_moves_with_it(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        entry_id = self.make_paid(task["id"], self.EMPLOYEE_A)
        self.change(task["id"], self.EMPLOYEE_B)
        moved = self.assignment(self.EMPLOYEE_B)
        self.assertEqual(moved["assignment_status"], "done")
        with application_module.app.app_context():
            entry = application_module.get_db().execute(
                "SELECT employee, amount FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        self.assertEqual((entry["employee"], entry["amount"]), (self.EMPLOYEE_B, 200))
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("перенесена на", page)

    def test_moving_a_paid_task_into_a_settled_week_warns(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        self.make_paid(task["id"], self.EMPLOYEE_A, work_date="2026-09-02")  # week of 2026-08-31
        with application_module.app.app_context():
            db = application_module.get_db()
            columns = [r["name"] for r in db.execute("PRAGMA table_info(payments)").fetchall()]
            values = {"employee": self.EMPLOYEE_A, "period_key": "2026-08-31", "amount": 200,
                      "paid_at": "2026-09-08", "created_at": "2026-09-08 10:00"}
            used = [c for c in columns if c in values]
            db.execute(
                f"INSERT INTO payments ({','.join(used)}) VALUES ({','.join('?' for _ in used)})",
                [values[c] for c in used],
            )
            db.commit()
        self.addCleanup(self._drop_settled_week, "2026-08-31")
        self.change(task["id"], self.EMPLOYEE_B)
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("уже отмечена оплаченной", page)

    def test_cannot_hand_a_task_to_someone_already_on_that_work(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)])
        task = self.assignment(self.EMPLOYEE_A)
        self.change(task["id"], self.EMPLOYEE_B)
        self.assertIsNotNone(self.assignment(self.EMPLOYEE_A))
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("уже назначен на эту работу", page)

    def test_cannot_hand_a_task_to_someone_who_is_not_a_tuningman(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        self.change(task["id"], "Несуществующий Человек")
        self.assertIsNotNone(self.assignment(self.EMPLOYEE_A))
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("нельзя назначить", page)

    def test_materials_and_schedule_placement_follow_the_new_employee(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            cols = [r["name"] for r in db.execute("PRAGMA table_info(supply_writeoffs)").fetchall()]
            self.assertIn("employee_name", cols)
            schedule_id = db.execute(
                "INSERT INTO tuning_schedule_tasks (assignment_id, employee_name, title, rate, "
                "created_at) VALUES (?, ?, 'Полировка корпуса', 100, '2026-09-01 10:00')",
                (task["id"], self.EMPLOYEE_A),
            ).lastrowid
            db.commit()
        self.change(task["id"], self.EMPLOYEE_B)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT employee_name FROM tuning_schedule_tasks WHERE id = ?", (schedule_id,)
            ).fetchone()
        self.assertEqual(row["employee_name"], self.EMPLOYEE_B)

    def test_the_board_offers_an_employee_selector_for_every_task(self):
        self.assign_many([(self.EMPLOYEE_A, 100, 1), (self.EMPLOYEE_B, 100, 1)])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        for employee in (self.EMPLOYEE_A, self.EMPLOYEE_B):
            self.assertIn(f"/tuning/assignments/{self.assignment(employee)['id']}/employee", page)


class _ContractorFixture(_TuningTaskFixture):
    """A work item's task can go to a tuning partner (contractor) instead of
    an employee: stored with partner_id, no payroll, no employee schedule."""

    PARTNER_TOKEN = "contractor-test-partner"
    PLAIN_TOKEN = "contractor-test-plain-client"

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            self.partner_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Мастерская Подряд', '', '+79990001122', ?, 'neutral', '2026-09-01 10:00')",
                (self.PARTNER_TOKEN,),
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'partner', '2026-09-01 10:00')", (self.partner_id,),
            )
            self.plain_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Обычный Клиент', '', '', ?, 'neutral', '2026-09-01 10:00')",
                (self.PLAIN_TOKEN,),
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (self.plain_id,),
            )
            db.commit()

    def tearDown(self):
        super().tearDown()
        with application_module.app.app_context():
            db = application_module.get_db()
            for token in (self.PARTNER_TOKEN, self.PLAIN_TOKEN):
                db.execute(
                    "DELETE FROM client_segments WHERE client_id IN (SELECT id FROM clients WHERE token = ?)",
                    (token,),
                )
                db.execute("DELETE FROM clients WHERE token = ?", (token,))
            db.commit()

    @property
    def partner_key(self):
        return f"partner:{self.partner_id}"

    def rows(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? ORDER BY id", (self.item_id,)
            ).fetchall()]

    def assign_quietly(self, rows):
        with mock.patch.object(
            application_module, "send_telegram_notification_to_employee"
        ) as notifier:
            response = self.assign_many(rows)
        return response, notifier

class TuningTaskContractorTests(_ContractorFixture, unittest.TestCase):
    def test_a_task_can_be_given_to_a_partner(self):
        response, notifier = self.assign_quietly([(self.partner_key, 5000, 1)])
        self.assertEqual(response.status_code, 302)
        (row,) = self.rows()
        self.assertEqual(row["partner_id"], self.partner_id)
        self.assertEqual(row["employee_name"], "Мастерская Подряд")
        self.assertEqual((row["rate"], row["norm_hours"]), (5000, 1))
        self.assertEqual(row["assignment_status"], "accepted")
        notifier.assert_not_called()  # no Telegram for a contractor

    def test_employee_and_partner_can_share_one_work(self):
        self.assign_quietly([(self.EMPLOYEE_A, 1000, 2), (self.partner_key, 3000, 1)])
        by_name = {r["employee_name"]: r for r in self.rows()}
        self.assertIsNone(by_name[self.EMPLOYEE_A]["partner_id"])
        self.assertEqual(by_name["Мастерская Подряд"]["partner_id"], self.partner_id)

    def test_someone_who_is_not_a_tuning_partner_is_refused(self):
        self.assign_quietly([(f"partner:{self.plain_id}", 5000, 1), ("partner:999999", 1, 1), ("partner:x", 1, 1)])
        self.assertEqual(self.rows(), [])

    def test_the_same_partner_twice_gives_one_task(self):
        self.assign_quietly([(self.partner_key, 100, 1), (self.partner_key, 200, 1)])
        self.assertEqual(len(self.rows()), 1)

    def test_finishing_a_contractor_task_creates_no_payroll_entry(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        (row,) = self.rows()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/status", data={"status": "done"})
        (row,) = self.rows()
        self.assertEqual(row["assignment_status"], "done")
        self.assertIsNone(row["entry_id"])
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM entries WHERE employee = 'Мастерская Подряд'"
            ).fetchone())
            self.assertEqual(application_module._repair_tuning_assignment_payouts(db), [])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertNotIn("Нет выплаты", page)

    def test_the_contractor_cost_counts_against_the_work_budget(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        with application_module.app.app_context():
            total = application_module._tuning_item_assigned_labor_total(
                application_module.get_db(), self.item_id
            )
        self.assertEqual(total, 5000)

    def test_the_contractor_is_not_in_any_employees_task_list(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(  # an employee who happens to share the partner's name
                "UPDATE employees SET name = 'Мастерская Подряд' WHERE name = ?", (self.EMPLOYEE_A,)
            )
            db.execute(
                "UPDATE team_accounts SET employee_name = 'Мастерская Подряд' WHERE employee_name = ?",
                (self.EMPLOYEE_A,),
            )
            db.commit()
        try:
            self.login_team(self.USERNAME_A, "Мастерская Подряд")
            response = self.client.get("/team/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Полировка корпуса", response.get_data(as_text=True))
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("UPDATE employees SET name = ? WHERE name = 'Мастерская Подряд'", (self.EMPLOYEE_A,))
                db.execute(
                    "UPDATE team_accounts SET employee_name = ? WHERE employee_name = 'Мастерская Подряд'",
                    (self.EMPLOYEE_A,),
                )
                db.commit()

    def test_the_board_offers_partners_and_marks_a_contractor_task(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Подрядчики (партнёры тюнинга)", page)
        self.assertIn(f'value="partner:{self.partner_id}"', page)
        self.assertIn(">Подрядчик<", page)
        self.assertIn("оплата подрядчику вне зарплат", page)

    def test_the_new_task_form_offers_partners_too(self):
        self.login_admin()
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn(f'value="partner:{self.partner_id}"', page)

    def test_an_employee_task_can_be_moved_to_a_partner_and_back(self):
        self.assign_quietly([(self.EMPLOYEE_A, 1000, 2)])
        (row,) = self.rows()
        with application_module.app.app_context():
            db = application_module.get_db()
            task_id = db.execute(
                "INSERT INTO tuning_schedule_tasks (assignment_id, employee_name, title, rate, status, "
                "created_at) VALUES (?, ?, 'Полировка корпуса', 1000, 'pending', '2026-09-01 10:00')",
                (row["id"], self.EMPLOYEE_A),
            ).lastrowid
            db.commit()
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee") as notifier:
            self.client.post(f"/tuning/assignments/{row['id']}/employee", data={"employee_name": self.partner_key})
        (row,) = self.rows()
        self.assertEqual((row["partner_id"], row["employee_name"], row["assignment_status"]),
                         (self.partner_id, "Мастерская Подряд", "accepted"))
        self.assertEqual(notifier.call_count, 1)  # the employee is told it was taken away
        with application_module.app.app_context():
            self.assertIsNone(application_module.get_db().execute(
                "SELECT 1 FROM tuning_schedule_tasks WHERE id = ?", (task_id,)).fetchone())
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.client.post(f"/tuning/assignments/{row['id']}/employee", data={"employee_name": self.EMPLOYEE_B})
        (row,) = self.rows()
        self.assertEqual((row["partner_id"], row["employee_name"], row["assignment_status"]),
                         (None, self.EMPLOYEE_B, "pending"))

    def test_a_task_already_paid_to_an_employee_cannot_go_to_a_partner(self):
        self.assign_quietly([(self.EMPLOYEE_A, 1000, 2)])
        (row,) = self.rows()
        with application_module.app.app_context():
            db = application_module.get_db()
            entry_id = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, 'Полировка корпуса', 1000, 2, 2000, '2026-09-01', '2026-09-01 12:00')",
                (self.EMPLOYEE_A,),
            ).lastrowid
            db.execute("UPDATE tuning_item_assignments SET entry_id = ?, assignment_status = 'done' WHERE id = ?",
                       (entry_id, row["id"]))
            db.commit()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/employee", data={"employee_name": self.partner_key})
        (row,) = self.rows()
        self.assertIsNone(row["partner_id"])
        self.assertEqual(row["employee_name"], self.EMPLOYEE_A)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            db.commit()

    def test_a_renamed_partner_shows_under_the_new_name(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE clients SET client_name = 'Новое Название' WHERE id = ?", (self.partner_id,))
            db.commit()
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Новое Название", page)


class TuningTaskContractorCostTests(_ContractorFixture, unittest.TestCase):
    """The contractor's single cost is a project expense in Аналитика."""

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            self.project_id = db.execute(
                "INSERT INTO projects (name, tuning_order_id, created_at) VALUES ('Подряд', ?, "
                "'2026-09-01 10:00')", (self.order_id,),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (self.order_id,))
            db.commit()
        super().tearDown()

    def expense(self):
        with application_module.app.app_context():
            return application_module._project_totals(application_module.get_db(), self.project_id)[1]

    def test_the_cost_is_one_amount_whatever_hours_are_posted(self):
        self.assign_quietly([(self.partner_key, 5000, 7)])
        (row,) = self.rows()
        self.assertEqual((row["rate"], row["norm_hours"]), (5000, 1))

    def test_a_contractor_row_needs_no_hours_field_at_all(self):
        self.login_admin()
        self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/assign",
            data={"employee_name[]": self.partner_key, "rate[]": "4200", "comment": ""},
        )
        (row,) = self.rows()
        self.assertEqual((row["rate"], row["norm_hours"]), (4200, 1))

    def test_the_cost_is_added_to_the_project_expenses(self):
        before = self.expense()
        self.assign_quietly([(self.partner_key, 5000, 1), (self.EMPLOYEE_A, 1000, 2)])
        self.assertEqual(self.expense() - before, 5000)  # an employee's pay waits for "done"

    def test_the_cost_follows_the_task_edit_revoke_and_reject(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        (row,) = self.rows()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/rate", data={"rate": "6500"})
        self.assertEqual(self.expense(), 6500)
        self.client.post(f"/tuning/assignments/{row['id']}/rate", data={"rate": "0"})
        self.assertEqual(self.expense(), 6500)  # a contractor's cost can't be zero
        self.client.post(f"/tuning/assignments/{row['id']}/status", data={"status": "rejected"})
        self.assertEqual(self.expense(), 0)
        self.client.post(f"/tuning/assignments/{row['id']}/status", data={"status": "accepted"})
        self.assertEqual(self.expense(), 6500)
        self.client.post(f"/tuning/assignments/{row['id']}/revoke")
        self.assertEqual(self.expense(), 0)

    def test_moving_an_employee_task_to_a_partner_turns_pay_into_one_cost(self):
        self.assign_quietly([(self.EMPLOYEE_A, 1000, 3)])
        (row,) = self.rows()
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.client.post(f"/tuning/assignments/{row['id']}/employee", data={"employee_name": self.partner_key})
        (row,) = self.rows()
        self.assertEqual((row["rate"], row["norm_hours"]), (3000, 1))
        self.assertEqual(self.expense(), 3000)

    def test_the_project_page_lists_the_contractor_cost(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        self.login_admin()
        page = self.client.get(f"/analytics/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("Подрядчики", page)
        self.assertIn("Мастерская Подряд", page)
        self.assertIn("5", page)

    def test_the_projects_list_and_month_totals_count_it(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        self.login_admin()
        response = self.client.get("/analytics/projects")
        self.assertEqual(response.status_code, 200)
        with application_module.app.app_context():
            db = application_module.get_db()
            costs = application_module._project_contractor_costs(db, self.project_id)
        self.assertEqual([(c["partner_name"], c["amount"]) for c in costs], [("Мастерская Подряд", 5000)])

    def test_the_item_profitability_subtracts_the_contractor(self):
        self.assign_quietly([(self.partner_key, 500, 1)])
        with application_module.app.app_context():
            (row,) = application_module._item_profitability(application_module.get_db(), self.order_id)
        self.assertEqual(row["contractor_expense"], 500)
        self.assertEqual(row["profit"], row["price"] - 500)

    def test_the_board_shows_one_cost_field_for_a_contractor(self):
        self.assign_quietly([(self.partner_key, 5000, 1)])
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn('aria-label="Стоимость, ₽"', page)
        self.assertIn("Расход проекта: 5", page)
        self.assertIn("syncAssignRowKind", page)


class TuningTaskContractorLocationTests(_ContractorFixture, unittest.TestCase):
    """"На территории подрядчика": the boat is off the shop map for the
    task's period and back on the date the period ends."""

    def setUp(self):
        super().setUp()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO shop_map_boats (order_id, x_m, y_m, created_at, updated_at) "
                "VALUES (?, 1, 1, 'x', 'x')", (self.order_id,),
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM shop_map_boats WHERE order_id = ?", (self.order_id,))
            db.commit()
        super().tearDown()

    def in_shop(self, day):
        with application_module.app.app_context():
            boats = application_module._shop_map_boats(application_module.get_db(), on_date=day)
        return self.order_id in [b["order_id"] for b in boats]

    def give(self, location, due_from="2030-05-10", due_to="2030-05-14", comment=""):
        self.login_admin()
        data = {"employee_name[]": self.partner_key, "rate[]": "5000", "comment": comment,
                "partner_location[]": location, "due_from": due_from or "", "due_to": due_to or ""}
        return self.client.post(f"/tuning/{self.order_id}/item/{self.item_id}/assign", data=data)

    def test_the_boat_leaves_the_map_for_the_period_and_is_back_on_its_last_day(self):
        self.give("partner")
        (row,) = self.rows()
        self.assertEqual(row["at_partner"], 1)
        self.assertTrue(self.in_shop("2030-05-09"))
        for day in ("2030-05-10", "2030-05-11", "2030-05-13"):
            self.assertFalse(self.in_shop(day), day)
        self.assertTrue(self.in_shop("2030-05-14"))
        self.assertTrue(self.in_shop("2030-05-20"))

    def test_on_our_territory_the_boat_stays_on_the_map(self):
        self.give("own")
        (row,) = self.rows()
        self.assertEqual(row["at_partner"], 0)
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_a_one_day_task_takes_the_boat_away_for_that_day_only(self):
        self.give("partner", due_from="2030-05-10", due_to="")
        self.assertTrue(self.in_shop("2030-05-09"))
        self.assertFalse(self.in_shop("2030-05-10"))
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_the_boat_returns_on_the_real_completion_date_when_the_task_is_done(self):
        self.give("partner")
        (row,) = self.rows()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/status", data={"status": "done"})
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_item_assignments SET completed_at = '2030-05-12 15:00' WHERE id = ?",
                       (row["id"],))
            db.commit()
        self.assertFalse(self.in_shop("2030-05-11"))
        self.assertTrue(self.in_shop("2030-05-12"))

    def test_a_rejected_task_does_not_take_the_boat_away(self):
        self.give("partner")
        (row,) = self.rows()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/status", data={"status": "rejected"})
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_revoking_the_task_brings_the_boat_back(self):
        self.give("partner")
        (row,) = self.rows()
        self.login_admin()
        self.client.post(f"/tuning/assignments/{row['id']}/revoke")
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_the_contractor_premises_need_a_due_date(self):
        response = self.give("partner", due_from="", due_to="")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("укажите срок исполнения", page)

    def test_the_location_of_an_existing_task_can_be_switched_on_the_board(self):
        self.give("own")
        (row,) = self.rows()
        self.client.post(f"/tuning/assignments/{row['id']}/location", data={"location": "partner"})
        self.assertFalse(self.in_shop("2030-05-11"))
        self.client.post(f"/tuning/assignments/{row['id']}/location", data={"location": "own"})
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_switching_to_the_contractor_premises_without_a_due_date_is_refused(self):
        self.give("own", due_from="", due_to="")
        (row,) = self.rows()
        self.client.post(f"/tuning/assignments/{row['id']}/location", data={"location": "partner"})
        (row,) = self.rows()
        self.assertEqual(row["at_partner"], 0)

    def test_an_employee_task_ignores_the_location(self):
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/assign",
                data={"employee_name[]": self.EMPLOYEE_A, "rate[]": "100", "norm_hours[]": "1",
                      "partner_location[]": "partner", "due_from": "2030-05-10", "due_to": "2030-05-14"},
            )
        (row,) = self.rows()
        self.assertEqual(row["at_partner"], 0)
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_giving_the_task_to_an_employee_clears_the_location(self):
        self.give("partner")
        (row,) = self.rows()
        self.login_admin()
        with mock.patch.object(application_module, "send_telegram_notification_to_employee"):
            self.client.post(f"/tuning/assignments/{row['id']}/employee", data={"employee_name": self.EMPLOYEE_A})
        (row,) = self.rows()
        self.assertEqual((row["partner_id"], row["at_partner"]), (None, 0))
        self.assertTrue(self.in_shop("2030-05-11"))

    def test_the_map_page_lists_the_boat_at_the_contractor_and_events(self):
        self.give("partner")
        self.login_admin()
        away = self.client.get("/tuning/shop-map?date=2030-05-11").get_data(as_text=True)
        self.assertIn("На территории подрядчика «Мастерская Подряд»", away)
        self.assertIn("вернётся", away)
        leaves = self.client.get("/tuning/shop-map?date=2030-05-10").get_data(as_text=True)
        self.assertIn("Уезжает к подрядчику", leaves)
        back = self.client.get("/tuning/shop-map?date=2030-05-14").get_data(as_text=True)
        self.assertIn("Возвращается от подрядчика", back)
        self.assertNotIn("На территории подрядчика «", back)

    def test_the_board_offers_the_location_choice_for_a_contractor_task(self):
        self.give("partner")
        self.login_admin()
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("На территории подрядчика", page)
        self.assertIn(f"/tuning/assignments/{self.rows()[0]['id']}/location", page)
        self.assertIn("syncAssignDueRequired", page)


if __name__ == "__main__":
    unittest.main()
