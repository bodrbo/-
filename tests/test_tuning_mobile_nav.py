import re
import unittest

from support import application_module


class TuningMobileNavTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def current_label(self, html):
        match = re.search(r'<span class="sub-tabs-toggle-current">([^<]+)</span>', html)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_collapsible_subnav_names_current_section(self):
        for url, label in (
            ("/tuning/board", "Доска проектов"),
            ("/tuning", "Заказы"),
            ("/tuning/subcontracts", "Субподряды"),
            ("/tuning/trailers", "Каталог прицепов"),
            ("/tuning/shop-map", "Карты пространств"),
        ):
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                self.assertEqual(self.current_label(html), label)
                self.assertIn('aria-controls="tuningSubTabs"', html)
                self.assertIn('Выбрать раздел', html)
                self.assertIn('<nav class="sub-tabs">', html)
                self.assertRegex(html, rf'<a aria-current="page" href="[^"]+" class="active"[^>]*>{label}</a>')

    def test_orders_heading_has_plus_with_create_choices(self):
        html = self.client.get("/tuning").get_data(as_text=True)
        self.assertIn('class="tuning-create-plus"', html)
        self.assertIn("Что создать?", html)
        self.assertIn('href="/tuning/add" class="tuning-create-option"', html)
        self.assertIn('href="/tuning/subcontract/add" class="tuning-create-option"', html)
        # Desktop keeps its text buttons; CSS hides them on phones.
        self.assertIn('class="btn-add tuning-create-desktop" data-tour-target="tuning-add-order">Добавить заказ</a>', html)
        self.assertEqual(html.count("<dialog class=\"tuning-create-dialog\""), 1)

    def test_subcontracts_heading_has_plus(self):
        html = self.client.get("/tuning/subcontracts").get_data(as_text=True)
        self.assertIn('class="tuning-create-plus"', html)
        self.assertEqual(html.count("<dialog class=\"tuning-create-dialog\""), 1)


if __name__ == "__main__":
    unittest.main()
