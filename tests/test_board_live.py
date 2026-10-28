import re
import unittest

from support import application_module

CLIENT = "board-live-test"


class BoardLiveTests(unittest.TestCase):
    """The task board saves in the background: its forms are posted by
    board-live.js and the answer (the board itself) replaces only what changed,
    so the page pieces it relies on must be there."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())
            db = application_module.get_db()
            now = "2026-10-01 10:00"
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, phone, subtotal, "
                "total, status, order_date, created_at, updated_at) "
                "VALUES (?, 'boat', 'Salute', 'direct', '', 0, 0, 'in_progress', '2026-10-10', ?, ?)",
                (CLIENT, now, now)).lastrowid
            self.items = []
            for position, name in enumerate(("Первая работа", "Вторая работа"), 1):
                item_id = db.execute(
                    "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, status, position) "
                    "VALUES (?, ?, 1000, 2, 2000, 'in_progress', ?)", (self.order_id, name, position)).lastrowid
                self.items.append(item_id)
            self.assignment_id = db.execute(
                "INSERT INTO tuning_item_assignments (item_id, employee_name, rate, norm_hours, comment, "
                "assignment_status, assigned_at) VALUES (?, 'Иван Иванов', 300, 2, '', 'accepted', ?)",
                (self.items[0], now)).lastrowid
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        ids = [r["id"] for r in db.execute("SELECT id FROM tuning_orders WHERE client_name = ?", (CLIENT,)).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.commit()

    def board(self):
        return self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)

    def test_every_column_is_addressable_and_the_messages_have_one_container(self):
        page = self.board()
        for item_id in self.items:
            self.assertIn(f'<section class="board-column" role="listitem" data-item-id="{item_id}">', page)
        self.assertEqual(page.count('id="board-alerts"'), 1)
        self.assertIn('class="panel board-order-summary"', page)
        self.assertIn("board-live.js", page)

    def test_the_form_errors_are_marked_apart_from_warnings(self):
        with self.client.session_transaction() as session:
            session["tuning_assign_error"] = "Не удалось поручить"
        page = self.board()
        self.assertRegex(page, r'<div class="alert" role="alert" data-form-error>Не удалось поручить</div>')

    def test_a_background_post_gets_the_updated_board_back(self):
        response = self.client.post(
            f"/tuning/assignments/{self.assignment_id}/status", data={"status": "in_progress"},
            headers={"X-Requested-With": "XMLHttpRequest"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('tuning-board-page"', page)
        self.assertIn(f'name="status" value="in_progress" data-status-value', page)
        # the notice of the change is rendered in the same answer, in the swapped container
        self.assertIn('id="board-alerts"', page)

    def test_a_task_comment_and_dates_post_back_to_the_board(self):
        for path, data in (
            (f"/tuning/assignments/{self.assignment_id}/comment", {"comment": "К пятнице"}),
            (f"/tuning/assignments/{self.assignment_id}/dates", {"due_from": "2026-11-01", "due_to": ""}),
        ):
            response = self.client.post(path, data=data, follow_redirects=True)
            self.assertIn('tuning-board-page"', response.get_data(as_text=True), path)
        self.assertIn("К пятнице", self.board())

    def test_the_status_picker_knows_its_colour_prefix_for_the_instant_update(self):
        page = self.board()
        self.assertIn('data-status-prefix="status-"', page)

    def test_the_script_posts_in_the_background_and_guards_the_exit(self):
        script = self.client.get("/static/board-live.js").get_data(as_text=True)
        for marker in (
            'page.addEventListener("submit"', "X-Requested-With", "DOMParser", "data-item-id",
            "beforeunload", "data-form-error", "collectDirty", "reconcile",
        ):
            self.assertIn(marker, script)


if __name__ == "__main__":
    unittest.main()
