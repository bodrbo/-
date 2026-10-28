import re
import unittest

from flask import render_template_string
from werkzeug.datastructures import MultiDict

from modules.fleet.schema import boats_for_db
from support import application_module

CLIENT = "live-status-test"
TOKEN = "live-status-token"


def script_order(page, first, second):
    return page.index(first) < page.index(second)


class LiveStatusScriptTests(unittest.TestCase):
    """A status is shown at once and saved in the background (no reload)."""

    def script(self, name):
        response = application_module.app.test_client().get(f"/static/{name}")
        text = response.get_data(as_text=True)
        response.close()
        return text

    def test_the_background_saver_queues_guards_the_exit_and_reports_failures(self):
        script = self.script("live-save.js")
        for marker in ("X-Requested-With", "beforeunload", "data-live-save", "requestSubmit",
                       "window.LiveSave", "reread", "expired"):
            self.assertIn(marker, script)

    def test_the_picker_saves_through_the_saver_and_falls_back_to_a_plain_submit(self):
        script = self.script("status-picker.js")
        self.assertIn("window.LiveSave", script)
        self.assertIn("data-live-refresh", script)
        self.assertIn("tuning-board-page", script)   # the task board has its own saver
        self.assertIn("requestSubmit", script)

    def test_the_macro_passes_the_dependent_blocks_on(self):
        template = ("{% from '_status_picker.html' import status_picker %}"
                    "{{ status_picker('/set', 'new', [{'value': 'new', 'label': 'Новая'}], **extra) }}")
        with application_module.app.test_request_context():
            with_blocks = render_template_string(template, extra={"refresh": "#a|#b"})
            without = render_template_string(template, extra={})
        self.assertIn('data-live-refresh="#a|#b"', with_blocks)
        self.assertNotIn("data-live-refresh", without)


