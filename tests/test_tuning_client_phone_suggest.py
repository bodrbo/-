import pathlib
import unittest

from support import application_module


class TuningClientPhoneSuggestTests(unittest.TestCase):
    """The phone field of a tuning order offers the same clients as the name
    field (so does its search by digits)."""

    TOKEN = "tuning-phone-suggest-client"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            db = application_module.get_db()
            self._cleanup(db)
            client_id = db.execute(
                "INSERT INTO clients (client_name, boat_model, phone, token, status, created_at) "
                "VALUES ('Подсказкин Пётр', 'Salute', '+7 (999) 123-45-67', ?, 'neutral', '2026-09-01 10:00')",
                (self.TOKEN,)).lastrowid
            db.execute("INSERT INTO client_segments (client_id, segment, relationship_type, created_at) "
                       "VALUES (?, 'tuning', 'client', '2026-09-01 10:00')", (client_id,))
            db.commit()
        self.addCleanup(self._cleanup_now)

    def _cleanup_now(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        row = db.execute("SELECT id FROM clients WHERE token = ?", (self.TOKEN,)).fetchone()
        if row:
            db.execute("DELETE FROM client_segments WHERE client_id = ?", (row["id"],))
            db.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
        db.commit()

    def page(self):
        return self.client.get("/tuning/add").get_data(as_text=True)

    def test_the_order_form_hands_the_phone_field_the_clients_with_their_numbers(self):
        page = self.page()
        self.assertIn("data-tuning-client-phone", page)
        self.assertIn('data-client-phone="+7 (999) 123-45-67"', page)
        self.assertIn('data-client-id="', page)

    def test_the_name_search_also_knows_the_digits_of_the_number(self):
        page = self.page()
        import re
        (search,) = re.findall(r'data-search="([^"]*подсказкин[^"]*)"', page)
        self.assertIn("79991234567", search)  # typing "999123" finds it
        self.assertIn("+7 (999) 123-45-67", search)

    def test_the_script_attaches_a_suggestion_list_to_the_phone_field(self):
        script = (pathlib.Path(application_module.__file__).parent / "static" / "loader.js").read_text(
            encoding="utf-8")
        block = script.split("The phone field of a tuning order offers")[1].split("Wide-screen navigation rail")[0]
        for part in ('querySelector("[data-tuning-client-phone]")', "digitsOf", 'charAt(0) === "8"',
                     "clientId.value = client.id", "phone.value = client.phone", "nameValue.value = client.name"):
            self.assertIn(part, block, part)


if __name__ == "__main__":
    unittest.main()
