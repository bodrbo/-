import unittest

from support import application_module


class ClientEquipmentTests(unittest.TestCase):
    """A client can have several pieces of equipment: staff add, edit and delete
    them; the cabinet shows them in a block of their own."""

    NAME = "client-equipment-test"
    TOKEN = "client-equipment-token"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.http = application_module.app.test_client()
        with self.http.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())
            db = application_module.get_db()
            self.client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', '', ?, 'neutral', '2026-09-01 10:00')", (self.NAME, self.TOKEN)).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (self.client_id,))
            db.commit()
        self.addCleanup(self.cleanup_now)

    def cleanup_now(self):
        with application_module.app.app_context():
            self.cleanup(application_module.get_db())

    def cleanup(self, db):
        for row in db.execute("SELECT id FROM clients WHERE token = ?", (self.TOKEN,)).fetchall():
            db.execute("DELETE FROM client_equipment WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def items(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM client_equipment WHERE client_id = ? ORDER BY id", (self.client_id,)).fetchall()]

    def add(self, **data):
        data = {"kind": "boat", "name": "Finnmaster T8", **data}
        return self.http.post(f"/admin/clients/{self.client_id}/equipment/add", data=data)

    def test_several_items_can_be_added(self):
        self.add(serial_number="FIN-1", year="2021")
        self.add(kind="motor", name="Yamaha F150", note="на катере")
        items = self.items()
        self.assertEqual([i["name"] for i in items], ["Finnmaster T8", "Yamaha F150"])
        self.assertEqual((items[0]["serial_number"], items[0]["year"]), ("FIN-1", 2021))
        self.assertEqual(items[1]["kind"], "motor")

    def test_a_name_is_required_and_the_year_is_checked(self):
        self.add(name="  ")
        self.add(year="abc")
        self.add(year="1800")
        self.assertEqual(self.items(), [])

    def test_an_item_can_be_edited_and_deleted_only_through_its_own_client(self):
        self.add()
        item_id = self.items()[0]["id"]
        self.http.post(f"/admin/clients/{self.client_id}/equipment/{item_id}/update",
                       data={"kind": "boat", "name": "Другая модель", "year": ""})
        self.assertEqual(self.items()[0]["name"], "Другая модель")
        self.http.post(f"/admin/clients/{self.client_id + 999999}/equipment/{item_id}/delete")
        self.assertEqual(len(self.items()), 1)
        self.http.post(f"/admin/clients/{self.client_id}/equipment/{item_id}/delete")
        self.assertEqual(self.items(), [])

    def test_the_cabinet_shows_the_block_and_the_client_cannot_edit_it(self):
        self.add(serial_number="FIN-1")
        self.add(name="Mercury 90")
        public = application_module.app.test_client().get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn("Техника клиента", public)
        self.assertIn("Finnmaster T8", public)
        self.assertIn("Mercury 90", public)
        self.assertIn("FIN-1", public)
        self.assertIn("Finnmaster T8 и ещё 1", public)
        self.assertNotIn("Добавить технику", public)
        staff = self.http.get(f"/admin/clients/{self.client_id}/cabinet?section=tuning").get_data(as_text=True)
        self.assertIn("Добавить технику", staff)

    def test_an_archived_item_is_marked_in_the_cabinet_and_leaves_the_summary(self):
        self.add(name="Старая лодка", status="archive")
        self.add(name="Новая лодка")
        items = self.items()
        self.assertEqual([i["status"] for i in items], ["archive", "active"])
        public = application_module.app.test_client().get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertIn("client-equipment-card is-archive", public)
        self.assertIn("client-equipment-archive-mark", public)
        self.assertEqual(public.count("client-equipment-archive-mark\">архив"), 1)
        self.assertNotIn("Новая лодка и ещё", public)
        self.assertIn('<span class="v" style="font-size:20px;">Новая лодка</span>', public)
        self.assertLess(public.index("Новая лодка"), public.index("Старая лодка"))

    def test_the_status_is_set_by_staff_only_and_defaults_to_active(self):
        self.add(status="bogus")
        item_id = self.items()[0]["id"]
        self.assertEqual(self.items()[0]["status"], "active")
        self.http.post(f"/admin/clients/{self.client_id}/equipment/{item_id}/update",
                       data={"kind": "boat", "name": "Finnmaster T8", "status": "archive"})
        self.assertEqual(self.items()[0]["status"], "archive")
        staff = self.http.get(f"/admin/clients/{self.client_id}/cabinet?section=tuning").get_data(as_text=True)
        self.assertIn('name="status"', staff)
        public = application_module.app.test_client().get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertNotIn('name="status"', public)

    def test_the_public_cabinet_hides_an_empty_block_but_staff_see_the_form(self):
        public = application_module.app.test_client().get(f"/client/{self.TOKEN}").get_data(as_text=True)
        self.assertNotIn("Техника клиента", public)
        staff = self.http.get(f"/admin/clients/{self.client_id}/cabinet?section=tuning").get_data(as_text=True)
        self.assertIn("Техника клиента", staff)

    def test_the_routes_are_closed_without_a_login(self):
        anonymous = application_module.app.test_client()
        anonymous.post(f"/admin/clients/{self.client_id}/equipment/add", data={"name": "X"})
        self.assertEqual(self.items(), [])


if __name__ == "__main__":
    unittest.main()
