import unittest

from support import application_module
import test_tuning_goods_writeoff as _writeoff


class _Fixture:
    """The write-off tests' warehouse/product/order fixture, without their tests."""

    setUp = _writeoff.TuningGoodsWriteoffTests.setUp
    tearDown = _writeoff.TuningGoodsWriteoffTests.tearDown
    _cleanup = _writeoff.TuningGoodsWriteoffTests._cleanup
    set_status = _writeoff.TuningGoodsWriteoffTests.set_status
    add = _writeoff.TuningGoodsWriteoffTests.add
    stock = _writeoff.TuningGoodsWriteoffTests.stock
    writeoffs = _writeoff.TuningGoodsWriteoffTests.writeoffs
    page = _writeoff.TuningGoodsWriteoffTests.page


class TuningGoodsQuantityTests(_Fixture, unittest.TestCase):
    """The quantity of a goods line can be changed after it was added; a line
    already written off moves the difference to/from its warehouse."""

    MARK = "goods-quantity-test"

    def line(self):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_order_products WHERE order_id = ?", (self.order_id,)
            ).fetchone())

    def order_total(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT subtotal FROM tuning_orders WHERE id = ?", (self.order_id,)
            ).fetchone()["subtotal"]

    def set_quantity(self, value):
        line_id = self.line()["id"]
        return self.client.post(
            f"/tuning/{self.order_id}/products/{line_id}/quantity", data={"quantity": value}
        )

    def test_the_quantity_field_is_in_the_goods_table(self):
        self.add()
        line_id = self.line()["id"]
        page = self.page()
        self.assertIn(f"/tuning/{self.order_id}/products/{line_id}/quantity", page)
        self.assertIn('name="quantity" value="3"', page)

    def test_changing_the_quantity_updates_the_line_and_the_total(self):
        self.add()
        response = self.set_quantity("4,5")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("#goods"))
        self.assertEqual(self.line()["quantity"], 4.5)
        self.assertEqual(self.order_total(), 4.5 * 500)
        self.assertIn("Количество товара в заказе обновлено.", self.page())

    def test_a_bad_quantity_is_refused(self):
        self.add()
        for value in ("0", "-1", "abc", ""):
            self.set_quantity(value)
            self.assertEqual(self.line()["quantity"], 3)
        self.assertIn("Количество должно быть числом больше нуля.", self.page())

    def test_a_written_off_line_moves_the_difference_on_the_warehouse(self):
        self.add(warehouse=self.warehouse_id)
        self.set_status("in_progress")
        self.assertEqual(self.stock(), 7)

        self.set_quantity("5")
        self.assertEqual(self.stock(), 5)
        rows = self.writeoffs()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["quantity"], rows[0]["amount"]), (5, 500))

        self.set_quantity("1")
        self.assertEqual(self.stock(), 9)
        self.assertEqual(self.writeoffs()[0]["quantity"], 1)

        self.set_status("approved")  # sent back: the whole new quantity returns
        self.assertEqual(self.stock(), 10)

    def test_not_enough_stock_for_more_keeps_the_old_quantity(self):
        self.add(warehouse=self.warehouse_id)
        self.set_status("in_progress")
        self.set_quantity("20")  # 7 left, 17 more needed
        self.assertEqual(self.line()["quantity"], 3)
        self.assertEqual(self.stock(), 7)
        self.assertEqual(self.writeoffs()[0]["quantity"], 3)
        self.assertIn("не изменено", self.page())

    def test_a_line_waiting_for_the_write_off_uses_the_new_quantity_later(self):
        self.add(warehouse=self.warehouse_id)
        self.set_quantity("6")
        self.assertEqual(self.stock(), 10)
        self.set_status("in_progress")
        self.assertEqual(self.stock(), 4)

    def test_another_orders_line_cannot_be_changed(self):
        self.add()
        line_id = self.line()["id"]
        self.client.post(f"/tuning/99999999/products/{line_id}/quantity", data={"quantity": "9"})
        self.assertEqual(self.line()["quantity"], 3)

if __name__ == "__main__":
    unittest.main()
