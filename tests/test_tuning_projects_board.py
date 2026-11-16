import datetime as dt
import re
import unittest

from support import application_module


class TuningProjectsBoardTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            self._clear(application_module.get_db())
        self.addCleanup(self._cleanup)

    @staticmethod
    def _clear(db):
        db.execute("DELETE FROM task_notification_deliveries WHERE assignment_type = 'tuning'")
        db.execute("DELETE FROM tuning_item_assignments")
        db.execute("DELETE FROM tuning_order_items")
        db.execute("DELETE FROM tuning_payments")
        db.execute("DELETE FROM projects WHERE tuning_order_id IS NOT NULL")
        db.execute("DELETE FROM tuning_orders")
        db.commit()

    def _cleanup(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    @staticmethod
    def add_order(db, status, boat_model="", *, client_name="Клиент", total=0,
                  deadline=None, equipment_type="boat", motor_model="", source="manual"):
        return db.execute(
            "INSERT INTO tuning_orders "
            "(client_name, equipment_type, boat_model, motor_model, sale_channel, phone, "
            "subtotal, total, status, order_date, deadline_date, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'direct', '', ?, ?, ?, '2026-11-01', ?, ?, "
            "'2026-11-01 10:00', '2026-11-01 10:00')",
            (client_name, equipment_type, boat_model, motor_model, total, total, status,
             deadline, source),
        ).lastrowid

    @staticmethod
    def add_item(db, order_id, status="approved", price_pending=0):
        return db.execute(
            "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
            "price_pending, status) VALUES (?, 'Работа', 0, 1, 1000, ?, ?)",
            (order_id, price_pending, status),
        ).lastrowid

    @staticmethod
    def add_assignment(db, item_id, employee, status):
        db.execute(
            "INSERT INTO tuning_item_assignments (item_id, employee_name, rate, norm_hours, "
            "comment, assignment_status, assigned_at) VALUES (?, ?, 0, 0, '', ?, '2026-11-02 10:00')",
            (item_id, employee, status),
        )

    def column_html(self, html, status):
        match = re.search(
            rf'<section class="projects-board-column" data-status="{status}".*?</section>',
            html, re.S,
        )
        self.assertIsNotNone(match, status)
        return match.group(0)

    def test_orders_are_split_into_stage_columns(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.add_order(db, "approved", "Лодка согласована")
            self.add_order(db, "in_progress", "Лодка в работе")
            self.add_order(db, "qc", "Лодка на контроле")
            self.add_order(db, "done", "Лодка ждёт выдачи")
            self.add_order(db, "handed_over", "Лодка передана")
            self.add_order(db, "cancelled", "Лодка отменена")
            self.add_order(db, "new_request", "Лодка новая заявка")
            self.add_order(db, "estimate", "Лодка на согласовании")
            self.add_order(db, "in_progress", "Лодка субподряда", source="subcontract_request")
            db.commit()

        self.login()
        response = self.client.get("/tuning/board")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Лодка согласована", self.column_html(html, "approved"))
        self.assertIn("Лодка в работе", self.column_html(html, "in_progress"))
        self.assertIn("Лодка на контроле", self.column_html(html, "qc"))
        self.assertIn("Лодка ждёт выдачи", self.column_html(html, "done"))
        for hidden in ("Лодка передана", "Лодка отменена", "Лодка новая заявка",
                       "Лодка на согласовании", "Лодка субподряда"):
            self.assertNotIn(hidden, html)
        self.assertIn("До начала работ: 2", html)
        self.assertIn('class="active">Доска проектов</a>', html)

    def test_card_links_to_order_task_board_and_shows_progress(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = self.add_order(db, "in_progress", "Лодка прогресса")
            done_item = self.add_item(db, order_id, "done")
            working_item = self.add_item(db, order_id, "in_progress")
            self.add_item(db, order_id, "removed")
            self.add_assignment(db, done_item, "Олег", "done")
            self.add_assignment(db, working_item, "Миша", "in_progress")
            db.commit()

        self.login()
        html = self.client.get("/tuning/board").get_data(as_text=True)

        self.assertIn(f'href="/tuning/{order_id}/board"', html)
        self.assertIn("Работы: 1/2 · Миша", html)
        self.assertIn('aria-valuenow="50"', html)
        self.assertIn(f'data-status-url="/tuning/{order_id}/status"', html)

    def test_attention_flags_and_counters(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        with application_module.app.app_context():
            db = application_module.get_db()
            overdue = self.add_order(db, "in_progress", "Просроченная лодка", deadline=yesterday)
            rejected_item = self.add_item(db, overdue, "in_progress")
            self.add_assignment(db, rejected_item, "Олег", "rejected")
            pending = self.add_order(db, "approved", "Лодка без цены")
            self.add_item(db, pending, "pending", price_pending=1)
            unpaid = self.add_order(db, "done", "Неоплаченная лодка", total=30000)
            db.execute(
                "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) "
                "VALUES (?, 10000, '2026-11-02', '2026-11-02 10:00')",
                (unpaid,),
            )
            paid = self.add_order(db, "done", "Оплаченная лодка", total=5000)
            db.execute(
                "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) "
                "VALUES (?, 5000, '2026-11-02', '2026-11-02 10:00')",
                (paid,),
            )
            db.commit()

        self.login()
        html = self.client.get("/tuning/board").get_data(as_text=True)

        def chip_count(flag):
            match = re.search(
                rf'data-flag="{flag}".*?<strong>(\d+)</strong>', html, re.S
            )
            self.assertIsNotNone(match, flag)
            return int(match.group(1))

        self.assertEqual(chip_count("overdue"), 1)
        self.assertEqual(chip_count("rejected"), 1)
        # The rejected task left its work without anyone on it.
        self.assertEqual(chip_count("unassigned"), 1)
        self.assertEqual(chip_count("pending_price"), 1)
        self.assertEqual(chip_count("unpaid"), 1)
        self.assertIn("Отклонено задач: 1", html)
        self.assertIn("Без исполнителя: 1", html)
        self.assertIn("Ждёт расчёта: 1", html)
        self.assertIn("Не оплачено 20 000 ₽", html)
        self.assertRegex(html, r'projects-board-card is-overdue"[^>]*data-flags="overdue rejected unassigned"')

    def test_motor_and_trailer_cards_show_their_model(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.add_order(db, "in_progress", equipment_type="motor", motor_model="Мотор M-90")
            self.add_order(db, "in_progress", "Прицеп П-2", equipment_type="trailer")
            db.commit()

        self.login()
        html = self.client.get("/tuning/board").get_data(as_text=True)

        self.assertIn("Мотор · №", html)
        self.assertIn("Мотор M-90", html)
        self.assertIn("Прицеп · №", html)
        self.assertIn("Прицеп П-2", html)

    def test_order_board_links_back_to_projects_board(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = self.add_order(db, "in_progress", "Лодка доски")
            sub_id = self.add_order(db, "in_progress", "Лодка субподряда", source="subcontract_request")
            db.commit()

        self.login()
        html = self.client.get(f"/tuning/{order_id}/board").get_data(as_text=True)
        self.assertIn('href="/tuning/board" class="term board-back-link">← Доска проектов</a>', html)
        self.assertIn(f'Заказ №{order_id}</a>', html)
        sub_html = self.client.get(f"/tuning/{sub_id}/board").get_data(as_text=True)
        self.assertNotIn("← Доска проектов", sub_html)

    def test_status_change_from_board_returns_to_board(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = self.add_order(db, "approved", "Лодка для перетаскивания")
            db.commit()

        self.login()
        response = self.client.post(
            f"/tuning/{order_id}/status",
            data={"status": "in_progress", "next": "/tuning/board"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/tuning/board"))
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()[0]
        self.assertEqual(status, "in_progress")

    def test_empty_board(self):
        self.login()
        html = self.client.get("/tuning/board").get_data(as_text=True)
        self.assertIn("Сейчас нет заказов в работе", html)


if __name__ == "__main__":
    unittest.main()
