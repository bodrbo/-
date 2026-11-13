import unittest

from werkzeug.datastructures import MultiDict

from support import application_module

PREFIX = "costtype-test-"
CLIENT = "Тест Типов Транзакций"


class TransactionCostTypeTests(unittest.TestCase):
    """Расходная транзакция, привязанная к проекту/работе, получает тип:
    «Работа» (→ «Работы») или «Закупка материалов/оборудования» (→ «Материалы»)."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, phone, discount_pct, "
                "status, order_date, created_at, updated_at, source, subtotal, total) VALUES (?, 'boat', 'Лодка', "
                "'direct', '', 0, 'in_progress', '2026-10-01', '2026-10-01 10:00', '2026-10-01 10:00', 'manual', "
                "10000, 10000)", (CLIENT,)).lastrowid
            self.order_id = order_id
            self.item_id = db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, price_pending, "
                "status) VALUES (?, 'Покраска', 1, 1, 10000, 0, 'in_progress')", (order_id,)).lastrowid
            self.project_id = db.execute(
                "INSERT INTO projects (name, tuning_order_id, created_at) VALUES ('Проект типов', ?, "
                "'2026-10-01')", (order_id,)).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    @staticmethod
    def cleanup(db):
        db.execute("DELETE FROM transaction_splits WHERE transaction_id IN "
                   "(SELECT id FROM bank_transactions WHERE operation_id LIKE ?)", (PREFIX + "%",))
        db.execute("DELETE FROM bank_transactions WHERE operation_id LIKE ?", (PREFIX + "%",))
        db.execute("DELETE FROM tuning_order_items WHERE order_id IN (SELECT id FROM tuning_orders WHERE client_name = ?)",
                   (CLIENT,))
        db.execute("DELETE FROM projects WHERE tuning_order_id IN (SELECT id FROM tuning_orders WHERE client_name = ?)",
                   (CLIENT,))
        db.execute("DELETE FROM tuning_orders WHERE client_name = ?", (CLIENT,))
        db.commit()

    def transaction(self, direction="out", amount=1000, name="a", purpose="Оплата"):
        with application_module.app.app_context():
            db = application_module.get_db()
            tid = db.execute(
                "INSERT INTO bank_transactions (operation_id, account_number, operation_date, amount, direction, "
                "counterparty_name, purpose, created_at) VALUES (?, 'acc', '2026-10-05', ?, ?, 'Контрагент ' || ?, ?, "
                "'2026-10-05 10:00')", (PREFIX + name, amount, direction, name, purpose)).lastrowid
            db.commit()
            return tid

    def bind(self, tid, target, cost_type=None):
        data = {"transaction_id": str(tid), "target": target, "next": "/analytics"}
        if cost_type:
            data["cost_type"] = cost_type
        return self.http.post("/analytics/transactions/project", data=data)

    def tx_row(self, tid):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT project_id, item_id, cost_type FROM bank_transactions WHERE id = ?", (tid,)).fetchone())

    def profitability(self):
        with application_module.app.app_context():
            (row,) = application_module._item_profitability(application_module.get_db(), self.order_id)
            return row

    # ---- binding

    def test_an_expense_needs_a_type_to_be_bound(self):
        tid = self.transaction()
        response = self.bind(tid, f"i{self.item_id}")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Выберите тип", response.get_data(as_text=True))
        self.assertEqual(self.tx_row(tid), {"project_id": None, "item_id": None, "cost_type": None})
        response = self.bind(tid, f"i{self.item_id}", "bogus")
        self.assertEqual(response.status_code, 400)

    def test_an_expense_is_bound_with_its_type(self):
        tid = self.transaction()
        self.assertEqual(self.bind(tid, f"i{self.item_id}", "work").status_code, 302)
        self.assertEqual(self.tx_row(tid), {"project_id": self.project_id, "item_id": self.item_id, "cost_type": "work"})

    def test_an_income_needs_no_type_and_keeps_none(self):
        tid = self.transaction("in")
        self.assertEqual(self.bind(tid, f"p{self.project_id}").status_code, 302)
        self.assertEqual(self.tx_row(tid)["cost_type"], None)
        self.bind(tid, f"p{self.project_id}", "work")
        self.assertEqual(self.tx_row(tid)["cost_type"], None)

    def test_unbinding_clears_the_type(self):
        tid = self.transaction()
        self.bind(tid, f"p{self.project_id}", "materials")
        self.assertEqual(self.bind(tid, "").status_code, 302)
        self.assertEqual(self.tx_row(tid), {"project_id": None, "item_id": None, "cost_type": None})

    def test_the_type_can_be_changed_later(self):
        tid = self.transaction()
        self.bind(tid, f"i{self.item_id}", "work")
        response = self.http.post("/analytics/transactions/cost-type",
                                  data={"transaction_id": str(tid), "cost_type": "materials", "next": "/analytics"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.tx_row(tid)["cost_type"], "materials")
        response = self.http.post("/analytics/transactions/cost-type", data={"transaction_id": str(tid), "cost_type": ""})
        self.assertEqual(response.status_code, 400)

    # ---- the work table

    def test_a_work_transaction_goes_to_works_and_a_purchase_to_materials(self):
        self.bind(self.transaction(amount=1500, name="w"), f"i{self.item_id}", "work")
        self.bind(self.transaction(amount=400, name="m"), f"i{self.item_id}", "materials")
        row = self.profitability()
        self.assertEqual(row["works_expense"], 1500)
        self.assertEqual(row["materials_expense"], 400)
        self.assertEqual(row["tx_untyped"], 0)
        self.assertEqual(row["profit"], 10000 - 1500 - 400)

    def test_a_transaction_bound_before_types_still_reduces_profit_and_is_flagged(self):
        tid = self.transaction(amount=700)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE bank_transactions SET project_id = ?, item_id = ? WHERE id = ?",
                       (self.project_id, self.item_id, tid))
            db.commit()
        row = self.profitability()
        self.assertEqual((row["works_expense"], row["materials_expense"], row["tx_untyped"]), (0, 0, 700))
        self.assertEqual(row["profit"], 10000 - 700)
        page = self.http.get(f"/analytics/projects/{self.project_id}").get_data(as_text=True)
        self.assertIn("без типа", page)
        self.assertIn("Выберите тип…", page)

    def test_split_parts_carry_their_own_types(self):
        tid = self.transaction(amount=1000)
        response = self.http.post(f"/analytics/transactions/{tid}/split", data=MultiDict([
            ("next", "/analytics"),
            ("target[]", f"i{self.item_id}"), ("cost_type[]", "work"), ("amount[]", "600"),
            ("target[]", f"p{self.project_id}"), ("cost_type[]", "materials"), ("amount[]", "400"),
        ]))
        self.assertEqual(response.status_code, 302)
        row = self.profitability()
        self.assertEqual(row["works_expense"], 600)            # the part bound to the work
        page = self.http.get(f"/analytics/projects/{self.project_id}").get_data(as_text=True)
        section = page[page.index("<h2>Материалы</h2>"):]
        self.assertIn("Контрагент a", section[:section.index("</section>")])   # the project-wide part

    def test_a_split_expense_part_needs_a_type(self):
        tid = self.transaction(amount=1000)
        response = self.http.post(f"/analytics/transactions/{tid}/split", data=MultiDict([
            ("next", "/analytics"),
            ("target[]", f"i{self.item_id}"), ("cost_type[]", "work"), ("amount[]", "600"),
            ("target[]", f"p{self.project_id}"), ("cost_type[]", ""), ("amount[]", "400"),
        ]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("выберите тип", response.get_data(as_text=True))
        with application_module.app.app_context():
            self.assertEqual(application_module.get_db().execute(
                "SELECT COUNT(*) FROM transaction_splits WHERE transaction_id = ?", (tid,)).fetchone()[0], 0)

    def test_collapsing_a_split_keeps_the_type(self):
        tid = self.transaction(amount=1000)
        self.http.post(f"/analytics/transactions/{tid}/split", data=MultiDict([
            ("next", "/analytics"),
            ("target[]", f"i{self.item_id}"), ("cost_type[]", "work"), ("amount[]", "600"),
            ("target[]", f"p{self.project_id}"), ("cost_type[]", "materials"), ("amount[]", "400"),
        ]))
        with application_module.app.app_context():
            split_id = application_module.get_db().execute(
                "SELECT id FROM transaction_splits WHERE transaction_id = ? AND item_id IS NULL", (tid,)).fetchone()[0]
        self.http.post("/analytics/transactions/split/remove", data={"split_id": str(split_id), "next": "/analytics"})
        self.assertEqual(self.tx_row(tid), {"project_id": self.project_id, "item_id": self.item_id, "cost_type": "work"})

    # ---- project page

    def test_project_wide_transactions_land_in_general_works_and_materials(self):
        self.bind(self.transaction(amount=900, name="gw", purpose="Мойка"), f"p{self.project_id}", "work")
        self.bind(self.transaction(amount=300, name="gm", purpose="Краска"), f"p{self.project_id}", "materials")
        page = self.http.get(f"/analytics/projects/{self.project_id}").get_data(as_text=True)
        works = page[page.index("Общие работы по проекту"):]
        works = works[:works.index("</section>")]
        materials = page[page.index("<h2>Материалы</h2>"):]
        materials = materials[:materials.index("</section>")]
        self.assertIn("Мойка", works)
        self.assertNotIn("Краска", works)
        self.assertIn("Краска", materials)
        self.assertNotIn("Мойка", materials)
        self.assertNotIn("Списанные материалы", page)

    def test_item_transactions_are_not_repeated_in_the_general_sections(self):
        self.bind(self.transaction(amount=900, name="iw", purpose="Шлифовка"), f"i{self.item_id}", "work")
        page = self.http.get(f"/analytics/projects/{self.project_id}").get_data(as_text=True)
        works = page[page.index("Общие работы по проекту"):]
        self.assertNotIn("Шлифовка", works[:works.index("</section>")])

    def test_the_transactions_table_offers_the_types(self):
        tid = self.transaction()
        page = self.http.get("/analytics").get_data(as_text=True)
        self.assertIn('name="cost_type"', page)
        self.assertIn("Закупка материалов/оборудования", page)
        self.assertIn("txTargetChanged", page)

    def test_the_split_form_has_a_type_for_an_expense_only(self):
        expense = self.transaction(name="e")
        income = self.transaction("in", name="i")
        self.assertIn('name="cost_type[]"', self.http.get(f"/analytics/transactions/{expense}/split").get_data(as_text=True))
        self.assertNotIn('name="cost_type[]"', self.http.get(f"/analytics/transactions/{income}/split").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
