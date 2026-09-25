import unittest

from support import application_module


class PartnerCommissionTests(unittest.TestCase):
    MARK = "partner-commission-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            self.partner_id = db.execute(
                "INSERT INTO clients (client_name, token, created_at) VALUES ('Партнёр Комиссий', ?, "
                "'2026-09-01 10:00')", (f"{self.MARK}-token",),
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at, relationship_type) "
                "VALUES (?, 'tuning', '2026-09-01 10:00', 'partner')", (self.partner_id,),
            )
            db.commit()
            self.order_id = self.make_order(db, self.partner_id, 20000)

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.execute(
            "DELETE FROM client_segments WHERE client_id IN (SELECT id FROM clients WHERE token = ?)",
            (f"{self.MARK}-token",),
        )
        db.execute("DELETE FROM clients WHERE token = ?", (f"{self.MARK}-token",))
        db.commit()

    def make_order(self, db, client_id, total, source="manual", status="estimate"):
        order_id = db.execute(
            "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
            "sale_channel, phone, subtotal, total, status, order_date, created_at, updated_at, "
            "source, source_ref) VALUES (?, 'Партнёр Комиссий', 'boat', 'Тест-катер', 'direct', '', ?, ?, "
            "?, '2026-09-10', '2026-09-10 10:00', '2026-09-10 10:00', ?, ?)",
            (client_id, total, total, status, source, f"{self.MARK}:{source}:{total}:{status}"),
        ).lastrowid
        db.execute(
            "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
            "price_pending, status) VALUES (?, 'Работа', 1, 1, ?, 0, 'pending')", (order_id, total),
        )
        db.commit()
        return order_id

    def set_scheme(self, scheme, value=""):
        return self.client.post(
            f"/admin/clients/{self.partner_id}/work-scheme",
            data={"work_scheme": scheme, "work_scheme_value": value},
        )

    def set_status(self, order_id, status):
        self.client.post(f"/tuning/{order_id}/status", data={"status": status})

    def commission(self, order_id=None):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT * FROM partner_commissions WHERE order_id = ?", (order_id or self.order_id,)
            ).fetchone()

    def scheme_row(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT work_scheme, work_scheme_value FROM client_segments WHERE client_id = ?",
                (self.partner_id,),
            ).fetchone()

    def test_scheme_validation_and_storage(self):
        self.set_scheme("percent", "12,5")
        self.assertEqual(tuple(self.scheme_row()), ("percent", 12.5))
        for bad in ("0", "101", "abc", ""):
            self.set_scheme("percent", bad)
            self.assertEqual(tuple(self.scheme_row()), ("percent", 12.5), bad)
        self.set_scheme("fixed", "3 500")
        self.assertEqual(tuple(self.scheme_row()), ("fixed", 3500.0))
        self.set_scheme("fixed", "-1")
        self.assertEqual(tuple(self.scheme_row()), ("fixed", 3500.0))
        self.set_scheme("", "")
        self.assertEqual(tuple(self.scheme_row()), ("", 0.0))
        page = self.client.get("/admin/clients?section=tuning&relationship=partner").get_data(as_text=True)
        self.assertIn("Схема работы", page)

    def test_percent_commission_appears_in_work_and_follows_the_total(self):
        self.set_scheme("percent", "10")
        self.assertIsNone(self.commission())  # not in work yet
        self.set_status(self.order_id, "in_progress")
        row = self.commission()
        self.assertEqual((row["scheme"], row["rate"], row["amount"]), ("percent", 10.0, 2000.0))
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_items SET price = 30000 WHERE order_id = ?", (self.order_id,))
            application_module._recompute_order_totals(db, self.order_id)
        self.assertEqual(self.commission()["amount"], 3000.0)
        # sent back or cancelled -> the record goes away
        self.set_status(self.order_id, "estimate")
        self.assertIsNone(self.commission())
        self.set_status(self.order_id, "in_progress")
        self.assertIsNotNone(self.commission())
        self.set_status(self.order_id, "cancelled")
        self.assertIsNone(self.commission())

    def test_fixed_tariff_is_the_same_for_every_order(self):
        self.set_scheme("fixed", "5000")
        self.set_status(self.order_id, "in_progress")
        self.assertEqual(self.commission()["amount"], 5000.0)
        self.set_status(self.order_id, "handed_over")
        self.assertEqual(self.commission()["amount"], 5000.0)

    def test_changing_the_scheme_only_affects_new_orders(self):
        self.set_scheme("percent", "10")
        self.set_status(self.order_id, "in_progress")
        self.set_scheme("fixed", "7000")
        self.assertEqual(self.commission()["amount"], 2000.0)  # still booked under 10 %
        with application_module.app.app_context():
            second = self.make_order(application_module.get_db(), self.partner_id, 9000)
        self.set_status(second, "in_progress")
        self.assertEqual(self.commission(second)["amount"], 7000.0)

    def test_no_commission_without_a_scheme_or_for_subcontract_orders(self):
        self.set_status(self.order_id, "in_progress")
        self.assertIsNone(self.commission())  # partner has no scheme
        self.set_scheme("percent", "10")
        # setting the scheme books the orders already in work
        self.assertEqual(self.commission()["amount"], 2000.0)
        with application_module.app.app_context():
            sub = self.make_order(
                application_module.get_db(), self.partner_id, 8000,
                source=application_module.SUBCONTRACT_REQUEST_SOURCE,
            )
        self.set_status(sub, "in_progress")
        self.assertIsNone(self.commission(sub))

    def test_cabinet_shows_income_and_deleting_the_order_drops_it(self):
        self.set_scheme("percent", "10")
        self.set_status(self.order_id, "in_progress")
        for url in (
            f"/client/{self.MARK}-token",
            f"/admin/clients/{self.partner_id}/cabinet?section=tuning&relationship=partner",
        ):
            page = self.client.get(url).get_data(as_text=True)
            self.assertIn("Доходность", page, url)
            self.assertIn("10 % с заказа", page, url)
            self.assertIn(f"№{self.order_id}", page, url)
        self.client.post(f"/tuning/delete/{self.order_id}")
        self.assertIsNone(self.commission())


if __name__ == "__main__":
    unittest.main()
