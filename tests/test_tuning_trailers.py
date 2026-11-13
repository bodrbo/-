import unittest

from werkzeug.datastructures import MultiDict

from support import application_module

CLIENT = "Тест Прицепов Клиент"
PARTNER = "Тест Прицепов Партнёр"
CLIENT_TOKEN = "trailer-test-client"
PARTNER_TOKEN = "trailer-test-partner"
TRAILER = "Трейлер Тест 750"
TRAILER_2 = "Трейлер Тест 900"
BOAT = "Лодка Для Прицепа Тест"


class TuningTrailerTests(unittest.TestCase):
    """«Прицеп» — третий вид техники тюнинг-заказа: свой каталог, форма заказа,
    заявки партнёров."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, ?, '+79990001122', ?, 'neutral', '2026-09-01 10:00')",
                (CLIENT, BOAT, CLIENT_TOKEN)).lastrowid
            self.partner_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', '+79990003344', ?, 'neutral', '2026-09-01 10:00')",
                (PARTNER, PARTNER_TOKEN)).lastrowid
            db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, partner_title, "
                       "created_at) VALUES (?, 'tuning', 'partner', 'Партнёр', '2026-09-01 10:00')",
                       (self.partner_id,))
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE client_name IN (?, ?)", (CLIENT, PARTNER)).fetchall()]
        for oid in ids:
            for table in ("tuning_order_items", "tuning_order_motors", "tuning_order_products", "tuning_payments"):
                db.execute(f"DELETE FROM {table} WHERE order_id = ?", (oid,))
            db.execute("DELETE FROM projects WHERE tuning_order_id = ?", (oid,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (oid,))
        for model in (TRAILER, TRAILER_2, "Новое Имя Прицепа"):
            for kind in ("trailer", "boat"):
                db.execute("DELETE FROM tuning_boat_profiles WHERE model_key = ?",
                           (application_module._tuning_equipment_profile_key(kind, model),))
        db.execute("DELETE FROM tuning_boat_profiles WHERE model_key = ?",
                   (application_module._tuning_equipment_profile_key("boat", BOAT),))
        db.execute("DELETE FROM client_segments WHERE client_id IN (SELECT id FROM clients WHERE token IN (?, ?))",
                   (CLIENT_TOKEN, PARTNER_TOKEN))
        db.execute("DELETE FROM clients WHERE token IN (?, ?)", (CLIENT_TOKEN, PARTNER_TOKEN))
        db.commit()

    # ---- helpers

    @staticmethod
    def form(model=TRAILER, number="АБ 1234 77", equipment_type="trailer", client=CLIENT, **extra):
        data = MultiDict([
            ("client_name", client), ("equipment_type", equipment_type),
            ("trailer_model", model), ("trailer_registration_number", number),
            ("boat_model", ""), ("boat_registration_number", ""), ("motor_model", ""),
            ("phone", "+79990001122"), ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", "percent"), ("discount_value", "0"),
            ("work_name[]", "Замена ступиц"), ("cost_price[]", "1000"), ("multiplier[]", "2"), ("item_id[]", ""),
        ])
        for key, value in extra.items():
            data.setlist(key, value if isinstance(value, list) else [value])
        return data

    def add_order(self, **kwargs):
        response = self.http.post("/tuning/add", data=self.form(**kwargs))
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1",
                (kwargs.get("client", CLIENT),)).fetchone())

    def profile(self, kind, model):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT * FROM tuning_boat_profiles WHERE model_key = ?",
                (application_module._tuning_equipment_profile_key(kind, model),)).fetchone()
            return dict(row) if row else None

    # ---- the form

    def test_form_parser_maps_a_trailer_and_ignores_boat_and_motor_leftovers(self):
        form = self.form(boat_model="Лодка-призрак", boat_registration_number="БН-1")
        form.add("motor_model", "Мотор-призрак")
        errors, data = application_module._process_tuning_form(form)
        self.assertEqual(errors, [])
        self.assertEqual((data["equipment_type"], data["boat_model"], data["boat_registration_number"]),
                         ("trailer", TRAILER, "АБ 1234 77"))
        self.assertEqual((data["motor_model"], data["motor_serial_number"], data["boat_motors"]), ("", "", []))

    def test_a_trailer_needs_a_model(self):
        errors, data = application_module._process_tuning_form(self.form(model=""))
        self.assertIn("Укажите модель прицепа.", errors)
        self.assertIsNone(data)

    def test_the_order_form_offers_the_trailer_and_its_catalog_choices(self):
        self.add_order()
        page = self.http.get("/tuning/add").get_data(as_text=True)
        for text in ('name="equipment_type" value="trailer"', 'name="trailer_model"',
                     'name="trailer_registration_number"', f'data-value="{TRAILER}"', "Прицеп"):
            self.assertIn(text, page)

    def test_a_trailer_order_is_saved_and_listed_as_a_trailer(self):
        order = self.add_order()
        self.assertEqual((order["equipment_type"], order["boat_model"], order["boat_registration_number"],
                          order["motor_model"]), ("trailer", TRAILER, "АБ 1234 77", ""))
        page = self.http.get("/tuning", query_string={"q": CLIENT}).get_data(as_text=True)
        self.assertIn(f'<span class="equipment-kind">Прицеп</span>', page)
        self.assertIn(TRAILER, page)
        self.assertIn("Номер прицепа: АБ 1234 77", page)
        self.assertNotIn("Бортовой №: АБ 1234 77", page)
        self.assertNotIn('<span class="equipment-kind">Лодка</span>', page.split(TRAILER)[0].rsplit("<tr", 1)[-1])

    def test_a_trailer_does_not_overwrite_the_boat_of_the_client(self):
        self.add_order()
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT boat_model FROM clients WHERE id = ?", (self.client_id,)).fetchone()
        self.assertEqual(row["boat_model"], BOAT)

    def test_the_edit_page_shows_the_trailer_and_keeps_it_on_save(self):
        order = self.add_order()
        page = self.http.get(f"/tuning/edit/{order['id']}").get_data(as_text=True)
        self.assertIn('name="equipment_type" value="trailer"\n                     checked', page)
        self.assertIn(f'value="{TRAILER}"', page)
        self.assertIn('value="АБ 1234 77"', page)
        self.assertIn("Прицеп", page)
        self.assertIn("· № АБ 1234 77", page)
        link = f"/tuning/trailers/{self.profile('trailer', TRAILER)['id']}"
        self.assertIn(link, page)
        # changing the model saves the new trailer
        form = self.form(model=TRAILER_2, number="")
        response = self.http.post(f"/tuning/edit/{order['id']}", data=form)
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            saved = application_module.get_db().execute(
                "SELECT equipment_type, boat_model, boat_registration_number FROM tuning_orders WHERE id = ?",
                (order["id"],)).fetchone()
        self.assertEqual(tuple(saved), ("trailer", TRAILER_2, ""))

    def test_the_label_of_a_trailer(self):
        label = application_module.tuning_equipment_label
        self.assertEqual(label({"equipment_type": "trailer", "boat_model": TRAILER,
                                "boat_registration_number": "АБ 1"}), f"{TRAILER} · № АБ 1")
        self.assertEqual(label({"equipment_type": "trailer", "boat_model": ""}), "Прицеп")
        self.assertEqual(application_module.equipment_kind_label("trailer"), "Прицеп")
        self.assertEqual(application_module.equipment_kind_label("motor"), "Мотор")
        self.assertEqual(application_module.equipment_kind_label(None), "Лодка")

    # ---- the catalog

    def test_the_catalog_of_trailers_is_separate_from_boats_and_motors(self):
        self.add_order()
        profile = self.profile("trailer", TRAILER)
        self.assertEqual(profile["equipment_type"], "trailer")
        self.assertEqual(profile["model_key"], f"trailer:{TRAILER.casefold()}")
        self.assertIsNone(self.profile("boat", TRAILER))
        catalog = self.http.get("/tuning/trailers").get_data(as_text=True)
        self.assertIn("Каталог прицепов", catalog)
        self.assertIn("Прицепы, с которыми мы работали", catalog)
        self.assertIn(TRAILER, catalog)
        self.assertIn(f"/tuning/trailers/{profile['id']}", catalog)
        self.assertNotIn(TRAILER, self.http.get("/tuning/boats").get_data(as_text=True))
        self.assertNotIn(TRAILER, self.http.get("/tuning/motors").get_data(as_text=True))

    def test_the_tuning_navigation_links_the_trailer_catalog(self):
        for url in ("/tuning", "/tuning/boats", "/tuning/trailers"):
            self.assertIn('href="/tuning/trailers"', self.http.get(url).get_data(as_text=True), url)

    def test_the_profile_of_a_trailer(self):
        self.add_order()
        profile = self.profile("trailer", TRAILER)
        page = self.http.get(f"/tuning/trailers/{profile['id']}").get_data(as_text=True)
        self.assertEqual(self.http.get(f"/tuning/trailers/{profile['id']}").status_code, 200)
        for text in (TRAILER, "Характеристики прицепа", "Номер прицепа", "АБ 1234 77", "Заказы по модели",
                     "TRAILER /"):
            self.assertIn(text, page)
        for text in ("Длина, м", "Бортовой номер", "Совместимые расходники", 'value="motor"'):
            self.assertNotIn(text, page)
        # a trailer profile opened through the boat url goes to the trailer page
        response = self.http.get(f"/tuning/boats/{profile['id']}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/trailers/{profile['id']}"))

    def test_a_trailer_profile_can_be_edited_and_renamed_with_its_orders(self):
        order = self.add_order()
        profile = self.profile("trailer", TRAILER)
        response = self.http.post(f"/tuning/equipment/{profile['id']}/edit", data={
            "model_name": "Новое Имя Прицепа", "specifications": "Грузоподъёмность: 750 кг"})
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/trailers/{profile['id']}"))
        with application_module.app.app_context():
            db = application_module.get_db()
            saved = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order["id"],)).fetchone()
            row = db.execute("SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile["id"],)).fetchone()
            client = db.execute("SELECT boat_model FROM clients WHERE id = ?", (self.client_id,)).fetchone()
        self.assertEqual((saved["equipment_type"], saved["boat_model"]), ("trailer", "Новое Имя Прицепа"))
        self.assertEqual((row["equipment_type"], row["model_key"], row["specifications"]),
                         ("trailer", "trailer:новое имя прицепа", "Грузоподъёмность: 750 кг"))
        self.assertEqual(client["boat_model"], BOAT)
        page = self.http.get(f"/tuning/trailers/{profile['id']}").get_data(as_text=True)
        self.assertIn("Название модели и связанные записи обновлены.", page)
        self.assertIn("Грузоподъёмность", page)

    def test_a_trailer_profile_cannot_be_converted_to_a_boat_or_motor(self):
        self.add_order()
        profile = self.profile("trailer", TRAILER)
        for target in ("boat", "motor"):
            response = self.http.post(f"/tuning/equipment/{profile['id']}/type", data={"equipment_type": target})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.profile("trailer", TRAILER)["equipment_type"], "trailer")

    def test_a_boat_and_a_trailer_may_share_a_model_name(self):
        self.add_order(model=BOAT)
        with self.http.session_transaction():
            pass
        boat_form = self.form(equipment_type="boat")
        boat_form.setlist("boat_model", [BOAT])
        self.assertEqual(self.http.post("/tuning/add", data=boat_form).status_code, 302)
        self.assertEqual(self.profile("trailer", BOAT)["equipment_type"], "trailer")
        self.assertEqual(self.profile("boat", BOAT)["equipment_type"], "boat")

    # ---- partner requests

    def partner_form(self, **extra):
        data = MultiDict([("equipment_type", "trailer"), ("trailer_model", TRAILER),
                          ("trailer_registration_number", "ПР 77"), ("work_name[]", "Проверить тормоза")])
        for key, value in extra.items():
            data.setlist(key, value if isinstance(value, list) else [value])
        return data

    def test_the_partner_request_page_offers_the_trailer(self):
        page = self.http.get(f"/client/{PARTNER_TOKEN}/estimate-request").get_data(as_text=True)
        for text in ('name="equipment_type" value="trailer"', 'name="trailer_model"',
                     'name="trailer_registration_number"', "partnerTrailerFields", "Модель прицепа"):
            self.assertIn(text, page)

    def test_a_partner_can_send_a_trailer_request(self):
        response = self.http.post(f"/client/{PARTNER_TOKEN}/estimate-request", data=self.partner_form())
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            order = application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE client_id = ?", (self.partner_id,)).fetchone()
        self.assertEqual((order["equipment_type"], order["boat_model"], order["boat_registration_number"],
                          order["motor_model"], order["status"]),
                         ("trailer", TRAILER, "ПР 77", "", application_module.REQUEST_ORDER_STATUS))
        self.assertEqual(self.profile("trailer", TRAILER)["equipment_type"], "trailer")

    def test_a_partner_trailer_request_without_a_model_is_refused(self):
        response = self.http.post(f"/client/{PARTNER_TOKEN}/estimate-request", data=self.partner_form(trailer_model=""))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Укажите модель прицепа.", response.get_data(as_text=True))
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_orders WHERE client_id = ?", (self.partner_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_partner_boat_fields_do_not_leak_into_a_trailer_request(self):
        form = self.partner_form()
        form.add("boat_model", "Лодка-призрак")
        form.add("motor_model", "Мотор-призрак")
        form.add("boat_motor_model[]", "Мотор-призрак")
        self.http.post(f"/client/{PARTNER_TOKEN}/estimate-request", data=form)
        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute("SELECT * FROM tuning_orders WHERE client_id = ?", (self.partner_id,)).fetchone()
            motors = db.execute("SELECT * FROM tuning_order_motors WHERE order_id = ?", (order["id"],)).fetchall()
        self.assertEqual((order["boat_model"], order["motor_model"], len(motors)), (TRAILER, "", 0))

    # ---- around it

    def test_a_trailer_order_cannot_go_to_the_dealer_boats_and_motors(self):
        order = self.add_order()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order["id"],))
            db.commit()
        self.http.post("/tuning/bulk", data={"action": "convert", "bulk_target": "equipment",
                                              "order_id": [str(order["id"])]})
        with self.http.session_transaction() as session:
            error = session.pop("tuning_bulk_error", "")
        self.assertIn("прицеп нельзя перенести", error)
        with application_module.app.app_context():
            exists = application_module.get_db().execute(
                "SELECT 1 FROM tuning_orders WHERE id = ?", (order["id"],)).fetchone()
        self.assertIsNotNone(exists)

    def test_the_client_list_names_the_kind_of_the_last_order(self):
        self.add_order()
        page = self.http.get("/admin/clients", query_string={"q": CLIENT}).get_data(as_text=True)
        self.assertIn("Прицеп", page)
        self.assertIn(TRAILER, page)


if __name__ == "__main__":
    unittest.main()
