import io
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from support import application_module as a
import test_dealer_center as dealer_tests


class DealerOptionsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dealer_tests.DealerCenterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = patch.dict(a.app.config, DEALER_OPTION_PHOTO_DIR=self.directory.name)
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(self.clear_options)

    def clear_options(self):
        with a.app.app_context():
            db = a.get_db()
            db.execute("DELETE FROM dealer_boat_options")
            db.commit()

    def rows(self, sql, args=()):
        with a.app.app_context():
            return [dict(r) for r in a.get_db().execute(sql, args).fetchall()]

    def photo(self):
        output = io.BytesIO()
        Image.new('RGB', (8, 6), 'blue').save(output, 'PNG')
        output.seek(0)
        return output, 'photo.png'

    def option_form(self, **values):
        data = dict(name='Тент для лодки', description='Защита от дождя и солнца', price='25000',
                    profile_id=str(self.fixture.boat_id))
        data.update(values)
        return data

    def create_option(self, **values):
        response = self.client.post('/dealer/options', data={**self.option_form(**values), 'photo': self.photo()})
        self.assertEqual(response.status_code, 302)
        return self.rows('SELECT * FROM dealer_boat_options ORDER BY id DESC LIMIT 1')[0]

    def order_form(self, option_ids, quantity='1', item_id='', price='100000', **overrides):
        form = self.fixture.form(items=[('boat', str(self.fixture.boat_id), 'Дилерская лодка Тест', '', quantity, price)], **overrides)
        form['item_option_ids[]'] = json.dumps(option_ids)
        form['item_id[]'] = str(item_id)
        return form

    def create_order(self, option_ids, **fields):
        response = self.client.post('/dealer/orders/new', data=self.order_form(option_ids, **fields))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return self.fixture.order()

    def test_catalog_photo_and_admin_access(self):
        option = self.create_option()
        page = self.client.get('/dealer/options').get_data(as_text=True)
        self.assertIn(option['name'], page)
        self.assertIn(option['description'], page)
        photo = self.client.get('/dealer/options/photos/' + option['photo_filename'])
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, 'image/jpeg')
        self.assertEqual(Image.open(io.BytesIO(photo.get_data())).size, (8, 6))
        photo.close()
        anonymous = a.app.test_client()
        self.assertEqual(anonymous.get('/dealer/options').status_code, 302)
        self.assertEqual(anonymous.post('/dealer/options', data={}).status_code, 302)
        self.assertEqual(anonymous.post(f"/dealer/options/{option['id']}/active", data={}).status_code, 302)
        self.assertEqual(self.client.get('/dealer/options/photos/invalid.jpg').status_code, 404)

    def test_schema_upgrade_preserves_old_selling_prices(self):
        from modules.dealer.schema import init_schema
        from modules.dealer.options import item_view

        with sqlite3.connect(':memory:') as db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE dealer_order_items (id INTEGER PRIMARY KEY, order_id INTEGER, price REAL)')
            db.execute('INSERT INTO dealer_order_items VALUES (1, 1, 100000)')
            init_schema(db)
            init_schema(db)
            item = item_view(db.execute('SELECT * FROM dealer_order_items').fetchone())
            self.assertEqual((item['price'], item['base_price'], item['options']), (100000, 100000, []))

    def test_required_photo_description_and_valid_cost(self):
        cases = [self.option_form(), self.option_form(description=''), self.option_form(price='-1'),
                 self.option_form(price='nan'), self.option_form(profile_id=str(self.fixture.motor_id))]
        for data in cases:
            with self.subTest(data=data):
                response = self.client.post('/dealer/options', data=data)
                self.assertEqual(response.status_code, 400)
        response = self.client.post('/dealer/options', data={**self.option_form(), 'photo': (io.BytesIO(b'<svg/>'), 'photo.jpg')})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rows('SELECT id FROM dealer_boat_options'), [])

    def test_options_are_per_boat_and_count_in_quantity_and_discount(self):
        option = self.create_option()
        order = self.create_order([option['id']], quantity='2', discount_value='10')
        self.assertEqual((order['subtotal'], order['total']), (250000, 225000))
        item = self.fixture.items(order['id'])[0]
        self.assertEqual(item['price'], 125000)
        self.assertEqual(json.loads(item['options_json'])[0]['description'], option['description'])
        page = self.client.get(f"/dealer/orders/{order['id']}").get_data(as_text=True)
        data = json.loads(page.split('<script id="dealerItemsData" type="application/json">')[1].split('</script>')[0])
        self.assertEqual(data[0]['base_price'], 100000)
        self.assertEqual(data[0]['options'][0]['id'], option['id'])
        with a.app.app_context():
            cabinet = a._client_dealer_orders(a.get_db(), order['client_id'], False)
            token = a.get_db().execute('SELECT token FROM clients WHERE id = ?', (order['client_id'],)).fetchone()['token']
        self.assertEqual(cabinet[0]['items'][0]['options'][0]['name'], option['name'])
        page = a.app.test_client().get(f'/client/{token}').get_data(as_text=True)
        self.assertIn(option['description'], page)
        self.assertIn('/dealer/options/photos/' + option['photo_filename'], page)
        self.assertEqual(self.rows('SELECT id FROM supply_writeoffs WHERE dealer_order_id = ?', (order['id'],)), [])

    def test_saved_snapshots_survive_price_photo_changes_and_archiving(self):
        option = self.create_option()
        order = self.create_order([option['id']])
        item = self.fixture.items(order['id'])[0]
        response = self.client.post(f"/dealer/options/{option['id']}", data={
            **self.option_form(price='50000', description='Новое описание'), 'photo': self.photo(),
        })
        self.assertEqual(response.status_code, 302)
        self.client.post(f"/dealer/options/{option['id']}/active", data={'active': '0'})
        response = self.client.post(f"/dealer/orders/{order['id']}", data=self.order_form([option['id']], item_id=item['id']))
        self.assertEqual(response.status_code, 302)
        saved = self.fixture.items(order['id'])[0]
        self.assertEqual(saved['price'], 125000)
        snapshot = json.loads(saved['options_json'])[0]
        self.assertEqual(snapshot['description'], option['description'])
        self.assertEqual(snapshot['photo_filename'], option['photo_filename'])
        photo = self.client.get('/dealer/options/photos/' + option['photo_filename'])
        self.assertEqual(photo.status_code, 200)
        photo.close()
        self.assertEqual(self.client.post('/dealer/orders/new', data=self.order_form([option['id']])).status_code, 400)
        self.assertEqual(self.client.post(f"/dealer/orders/{order['id']}", data=self.order_form([], item_id=saved['id'])).status_code, 302)
        self.assertEqual(self.fixture.order()['total'], 100000)

    def test_unknown_duplicate_and_incompatible_options_are_rejected(self):
        option = self.create_option()
        cases = [self.order_form([999999]), self.order_form([option['id'], option['id']])]
        motor = self.order_form([option['id']])
        motor['item_kind[]'] = 'motor'
        motor['item_profile_id[]'] = str(self.fixture.motor_id)
        cases.append(motor)
        free_boat = self.order_form([option['id']])
        free_boat['item_profile_id[]'] = ''
        cases.append(free_boat)
        invalid_json = self.order_form([])
        invalid_json['item_option_ids[]'] = '{"price":1}'
        cases.append(invalid_json)
        for form in cases:
            with self.subTest(form=list(form.items())):
                self.assertEqual(self.client.post('/dealer/orders/new', data=form).status_code, 400)
        self.assertIsNone(self.fixture.order())

    def test_order_validation_keeps_options_and_never_trusts_submitted_prices(self):
        option = self.create_option()
        form = self.order_form([option['id']])
        form['option_price[]'] = '1'
        form['client_name'] = ''
        response = self.client.post('/dealer/orders/new', data=form)
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.get_data(as_text=True).split('<script id="dealerItemsData" type="application/json">')[1].split('</script>')[0])
        self.assertEqual(json.loads(data[0]['option_ids_raw']), [option['id']])
        form['client_name'] = dealer_tests.CLIENT
        self.assertEqual(self.client.post('/dealer/orders/new', data=form).status_code, 302)
        self.assertEqual(self.fixture.order()['total'], 125000)

    def test_two_boats_have_independent_options_and_global_option_works(self):
        option = self.create_option(profile_id='')
        form = self.order_form([option['id']])
        second = self.fixture.form(items=[('boat', '', 'Вторая лодка', '', '1', '50000')])
        for key in ('item_kind[]', 'item_profile_id[]', 'item_name[]', 'item_serial[]', 'item_quantity[]', 'item_price[]'):
            form.add(key, second[key])
        form.add('item_option_ids[]', '[]')
        form.add('item_id[]', '')
        self.assertEqual(self.client.post('/dealer/orders/new', data=form).status_code, 302)
        order = self.fixture.order()
        self.assertEqual(order['total'], 175000)
        first, second = self.fixture.items(order['id'])
        self.assertEqual(len(json.loads(first['options_json'])), 1)
        self.assertEqual(json.loads(second['options_json']), [])


if __name__ == '__main__':
    unittest.main()
