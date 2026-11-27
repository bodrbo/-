import json
import unittest
from support import application_module as m
from modules.tuning_payment_terms import normalize_payment_terms, read_payment_terms, TermsError
from test_tuning_goods_price import _GoodsFixture


class TuningPaymentTermsTests(_GoodsFixture, unittest.TestCase):
    MARK = 'tuning-payment-terms-test'

    def setUp(self):
        super().setUp()
        self.url = f'/tuning/{self.order_id}/payment-terms'

    def test_prepaid_percent_persists_and_shows_in_summary(self):
        self.assertIsNone(self.client.get(self.url).get_json()['terms'])
        response = self.client.post(self.url, json={'type': 'prepayment', 'percent': '37,5'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['label'], 'Предоплата 37,5%')
        with m.app.app_context():
            order = m.get_db().execute('SELECT * FROM tuning_orders WHERE id=?', (self.order_id,)).fetchone()
            self.assertEqual(read_payment_terms(order), {'type': 'prepayment', 'percent': '37.5'})
        html = self.client.get(f'/tuning/edit/{self.order_id}').get_data(as_text=True)
        self.assertRegex(html, r'aria-haspopup="dialog">Предоплата 37,5%</button>')
        self.assertIsNone(self.client.get(f'/tuning/{self.other_order_id}/payment-terms').get_json()['terms'])

    def test_schedule_dates_and_amounts_are_sorted_and_exact(self):
        payload = {'type': 'schedule', 'payments': [
            {'date': '2026-12-20', 'amount': '1234,56'},
            {'date': '2026-12-01', 'amount': '10 000'},
        ]}
        response = self.client.post(self.url, json=payload)
        self.assertEqual(response.status_code, 200)
        terms = response.get_json()['terms']
        self.assertEqual(terms['payments'], [
            {'date': '2026-12-01', 'amount': '10000.00'},
            {'date': '2026-12-20', 'amount': '1234.56'},
        ])
        self.assertEqual(self.client.get(self.url).get_json()['terms'], terms)
        html = self.client.get(f'/tuning/edit/{self.order_id}').get_data(as_text=True)
        self.assertRegex(html, r'aria-haspopup="dialog">График платежей</button>')
        with m.app.app_context():
            db = m.get_db()
            self.assertEqual(db.execute('SELECT total FROM tuning_orders WHERE id=?', (self.order_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tuning_payments WHERE order_id=?', (self.order_id,)).fetchone()[0], 0)

    def test_validation_preserves_previous_conditions(self):
        old = {'type': 'prepayment', 'percent': '30'}
        self.client.post(self.url, json=old)
        bad = [None, [], {'type': 'wrong'}, {'type': 'prepayment', 'percent': 'NaN'},
               {'type': 'prepayment', 'percent': '101'}, {'type': 'prepayment', 'percent': '-5'},
               {'type': 'prepayment', 'percent': '30.333'}, {'type': 'schedule', 'payments': []},
               {'type': 'schedule', 'payments': [{'date': '2026-02-30', 'amount': '5'}]},
               {'type': 'schedule', 'payments': [{'date': '2026-12-01', 'amount': '-5'}]},
               {'type': 'schedule', 'payments': [{'date': '2026-12-01', 'amount': '0.001'}]}]
        for payload in bad:
            with self.subTest(payload=payload):
                response = self.client.post(self.url, data=json.dumps(payload), content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.client.get(self.url).get_json()['terms'], old)

    def test_conditions_can_be_changed_and_cleared(self):
        self.client.post(self.url, json={'type': 'prepayment', 'percent': 50})
        self.client.post(self.url, json={'type': 'schedule', 'payments': [{'date': '2027-01-01', 'amount': '200'}]})
        response = self.client.post(self.url, json={'type': 'none'})
        self.assertEqual(response.get_json(), {'terms': None, 'label': 'Назначить'})
        with m.app.app_context():
            value = m.get_db().execute('SELECT payment_terms_json FROM tuning_orders WHERE id=?', (self.order_id,)).fetchone()[0]
            self.assertIsNone(value)

    def test_access_control(self):
        anonymous = m.app.test_client()
        self.assertEqual(anonymous.get(self.url).status_code, 302)
        self.assertEqual(anonymous.post(self.url, json={'type': 'none'}).status_code, 302)
        self.assertEqual(self.client.get('/tuning/999999999/payment-terms').status_code, 404)
        self.assertEqual(self.client.post(self.url, json={'type': 'none'}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_helper_handles_legacy_orders_and_invalid_data(self):
        self.assertIsNone(read_payment_terms({}))
        self.assertIsNone(read_payment_terms({'payment_terms_json': '{bad json'}))
        self.assertEqual(normalize_payment_terms({'type': 'prepayment', 'percent': 0})['percent'], '0')
