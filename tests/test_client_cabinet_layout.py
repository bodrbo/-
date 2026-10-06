import pathlib
import unittest

from support import application_module


class ClientCabinetLayoutTests(unittest.TestCase):
    """Личные кабинеты клиента и партнёра: на всю ширину и без блоков внутри
    блоков (карточки заказов — единственные блоки в списке заказов)."""

    CLIENT_TOKEN = "cabinet-layout-client-test"
    PARTNER_TOKEN = "cabinet-layout-partner-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.ids = {}
            for token, name, partner in (
                (self.CLIENT_TOKEN, "Макет Клиент", False), (self.PARTNER_TOKEN, "Макет Партнёр", True),
            ):
                client_id = db.execute(
                    "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                    "VALUES (?, 'Salute', '', ?, 'neutral', '2026-09-01 10:00')", (name, token)).lastrowid
                self.ids[token] = client_id
                db.execute(
                    "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                    "VALUES (?, 'tuning', ?, '2026-09-01 10:00')", (client_id, "partner" if partner else "client"))
                order_id = db.execute(
                    "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, sale_channel, "
                    "phone, subtotal, total, status, order_date, created_at, updated_at, source) VALUES "
                    "(?, ?, 'boat', 'Salute', 'direct', '', 1000, 1000, 'in_progress', '2026-09-10', 'x', 'x', 'manual')",
                    (client_id, name)).lastrowid
                db.execute(
                    "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                    "price_pending, status) VALUES (?, 'Работа', 500, 2, 1000, 0, 'in_progress')", (order_id,))
            db.commit()
        self.addCleanup(self._clear_now)

    def _clear_now(self):
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    def _clear(self, db):
        for token in (self.CLIENT_TOKEN, self.PARTNER_TOKEN):
            row = db.execute("SELECT id FROM clients WHERE token = ?", (token,)).fetchone()
            if row:
                for order in db.execute("SELECT id FROM tuning_orders WHERE client_id = ?", (row["id"],)).fetchall():
                    db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order["id"],))
                db.execute("DELETE FROM tuning_orders WHERE client_id = ?", (row["id"],))
                db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
                db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def page(self, token):
        return self.http.get(f"/client/{token}").get_data(as_text=True)

    def test_both_cabinets_use_the_whole_width(self):
        for token in (self.CLIENT_TOKEN, self.PARTNER_TOKEN):
            self.assertIn('<main class="wrap client-cabinet">', self.page(token), token)

    def test_the_orders_are_not_wrapped_in_another_panel(self):
        for token in (self.CLIENT_TOKEN, self.PARTNER_TOKEN):
            page = self.page(token)
            self.assertIn('<section class="client-orders" id="orders">', page, token)
            self.assertNotIn('<section class="panel" id="orders">', page, token)
            self.assertIn('<details class="panel order-card"', page, token)  # the cards are the panels

    def test_the_partner_income_strip_has_no_cards_inside_the_panel(self):
        page = self.page(self.PARTNER_TOKEN)
        income = page.split('id="partner-income"')[1].split("</details>")[0]
        self.assertEqual(income.count('class="panel total-card'), 0)
        self.assertEqual(income.count('class="total-card'), 3)

    def test_the_layout_rules_apply_to_the_cabinets_only(self):
        css = (pathlib.Path(application_module.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
        tail = css.split("Личные кабинеты клиента и партнёра:")[1]
        rules = [line for line in tail.splitlines() if line.strip().endswith("}") and "{" in line]
        self.assertGreater(len(rules), 8)
        for line in rules:
            self.assertTrue(line.lstrip().startswith(".client-cabinet"), line)

    def test_the_staff_view_of_a_client_uses_the_same_layout(self):
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        page = self.http.get(f"/admin/clients/{self.ids[self.CLIENT_TOKEN]}/cabinet?section=tuning").get_data(as_text=True)
        self.assertIn('class="wrap client-cabinet"', page)
        self.assertNotIn('<section class="panel" id="orders">', page)


if __name__ == "__main__":
    unittest.main()
