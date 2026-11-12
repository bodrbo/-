import os
import shutil
import subprocess
import tempfile
import unittest

from support import application_module
from test_tuning_goods_price import _GoodsFixture


def pdf_text(pdf):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "estimate.pdf")
        with open(path, "wb") as handle:
            handle.write(pdf)
        return subprocess.run(
            ["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True
        ).stdout


class TuningEstimatePdfTests(_GoodsFixture, unittest.TestCase):
    """«Расчёт заказа» in the order's documents: works, goods, the discount,
    what is to be paid and a place for the signatures."""

    MARK = "estimate-pdf-test"

    ORDER = {"id": 77, "created_at": "2026-09-10 10:00:00", "client_name": "Тестовый заказчик",
             "boat_model": "Тестовая лодка"}

    def build(self, order, items, goods=()):
        with application_module.app.test_request_context():
            return application_module._build_estimate_pdf(order, items, goods)

    def test_the_link_is_among_the_orders_documents(self):
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn(f"/tuning/{self.order_id}/estimate.pdf", page)
        self.assertIn("Расчёт заказа", page)

    def test_the_route_returns_a_pdf_even_for_an_order_with_only_goods(self):
        self.add(self.order_id)
        response = self.client.get(f"/tuning/{self.order_id}/estimate.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_an_unknown_order_goes_back_to_the_list(self):
        self.assertEqual(self.client.get("/tuning/99999999/estimate.pdf").status_code, 302)

    def test_the_route_needs_a_login(self):
        anonymous = application_module.app.test_client()
        response = anonymous.get(f"/tuning/{self.order_id}/estimate.pdf")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("%PDF", response.get_data(as_text=True))

    def test_the_document_lists_works_goods_the_total_and_both_signatures(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        items = [
            {"work_name": "Полировка корпуса", "price": 9000.0, "price_pending": 0, "status": "pending",
             "list_price": 10000.0, "discount_type": "percent", "discount_value": 10.0},
            {"work_name": "Замена пайола", "price": 500.0, "price_pending": 0, "status": "pending"},
            {"work_name": "Работа без цены", "price": 0.0, "price_pending": 1, "status": "pending"},
        ]
        goods = [{"product_name": "Герметик", "unit_price": 300.0, "quantity": 2.0, "unit": "piece"}]
        order = dict(self.ORDER, discount_type="percent", discount_value=10)
        text = pdf_text(self.build(order, items, goods))
        self.assertIn("Расчёт заказа", text)
        self.assertIn("№ 77", text)
        self.assertIn("Тестовый заказчик", text)
        for name in ("Полировка корпуса", "Замена пайола", "Работа без цены", "Герметик"):
            self.assertIn(name, text)
        self.assertIn("Ждёт расчёта", text)
        # (9 000 + 500) works + 600 goods = 10 100, less 10% = 9 090
        self.assertIn("К оплате:", text)
        self.assertIn("9090,00", text)
        self.assertIn("Итого к оплате", text)
        self.assertIn("Заказчик ____", text)
        self.assertIn("Исполнитель____", text)

    def test_an_empty_order_still_builds(self):
        pdf = self.build(dict(self.ORDER), [], [])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        if shutil.which("pdftotext"):
            self.assertIn("В заказе пока нет работ и товаров.", pdf_text(pdf))


if __name__ == "__main__":
    unittest.main()
