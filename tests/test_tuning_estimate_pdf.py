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

    def test_a_total_of_a_million_and_more_builds_and_is_spoken_in_words(self):
        words = application_module._rubles_to_words
        self.assertEqual(words(1000000), "Один миллион рублей 00 копеек")
        self.assertEqual(words(2350000.5), "Два миллиона триста пятьдесят тысяч рублей 50 копеек")
        self.assertEqual(words(11000000), "Одиннадцать миллионов рублей 00 копеек")
        self.assertEqual(words(1500000000), "Один миллиард пятьсот миллионов рублей 00 копеек")
        self.assertEqual(words(-5), "Ноль рублей 00 копеек")
        items = [{"work_name": "Лодка целиком", "price": 3400000.0, "price_pending": 0, "status": "pending"}]
        pdf = self.build(dict(self.ORDER), items, [])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        if shutil.which("pdftotext"):
            self.assertIn("Три миллиона четыреста тысяч рублей", pdf_text(pdf))

    def test_markup_characters_in_names_do_not_break_the_document(self):
        items = [{"work_name": "Болт <20 мм & гайка", "price": 100.0, "price_pending": 0, "status": "pending"}]
        goods = [{"product_name": "Лента <b>жирная", "unit_price": 10.0, "quantity": 1.0, "unit": "piece"}]
        pdf = self.build(dict(self.ORDER, client_name="ИП <Иванов> & Ко"), items, goods)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        if shutil.which("pdftotext"):
            text = pdf_text(pdf)
            self.assertIn("Лента <b>жирная", text)
            self.assertIn("ИП <Иванов> & Ко", text)

    def test_an_unexpected_error_is_logged_and_answered_in_words(self):
        self.add(self.order_id)
        original = application_module._build_estimate_pdf
        application_module._build_estimate_pdf = lambda *args: 1 / 0
        try:
            with self.assertLogs(application_module.app.logger, level="ERROR"):
                response = self.client.get(f"/tuning/{self.order_id}/estimate.pdf")
        finally:
            application_module._build_estimate_pdf = original
        self.assertEqual(response.status_code, 500)
        self.assertIn("Не удалось сформировать документ", response.get_data(as_text=True))

    def test_an_empty_order_still_builds(self):
        pdf = self.build(dict(self.ORDER), [], [])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        if shutil.which("pdftotext"):
            self.assertIn("В заказе пока нет работ и товаров.", pdf_text(pdf))


if __name__ == "__main__":
    unittest.main()
