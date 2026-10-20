import re
import unittest

from support import application_module


class CabinetCancelledOrdersTests(unittest.TestCase):
    """A cancelled order stays in the cabinet's list but is left out of its
    statistics: the order count, the sum, what is paid and what is still to
    be paid — for clients and for partners."""

    NAME = "cabinet-cancelled-orders-test"
    TOKEN = "cabinet-cancelled-orders-token"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM clients WHERE token = ?", (self.TOKEN,)).fetchall():
            order_ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_orders WHERE client_id = ? OR partner_id = ?",
                (row["id"], row["id"])).fetchall()]
            for order_id in order_ids:
                db.execute("DELETE FROM partner_commissions WHERE order_id = ?", (order_id,))
                db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            if order_ids:
                application_module._delete_tuning_order_records(db, order_ids)
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def make_client(self, relationship="client"):
        with application_module.app.app_context():
            db = application_module.get_db()
            client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, 'Salute', '', ?, 'neutral', '2026-09-01 10:00')", (self.NAME, self.TOKEN)).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', ?, '2026-09-01 10:00')", (client_id, relationship))
            db.commit()
        return client_id

    def add_order(self, client_id, status, total, paid):
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute(
                "INSERT INTO tuning_orders (client_id, client_name, boat_model, order_date, created_at, "
                "updated_at, total, subtotal, phone, sale_channel, status) "
                "VALUES (?, ?, 'Salute', '2026-09-10', '2026-09-10 10:00', '2026-09-10 10:00', ?, ?, '', "
                "'direct', ?)", (client_id, self.NAME, total, total, status)).lastrowid
            if paid:
                db.execute(
                    "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) "
                    "VALUES (?, ?, '2026-09-11 10:00', '2026-09-11 10:00')", (order_id, paid))
            db.commit()
        return order_id

    @staticmethod
    def stats(page):
        found = {}
        for label, value in re.findall(
                r'<span class="k">([^<]+)</span>\s*<span class="v"[^>]*>([^<]*)</span>', page):
            found.setdefault(label.strip(), value.replace("\xa0", " ").strip())
        return found

    def assert_stats(self, page, orders, total, paid, remaining):
        stats = self.stats(page)
        self.assertEqual(stats["Заказов"], str(orders))
        self.assertEqual(stats["Сумма по заказам"], f"{total} ₽")
        self.assertEqual(stats["Оплачено"], f"{paid} ₽")
        self.assertEqual(stats["Осталось доплатить"], f"{remaining} ₽")

    def test_a_clients_cancelled_order_is_not_counted(self):
        client_id = self.make_client()
        self.add_order(client_id, "in_progress", 10000, 3000)
        self.add_order(client_id, "cancelled", 5000, 1000)
        self.add_order(client_id, "done", 2000, 2000)
        page = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assert_stats(page, 2, "12 000", "5 000", "7 000")

    def test_the_cancelled_order_is_still_listed(self):
        client_id = self.make_client()
        self.add_order(client_id, "in_progress", 10000, 0)
        cancelled = self.add_order(client_id, "cancelled", 5000, 0)
        page = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn(f"№{cancelled}", page)
        self.assertIn("Отменён", page)

    def test_only_cancelled_orders_give_zero_statistics(self):
        client_id = self.make_client()
        self.add_order(client_id, "cancelled", 5000, 1000)
        page = self.client.get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assert_stats(page, 0, "0", "0", "0")

    def test_the_admin_view_of_the_cabinet_counts_the_same_way(self):
        client_id = self.make_client()
        self.add_order(client_id, "approved", 8000, 500)
        self.add_order(client_id, "cancelled", 4000, 4000)
        page = self.client.get(f"/admin/clients/{client_id}/cabinet").get_data(as_text=True)
        self.assert_stats(page, 1, "8 000", "500", "7 500")

    def test_a_partner_cabinet_counts_the_same_way(self):
        partner_id = self.make_client("partner")
        self.add_order(partner_id, "in_progress", 10000, 0)
        self.add_order(partner_id, "cancelled", 6000, 0)
        page = self.client.get(f"/admin/clients/{partner_id}/cabinet?section=tuning").get_data(as_text=True)
        self.assert_stats(page, 1, "10 000", "0", "10 000")

    def test_the_incoming_and_outgoing_counters_skip_cancelled_orders(self):
        partner_id = self.make_client("partner")
        self.add_order(partner_id, "in_progress", 1000, 0)
        self.add_order(partner_id, "cancelled", 1000, 0)
        self.add_order(partner_id, "cancelled", 1000, 0)
        with application_module.app.app_context():
            db = application_module.get_db()
            incoming = self.add_order(partner_id, "approved", 1000, 0)
            cancelled_incoming = self.add_order(partner_id, "cancelled", 1000, 0)
            for order_id in (incoming, cancelled_incoming):
                db.execute(
                    "UPDATE tuning_orders SET source = ?, partner_id = ?, client_id = NULL WHERE id = ?",
                    (application_module.SUBCONTRACT_REQUEST_SOURCE, partner_id, order_id))
            db.commit()
        page = self.client.get(f"/admin/clients/{partner_id}/cabinet?section=tuning").get_data(as_text=True)
        self.assertRegex(page, r"Входящие заявки</span><strong>1</strong>")
        self.assertRegex(page, r"Исходящие заявки</span><strong>1</strong>")

    def test_a_commission_on_a_cancelled_order_is_not_shown_in_the_partner_income(self):
        partner_id = self.make_client("partner")
        live = self.add_order(partner_id, "in_progress", 10000, 0)
        cancelled = self.add_order(partner_id, "cancelled", 6000, 0)
        with application_module.app.app_context():
            db = application_module.get_db()
            for order_id, amount in ((live, 1000), (cancelled, 600)):
                db.execute(
                    "INSERT INTO partner_commissions (order_id, partner_id, scheme, rate, base_total, amount, "
                    "recorded_at, updated_at) VALUES (?, ?, 'percent', 10, ?, ?, '2026-09-12 10:00', '2026-09-12 10:00')",
                    (order_id, partner_id, amount * 10, amount))
            db.commit()
            summary = application_module._partner_income_summary(db, partner_id, {})
        self.assertEqual(summary["total"], 1000)
        self.assertEqual([e["order_id"] for e in summary["entries"]], [live])


if __name__ == "__main__":
    unittest.main()
