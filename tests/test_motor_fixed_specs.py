import unittest

from support import application_module


class MotorFixedSpecsTests(unittest.TestCase):
    MODEL = "Тест-мотор Спеки 40"

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
            key = application_module._tuning_equipment_profile_key("motor", self.MODEL)
            self.motor_id = db.execute(
                "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, "
                "specifications, created_at, updated_at) VALUES (?, ?, 'motor', '', 'x', 'x')",
                (key, self.MODEL),
            ).lastrowid
            boat_key = application_module._tuning_equipment_profile_key("boat", self.MODEL + " лодка")
            self.boat_id = db.execute(
                "INSERT INTO tuning_boat_profiles (model_key, model_name, equipment_type, "
                "specifications, created_at, updated_at) VALUES (?, ?, 'boat', '', 'x', 'x')",
                (boat_key, self.MODEL + " лодка"),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        db.execute("DELETE FROM tuning_boat_profiles WHERE model_name LIKE ?", (f"{self.MODEL}%",))
        db.commit()

    def save(self, profile_id=None, **fields):
        data = {"specifications": "", **fields}
        return self.client.post(f"/tuning/equipment/{profile_id or self.motor_id}/edit", data=data)

    def row(self, profile_id=None):
        with application_module.app.app_context():
            return dict(application_module.get_db().execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id or self.motor_id,)
            ).fetchone())

    def test_fixed_specs_are_saved_and_shown(self):
        response = self.save(
            brand="Yamaha", power_hp="40", oil_viscosity="  10W-40 ",
            crankcase_volume_l="1,5", gearbox_volume_l="0,35",
        )
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row["oil_viscosity"], "10W-40")
        self.assertEqual(row["crankcase_volume_l"], 1.5)
        self.assertEqual(row["gearbox_volume_l"], 0.35)
        page = self.client.get(f"/tuning/motors/{self.motor_id}").get_data(as_text=True)
        self.assertIn("Вязкость масла", page)
        self.assertIn("Объём картера, л", page)
        self.assertIn("Объём редуктора, л", page)
        self.assertIn('value="10W-40"', page)
        self.assertIn('name="crankcase_volume_l"\n                   value="1.5"', page)
        self.assertIn('name="gearbox_volume_l"\n                   value="0.35"', page)

    def test_specs_can_be_cleared(self):
        self.save(oil_viscosity="SAE 30", crankcase_volume_l="2", gearbox_volume_l="0.4")
        self.save(oil_viscosity="", crankcase_volume_l="", gearbox_volume_l="")
        row = self.row()
        self.assertIsNone(row["oil_viscosity"])
        self.assertIsNone(row["crankcase_volume_l"])
        self.assertIsNone(row["gearbox_volume_l"])

    def test_invalid_values_are_rejected_without_changing_anything(self):
        self.save(oil_viscosity="10W-40", crankcase_volume_l="1.5", gearbox_volume_l="0.3")
        for field, bad in (
            ("crankcase_volume_l", "abc"), ("crankcase_volume_l", "-1"), ("crankcase_volume_l", "0"),
            ("gearbox_volume_l", "5000"), ("gearbox_volume_l", "x"),
            ("oil_viscosity", "x" * 41),
        ):
            values = {"oil_viscosity": "5W-30", "crankcase_volume_l": "9", "gearbox_volume_l": "9"}
            values[field] = bad
            self.save(**values)
            row = self.row()
            self.assertEqual(
                (row["oil_viscosity"], row["crankcase_volume_l"], row["gearbox_volume_l"]),
                ("10W-40", 1.5, 0.3), f"{field}={bad!r}",
            )

    def test_boat_profiles_ignore_motor_specs(self):
        self.save(
            profile_id=self.boat_id, length_m="5", width_m="2",
            oil_viscosity="10W-40", crankcase_volume_l="1", gearbox_volume_l="1",
        )
        row = self.row(self.boat_id)
        self.assertIsNone(row["oil_viscosity"])
        self.assertIsNone(row["crankcase_volume_l"])
        self.assertIsNone(row["gearbox_volume_l"])
        page = self.client.get(f"/tuning/boats/{self.boat_id}").get_data(as_text=True)
        self.assertNotIn("Вязкость масла", page)


if __name__ == "__main__":
    unittest.main()
