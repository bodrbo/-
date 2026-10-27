import re
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "tuning-reorder-test"
TOKEN = "tuning-reorder-token"


class TuningOrderReorderTests(unittest.TestCase):
    """The works and the goods of a tuning order can be dragged into a new
    order; it is kept everywhere the lists are shown."""

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
        self.client.post("/tuning/add", data=self.form([("Первая", 100), ("Вторая", 200), ("Третья", 300)]))
        self.order_id = self.order()["id"]
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        ids = [r["id"] for r in db.execute("SELECT id FROM tuning_orders WHERE client_name = ?", (CLIENT,)).fetchall()]
        for order_id in ids:
            db.execute("DELETE FROM tuning_order_products WHERE order_id = ?", (order_id,))
        db.execute("DELETE FROM supply_products WHERE name = 'tuning-reorder-product'")
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        row = db.execute("SELECT id FROM clients WHERE token = ?", (TOKEN,)).fetchone()
        if row:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    # ---- helpers

    def form(self, works):
        """works: (name, cost[, item_id]) in the order of the rows on the page."""
        data = MultiDict([
            ("client_name", CLIENT), ("client_id", str(self.client_id)), ("equipment_type", "boat"),
            ("boat_model", "Salute"), ("boat_registration_number", ""), ("motor_model", ""),
            ("motor_serial_number", ""), ("phone", ""), ("order_date", "2026-09-10"),
            ("sale_channel", "direct"), ("discount_type", "percent"), ("discount_value", "0"),
        ])
        for work in works:
            data.add("work_name[]", work[0])
            data.add("cost_price[]", str(work[1]))
            data.add("multiplier[]", "2")
            data.add("item_discount_value[]", "")
            data.add("item_discount_type[]", "percent")
            data.add("item_id[]", str(work[2]) if len(work) > 2 else "")
        return data

    def order(self):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1", (CLIENT,)).fetchone())

    def works(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT id, work_name, position FROM tuning_order_items WHERE order_id = ? ORDER BY position, id",
                (self.order_id,)).fetchall()]

    def names(self):
        return [w["work_name"] for w in self.works()]

    def add_goods(self, names):
        with application_module.app.app_context():
            db = application_module.get_db()
            product = db.execute("SELECT id FROM supply_products WHERE name = 'tuning-reorder-product'").fetchone()
            product_id = product["id"] if product else db.execute(
                "INSERT INTO supply_products (name, cost_price, cost_unit, sale_price, created_at) "
                "VALUES ('tuning-reorder-product', 5, 'piece', 10, '2026-09-01 10:00')").lastrowid
            ids = []
            for name in names:
                ids.append(db.execute(
                    "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, unit_price, "
                    "cost_price, unit, created_at) VALUES (?, ?, ?, 1, 10, 5, 'piece', '2026-09-10 10:00')",
                    (self.order_id, product_id, name)).lastrowid)
            db.commit()
        return ids

    def goods(self):
        with application_module.app.app_context():
            return [r["product_name"] for r in application_module.get_db().execute(
                "SELECT product_name FROM tuning_order_products WHERE order_id = ? ORDER BY position, id",
                (self.order_id,)).fetchall()]

    def reorder_works(self, ids):
        return self.client.post(f"/tuning/{self.order_id}/works/reorder", data={"ids[]": [str(i) for i in ids]})

    def reorder_goods(self, ids):
        return self.client.post(f"/tuning/{self.order_id}/products/reorder", data={"ids[]": [str(i) for i in ids]})

    # ---- works

    def test_new_works_keep_the_order_of_the_form(self):
        self.assertEqual(self.names(), ["Первая", "Вторая", "Третья"])

    def test_the_works_are_moved_by_drag_and_drop(self):
        first, second, third = [w["id"] for w in self.works()]
        response = self.reorder_works([third, first, second])
        self.assertEqual(response.get_json(), {"ok": True, "count": 3})
        self.assertEqual(self.names(), ["Третья", "Первая", "Вторая"])

    def test_the_new_order_is_shown_on_the_order_page(self):
        first, second, third = [w["id"] for w in self.works()]
        self.reorder_works([second, third, first])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        order = [page.index(f">{name}</textarea>") for name in ("Вторая", "Третья", "Первая")]
        self.assertEqual(order, sorted(order))
        self.assertIn(f'data-reorder-url="/tuning/{self.order_id}/works/reorder"', page)
        self.assertIn('data-drag-handle', page)

    def test_the_new_order_is_shown_in_the_client_cabinet(self):
        first, second, third = [w["id"] for w in self.works()]
        self.reorder_works([third, second, first])
        page = self.client.get(f"/client/{TOKEN}").get_data(as_text=True)
        positions = [page.index(name) for name in ("Третья", "Вторая", "Первая")]
        self.assertEqual(positions, sorted(positions))

    def test_saving_the_form_keeps_the_dragged_order_and_the_ids(self):
        first, second, third = [w["id"] for w in self.works()]
        self.client.post(f"/tuning/edit/{self.order_id}", data=self.form(
            [("Третья", 300, third), ("Первая", 100, first), ("Вторая", 200, second)]))
        works = self.works()
        self.assertEqual([w["work_name"] for w in works], ["Третья", "Первая", "Вторая"])
        self.assertEqual([w["id"] for w in works], [third, first, second])     # ids untouched

    def test_a_work_added_by_the_form_takes_its_place_among_the_others(self):
        first, second, third = [w["id"] for w in self.works()]
        self.client.post(f"/tuning/edit/{self.order_id}", data=self.form(
            [("Первая", 100, first), ("Новая", 50), ("Вторая", 200, second), ("Третья", 300, third)]))
        self.assertEqual(self.names(), ["Первая", "Новая", "Вторая", "Третья"])

    def test_a_partial_or_foreign_list_cannot_lose_or_steal_rows(self):
        first, second, third = [w["id"] for w in self.works()]
        self.reorder_works([third, 999999, "abc", third])             # unknown / repeated / junk ids are ignored
        self.assertEqual(self.names(), ["Третья", "Первая", "Вторая"])
        self.reorder_works([second])                                  # only one id: the others follow in order
        self.assertEqual(self.names(), ["Вторая", "Третья", "Первая"])
        self.assertEqual(len(self.works()), 3)

    def test_an_unknown_order_is_refused(self):
        self.assertEqual(self.client.post("/tuning/999999/works/reorder", data={"ids[]": ["1"]}).status_code, 404)
        self.assertEqual(self.client.post("/tuning/999999/products/reorder", data={"ids[]": ["1"]}).status_code, 404)

    def test_login_is_required(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.post(f"/tuning/{self.order_id}/works/reorder", data={}).status_code, 302)
        self.assertEqual(anonymous.post(f"/tuning/{self.order_id}/products/reorder", data={}).status_code, 302)

    def test_a_copy_of_the_order_keeps_the_order_of_the_works(self):
        first, second, third = [w["id"] for w in self.works()]
        self.reorder_works([third, first, second])
        self.client.post(f"/tuning/{self.order_id}/copy")
        with application_module.app.app_context():
            db = application_module.get_db()
            copy_id = db.execute("SELECT MAX(id) FROM tuning_orders WHERE client_name = ?", (CLIENT,)).fetchone()[0]
            names = [r["work_name"] for r in db.execute(
                "SELECT work_name FROM tuning_order_items WHERE order_id = ? ORDER BY position, id", (copy_id,)).fetchall()]
        self.assertEqual(names, ["Третья", "Первая", "Вторая"])

    # ---- goods

    def test_goods_are_moved_by_drag_and_drop(self):
        a, b, c = self.add_goods(["Товар A", "Товар B", "Товар C"])
        self.assertEqual(self.goods(), ["Товар A", "Товар B", "Товар C"])
        response = self.reorder_goods([c, a, b])
        self.assertEqual(response.get_json(), {"ok": True, "count": 3})
        self.assertEqual(self.goods(), ["Товар C", "Товар A", "Товар B"])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        order = [page.index(name) for name in ("Товар C", "Товар A", "Товар B")]
        self.assertEqual(order, sorted(order))
        self.assertIn(f'data-reorder-url="/tuning/{self.order_id}/products/reorder"', page)

    def test_a_good_added_later_goes_last(self):
        a, b = self.add_goods(["Товар A", "Товар B"])
        self.reorder_goods([b, a])
        self.add_goods(["Товар Новый"])
        self.assertEqual(self.goods(), ["Товар B", "Товар A", "Товар Новый"])

    def test_the_works_of_another_order_are_not_touched(self):
        ids = [w["id"] for w in self.works()]
        self.client.post("/tuning/add", data=self.form([("Чужая 1", 1), ("Чужая 2", 2)]))
        other = self.order()["id"]
        with application_module.app.app_context():
            theirs = [r["id"] for r in application_module.get_db().execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ? ORDER BY id", (other,)).fetchall()]
        self.reorder_works(list(reversed(theirs)) + ids)              # foreign ids are ignored
        with application_module.app.app_context():
            names = [r["work_name"] for r in application_module.get_db().execute(
                "SELECT work_name FROM tuning_order_items WHERE order_id = ? ORDER BY position, id", (other,)).fetchall()]
        self.assertEqual(names, ["Чужая 1", "Чужая 2"])


class SortableScriptTests(unittest.TestCase):
    def test_the_drag_script_is_served_and_handles_touch_and_keyboard(self):
        client = application_module.app.test_client()
        script = client.get("/static/sortable.js").get_data(as_text=True)
        self.assertIn("pointerdown", script)
        self.assertIn("setPointerCapture", script)
        self.assertIn("ArrowUp", script)
        self.assertIn("window.initSortableList", script)


if __name__ == "__main__":
    unittest.main()
