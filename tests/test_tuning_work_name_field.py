import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningWorkNameFieldTests(unittest.TestCase):
    """The work name in the order editor is a wrapping, auto-growing text
    area (long names flow onto more lines) that still stores one line."""

    CLIENT_NAME = "tuning-work-name-field-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.commit()

    def create(self, work_name):
        data = MultiDict([
            ("client_name", self.CLIENT_NAME), ("equipment_type", "boat"),
            ("boat_model", "Тест-катер"), ("boat_registration_number", ""),
            ("motor_model", ""), ("motor_serial_number", ""), ("phone", ""),
            ("order_date", "2026-09-10"), ("sale_channel", "direct"),
            ("discount_type", "percent"), ("discount_value", "0"),
            ("work_name[]", work_name), ("cost_price[]", "1000"),
            ("multiplier[]", "2"), ("item_id[]", ""),
        ])
        return self.client.post("/tuning/add", data=data)

    def stored_names(self):
        with application_module.app.app_context():
            return [r["work_name"] for r in application_module.get_db().execute(
                "SELECT i.work_name FROM tuning_order_items i JOIN tuning_orders o ON o.id = i.order_id "
                "WHERE o.client_name = ? ORDER BY i.id", (self.CLIENT_NAME,)
            ).fetchall()]

    def order_id(self):
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM tuning_orders WHERE client_name = ?", (self.CLIENT_NAME,)
            ).fetchone()["id"]

    def test_the_editor_uses_a_wrapping_textarea_not_a_one_line_input(self):
        self.create("Установка пайола")
        page = self.client.get(f"/tuning/edit/{self.order_id()}").get_data(as_text=True)
        self.assertIn('<textarea name="work_name[]"', page)
        self.assertNotIn('<input type="text" name="work_name[]"', page)
        self.assertIn(">Установка пайола</textarea>", page)
        self.assertIn("autoGrowWorkName", page)
        # the blank template row used by "Добавить работу" is a textarea too
        template = page.split('id="work-row-template"')[1].split("</template>")[0]
        self.assertIn('<textarea name="work_name[]"', template)

    def test_a_very_long_name_is_stored_whole(self):
        long_name = "Установка и настройка комплекта оборудования " * 8
        long_name = long_name.strip()
        self.create(long_name)
        self.assertEqual(self.stored_names(), [long_name])

    def test_line_breaks_in_a_name_become_spaces(self):
        self.create("Полировка\r\nкорпуса \n  и  надстройки")
        self.assertEqual(self.stored_names(), ["Полировка корпуса и надстройки"])

    def test_a_name_that_is_only_line_breaks_counts_as_empty(self):
        response = self.create("\n \r\n")
        self.assertEqual(self.stored_names(), [])
        self.assertIn(response.status_code, (302, 400))


if __name__ == "__main__":
    unittest.main()