class LiveStatusPagesTests(unittest.TestCase):
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
        db.execute("DELETE FROM supply_requests WHERE employee_name = 'live-status-test'")
        db.commit()

    def get(self, path):
        return self.client.get(path).get_data(as_text=True)

    def assert_saver_first(self, page):
        self.assertIn("live-save.js", page)
        self.assertTrue(script_order(page, "live-save.js", "status-picker.js"))

    def assert_blocks_exist(self, page):
        """every block a status asks to re-read is on the page"""
        wanted = re.findall(r'data-live-refresh="([^"]+)"', page)
        self.assertTrue(wanted)
        for spec in wanted:
            for selector in spec.split("|"):
                self.assertIn(f'id="{selector[1:]}"', page, selector)

    def test_the_order_page_rereads_the_work_row_and_the_totals(self):
        page = self.get(f"/tuning/edit/{self.order_id}")
        self.assert_saver_first(page)
        self.assertIn(f'data-live-refresh="#tuning-item-row-{self.item_id}|#tuning-hero"', page)
        self.assert_blocks_exist(page)

    def test_the_orders_list_and_the_clients_directory_save_in_the_background(self):
        self.assert_saver_first(self.get("/tuning?q=live-status"))
        clients = self.get("/admin/clients?q=live-status")
        self.assert_saver_first(clients)
        self.assertRegex(clients, rf'<form action="/admin/clients/{self.client_id}/status"[^>]*data-status-picker-form')
        self.assertIn('<span class="status-picker-label">Нейтральный</span>', clients)
        self.assertIn('name="section"', clients)   # the extra fields travel with the form

    def test_the_task_board_keeps_its_own_saver(self):
        board = self.get(f"/tuning/{self.order_id}/board")
        self.assertIn("board-live.js", board)
        self.assertNotIn("live-save.js", board)

    def test_the_defect_lists_and_counters_are_reread_after_a_status_change(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            boat = boats_for_db(db)[0]["name"]
            defect_id = db.execute(
                "INSERT INTO boat_defects (boat, description, employee_name, status, reported_at, updated_at) "
                "VALUES (?, 'live-status-defect', 'Иван', 'new', '2026-10-01 10:00', '2026-10-01 10:00')", (boat,)).lastrowid
            db.commit()
        try:
            page = self.get("/fleet/0")
            self.assert_saver_first(page)
            self.assertIn('id="fleet-defects-current"', page)
            self.assertIn('id="fleet-defects-current-count"', page)
            self.assertIn('id="fleet-archive-body"', page)
            self.assertIn('id="fleet-archive-count"', page)
            self.assert_blocks_exist(page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM boat_defects WHERE id = ?", (defect_id,))
                db.commit()

    def test_the_defect_plan_steps_are_sent_by_the_page_in_the_background(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            boat = boats_for_db(db)[0]["name"]
            defect_id = db.execute(
                "INSERT INTO boat_defects (boat, description, employee_name, status, reported_at, updated_at) "
                "VALUES (?, 'live-status-plan', 'Иван', 'new', '2026-10-01 10:00', '2026-10-01 10:00')", (boat,)).lastrowid
            db.execute(
                "INSERT INTO defect_work_plan_items (defect_id, description, status, created_at, updated_at) "
                "VALUES (?, 'Шаг', 'pending', '2026-10-01 10:00', '2026-10-01 10:00')", (defect_id,))
            db.commit()
        try:
            page = self.get(f"/fleet/0/defects/{defect_id}")
            self.assertIn("live-save.js", page)
            self.assertRegex(page, r'<form action="[^"]*" method="post" data-live-save>')
            self.assertIn('id="defect-plan-list"', page)
            self.assertIn('id="defect-plan-progress"', page)
            self.assertIn("data-label-done=", page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM defect_work_plan_items WHERE defect_id = ?", (defect_id,))
                db.execute("DELETE FROM boat_defects WHERE id = ?", (defect_id,))
                db.commit()

    def test_the_supply_request_modal_updates_its_row_and_saves_in_the_background(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            request_id = db.execute(
                "INSERT INTO supply_requests (employee_name, status, created_at) "
                "VALUES ('live-status-test', 'new', '2026-10-01 10:00')").lastrowid
            db.commit()
        page = self.get("/supply/requests")
        self.assertIn(f'<tr id="supply-request-row-{request_id}">', page)
        self.assertRegex(page, r'<form id="request-status-form" method="post" class="assign-modal-card" data-live-save>')
        self.assertIn("live-save.js", page)
        self.assertIn("LiveSave.send", page)

    def test_software_requests_reread_the_counters(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            request_id = db.execute(
                "INSERT INTO software_requests (author_type, author_admin_id, author_name, description, page_path, "
                "status, created_at, updated_at) VALUES ('admin', 1, 'live-status-test', 'x', '/', 'new', "
                "'2026-10-01 10:00', '2026-10-01 10:00')").lastrowid
            db.commit()
        try:
            page = self.get("/settings/software-requests")
            self.assert_saver_first(page)
            self.assertIn('data-live-refresh=".software-request-filters|.software-requests-total"', page)
            self.assertIn('class="software-request-filters"', page)
            self.assertIn("software-requests-total", page)
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM software_requests WHERE id = ?", (request_id,))
                db.commit()

    def test_the_dealer_pages_use_the_background_saver(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute(
                "INSERT INTO dealer_orders (client_name, status, order_date, created_at, updated_at) "
                "VALUES (?, 'new_request', '2026-10-10', '2026-10-10 10:00', '2026-10-10 10:00')", (CLIENT,)).lastrowid
            db.commit()
        try:
            self.assert_saver_first(self.get("/dealer/orders"))
            self.assert_saver_first(self.get(f"/dealer/orders/{order_id}"))
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("DELETE FROM dealer_orders WHERE id = ?", (order_id,))
                db.commit()

    def test_a_background_status_change_answers_with_the_page_it_redirects_to(self):
        """the saver reads the redirect target to learn what was really stored"""
        response = self.client.post(
            f"/tuning/{self.order_id}/status", data={"status": "approved", "next": "/tuning?q=live-status"},
            headers={"X-Requested-With": "XMLHttpRequest"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertRegex(page, rf'<form action="/tuning/{self.order_id}/status"[^>]*data-status-picker-form')
        self.assertRegex(page, r'name="status" value="approved" data-status-value')


if __name__ == "__main__":
    unittest.main()
