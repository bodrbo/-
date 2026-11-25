import io
import os
import tempfile
import unittest
from unittest.mock import patch
from support import application_module as m
from test_tuning_goods_price import _GoodsFixture


class WorkPhotoVisibilityTests(_GoodsFixture, unittest.TestCase):
    MARK = 'photo-visibility-test'

    def test_visibility_controls_listing_and_file_access(self):
        with m.app.app_context():
            db = m.get_db()
            cid = db.execute("INSERT INTO clients (client_name, token, created_at) VALUES ('Photo test', 'photo-test-token', '2026-11-25')").lastrowid
            db.execute('UPDATE tuning_orders SET client_id = ? WHERE id = ?', (cid, self.order_id))
            item = db.execute("INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, status) VALUES (?, 'Photo work', 0, 1, 0, 'approved')", (self.order_id,)).lastrowid
            db.commit()
        try:
            with tempfile.TemporaryDirectory() as folder, patch.object(m, '_work_photo_directory', return_value=folder):
                self.client.post(f'/tuning/{self.order_id}/item/{item}/photo', data={'photo': (io.BytesIO(b'photo-data'), 'test.jpg')})
                with m.app.test_request_context():
                    db = m.get_db()
                    photo = dict(db.execute('SELECT * FROM work_item_photos WHERE item_id = ?', (item,)).fetchone())
                    self.assertEqual(photo['client_visible'], 0)
                    self.assertEqual(m.get_work_item_photos(db, item, 'photo-test-token'), [])
                anon = m.app.test_client()
                url = f"/client/photo-test-token/work-photos/{photo['id']}"
                self.assertEqual(anon.get(url).status_code, 404)
                endpoint = f"/tuning/work-photos/{photo['id']}/visibility"
                self.assertEqual(anon.post(endpoint, data={'client_visible': '1'}).status_code, 302)
                self.assertEqual(self.client.post(endpoint, data={'client_visible': '1'}).status_code, 200)
                response = anon.get(url)
                self.assertEqual(response.data, b'photo-data')
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(anon.get(url.replace('photo-test-token', 'wrong-token')).status_code, 404)
                with m.app.test_request_context():
                    self.assertEqual(len(m.get_work_item_photos(m.get_db(), item, 'photo-test-token')), 1)
                self.client.post(endpoint, data={'client_visible': '0'})
                self.assertEqual(anon.get(url).status_code, 404)
                self.assertEqual(self.client.get(f"/tuning/work-photos/{photo['id']}").status_code, 200)
        finally:
            with m.app.app_context():
                db = m.get_db()
                db.execute('UPDATE tuning_orders SET client_id = NULL WHERE id = ?', (self.order_id,))
                db.execute('DELETE FROM clients WHERE id = ?', (cid,))
                db.commit()
