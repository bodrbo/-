import json
import re
import sqlite3
import unittest
from pathlib import Path

from flask import Flask, session
from modules.release_notice import init_schema, register


class ReleaseNoticeTests(unittest.TestCase):
    def setUp(self):
        root = str(Path(__file__).resolve().parents[1])
        self.app = Flask('release-test', root_path=root)
        self.app.config.update(TESTING=True, SECRET_KEY='test-key')
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        init_schema(self.db)
        self.db.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, token TEXT)')
        self.db.execute("INSERT INTO clients VALUES (8, 'client-link')")
        self.app.add_url_rule('/page', 'page', lambda: '<html><body>Page</body></html>')
        self.app.add_url_rule('/client/<token>', 'client', lambda token: '<html><body>Cabinet</body></html>')
        self.app.add_url_rule('/data', 'data', lambda: {'ok': True})
        register(self.app, lambda: self.db,
                 lambda: ('employee', session['employee_id']) if session.get('employee_id') else None)
        self.client = self.app.test_client()
        self.addCleanup(self.db.close)

    def login(self, client=None, employee=1):
        with (client or self.client).session_transaction() as sess:
            sess['employee_id'] = employee

    def config(self, response):
        match = re.search(r'<script id="release-notice-config" type="application/json">(.*?)</script>',
                          response.get_data(as_text=True), re.S)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def ack(self, config, client=None):
        return (client or self.client).post(config['endpoint'], json={'token': config['token']})

    def test_acknowledgement_survives_new_session_and_next_release_shows_again(self):
        self.login()
        config = self.config(self.client.get('/page'))
        self.config(self.client.get('/page'))  # merely opening is not acknowledgement
        self.assertEqual(self.ack(config).status_code, 200)
        self.assertEqual(self.ack(config).status_code, 200)  # idempotent across tabs
        second_device = self.app.test_client()
        self.login(second_device)
        self.assertNotIn('id="release-notice"', second_device.get('/page').get_data(as_text=True))
        self.app.config['RELEASE_NOTICE'] = dict(self.app.config['RELEASE_NOTICE'], id='next-release')
        self.config(second_device.get('/page'))
        self.assertEqual(self.ack(config).status_code, 409)

    def test_other_accounts_must_acknowledge_for_themselves(self):
        self.login()
        config = self.config(self.client.get('/page'))
        self.login(employee=2)
        self.assertEqual(self.ack(config).status_code, 403)
        other_config = self.config(self.client.get('/page'))
        self.assertNotEqual(config['storageKey'], other_config['storageKey'])
        self.assertEqual(self.ack(other_config).status_code, 200)
        self.login(employee=1)
        self.config(self.client.get('/page'))

    def test_anonymous_and_background_requests_do_not_get_a_dialog(self):
        self.assertNotIn('release-notice', self.client.get('/page').get_data(as_text=True))
        self.login()
        self.assertNotIn('release-notice', self.client.get('/data').get_data(as_text=True))
        self.assertNotIn('release-notice', self.client.get('/page', headers={'X-Requested-With': 'XMLHttpRequest'}).get_data(as_text=True))
        self.assertNotIn('release-notice', self.client.get('/page', headers={'Sec-Fetch-Dest': 'empty'}).get_data(as_text=True))

    def test_client_links_have_separate_seen_state_and_client_summary(self):
        response = self.client.get('/client/client-link')
        config = self.config(response)
        self.assertNotIn('переключателем видимости', response.get_data(as_text=True))
        self.assertEqual(self.ack(config).status_code, 200)
        self.assertNotIn('release-notice', self.client.get('/client/client-link').get_data(as_text=True))
        self.login()
        self.config(self.client.get('/page'))

    def test_invalid_or_cross_site_acknowledgements_are_rejected(self):
        self.login()
        config = self.config(self.client.get('/page'))
        self.assertEqual(self.client.post(config['endpoint'], json={'token': 'fake'}).status_code, 400)
        self.assertEqual(self.client.post(config['endpoint'], json=[]).status_code, 400)
        self.assertEqual(self.client.post(config['endpoint'], json={'token': config['token']},
                                         headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)
        self.config(self.client.get('/page'))

    def test_demo_branding_does_not_show_company_logo(self):
        self.login()
        with self.client.session_transaction() as sess:
            sess['demo_tenant_id'] = 12
            sess['demo_tenant_name'] = 'Чужая верфь'
        html = self.client.get('/page').get_data(as_text=True)
        self.assertIn('Чужая верфь', html)
        self.assertNotIn('src="/static/logo-dark.svg', html)
