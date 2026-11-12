import unittest
from unittest import mock

from support import application_module

NAME = "Перенос Тюнинг В Дилерский"
REF = "to-dealer-test:"
ROPE = "Перенос Трос Тест"
BOAT = "Перенос Лодка Тест"
MOTOR_1 = "Перенос Мотор Один"
MOTOR_2 = "Перенос Мотор Два"
STORE = "Склад Переноса Тест"


class TuningOrderToDealerTests(unittest.TestCase):
    """Тюнинг-заказы → массовое действие «Перенести»: заказ без работ становится
    заказом дилерского центра («Морские товары» или «Лодки и моторы»)."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            self.product_id = db.execute(
                "INSERT INTO supply_products (name, sku, cost_price, cost_unit, sale_price, created_at) "
                "VALUES (?, 'TODEALER-TEST-ROPE', 500, 'piece', 1200, '2026-10-01 10:00')", (ROPE,)).lastrowid
            self.warehouse_id = db.execute(
                "INSERT INTO supply_warehouses (name, created_at) VALUES (?, '2026-10-01')", (STORE,)).lastrowid
            db.execute("INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, 10)",
                       (self.product_id, self.warehouse_id))
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', '+79990004455', 'to-dealer-token', 'neutral', '2026-09-01 10:00')", (NAME,)).lastrowid
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM dealer_orders WHERE client_name = ?", (NAME,)).fetchall():
            for pay in db.execute("SELECT id FROM dealer_payments WHERE order_id = ?", (row["id"],)).fetchall():
                db.execute("DELETE FROM dealer_modulkassa_receipts WHERE payment_id = ?", (pay["id"],))
            db.execute("DELETE FROM dealer_payments WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_order_items WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM supply_writeoffs WHERE dealer_order_id = ?", (row["id"],))
            db.execute("DELETE FROM order_events WHERE order_kind = 'dealer' AND order_id = ?", (row["id"],))
            db.execute("DELETE FROM dealer_orders WHERE id = ?", (row["id"],))
        for row in db.execute("SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (REF + "%",)).fetchall():
            oid = row["id"]
            for pay in db.execute("SELECT id FROM tuning_payments WHERE order_id = ?", (oid,)).fetchall():
                db.execute("DELETE FROM modulkassa_receipts WHERE payment_id = ?", (pay["id"],))
            db.execute("DELETE FROM supply_writeoffs WHERE tuning_order_product_id IN "
                       "(SELECT id FROM tuning_order_products WHERE order_id = ?)", (oid,))
            for table in ("tuning_order_items", "tuning_order_motors", "tuning_payments", "tuning_order_products",
                          "tuning_order_notes", "partner_commissions", "tuning_yookassa_payments"):
                db.execute(f"DELETE FROM {table} WHERE order_id = ?", (oid,))
            db.execute("DELETE FROM bank_transactions WHERE operation_id = 'to-dealer-bank-op'")
            db.execute("DELETE FROM bank_transactions WHERE project_id IN (SELECT id FROM projects WHERE tuning_order_id = ?)", (oid,))
            db.execute("DELETE FROM order_events WHERE order_kind = 'tuning' AND order_id = ?", (oid,))
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (oid,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (oid,))
        db.execute("DELETE FROM supply_stock WHERE product_id IN (SELECT id FROM supply_products WHERE sku LIKE 'TODEALER-TEST-%')")
        db.execute("DELETE FROM supply_warehouses WHERE name = ?", (STORE,))
        db.execute("DELETE FROM supply_products WHERE sku LIKE 'TODEALER-TEST-%'")
        db.execute("DELETE FROM clients WHERE token = 'to-dealer-token'")
        db.execute("DELETE FROM tuning_boat_profiles WHERE model_key LIKE 'todealer-test-%'")
        db.execute("DELETE FROM dealer_prices WHERE profile_id NOT IN (SELECT id FROM tuning_boat_profiles)")
        db.commit()

    # ---- builders

    def tuning_order(self, status="estimate", equipment_type="boat", boat=BOAT, motor="", discount=0,
                     discount_type="percent", goods=(), works=0, notes=(), payments=(), n=[0]):
        n[0] += 1
        with application_module.app.app_context():
            db = application_module.get_db()
            oid = db.execute(
                "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
                "boat_registration_number, motor_model, motor_serial_number, sale_channel, phone, discount_pct, "
                "discount_type, discount_value, subtotal, total, status, order_date, created_at, updated_at, "
                "source, source_ref, deadline_date) VALUES (?, ?, ?, ?, 'BORT-1', ?, 'SN-9', 'direct', "
                "'+79990004455', 0, ?, ?, 0, 0, ?, '2026-09-10', '2026-09-10 10:00', '2026-09-10 10:00', "
                "'manual', ?, '2026-10-01')",
                (self.client_id, NAME, equipment_type, boat, motor, discount_type, discount, status,
                 f"{REF}{n[0]}")).lastrowid
            db.execute("INSERT INTO projects (name, tuning_order_id, created_at) VALUES (?, ?, '2026-09-10')",
                       (f"Заказ №{oid}", oid))
            for index in range(works):
                db.execute("INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                           "price_pending, status) VALUES (?, 'Работа', 100, 2, 200, 0, 'pending')", (oid,))
            for position, (product_id, name, quantity, price, unit, warehouse) in enumerate(goods):
                db.execute(
                    "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, unit_price, "
                    "cost_price, unit, created_at, warehouse_id, position) VALUES (?, ?, ?, ?, ?, 500, ?, "
                    "'2026-09-10 10:00', ?, ?)", (oid, product_id, name, quantity, price, unit, warehouse, position))
            for text in notes:
                db.execute("INSERT INTO tuning_order_notes (order_id, text, created_at) VALUES (?, ?, '2026-09-11 10:00')",
                           (oid, text))
            for amount, kind in payments:
                pid = db.execute(
                    "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at, payment_type, is_correction, "
                    "no_receipt) VALUES (?, ?, '2026-09-12 12:00', '2026-09-12 12:00', 'CARD', 0, 0)",
                    (oid, amount)).lastrowid
                if kind:
                    db.execute("INSERT INTO modulkassa_receipts (payment_id, doc_id, status, fiscal_info_json, "
                               "created_at, updated_at) VALUES (?, ?, 'printed', '{\"qr\": \"t=1\"}', 'x', 'x')",
                               (pid, f"to-dealer-doc-{oid}-{pid}"))
            db.commit()
            application_module._recompute_order_totals(db, oid)
            return oid

    def convert(self, ids, target="goods"):
        response = self.client.post("/tuning/bulk", data={"action": "convert", "bulk_target": target,
                                                         "order_id": [str(i) for i in ids]})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            return session.pop("tuning_bulk_notice", None), session.pop("tuning_bulk_error", None)

    def dealer_orders(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM dealer_orders WHERE client_name = ? ORDER BY id", (NAME,)).fetchall()]

    def rows(self, sql, *params):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(sql, params).fetchall()]

    def tuning_exists(self, oid):
        return bool(self.rows("SELECT id FROM tuning_orders WHERE id = ?", oid))

    ROPE_LINE = lambda self, qty="3", price=1200: (self.product_id, ROPE, qty, price, "piece", None)

    # ---- the controls

    def test_the_bulk_block_offers_the_move(self):
        self.tuning_order(goods=[self.ROPE_LINE()])
        page = self.client.get("/tuning", query_string={"q": NAME}).get_data(as_text=True)
        for text in ('name="bulk_target"', 'value="goods"', 'value="equipment"', 'id="tuning-bulk-convert"',
                     "Морские товары", "Лодки и моторы"):
            self.assertIn(text, page)

    # ---- to «Морские товары»

    def test_a_goods_order_becomes_a_goods_order_of_the_dealer_center(self):
        oid = self.tuning_order(
            status="approved", discount=10, notes=["Позвонить в пятницу"],
            goods=[self.ROPE_LINE("3", 1200), (0, "Ручной товар", "2", 500, "piece", None)],
            payments=[(1000, True), (500, False)])
        notice, error = self.convert([oid])
        self.assertIsNone(error)
        self.assertIn(f"№{oid} →", notice)
        self.assertFalse(self.tuning_exists(oid))
        (order,) = self.dealer_orders()
        self.assertEqual(order["order_type"], "goods")
        self.assertEqual((order["client_id"], order["phone"], order["order_date"], order["deadline_date"]),
                         (self.client_id, "+79990004455", "2026-09-10", "2026-10-01"))
        self.assertEqual((order["subtotal"], order["discount_type"], order["discount_value"], order["total"]),
                         (4600, "percent", 10, 4140))
        self.assertEqual(order["status"], "new_request")
        self.assertIn(f"Перенесён из тюнинг-заказа №{oid}", order["comment"])
        self.assertIn("Позвонить в пятницу", order["comment"])
        self.assertIn(f"лодка {BOAT}", order["comment"])
        lines = self.rows("SELECT * FROM dealer_order_items WHERE order_id = ? ORDER BY position", order["id"])
        self.assertEqual([(l["kind"], l["product_id"], l["name"], l["quantity"], l["price"], l["unit"]) for l in lines],
                         [("goods", self.product_id, ROPE, 3, 1200, "piece"),
                          ("goods", None, "Ручной товар", 2, 500, "piece")])
        # what belonged to the old order is gone
        for table in ("tuning_order_products", "tuning_order_notes", "tuning_payments"):
            self.assertEqual(self.rows(f"SELECT id FROM {table} WHERE order_id = ?", oid), [], table)
        self.assertEqual(self.rows("SELECT id FROM projects WHERE tuning_order_id = ?", oid), [])
        # the new order opens in the dealer center
        page = self.client.get(f"/dealer/orders/{order['id']}").get_data(as_text=True)
        self.assertIn('id="dealerItems"', page)
        self.assertIn("Морские товары", page)

    def test_payments_and_their_receipts_move_along(self):
        oid = self.tuning_order(goods=[self.ROPE_LINE()], payments=[(1000, True), (500, False)])
        self.convert([oid])
        (order,) = self.dealer_orders()
        payments = self.rows("SELECT * FROM dealer_payments WHERE order_id = ? ORDER BY id", order["id"])
        self.assertEqual([(p["amount"], p["paid_at"], p["payment_type"], p["no_receipt"]) for p in payments],
                         [(1000, "2026-09-12 12:00", "CARD", 0), (500, "2026-09-12 12:00", "CARD", 0)])
        receipts = self.rows("SELECT * FROM dealer_modulkassa_receipts WHERE payment_id = ?", payments[0]["id"])
        self.assertEqual([(r["status"], r["doc_id"].startswith("to-dealer-doc-")) for r in receipts],
                         [("printed", True)])
        self.assertEqual(self.rows("SELECT id FROM modulkassa_receipts WHERE doc_id LIKE 'to-dealer-doc-%'"), [])
        page = self.client.get(f"/dealer/orders/{order['id']}").get_data(as_text=True)
        self.assertIn("Пробит", page)
        self.assertIn("PDF чека", page)

    def test_the_events_of_the_order_move_to_the_new_order(self):
        oid = self.tuning_order(goods=[self.ROPE_LINE()])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO order_events (order_kind, order_id, kind, text, event_date, created_at) "
                       "VALUES ('tuning', ?, 'dialog', 'Звонил клиент', '2026-09-11', '2026-09-11 10:00')", (oid,))
            db.commit()
        self.convert([oid])
        (order,) = self.dealer_orders()
        self.assertEqual([e["text"] for e in self.rows(
            "SELECT text FROM order_events WHERE order_kind = 'dealer' AND order_id = ?", order["id"])],
            ["Звонил клиент"])
        self.assertEqual(self.rows("SELECT id FROM order_events WHERE order_kind = 'tuning' AND order_id = ?", oid), [])

    def test_the_status_of_the_new_order_follows_the_old_one(self):
        cases = [("cancelled", [], "cancelled"), ("done", [], "completed"), ("handed_over", [], "completed"),
                 ("in_progress", [(1000, False)], "new_request"), ("in_progress", [(3600, False)], "paid"),
                 ("estimate", [], "new_request")]
        for status, payments, expected in cases:
            oid = self.tuning_order(status=status, goods=[self.ROPE_LINE("3", 1200)], payments=payments)
            self.convert([oid])
            order = self.dealer_orders()[-1]
            self.assertEqual(order["status"], expected, (status, payments))
            self.assertEqual(bool(order["completed_at"]), expected == "completed")
            self.cleanup_now()
            with application_module.app.app_context():
                db = application_module.get_db()
                db.execute("INSERT OR IGNORE INTO clients (id, client_name, boat_model, phone, token, status, created_at) "
                           "VALUES (?, ?, '', '+79990004455', 'to-dealer-token', 'neutral', '2026-09-01')",
                           (self.client_id, NAME))
                db.execute("INSERT OR IGNORE INTO supply_products (id, name, sku, cost_price, cost_unit, sale_price, "
                           "created_at) VALUES (?, ?, 'TODEALER-TEST-ROPE', 500, 'piece', 1200, 'x')",
                           (self.product_id, ROPE))
                db.commit()

    def test_stock_written_off_for_the_tuning_order_stays_written_off_as_a_shipment(self):
        oid = self.tuning_order(status="in_progress", goods=[(self.product_id, ROPE, "4", 1200, "piece", self.warehouse_id)])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE tuning_orders SET status = 'approved' WHERE id = ?", (oid,))
            db.commit()
            db.execute("UPDATE tuning_orders SET status = 'in_progress' WHERE id = ?", (oid,))
            application_module._sync_order_goods_writeoff(db, oid)
            db.commit()
        self.assertEqual(self.rows("SELECT quantity FROM supply_stock WHERE product_id = ?", self.product_id)[0]["quantity"], 6)
        self.convert([oid])
        (order,) = self.dealer_orders()
        self.assertEqual((order["status"], order["ship_warehouse_id"]), ("shipped", self.warehouse_id))
        (writeoff,) = self.rows("SELECT * FROM supply_writeoffs WHERE dealer_order_id = ?", order["id"])
        self.assertEqual((writeoff["quantity"], writeoff["warehouse_id"], writeoff["tuning_order_product_id"],
                          writeoff["project_id"]), (4, self.warehouse_id, None, None))
        self.assertEqual(self.rows("SELECT quantity FROM supply_stock WHERE product_id = ?", self.product_id)[0]["quantity"], 6)
        # and it is an ordinary shipment now: going back returns the goods
        self.client.post(f"/dealer/orders/{order['id']}/status", data={"status": "paid"})
        self.assertEqual(self.rows("SELECT quantity FROM supply_stock WHERE product_id = ?", self.product_id)[0]["quantity"], 10)

    # ---- to «Лодки и моторы»

    def test_a_boat_order_becomes_a_boat_and_motor_order(self):
        oid = self.tuning_order(status="approved", payments=[(100, False)])
        with application_module.app.app_context():
            db = application_module.get_db()
            profile_id = application_module._tuning_equipment_profile_id(db, "boat", BOAT)
            if profile_id is None:
                profile_id = db.execute(
                    "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, specifications, "
                    "created_at, updated_at) VALUES (?, ?, 'boat', '', 'x', 'x')",
                    (application_module._tuning_equipment_profile_key("boat", BOAT), BOAT)).lastrowid
            db.execute("INSERT OR REPLACE INTO dealer_prices (profile_id, price, updated_at) VALUES (?, 900000, 'x')",
                       (profile_id,))
            db.executemany(
                "INSERT INTO tuning_order_motors (order_id, motor_model, motor_serial_number, position, created_at) "
                "VALUES (?, ?, ?, ?, 'x')", [(oid, MOTOR_1, "M-1", 0), (oid, MOTOR_2, "", 1)])
            db.commit()
        notice, error = self.convert([oid], "equipment")
        self.assertIsNone(error, error)
        (order,) = self.dealer_orders()
        self.assertEqual((order["order_type"], order["status"]), ("equipment", "approved"))
        lines = self.rows("SELECT * FROM dealer_order_items WHERE order_id = ? ORDER BY position", order["id"])
        self.assertEqual([(l["kind"], l["name"], l["serial_number"], l["quantity"], l["price"]) for l in lines],
                         [("boat", BOAT, "BORT-1", 1, 900000), ("motor", MOTOR_1, "M-1", 1, 0),
                          ("motor", MOTOR_2, "", 1, 0)])
        self.assertIsNotNone(lines[0]["profile_id"])
        self.assertEqual(order["total"], 900000)
        self.assertEqual(self.rows("SELECT amount FROM dealer_payments WHERE order_id = ?", order["id"])[0]["amount"], 100)
        self.assertFalse(self.tuning_exists(oid))
        self.assertIn("Лодки и моторы", notice)

    def test_a_motor_order_becomes_one_motor_line(self):
        oid = self.tuning_order(equipment_type="motor", boat="", motor=MOTOR_1)
        self.convert([oid], "equipment")
        (order,) = self.dealer_orders()
        lines = self.rows("SELECT kind, name, serial_number FROM dealer_order_items WHERE order_id = ?", order["id"])
        self.assertEqual(lines, [{"kind": "motor", "name": MOTOR_1, "serial_number": "SN-9"}])

    # ---- what cannot be moved

    def test_an_order_with_works_is_refused_and_nothing_moves(self):
        with_works = self.tuning_order(goods=[self.ROPE_LINE()], works=2)
        clean = self.tuning_order(goods=[self.ROPE_LINE()])
        for target in ("goods", "equipment"):
            notice, error = self.convert([clean, with_works], target)
            self.assertIsNone(notice)
            self.assertIn(f"Заказ №{with_works}", error)
            self.assertIn("есть работы (2)", error)
            self.assertIn("Ничего не перенесено", error)
        self.assertTrue(self.tuning_exists(with_works))
        self.assertTrue(self.tuning_exists(clean))                  # the good one stays too
        self.assertEqual(self.dealer_orders(), [])

    def test_removed_works_do_not_block_the_move(self):
        oid = self.tuning_order(goods=[self.ROPE_LINE()])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, "
                       "price_pending, status) VALUES (?, 'Снятая', 1, 1, 1, 0, 'removed')", (oid,))
            db.commit()
        _, error = self.convert([oid])
        self.assertIsNone(error)
        self.assertFalse(self.tuning_exists(oid))
        self.assertEqual(self.rows("SELECT id FROM tuning_order_items WHERE order_id = ?", oid), [])

    def test_goods_do_not_fit_an_order_of_boats_and_motors_and_the_other_way_round(self):
        with_goods = self.tuning_order(goods=[self.ROPE_LINE()])
        notice, error = self.convert([with_goods], "equipment")
        self.assertIn("есть товары", error)
        self.assertIn("Морские товары", error)
        no_goods = self.tuning_order()
        _, error = self.convert([no_goods], "goods")
        self.assertIn("нет товаров", error)
        no_equipment = self.tuning_order(boat="")
        _, error = self.convert([no_equipment], "equipment")
        self.assertIn("не указаны лодка или мотор", error)
        for oid in (with_goods, no_goods, no_equipment):
            self.assertTrue(self.tuning_exists(oid))
        self.assertEqual(self.dealer_orders(), [])

    def test_orders_tied_to_other_records_are_refused(self):
        partner = self.tuning_order(goods=[self.ROPE_LINE()])
        yoo = self.tuning_order(goods=[self.ROPE_LINE()])
        bank = self.tuning_order(goods=[self.ROPE_LINE()])
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("INSERT INTO partner_commissions (order_id, partner_id, scheme, rate, base_total, amount, "
                       "recorded_at, updated_at) VALUES (?, 1, 'percent', 10, 1000, 100, 'x', 'x')", (partner,))
            db.execute("INSERT INTO tuning_yookassa_payments (order_id, yookassa_payment_id, amount, confirmation_url, "
                       "created_at, updated_at) VALUES (?, 'to-dealer-yoo', 1, 'u', 'x', 'x')", (yoo,))
            project = db.execute("SELECT id FROM projects WHERE tuning_order_id = ?", (bank,)).fetchone()["id"]
            db.execute("INSERT INTO bank_transactions (operation_id, account_number, operation_date, amount, direction, "
                       "project_id, created_at) VALUES ('to-dealer-bank-op', '1', '2026-09-01', 10, 'in', ?, 'x')", (project,))
            db.commit()
        for oid, text in ((partner, "комиссия партнёра"), (yoo, "ЮKassa"), (bank, "банковские операции")):
            _, error = self.convert([oid])
            self.assertIn(text, error)
            self.assertTrue(self.tuning_exists(oid))

    def test_a_failure_in_the_middle_changes_nothing(self):
        first = self.tuning_order(goods=[self.ROPE_LINE()], payments=[(1000, True)])
        second = self.tuning_order(goods=[self.ROPE_LINE()])
        with mock.patch.object(application_module, "_delete_tuning_order_records", side_effect=RuntimeError("boom")), \
                self.assertLogs(application_module.app.logger, level="ERROR"):
            _, error = self.convert([first, second])
        self.assertIn("ничего не изменено", error)
        self.assertEqual(self.dealer_orders(), [])
        self.assertTrue(self.tuning_exists(first) and self.tuning_exists(second))
        self.assertEqual(len(self.rows("SELECT id FROM modulkassa_receipts WHERE doc_id LIKE 'to-dealer-doc-%'")), 1)

    def test_a_target_is_required_and_subcontracts_are_not_touched(self):
        oid = self.tuning_order(goods=[self.ROPE_LINE()])
        notice, error = self.convert([oid], "")
        self.assertIn("Выберите, куда перенести", error)
        notice, error = self.convert([], "goods")
        self.assertIn("Выберите хотя бы один заказ", error)
        self.assertTrue(self.tuning_exists(oid))

    def test_several_orders_are_moved_at_once(self):
        ids = [self.tuning_order(goods=[self.ROPE_LINE("1", 1200)]), self.tuning_order(goods=[self.ROPE_LINE("2", 1200)])]
        notice, error = self.convert(ids)
        self.assertIsNone(error)
        self.assertIn("2 заказа", notice)
        self.assertEqual([o["total"] for o in self.dealer_orders()], [1200, 2400])


if __name__ == "__main__":
    unittest.main()
