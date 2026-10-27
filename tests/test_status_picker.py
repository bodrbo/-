import re
import unittest

from flask import render_template_string
from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "status-picker-test"
TOKEN = "status-picker-token"


class StatusPickerMacroTests(unittest.TestCase):
    """The clickable status: the badge is the control, no separate column."""

    def render(self, **kwargs):
        template = (
            "{% from '_status_picker.html' import status_picker %}"
            "{{ status_picker('/set', current, options, **extra) }}"
        )
        options = kwargs.pop("options", [
            {"value": "new", "label": "Новая"}, {"value": "done", "label": "Готово"},
            {"value": "old", "label": "Старая", "disabled": True},
        ])
        with application_module.app.test_request_context():
            return render_template_string(template, current=kwargs.pop("current", "new"),
                                          options=options, extra=kwargs)

    def test_the_badge_is_a_button_that_shows_the_current_status(self):
        html = self.render()
        self.assertIn('class="status-badge status-new status-picker-trigger"', html)
        self.assertIn('<span class="status-picker-label">Новая</span>', html)
        self.assertIn('name="status" value="new" data-status-value', html)
        self.assertIn('action="/set"', html)

    def test_every_status_is_offered_and_the_current_one_is_marked(self):
        html = self.render()
        for value, label in (("new", "Новая"), ("done", "Готово"), ("old", "Старая")):
            self.assertIn(f'data-value="{value}"', html)
            self.assertIn(f'<span class="status-badge status-{value}">{label}</span>', html)
        self.assertIn('class="status-picker-option is-current"', html)
        self.assertRegex(html, r'data-value="old"[^>]*disabled')

    def test_the_colour_prefix_extra_fields_and_next_are_used(self):
        html = self.render(prefix="status-defect-", extra={"boat_index": 3}, next_url="/tuning?q=x", name="state")
        self.assertIn("status-defect-new", html)
        self.assertIn('<input type="hidden" name="boat_index" value="3">', html)
        self.assertIn('<input type="hidden" name="next" value="/tuning?q=x">', html)
        self.assertIn('name="state" value="new" data-status-value', html)

    def test_an_unknown_current_value_still_renders(self):
        html = self.render(current="mystery")
        self.assertIn("mystery", html)

    def test_the_script_is_served_with_mouse_touch_and_keyboard_support(self):
        script = application_module.app.test_client().get("/static/status-picker.js").get_data(as_text=True)
        for marker in ("data-status-trigger", "requestSubmit", "Escape", "ArrowDown", 'position = "fixed"'):
            self.assertIn(marker, script)


