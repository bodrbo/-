import unittest

from support import application_module

BOAT_MODEL = "Пространственный катер"
CLIENT = "Клиент Пространств"


class ShopMapSpacesTests(unittest.TestCase):
    """Карты пространств: several spaces (name, length × width, objects) and
    moving a boat from one to another."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self.cleanup(db)
            self.key = application_module._tuning_equipment_profile_key("boat", BOAT_MODEL)
            db.execute(
                "INSERT OR REPLACE INTO tuning_boat_profiles (model_key, model_name, equipment_type, "
                "specifications, length_m, width_m, created_at, updated_at) "
                "VALUES (?, ?, 'boat', '', 5, 2, 'x', 'x')", (self.key, BOAT_MODEL))
            self.first_space = application_module._shop_map_default_room(db)["id"]
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, phone, "
                "subtotal, total, status, order_date, acceptance_date, deadline_date, created_at, updated_at) "
                "VALUES (?, 'boat', ?, 'direct', '', 1000, 1000, 'in_progress', '2030-05-01', '2030-05-01', "
                "'2030-06-01', '2030-05-01 10:00', '2030-05-01 10:00')", (CLIENT, BOAT_MODEL)).lastrowid
            application_module._auto_place_boat_on_shop_map(db, {"id": self.order_id})
            db.commit()
            self.boat_id = db.execute(
                "SELECT id FROM shop_map_boats WHERE order_id = ?", (self.order_id,)).fetchone()["id"]
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM tuning_orders WHERE client_name = ?", (CLIENT,)).fetchall():
            db.execute("DELETE FROM shop_map_boats WHERE order_id = ?", (row["id"],))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (row["id"],))
        for row in db.execute("SELECT id FROM shop_map_room WHERE name LIKE 'Тест-пространство%'").fetchall():
            db.execute("DELETE FROM shop_map_elements WHERE room_id = ?", (row["id"],))
            db.execute("DELETE FROM shop_map_room WHERE id = ?", (row["id"],))
        db.execute("DELETE FROM shop_map_elements WHERE name LIKE 'Тест-объект%'")
        db.execute("DELETE FROM tuning_boat_profiles WHERE model_key = ?", (self.key if hasattr(self, "key") else "",))
        db.commit()

    # ---- helpers

    def add_space(self, name="Тест-пространство", length="20", width="10", objects="", kind="hangar"):
        return self.client.post("/tuning/shop-map/spaces/add", data={
            "name": name, "length_m": length, "width_m": width, "bulk_text": objects, "kind": kind})

    def space_id(self, name):
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT id FROM shop_map_room WHERE name = ?", (name,)).fetchone()
            return row["id"] if row else None

    def boat(self):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM shop_map_boats WHERE id = ?", (self.boat_id,)).fetchone())

    def notice(self, key):
        with self.client.session_transaction() as session:
            return session.get(key)

    # ---- the rename and the migration

    def test_the_page_and_menu_are_called_maps_of_spaces(self):
        page = self.client.get("/tuning/shop-map").get_data(as_text=True)
        self.assertIn("<h1>Карты пространств</h1>", page)
        self.assertIn(">Карты пространств</a>", page)
        self.assertNotIn("Карта цеха", page)

    def test_the_existing_room_became_the_first_named_space(self):
        with application_module.app.app_context():
            room = application_module._shop_map_default_room(application_module.get_db())
        self.assertTrue(room["name"])
        self.assertEqual(self.boat()["room_id"], room["id"])

    # ---- the type of a space: двор or ангар

    def kind_of(self, space):
        with application_module.app.app_context():
            return application_module._shop_map_room_by_id(application_module.get_db(), space)["kind"]

    def test_the_existing_space_is_an_hangar(self):
        self.assertEqual(self.kind_of(self.first_space), "hangar")

    def test_a_space_is_added_as_a_yard_or_a_hangar(self):
        self.add_space(name="Тест-пространство", kind="yard")
        self.add_space(name="Тест-пространство 2", kind="hangar")
        self.assertEqual(self.kind_of(self.space_id("Тест-пространство")), "yard")
        self.assertEqual(self.kind_of(self.space_id("Тест-пространство 2")), "hangar")

    def test_the_type_is_required_and_must_be_known(self):
        for kind in ("", "garage", "ДВОР"):
            self.add_space(kind=kind)
            self.assertIsNone(self.space_id("Тест-пространство"), kind)
            self.assertIn("тип пространства", self.notice("shop_map_space_error"))
        response = self.client.post("/tuning/shop-map/spaces/add", data={
            "name": "Тест-пространство", "length_m": "5", "width_m": "5"})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.space_id("Тест-пространство"))

    def test_the_type_can_be_changed_and_a_bad_one_is_refused(self):
        self.add_space(kind="hangar")
        space = self.space_id("Тест-пространство")
        self.client.post("/tuning/shop-map/room", data={
            "space": space, "name": "Тест-пространство", "kind": "yard", "length_m": "20", "width_m": "10"})
        self.assertEqual(self.kind_of(space), "yard")
        self.client.post("/tuning/shop-map/room", data={
            "space": space, "name": "Тест-пространство", "kind": "bogus", "length_m": "20", "width_m": "10"})
        self.assertEqual(self.kind_of(space), "yard")
        self.assertIn("тип пространства", self.notice("shop_map_room_error"))
        # an edit that doesn't send the type keeps it
        self.client.post("/tuning/shop-map/room", data={
            "space": space, "name": "Тест-пространство", "length_m": "21", "width_m": "10"})
        self.assertEqual(self.kind_of(space), "yard")

    def test_the_type_is_shown_on_the_tabs_the_heading_and_in_the_forms(self):
        self.add_space(kind="yard")
        space = self.space_id("Тест-пространство")
        page = self.client.get(f"/tuning/shop-map?space={space}").get_data(as_text=True)
        self.assertIn('<span class="shop-space-kind">Двор</span>', page)
        self.assertIn('<span class="shop-space-kind">Ангар</span>', page)
        self.assertIn("Тест-пространство · Двор", page)
        self.assertIn('<option value="yard" selected>Двор</option>', page)
        self.assertIn('id="spaceKind"', page)

    # ---- adding spaces

    def test_a_space_is_added_with_its_size_and_objects(self):
        response = self.add_space(objects="Тест-объект А; 1; 1; 4; 0.6\nТест-объект Б; 10; 5; 2; 2")
        self.assertEqual(response.status_code, 302)
        space = self.space_id("Тест-пространство")
        self.assertIsNotNone(space)
        self.assertIn(f"space={space}", response.headers["Location"])
        with application_module.app.app_context():
            db = application_module.get_db()
            room = dict(application_module._shop_map_room_by_id(db, space))
            elements = db.execute(
                "SELECT name FROM shop_map_elements WHERE room_id = ? ORDER BY id", (space,)).fetchall()
        self.assertEqual((room["length_m"], room["width_m"]), (20, 10))
        self.assertEqual([e["name"] for e in elements], ["Тест-объект А", "Тест-объект Б"])
        self.assertIn("добавлено, объектов: 2", self.notice("shop_map_space_notice"))

    def test_bad_input_adds_nothing(self):
        cases = [
            dict(name=""), dict(length="0"), dict(width="-3"), dict(length="abc"),
            dict(objects="Тест-объект; 1; 1; 4"),                      # not 5 values
            dict(objects="Тест-объект; 18; 1; 4; 1"),                  # sticks out of 20 m
        ]
        for overrides in cases:
            self.add_space(**overrides)
            self.assertIsNone(self.space_id("Тест-пространство"), overrides)
            self.assertTrue(self.notice("shop_map_space_error"), overrides)
        self.add_space()
        self.add_space()                                               # same name twice
        self.assertIn("уже есть", self.notice("shop_map_space_error"))

    def test_the_map_shows_the_chosen_space_with_its_own_objects(self):
        self.add_space(objects="Тест-объект А; 1; 1; 4; 0.6")
        space = self.space_id("Тест-пространство")
        other = self.client.get(f"/tuning/shop-map?space={space}").get_data(as_text=True)
        self.assertIn("Тест-пространство", other)
        self.assertIn("20.0 × 10.0 м", other)
        self.assertIn("Тест-объект А", other)
        main = self.client.get(f"/tuning/shop-map?space={self.first_space}").get_data(as_text=True)
        self.assertNotIn("Тест-объект А", main)
        # a bad / unknown ?space= falls back to the first space instead of failing
        self.assertEqual(self.client.get("/tuning/shop-map?space=999999").status_code, 200)
        self.assertEqual(self.client.get("/tuning/shop-map?space=abc").status_code, 200)

    def test_objects_are_added_to_the_selected_space_only(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        self.client.post("/tuning/shop-map/elements/add", data={
            "space": space, "name": "Тест-объект В", "x_m": "1", "y_m": "1", "width_m": "3", "height_m": "1"})
        self.client.post("/tuning/shop-map/elements/bulk-add", data={
            "space": space, "bulk_text": "Тест-объект Г; 5; 5; 2; 2"})
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT name, room_id FROM shop_map_elements WHERE name LIKE 'Тест-объект%'").fetchall()
        self.assertEqual({(r["name"], r["room_id"]) for r in rows},
                         {("Тест-объект В", space), ("Тест-объект Г", space)})

    def test_an_object_must_fit_into_its_own_space(self):
        self.add_space(length="6", width="4")
        space = self.space_id("Тест-пространство")
        self.client.post("/tuning/shop-map/elements/add", data={
            "space": space, "name": "Тест-объект Д", "x_m": "5", "y_m": "0", "width_m": "3", "height_m": "1"})
        self.assertIn("за пределы пространства", self.notice("shop_map_element_error"))

    def test_the_space_can_be_renamed_and_resized(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        self.client.post("/tuning/shop-map/room", data={
            "space": space, "name": "Тест-пространство 2", "length_m": "30", "width_m": "12"})
        with application_module.app.app_context():
            room = dict(application_module._shop_map_room_by_id(application_module.get_db(), space))
        self.assertEqual((room["name"], room["length_m"], room["width_m"]), ("Тест-пространство 2", 30, 12))

    # ---- moving a boat between spaces

    def test_a_boat_moves_to_another_space_and_lands_on_a_free_spot(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        response = self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={"room_id": space})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"] and body["free_spot"])
        boat = self.boat()
        self.assertEqual(boat["room_id"], space)
        self.assertEqual((boat["x_m"], boat["y_m"]), (0.0, 0.0))     # the first free spot
        self.assertIn("перенесена в «Тест-пространство»", self.notice("shop_map_boat_notice"))
        there = self.client.get(f"/tuning/shop-map?space={space}&date=2030-05-10").get_data(as_text=True)
        self.assertIn(f'data-boat-id="{self.boat_id}"', there)
        here = self.client.get(f"/tuning/shop-map?space={self.first_space}&date=2030-05-10").get_data(as_text=True)
        self.assertNotIn(f'data-boat-id="{self.boat_id}"', here)

    def test_the_boat_can_come_back(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={"room_id": space})
        back = self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space",
                                data={"room_id": self.first_space})
        self.assertTrue(back.get_json()["ok"])
        self.assertEqual(self.boat()["room_id"], self.first_space)

    def test_moving_keeps_the_heading_and_checks_the_fit_with_it(self):
        self.add_space(length="6", width="3")           # a 5 × 2 boat fits only lengthwise
        space = self.space_id("Тест-пространство")
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("UPDATE shop_map_boats SET rotation_deg = 90 WHERE id = ?", (self.boat_id,))
            db.commit()
        body = self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space",
                                data={"room_id": space}).get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["free_spot"])            # standing across it does not fit 6 × 3
        self.assertIn("Свободного места там не нашлось", self.notice("shop_map_boat_notice"))
        self.assertEqual(self.boat()["rotation_deg"], 90)

    def test_boats_of_another_space_do_not_block_the_free_spot(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        # the boat stands at the very corner of the first space; an object of
        # the first space at the same place does not matter in the new one
        self.client.post("/tuning/shop-map/elements/add", data={
            "space": self.first_space, "name": "Тест-объект Е", "x_m": "0", "y_m": "0", "width_m": "9", "height_m": "9"})
        self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={"room_id": space})
        boat = self.boat()
        self.assertEqual((boat["x_m"], boat["y_m"]), (0.0, 0.0))

    def test_moving_to_the_same_or_an_unknown_space_is_refused(self):
        same = self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space",
                                data={"room_id": self.first_space})
        self.assertEqual(same.status_code, 400)
        unknown = self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={"room_id": 999999})
        self.assertEqual(unknown.status_code, 404)
        missing = self.client.post("/tuning/shop-map/boats/999999/move-space", data={"room_id": self.first_space})
        self.assertEqual(missing.status_code, 404)

    def test_the_boat_has_the_context_menu_data_and_the_menu_exists(self):
        page = self.client.get("/tuning/shop-map?date=2030-05-10").get_data(as_text=True)
        self.assertIn(f"/tuning/shop-map/boats/{self.boat_id}/move-space", page)
        self.assertIn('id="shopMapContextMenu"', page)
        self.assertIn('addEventListener("contextmenu"', page)

    def test_auto_placement_still_uses_the_first_space(self):
        self.add_space()
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, sale_channel, phone, "
                "subtotal, total, status, order_date, acceptance_date, deadline_date, created_at, updated_at) "
                "VALUES (?, 'boat', ?, 'direct', '', 1, 1, 'in_progress', '2030-05-01', '2030-05-01', "
                "'2030-06-01', '2030-05-01 10:00', '2030-05-01 10:00')", (CLIENT, BOAT_MODEL)).lastrowid
            application_module._auto_place_boat_on_shop_map(db, {"id": order_id})
            room_id = db.execute("SELECT room_id FROM shop_map_boats WHERE order_id = ?", (order_id,)).fetchone()["room_id"]
        self.assertEqual(room_id, self.first_space)

    # ---- deleting spaces

    def test_an_empty_space_is_deleted_with_its_objects(self):
        self.add_space(objects="Тест-объект А; 1; 1; 4; 0.6")
        space = self.space_id("Тест-пространство")
        response = self.client.post(f"/tuning/shop-map/spaces/{space}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.space_id("Тест-пространство"))
        with application_module.app.app_context():
            left = application_module.get_db().execute(
                "SELECT COUNT(*) FROM shop_map_elements WHERE room_id = ?", (space,)).fetchone()[0]
        self.assertEqual(left, 0)

    def test_a_space_with_a_boat_is_not_deleted(self):
        self.add_space()
        space = self.space_id("Тест-пространство")
        self.client.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={"room_id": space})
        self.client.post(f"/tuning/shop-map/spaces/{space}/delete")
        self.assertIsNotNone(self.space_id("Тест-пространство"))
        self.assertIn("стоят лодки", self.notice("shop_map_space_error"))

    def test_the_last_space_cannot_be_deleted(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            extra = [r["id"] for r in db.execute("SELECT id FROM shop_map_room WHERE id != ?", (self.first_space,)).fetchall()]
        if extra:
            self.skipTest("other spaces exist in this database")
        self.client.post(f"/tuning/shop-map/spaces/{self.first_space}/delete")
        with application_module.app.app_context():
            self.assertIsNotNone(application_module._shop_map_room_by_id(application_module.get_db(), self.first_space))
        self.assertIn("Последнее", self.notice("shop_map_space_error"))

    def test_login_is_required(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.post("/tuning/shop-map/spaces/add", data={}).status_code, 302)
        self.assertEqual(
            anonymous.post(f"/tuning/shop-map/boats/{self.boat_id}/move-space", data={}).status_code, 302)


if __name__ == "__main__":
    unittest.main()
