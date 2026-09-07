import unittest

from reportlab.lib.units import mm

from support import application_module


class TuningActPdfTests(unittest.TestCase):
    def test_review_qr_contains_yandex_review_link(self):
        drawing = application_module._build_tuning_review_qr(30 * mm)

        self.assertEqual(drawing.width, 30 * mm)
        self.assertEqual(drawing.height, 30 * mm)
        self.assertEqual(len(drawing.contents), 1)
        self.assertEqual(
            drawing.contents[0].value,
            "https://yandex.ru/maps/org/bodry_botsman/15778336383/reviews/"
            "?add-review=true",
        )
        self.assertIn("add-review=true", drawing.contents[0].value)

    def test_completed_work_act_with_review_qr_builds_as_pdf(self):
        order = {
            "id": 42,
            "created_at": "2026-08-28 12:00:00",
            "client_name": "Тестовый заказчик",
            "boat_model": "Тестовая лодка",
        }
        items = [{"work_name": "Диагностика двигателя", "price": 2500.0}]

        pdf = application_module._build_act_pdf(order, items)

        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 1000)

    def test_handover_act_builds_with_work_awaiting_price(self):
        order = {
            "id": 43,
            "created_at": "2026-08-28 12:00:00",
            "client_name": "Тестовый заказчик",
            "boat_model": "Тестовая лодка",
            "discount_type": "percent",
            "discount_value": 0,
        }
        items = [{
            "work_name": "Работа после дефектовки",
            "price": 0.0,
            "price_pending": 1,
        }]

        pdf = application_module._build_handover_act_pdf(order, items)

        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 1000)


if __name__ == "__main__":
    unittest.main()