class StatusPickerPagesTests(unittest.TestCase):
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
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, 'Salute', '', ?, 'neutral', '2026-09-01 10:00')", (CLIENT, TOKEN)).lastrowid
            db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                       "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (self.client_id,))
            db.commit()
        data = MultiDict([
            ("client_name", CLIENT), ("client_id", str(self.client_id)), ("equipment_type", "boat"),
            ("boat_model", "Salute"), ("boat_registration_number", ""), ("motor_model", ""),
            ("motor_serial_number", ""), ("phone", ""), ("order_date", "2026-09-10"),
            ("sale_channel", "direct"), ("discount_type", "percent"), ("discount_value", "0"),
            ("work_name[]", "Полировка"), ("cost_price[]", "100"), ("multiplier[]", "2"),
            ("item_discount_value[]", ""), ("item_discount_type[]", "percent"), ("item_id[]", ""),
        ])
        self.client.post("/tuning/add", data=data)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.order_id = db.execute(
                "SELECT id FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1", (CLIENT,)).fetchone()["id"]
            self.item_id = db.execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ?", (self.order_id,)).fetchone()["id"]
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        ids = [r["id"] for r in db.execute("SELECT id FROM tuning_orders WHERE client_name = ?", (CLIENT,)).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        row = db.execute("SELECT id FROM clients WHERE token = ?", (TOKEN,)).fetchone()
        if row:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.execute("DELETE FROM supply_requests WHERE employee_name = 'status-picker-test'")
        db.commit()

    def order_status(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT status FROM tuning_orders WHERE id = ?", (self.order_id,)).fetchone()["status"]

    # ---- the pages

    def test_the_order_page_has_clickable_order_and_work_statuses_and_no_selects(self):
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertEqual(page.count("data-status-picker-form"), 2)          # the order and its work
        self.assertIn(f'/tuning/{self.order_id}/status', page)
        self.assertIn(f'/tuning/{self.order_id}/item/{self.item_id}/status', page)
        self.assertNotIn('class="status-select-form tuning-order-status-select"', page)
        self.assertIn("status-picker.js", page)

    def test_the_orders_list_status_is_clickable_and_comes_back_to_the_list(self):
        page = self.client.get("/tuning?q=status-picker").get_data(as_text=True)
        self.assertIn(f'/tuning/{self.order_id}/status', page)
        self.assertIn('data-status-trigger', page)
        self.assertRegex(page, r'<input type="hidden" name="next" value="/tuning\?q=status-picker">')
        response = self.client.post(f"/tuning/{self.order_id}/status",
                                    data={"status": "approved", "next": "/tuning?q=status-picker"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/tuning?q=status-picker")
        self.assertEqual(self.order_status(), "approved")

    def test_a_status_change_without_next_still_returns_to_the_order(self):
        response = self.client.post(f"/tuning/{self.order_id}/status", data={"status": "in_progress"})
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/edit/{self.order_id}"))

    def test_a_next_that_leaves_the_site_is_ignored(self):
        for bad in ("//evil.example/x", "https://evil.example", "javascript:alert(1)", "/ok\\evil", "evil"):
            response = self.client.post(f"/tuning/{self.order_id}/status", data={"status": "approved", "next": bad})
            self.assertTrue(response.headers["Location"].endswith(f"/tuning/edit/{self.order_id}"), bad)

    def test_the_board_work_status_is_clickable_and_returns_to_the_board(self):
        board = self.client.get(f"/tuning/{self.order_id}/board").get_data(as_text=True)
        self.assertIn(f'/tuning/{self.order_id}/item/{self.item_id}/status', board)
        self.assertIn(f'name="next" value="/tuning/{self.order_id}/board#board-work-{self.item_id}"', board)
        response = self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/status",
            data={"status": "in_progress", "next": f"/tuning/{self.order_id}/board#board-work-{self.item_id}"})
        self.assertEqual(response.headers["Location"], f"/tuning/{self.order_id}/board#board-work-{self.item_id}")
        with application_module.app.app_context():
            status = application_module.get_db().execute(
                "SELECT status FROM tuning_order_items WHERE id = ?", (self.item_id,)).fetchone()["status"]
        self.assertEqual(status, "in_progress")

    def test_the_supply_request_status_is_the_button_and_the_extra_column_is_gone(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO supply_requests (employee_name, status, created_at) "
                       "VALUES ('status-picker-test', 'new', '2026-10-01 10:00')")
            db.commit()
        page = self.client.get("/supply/requests").get_data(as_text=True)
        self.assertNotIn("Изменить статус\n", page.split('<div id="request-status-modal"')[0])
        self.assertRegex(page, r'<button type="button" class="status-badge status-supplyreq-new status-picker-trigger"')
        self.assertEqual(page.split('<div id="request-status-modal"')[0].count("<th>"), 7)

    def test_the_dealer_pages_use_the_clickable_status(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute(
                "INSERT INTO dealer_orders (client_name, status, order_date, created_at, updated_at) "
                "VALUES (?, 'new_request', '2026-10-10', '2026-10-10 10:00', '2026-10-10 10:00')", (CLIENT,)).lastrowid
            db.commit()
        try:
            listing = self.client.get("/dealer/orders").get_data(as_text=True)
            self.assertIn(f"/dealer/orders/{order_id}/status", listing)
            form = self.client.get(f"/dealer/orders/{order_id}").get_data(as_text=True)
            self.assertIn("data-status-picker-form", form)
            self.assertNotIn("Сменить статус", form)
            response = self.client.post(f"/dealer/orders/{order_id}/status",
                                        data={"status": "approved", "next": "/dealer/orders?status=new_request"})
            self.assertEqual(response.headers["Location"], "/dealer/orders?status=new_request")
            evil = self.client.post(f"/dealer/orders/{order_id}/status", data={"status": "ready", "next": "//evil.example"})
            self.assertTrue(evil.headers["Location"].endswith(f"/dealer/orders/{order_id}"))
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM dealer_orders WHERE id = ?", (order_id,))
                db.commit()


if __name__ == "__main__":
    unittest.main()
