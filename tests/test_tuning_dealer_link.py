import unittest
from support import application_module as m
from test_tuning_goods_price import _GoodsFixture


class TuningDealerLinkTests(_GoodsFixture, unittest.TestCase):
    MARK = 'tuning-dealer-link-test'

    def setUp(self):
        super().setUp()
        with m.app.app_context():
            db = m.get_db()
            self.dealer_id = db.execute(
                "INSERT INTO dealer_orders (client_name, order_date, created_at, updated_at) VALUES (?, '2026-11-26', '2026-11-26', '2026-11-26')",
                (self.MARK,)).lastrowid
            db.execute("INSERT INTO dealer_order_items (order_id, kind, name) VALUES (?, 'boat', 'Касатка 520')", (self.dealer_id,))
            db.commit()
        self.addCleanup(self.clear_dealer)
        self.url = f'/tuning/{self.order_id}/dealer-link'

    def clear_dealer(self):
        with m.app.app_context():
            db = m.get_db()
            db.execute('DELETE FROM dealer_order_items WHERE order_id=?', (self.dealer_id,))
            db.execute('DELETE FROM dealer_orders WHERE id=?', (self.dealer_id,))
            db.commit()

    def linked(self):
        with m.app.app_context():
            return m.get_db().execute('SELECT dealer_order_id FROM tuning_orders WHERE id=?', (self.order_id,)).fetchone()[0]

    def test_link_and_unlink_preserve_both_orders_and_show_standard_badge(self):
        self.add(self.order_id)
        before, total = self.lines(self.order_id)
        response = self.client.post(self.url, data={'dealer_order_id': self.dealer_id})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.linked(), self.dealer_id)
        after, new_total = self.lines(self.order_id)
        self.assertEqual([dict(row) for row in before], [dict(row) for row in after])
        self.assertEqual(total, new_total)
        html = self.client.get('/tuning').get_data(as_text=True)
        self.assertIn('Куплена у Бодрого Боцмана', html)
        self.assertIn(f'/dealer/orders/{self.dealer_id}', html)
        self.client.post(self.url, data={'dealer_order_id': ''})
        self.assertIsNone(self.linked())
        with m.app.app_context():
            self.assertIsNotNone(m.get_db().execute('SELECT id FROM dealer_orders WHERE id=?', (self.dealer_id,)).fetchone())

    def test_search_by_number_and_cyrillic_equipment_and_current_link(self):
        data = self.client.get(self.url, query_string={'q': 'КАСАТКА 520'}).get_json()
        self.assertIn(self.dealer_id, [row['id'] for row in data['orders']])
        self.client.post(self.url, data={'dealer_order_id': self.dealer_id})
        data = self.client.get(self.url, query_string={'q': 'ничего-не-найдётся'}).get_json()
        self.assertEqual(data['current_id'], self.dealer_id)
        self.assertIn(self.dealer_id, [row['id'] for row in data['orders']])

    def test_invalid_dealer_order_does_not_overwrite_existing_link(self):
        self.client.post(self.url, data={'dealer_order_id': self.dealer_id})
        for value in ('invalid', '-1', '99999999', '9' * 40):
            self.client.post(self.url, data={'dealer_order_id': value})
            self.assertEqual(self.linked(), self.dealer_id)

    def test_requires_admin_and_only_applies_to_regular_tuning_orders(self):
        anonymous = m.app.test_client()
        self.assertEqual(anonymous.get(self.url).status_code, 302)
        self.assertEqual(anonymous.post(self.url, data={'dealer_order_id': self.dealer_id}).status_code, 302)
        with m.app.app_context():
            db = m.get_db()
            db.execute("UPDATE tuning_orders SET source='subcontract_request' WHERE id=?", (self.order_id,))
            db.commit()
        self.assertEqual(self.client.post(self.url, data={'dealer_order_id': self.dealer_id}).status_code, 404)
