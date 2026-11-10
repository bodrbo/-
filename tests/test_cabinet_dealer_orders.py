import unittest

from support import application_module


class CabinetDealerOrdersTests(unittest.TestCase):
    """Dealer-center orders (boats and motors) show up in the client's cabinet:
    the client sees the composition and what is left to pay, the admin also
    sees the payments; a cancelled one is listed but not counted."""

    NAME = "cabinet-dealer-orders-test"
    OTHER = "cabinet-dealer-orders-other"
    TOKEN = "cabinet-dealer-orders-token"
    OTHER_TOKEN = "cabinet-dealer-orders-other-token"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())
        self.addCleanup(self.cleanup_now)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.client_id = self.make_client(db, self.NAME, self.TOKEN)
            self.other_id = self.make_client(db, self.OTHER, self.OTHER_TOKEN)
            db.commit()

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for name in (self.NAME, self.OTHER):
            for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (name,)).fetchall():
                db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
                db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
                db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        for token in (self.TOKEN, self.OTHER_TOKEN):
            for row in db.execute("SELECT id FROM clients WHERE token = ?", (token,)).fetchall():
                db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
                db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def make_client(self, db, name, token):
        client_id = db.execute(
            "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
            "VALUES (?, '', '', ?, 'neutral', '2026-09-01 10:00')", (name, token)).lastrowid
        db.execute(
            "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
            "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (client_id,))
        return client_id

    def add_order(self, client_id, name, status, total, paid, item="Лодка Тест-500", serial="SN-777"):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute(
                "INSERT INTO dealer_orders (client_id, client_name, status, order_date, subtotal, total, "
                "created_at, updated_at) VALUES (?, ?, ?, '2026-09-10', ?, ?, '2026-09-10 10:00', '2026-09-10 10:00')",
                (client_id, name, status, total, total)).lastrowid
            db.execute(
                "INSERT INTO dealer_order_items (order_id, kind, name, serial_number, quantity, price, position) "
                "VALUES (?, 'boat', ?, ?, 1, ?, 0)", (order_id, item, serial, total))
            if paid:
                db.execute(
                    "INSERT INTO dealer_payments (order_id, amount, paid_at, payment_type, created_at) "
                    "VALUES (?, ?, '2026-09-11', 'CARD', '2026-09-11 10:00')", (order_id, paid))
            db.commit()
        return order_id

    def login_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def test_the_client_sees_the_order_without_payment_rows(self):
        self.add_order(self.client_id, self.NAME, "approved", 1000000, 300000)
        html = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn("Лодки и моторы", html)
        self.assertIn("Лодка Тест-500", html)
        self.assertIn("SN-777", html)
        self.assertIn("К доплате", html)
        self.assertNotIn("Открыть заказ в дилерском центре", html)
        self.assertNotIn("Платежи", html)
        self.assertNotIn("Заказов пока нет", html)
        self.assertNotIn("Ваши заказы", html)

    def test_the_admin_sees_payments_and_the_link(self):
        order_id = self.add_order(self.client_id, self.NAME, "approved", 1000000, 300000)
        self.login_admin()
        html = self.client.get(f"/admin/clients/{self.client_id}/cabinet").get_data(as_text=True)
        self.assertIn("Лодка Тест-500", html)
        self.assertIn("Платежи", html)
        self.assertIn(f"/dealer/orders/{order_id}", html)

    def test_the_orders_are_counted_in_the_totals_but_cancelled_ones_are_not(self):
        self.add_order(self.client_id, self.NAME, "approved", 1000000, 300000)
        self.add_order(self.client_id, self.NAME, "cancelled", 500000, 0, item="Мотор Отмена-9")
        html = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn("Мотор Отмена-9", html)
        self.assertIn("1\u00a0000\u00a0000", html)
        self.assertNotIn("1\u00a0500\u00a0000", html)
        self.assertIn("700\u00a0000", html)

    def test_another_clients_order_is_not_shown(self):
        self.add_order(self.other_id, self.OTHER, "approved", 900000, 0, item="Чужая лодка-1")
        html = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertNotIn("Чужая лодка-1", html)
        self.assertNotIn("Лодки и моторы", html)
        self.assertIn("Заказов пока нет", html)


if __name__ == "__main__":
    unittest.main()
