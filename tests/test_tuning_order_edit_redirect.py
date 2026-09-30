import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningOrderEditRedirectTests(unittest.TestCase):
    """Saving the order-edit form (mainly used to add/remove work items) must
    keep the admin on the same order card, anchored at the works block —
    not send them back to the general orders table."""

    MARK = "tuning-edit-redirect-test"

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
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, "
                "phone, subtotal, total, status, order_date, created_at, updated_at, source, source_ref) "
                "VALUES ('Клиент', 'boat', 'Тест-катер', 'direct', '', 1000, 1000, 'estimate', "
                "'2026-09-10', '2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
                (self.MARK,),
            ).lastrowid
            self.item_id = db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                "price_pending, status) VALUES (?, 'Диагностика', 500, 2, 1000, 0, 'pending')",
                (self.order_id,),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (self.MARK,)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.commit()

    def edit_form(self, **overrides):
        data = MultiDict([
            ("client_name", "Клиент"),
            ("equipment_type", "boat"),
            ("boat_model", "Тест-катер"),
            ("boat_registration_number", ""),
            ("motor_model", ""),
            ("motor_serial_number", ""),
            ("phone", ""),
            ("order_date", "2026-09-10"),
            ("sale_channel", "direct"),
            ("discount_type", "percent"),
            ("discount_value", "0"),
            ("work_name[]", "Диагностика"),
            ("cost_price[]", "500"),
            ("multiplier[]", "2"),
            ("item_id[]", str(self.item_id)),
        ])
        for key, value in overrides.items():
            data[key] = value
        return data

    def test_saving_the_order_form_redirects_back_to_the_same_card(self):
        response = self.client.post(f"/tuning/edit/{self.order_id}", data=self.edit_form())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"/tuning/edit/{self.order_id}#work-container",
        )

    def test_adding_a_new_work_row_also_stays_on_the_card(self):
        form = self.edit_form()
        form.add("work_name[]", "Полировка")
        form.add("cost_price[]", "300")
        form.add("multiplier[]", "1.5")
        form.add("item_id[]", "")
        response = self.client.post(f"/tuning/edit/{self.order_id}", data=form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"/tuning/edit/{self.order_id}#work-container",
        )
        # follow the redirect: the works block is the same page's content
        follow = self.client.get(response.headers["Location"].split("#")[0])
        page = follow.get_data(as_text=True)
        self.assertIn('id="work-container"', page)
        self.assertIn("Полировка", page)

    def test_a_validation_error_re_renders_the_same_card_too(self):
        # a work row with a price but no name is a real validation error
        # (a fully empty row is silently dropped instead — not this case)
        form = self.edit_form()
        form.setlist("work_name[]", [""])
        form.setlist("cost_price[]", ["500"])
        form.setlist("multiplier[]", ["2"])
        response = self.client.post(f"/tuning/edit/{self.order_id}", data=form)
        self.assertEqual(response.status_code, 400)
        page = response.get_data(as_text=True)
        self.assertIn("не указано название", page)
        self.assertIn('id="work-container"', page)


if __name__ == "__main__":
    unittest.main()
