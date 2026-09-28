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

    def test_update_assignment_terms_is_blocked_once_paid(self):
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
            db.execute(
                "UPDATE tuning_item_assignments SET entry_id = ? WHERE id = ?",
                (entry.lastrowid, assignment_id),
            )
            db.commit()

        self.login_admin()
        response = self.client.post(
            f"/tuning/assignments/{assignment_id}/rate",
            data={"rate": "999", "norm_hours": "9"},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT rate, norm_hours FROM tuning_item_assignments WHERE id = ?",
                (assignment_id,),
            ).fetchone()
        self.assertEqual(row["rate"], 100)
        self.assertEqual(row["norm_hours"], 2)

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

    def test_dates_are_stamped_automatically(self):
        today = application_module.dt.date.today().isoformat()
        self.assign(self.EMPLOYEE_A, 100, 2)
        task = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(task["assigned_at"][:10], today)
        self.assertIsNone(task["completed_at"])
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("Поручена", page)
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

    def edit_dates(self, task_id, assigned="", completed=""):
        self.login_admin()
        return self.client.post(
            f"/tuning/assignments/{task_id}/dates",
            data={"assigned_date": assigned, "completed_date": completed},
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
        response = self.edit_dates(task_id, "2026-08-28", "2026-09-05")
        self.assertEqual(response.status_code, 302)
        task = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(task["assigned_at"], "2026-08-28 10:30")
        self.assertEqual(task["completed_at"], "2026-09-05 17:45")
        self.assertIn("Даты задачи обновлены", self.client.get(
            f"/tuning/{self.order_id}/board").get_data(as_text=True))
        # the completion date can be cleared on a done task
        self.edit_dates(task_id, "2026-08-28", "")
        self.assertIsNone(self.assignment(self.EMPLOYEE_A)["completed_at"])

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
        self.edit_dates(task_id, "2026-09-10", "2026-09-05")  # completed before assigned
        after = self.assignment(self.EMPLOYEE_A)
        self.assertEqual((after["assigned_at"], after["completed_at"]),
                         (before["assigned_at"], before["completed_at"]))
        page = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn("не может быть раньше", page)

    def test_a_task_that_is_not_done_has_no_completion_date_to_edit(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        task_id = self.assignment(self.EMPLOYEE_A)["id"]
        self.edit_dates(task_id, "2026-08-20", "2026-09-05")
        task = self.assignment(self.EMPLOYEE_A)
        self.assertEqual(task["assigned_at"][:10], "2026-08-20")
        self.assertIsNone(task["completed_at"])

    def test_team_dashboard_shows_the_dates(self):
        self.assign(self.EMPLOYEE_A, 100, 2)
        self.login_team(self.USERNAME_A, self.EMPLOYEE_A)
        page = self.client.get("/team/").get_data(as_text=True)
        self.assertIn("Поручена:", page)


if __name__ == "__main__":
    unittest.main()
