import json
import unittest
from unittest import mock

from support import application_module


class TuningWorkItemVideoTests(unittest.TestCase):
    """Videos attached to a work item live entirely on the admin's own
    Яндекс.Диск — we only store the public link they paste, validate it
    against the Yandex Disk public API, and resolve a fresh signed
    download URL (via a redirect) each time someone plays it."""

    MARK = "tuning-work-item-video-test"

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
            self.order_id = db.execute(
                "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, "
                "sale_channel, phone, subtotal, total, status, order_date, created_at, "
                "updated_at, source, source_ref) VALUES ('Клиент', 'boat', 'Тест-катер', "
                "'direct', '', 1000, 1000, 'estimate', '2026-09-10', '2026-09-10 10:00', "
                "'2026-09-10 10:00', 'manual', ?)",
                (self.MARK,),
            ).lastrowid
            self.item_id = db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, "
                "multiplier, price, price_pending, status) VALUES (?, 'Диагностика', "
                "500, 2, 1000, 0, 'pending')",
                (self.order_id,),
            ).lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (self.MARK,)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.commit()

    def rows(self):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM work_item_videos WHERE item_id = ? ORDER BY id", (self.item_id,)
            ).fetchall()]

    def _mock_metadata(self, media_type="video", resource_type="file"):
        resp = mock.Mock(status_code=200, ok=True)
        resp.json.return_value = {"type": resource_type, "media_type": media_type, "name": "ролик.mp4"}
        return resp

    def test_valid_video_link_is_saved_after_validation(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata()) as mocked:
            response = self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123", "comment": "демо"},
            )
            mocked.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], f"/tuning/edit/{self.order_id}#work-container"
        )
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["yandex_public_key"], "https://disk.yandex.ru/i/abc123")
        self.assertEqual(rows[0]["display_name"], "ролик.mp4")
        self.assertEqual(rows[0]["comment"], "демо")

        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Видео добавлено", page)
        # the Cyrillic display name is JSON-escaped (\uXXXX) inside the
        # inline onclick attribute, so check for the escaped form
        self.assertIn(json.dumps("ролик.mp4")[1:-1], page)
        self.assertIn("openVideoModal", page)

    def test_empty_link_is_rejected(self):
        response = self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/video", data={"yandex_url": ""}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Вставьте ссылку", page)

    def test_a_folder_link_is_rejected(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata(resource_type="dir")):
            response = self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/d/abc123"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("не на папку", page)

    def test_a_non_video_file_is_rejected(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata(media_type="image")):
            response = self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("не похож на видео", page)

    def test_a_missing_or_private_link_is_rejected(self):
        not_found = mock.Mock(status_code=404, ok=False)
        with mock.patch("app.requests.get", return_value=not_found):
            response = self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/gone"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("не нашёл файл", page)

    def test_play_redirects_to_a_freshly_resolved_yandex_url(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata()):
            self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123"},
            )
        video_id = self.rows()[0]["id"]

        download_resp = mock.Mock(status_code=200, ok=True)
        download_resp.json.return_value = {"href": "https://downloader.disk.yandex.ru/signed"}
        with mock.patch("app.requests.get", return_value=download_resp) as mocked:
            response = self.client.get(
                f"/tuning/{self.order_id}/item/{self.item_id}/video/{video_id}/play"
            )
            mocked.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://downloader.disk.yandex.ru/signed")

    def test_play_fails_gracefully_when_yandex_is_unreachable(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata()):
            self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123"},
            )
        video_id = self.rows()[0]["id"]

        import requests as requests_module
        with mock.patch("app.requests.get", side_effect=requests_module.RequestException()):
            response = self.client.get(
                f"/tuning/{self.order_id}/item/{self.item_id}/video/{video_id}/play"
            )
        self.assertEqual(response.status_code, 502)

    def test_delete_removes_the_row_but_never_touches_yandex_disk(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata()):
            self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123"},
            )
        video_id = self.rows()[0]["id"]
        response = self.client.post(
            f"/tuning/{self.order_id}/item/{self.item_id}/video/{video_id}/delete"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])

    def test_removing_the_work_row_on_order_edit_cleans_up_its_videos(self):
        with mock.patch("app.requests.get", return_value=self._mock_metadata()):
            self.client.post(
                f"/tuning/{self.order_id}/item/{self.item_id}/video",
                data={"yandex_url": "https://disk.yandex.ru/i/abc123"},
            )
        self.assertEqual(len(self.rows()), 1)

        from werkzeug.datastructures import MultiDict
        edit_form = MultiDict([
            ("client_name", "Клиент"),
            ("equipment_type", "boat"),
            ("boat_model", "Тест-катер"),
            ("boat_registration_number", ""),
            ("motor_model", ""),
            ("motor_serial_number", ""),
            ("phone", ""),
            ("order_date", "2026-09-10"),
            ("sale_channel", "direct"),
            ("discount_type", "percent"),
            ("discount_value", "0"),
            # a different, brand-new work row is submitted (item_id[] blank)
            # while the original item_id is left out entirely -> the form
            # treats the original row (and its video) as removed by the user
            ("work_name[]", "Другая работа"),
            ("cost_price[]", "500"),
            ("multiplier[]", "1"),
            ("item_id[]", ""),
        ])
        response = self.client.post(f"/tuning/edit/{self.order_id}", data=edit_form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
