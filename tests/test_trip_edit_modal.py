from urllib.parse import quote
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class TripEditModalTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self._clear_data()
        self.login()
        self.trip_id = self._create_trip()

    def tearDown(self):
        self._clear_data()

    @staticmethod
    def _clear_data():
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM trip_expenses")
            db.execute("DELETE FROM trip_labor")
            db.execute("DELETE FROM yclients_imports")
            db.execute("DELETE FROM trips")
            db.execute("DELETE FROM entries")
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    @staticmethod
    def trip_form(**overrides):
        values = {
            "boat": "Ларус",
            "trip_date": "2026-09-05",
            "trip_time": "13:00",
            "sale_channel": "direct",
            "commission_pct": "10",
            "revenue": "12000",
            "fuel_cost": "1500",
            "mooring_cost": "500",
            "employee[]": "Дмитрий Тарусов",
            "employee_custom[]": "",
            "work_type[]": "Большой тур",
            "work_type_custom[]": "",
            "quantity[]": "2.5",
            "rate[]": "1100",
            "expense_desc[]": "Лёд",
            "expense_amount[]": "300",
        }
        values.update(overrides)
        return MultiDict(values.items())

    def _create_trip(self):
        response = self.client.post("/trips/add", data=self.trip_form())
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            return application_module.get_db().execute(
                "SELECT id FROM trips ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]

    def test_trip_list_opens_editing_in_modal(self):
        response = self.client.get("/trips?month=2026-09&boat=Ларус")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('id="tripEditModal"', html)
        self.assertIn(f"openTripEditModal({self.trip_id})", html)
        self.assertNotIn(f'href="/trips/edit/{self.trip_id}"', html)
        self.assertIn(
            'value="/trips?month=2026-09&amp;boat=',
            html,
        )

    def test_modal_endpoint_returns_all_editable_trip_data(self):
        response = self.client.get(
            f"/trips/edit/{self.trip_id}?modal=1",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["trip"]["boat"], "Ларус")
        self.assertEqual(payload["trip"]["trip_time"], "13:00")
        self.assertEqual(payload["labor_items"][0]["employee"], "Дмитрий Тарусов")
        self.assertEqual(payload["expenses"][0]["description"], "Лёд")

    def test_ajax_save_keeps_filtered_return_url_and_updates_trip(self):
        return_url = "/trips?month=2026-09&boat=" + quote("Ларус")
        form = self.trip_form(
            trip_time="15:30",
            revenue="15000",
            **{"return_url": return_url},
        )
        response = self.client.post(
            f"/trips/edit/{self.trip_id}",
            data=form,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["redirect_url"], return_url)
        with application_module.app.app_context():
            trip = application_module.get_db().execute(
                "SELECT trip_time, revenue FROM trips WHERE id = ?", (self.trip_id,)
            ).fetchone()
        self.assertEqual(trip["trip_time"], "15:30")
        self.assertEqual(trip["revenue"], 15000)

    def test_changing_commission_marks_it_as_manual(self):
        response = self.client.post(
            f"/trips/edit/{self.trip_id}",
            data=self.trip_form(commission_pct="17"),
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(response.status_code, 200)
        with application_module.app.app_context():
            trip = application_module.get_db().execute(
                "SELECT commission_pct, commission_is_manual FROM trips WHERE id = ?",
                (self.trip_id,),
            ).fetchone()
        self.assertEqual(trip["commission_pct"], 17)
        self.assertEqual(trip["commission_is_manual"], 1)

    def test_ajax_validation_error_stays_in_modal(self):
        invalid = self.trip_form(**{"employee[]": "", "work_type[]": ""})
        response = self.client.post(
            f"/trips/edit/{self.trip_id}",
            data=invalid,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["errors"])


if __name__ == "__main__":
    unittest.main()
