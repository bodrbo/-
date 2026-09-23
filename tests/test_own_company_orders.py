import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class OwnCompanyTuningOrderTests(unittest.TestCase):
    MARK = "own-company-test"

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
            self.vessel = db.execute(
                "SELECT name FROM fleet_vessels WHERE deleted_at IS NULL ORDER BY id LIMIT 1"
            ).fetchone()["name"]
            self.own_id = application_module._own_company_client_id(db)
            self.other_client_id = db.execute(
                "INSERT INTO clients (client_name, token, created_at) VALUES (?, ?, '2026-09-01 10:00')",
                (f"{self.MARK} client", f"{self.MARK}-token"),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.execute("DELETE FROM clients WHERE token = ?", (f"{self.MARK}-token",))
        db.commit()

    def make_order(self, client_id, boat, status="estimate", price=1000):
        with application_module.app.app_context():
            db = application_module.get_db()
            cur = db.execute(
                "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
                "sale_channel, phone, subtotal, total, status, order_date, created_at, updated_at, "
                "source, source_ref) VALUES (?, 'x', 'boat', ?, 'direct', '', ?, ?, ?, '2026-09-10', "
                "'2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
                (client_id, boat, price, price, status, f"{self.MARK}:{client_id}:{status}"),
            )
            order_id = cur.lastrowid
            db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Работа', 500, 2, ?, 0, 'pending')",
                (order_id, price),
            )
            db.commit()
            return order_id

    def expense_trips(self, order_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            return [dict(r) for r in db.execute(
                "SELECT * FROM trips WHERE tuning_order_id = ?", (order_id,)
            ).fetchall()]

    def set_status(self, order_id, status):
        self.client.post(f"/tuning/{order_id}/status", data={"status": status})

    def test_company_client_and_fleet_boat_choices_exist(self):
        self.assertIsNotNone(self.own_id)
        with application_module.app.app_context():
            db = application_module.get_db()
            row = db.execute("SELECT client_name FROM clients WHERE id = ?", (self.own_id,)).fetchone()
            self.assertEqual(row["client_name"], application_module.OWN_COMPANY_NAME)
            self.assertIn(self.vessel, application_module._tuning_boat_model_choices(db))
            # own boats are offered from Флот but get no boat-catalog card
            self.assertIsNone(db.execute(
                "SELECT 1 FROM tuning_boat_profiles WHERE model_key = ?",
                (application_module._tuning_equipment_profile_key("boat", self.vessel),),
            ).fetchone())
            ids = [c["id"] for c in application_module._tuning_client_choices(db)]
            self.assertIn(self.own_id, ids)
            # idempotent
            application_module._ensure_own_company_client(db)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM clients WHERE is_own_company = 1").fetchone()[0], 1
            )

    def test_expense_follows_order_status_total_and_deletion(self):
        order_id = self.make_order(self.own_id, self.vessel)
        self.assertEqual(self.expense_trips(order_id), [])  # estimate: not started

        self.set_status(order_id, "in_progress")
        trips = self.expense_trips(order_id)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["boat"], self.vessel)
        self.assertEqual(trips[0]["is_expense"], 1)
        self.assertEqual(trips[0]["extra_total"], 1000)
        self.assertEqual(trips[0]["remainder"], -1000)
        self.assertEqual(trips[0]["source"], "tuning_order")

        # A changed price raises the total -> the expense follows.
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_order_items SET price = 1500 WHERE order_id = ?", (order_id,))
            application_module._recompute_order_totals(db, order_id)
        trips = self.expense_trips(order_id)
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["extra_total"], 1500)

        self.set_status(order_id, "cancelled")
        self.assertEqual(self.expense_trips(order_id), [])

        self.set_status(order_id, "in_progress")
        self.assertEqual(len(self.expense_trips(order_id)), 1)
        self.client.post(f"/tuning/delete/{order_id}")
        self.assertEqual(self.expense_trips(order_id), [])

    def test_regular_client_order_creates_no_expense(self):
        order_id = self.make_order(self.other_client_id, self.vessel, status="in_progress")
        self.set_status(order_id, "handed_over")
        self.assertEqual(self.expense_trips(order_id), [])

    def test_expense_shows_in_boat_analytics(self):
        order_id = self.make_order(self.own_id, self.vessel)
        self.set_status(order_id, "in_progress")
        page = self.client.get("/analytics/trips?month=all").get_data(as_text=True)
        self.assertIn(f"Доработка катера — заказ №{order_id}", page)

    def test_order_links_to_fleet_page_and_creates_no_catalog_profile(self):
        order_id = self.make_order(self.own_id, self.vessel)
        with application_module.app.app_context():
            db = application_module.get_db()
            application_module._sync_tuning_boat_profiles(db)
            db.commit()
            self.assertIsNone(db.execute(
                "SELECT 1 FROM tuning_boat_profiles WHERE model_key = ?",
                (application_module._tuning_equipment_profile_key("boat", self.vessel),),
            ).fetchone())
            index = application_module._fleet_boat_index(db, self.vessel)
        self.assertIsNotNone(index)
        fleet_url = f"/fleet/{index}"
        page = self.client.get(f"/tuning/edit/{order_id}").get_data(as_text=True)
        self.assertIn(f'href="{fleet_url}"', page)
        self.assertNotIn("/tuning/boats/", page.split("Техника", 1)[1].split("</span>", 2)[1])
        listing = self.client.get("/tuning").get_data(as_text=True)
        self.assertIn(f'href="{fleet_url}"', listing)

    def test_old_auto_created_fleet_catalog_cards_are_removed_unless_used(self):
        key = application_module._tuning_equipment_profile_key("boat", self.vessel)
        with application_module.app.app_context():
            db = application_module.get_db()
            insert = (
                "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, "
                "specifications, created_at, updated_at) VALUES (?, ?, 'boat', '', 'x', 'x')"
            )
            db.execute(insert, (key, self.vessel))
            application_module._remove_fleet_boat_catalog_profiles(db)
            self.assertIsNone(db.execute(
                "SELECT 1 FROM tuning_boat_profiles WHERE model_key = ?", (key,)).fetchone())
            db.commit()
            # a regular client's order with the same model keeps its card
        self.make_order(self.other_client_id, self.vessel)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(insert, (key, self.vessel))
            application_module._remove_fleet_boat_catalog_profiles(db)
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM tuning_boat_profiles WHERE model_key = ?", (key,)).fetchone())
            db.execute("DELETE FROM tuning_boat_profiles WHERE model_key = ?", (key,))
            db.commit()

    def test_own_company_order_requires_fleet_boat(self):
        form = MultiDict([
            ("client_name", application_module.OWN_COMPANY_NAME),
            ("client_id", str(self.own_id)),
            ("equipment_type", "boat"),
            ("boat_model", "Чужая лодка XYZ"),
            ("phone", ""), ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", "percent"), ("discount_value", "0"),
            ("work_name[]", "Диагностика"), ("cost_price[]", "1000"),
            ("multiplier[]", "2"), ("item_id[]", ""),
        ])
        response = self.client.post("/tuning/add", data=form)
        self.assertEqual(response.status_code, 400)
        self.assertIn("выберите катер из раздела", response.get_data(as_text=True))
        form.setlist("boat_model", [self.vessel])
        response = self.client.post("/tuning/add", data=form)
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM tuning_orders WHERE client_id = ? AND boat_model = ?",
                (self.own_id, self.vessel),
            ).fetchall()]
            application_module._delete_tuning_order_records(db, ids)
            db.commit()


if __name__ == "__main__":
    unittest.main()
