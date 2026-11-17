import datetime as dt
import shutil
import unittest

from flask import session

from support import application_module
from test_tuning_estimate_pdf import pdf_text
from test_tuning_goods_price import _GoodsFixture


class TuningCommercialOfferPdfTests(_GoodsFixture, unittest.TestCase):
    """«Коммерческое предложение» in the order's documents: a formal letter
    with no logo, the company's requisites, the works and goods with prices
    and the validity term."""

    MARK = "offer-pdf-test"

    ORDER = {"id": 78, "created_at": "2026-09-10 10:00:00", "client_name": "Тестовый заказчик",
             "boat_model": "Тестовая лодка"}

    def build(self, order, items, goods=(), demo=False, vat_tag=None):
        with application_module.app.test_request_context():
            if demo:
                session["demo_tenant_id"] = 1
                session["demo_tenant_name"] = "Демо-верфь"
            return application_module._build_commercial_offer_pdf(
                order, items, goods, today=dt.date(2026, 11, 17), vat_tag=vat_tag
            )

    def test_the_link_is_among_the_orders_documents(self):
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn(f"/tuning/{self.order_id}/offer.pdf", page)
        self.assertIn("Коммерческое предложение", page)

    def test_the_route_returns_a_pdf(self):
        self.add(self.order_id)
        response = self.client.get(f"/tuning/{self.order_id}/offer.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))

    def test_an_unknown_order_goes_back_to_the_list(self):
        self.assertEqual(self.client.get("/tuning/99999999/offer.pdf").status_code, 302)

    def test_the_route_needs_a_login(self):
        anonymous = application_module.app.test_client()
        response = anonymous.get(f"/tuning/{self.order_id}/offer.pdf")
        self.assertEqual(response.status_code, 302)

    def test_there_is_no_logo_unlike_the_estimate(self):
        items = [{"work_name": "Полировка", "price": 1000.0, "price_pending": 0, "status": "pending"}]
        with application_module.app.test_request_context():
            estimate = application_module._build_estimate_pdf(dict(self.ORDER), items, [])
        self.assertIn(b"/Subtype /Image", estimate)
        self.assertNotIn(b"/Subtype /Image", self.build(dict(self.ORDER), items, []))

    def test_the_document_has_the_requisites_prices_and_validity(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        items = [
            {"work_name": "Полировка корпуса", "price": 9000.0, "price_pending": 0, "status": "pending"},
            {"work_name": "Работа без цены", "price": 0.0, "price_pending": 1, "status": "pending"},
        ]
        goods = [{"product_name": "Герметик", "unit_price": 300.0, "quantity": 2.0, "unit": "piece"}]
        text = pdf_text(self.build(dict(self.ORDER), items, goods, vat_tag=1109))
        self.assertRegex(text, r"В том числе НДС 5%: 457,14 руб\.")
        self.assertIn("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ", text)
        self.assertIn("КП-78 от 17.11.2026", text)
        self.assertIn("Тестовый заказчик", text)
        for name in ("Полировка корпуса", "Работа без цены", "Герметик"):
            self.assertIn(name, text)
        self.assertRegex(text, r"9\s600,00 руб\.")
        self.assertIn("Девять тысяч шестьсот рублей", text)
        self.assertIn("будет сообщена дополнительно", text)
        self.assertIn("действительно до 01.12.2026", text)
        for value in ("7843019417", "784301001", "1217800157101", "40702810710000912141",
                      "044525974", "30101810145250000974", "АО «ТБанк»"):
            self.assertIn(value, text)
        self.assertIn("Генеральный директор", text)
        # requisites live only in the letterhead, no separate table
        self.assertNotIn("Реквизиты", text)
        self.assertNotIn("Полное наименование", text)
        self.assertIn("р/с 40702810710000912141 в АО «ТБанк»", text)
        self.assertIn("Д. Е. Жмаев", text)

    def test_the_vat_line_follows_the_rate_from_the_settings(self):
        line = application_module._commercial_offer_vat_line
        self.assertEqual(line(10500.0, 1109), "В том числе НДС 5%: 500,00 руб.")
        self.assertEqual(line(12200.0, 1113), "В том числе НДС 22%: 2\u00a0200,00 руб.")
        self.assertEqual(line(1000.0, 1105), "НДС не облагается.")
        self.assertIsNone(line(1000.0, 9999))

    def test_the_route_takes_the_vat_rate_from_the_settings(self):
        from modules.settings import repository as settings_repository

        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        with application_module.app.app_context():
            db = application_module.get_db()
            before = settings_repository.get_value(db, "modulkassa_vat_tag", "")
            settings_repository.set_value(db, "modulkassa_vat_tag", "1105", "2026-11-17 10:00:00")
            db.commit()
        try:
            response = self.client.get(f"/tuning/{self.order_id}/offer.pdf")
            self.assertIn("НДС не облагается.", pdf_text(response.data))
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                settings_repository.set_value(db, "modulkassa_vat_tag", before, "2026-11-17 10:00:00")
                db.commit()

    def test_a_demo_tenant_never_sees_our_requisites(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        items = [{"work_name": "Полировка", "price": 1000.0, "price_pending": 0, "status": "pending"}]
        text = pdf_text(self.build(dict(self.ORDER), items, [], demo=True))
        self.assertIn("Демо-верфь", text)
        self.assertNotIn("7843019417", text)
        self.assertNotIn("Бодрый Боцман", text)
        self.assertNotIn("Реквизиты", text)
        self.assertNotIn("Жмаев", text)

    def test_markup_and_an_empty_order_still_build(self):
        pdf = self.build(dict(self.ORDER, client_name="ИП <Иванов> & Ко"), [], [])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        if shutil.which("pdftotext"):
            text = pdf_text(pdf)
            self.assertIn("ИП <Иванов> & Ко", text)
            self.assertIn("Состав работ уточняется.", text)


if __name__ == "__main__":
    unittest.main()
