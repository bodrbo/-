import io
import os
import unittest

from support import application_module


class TuningShipTicketTests(unittest.TestCase):
    MARK = "tuning-ship-ticket-test"

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
            self.order_id = self._make_order(db, "boat", self.MARK)
            self.motor_order_id = self._make_order(db, "motor", self.MARK + "-motor")
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._cleanup(application_module.get_db())

    def _make_order(self, db, equipment_type, source_ref):
        return db.execute(
            "INSERT INTO tuning_orders (client_name, equipment_type, boat_model, motor_model, "
            "sale_channel, phone, subtotal, total, status, order_date, created_at, updated_at, "
            "source, source_ref) VALUES ('Клиент', ?, 'Тест-катер', '', 'direct', '', 0, 0, "
            "'estimate', '2026-09-10', '2026-09-10 10:00', '2026-09-10 10:00', 'manual', ?)",
            (equipment_type, source_ref),
        ).lastrowid

    def _cleanup(self, db):
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref LIKE ?", (f"{self.MARK}%",)
        ).fetchall()]
        if ids:
            application_module._delete_tuning_order_records(db, ids)
        db.commit()

    def tickets_dir(self):
        return os.path.join(application_module.app.static_folder, "tuning_ship_tickets")

    def upload(self, order_id, filename="ticket.pdf", content=b"%PDF-1.4 fake", field="ship_ticket"):
        return self.client.post(
            f"/tuning/edit/{order_id}/ship-ticket",
            data={field: (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    def rows(self, order_id):
        with application_module.app.app_context():
            return [dict(r) for r in application_module.get_db().execute(
                "SELECT * FROM tuning_order_ship_tickets WHERE order_id = ? ORDER BY id", (order_id,)
            ).fetchall()]

    def test_upload_saves_the_file_and_shows_it_in_documents(self):
        response = self.upload(self.order_id, "Судовой билет.pdf", b"%PDF-1.4 hello")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"], f"/tuning/edit/{self.order_id}#documents"
        )
        rows = self.rows(self.order_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["original_filename"], "Судовой билет.pdf")
        self.assertTrue(os.path.isfile(os.path.join(self.tickets_dir(), rows[0]["filename"])))

        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Судовой билет загружен", page)
        self.assertIn("Судовой билет — Судовой билет.pdf", page)
        self.assertIn(f"/tuning/edit/{self.order_id}/ship-ticket/{rows[0]['id']}", page)

    def test_downloaded_file_matches_the_upload_and_keeps_the_original_name(self):
        self.upload(self.order_id, "billet.pdf", b"original bytes here")
        ticket_id = self.rows(self.order_id)[0]["id"]
        response = self.client.get(f"/tuning/edit/{self.order_id}/ship-ticket/{ticket_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"original bytes here")
        self.assertIn("billet.pdf", response.headers["Content-Disposition"])

    def test_delete_removes_the_row_and_the_file(self):
        self.upload(self.order_id)
        ticket = self.rows(self.order_id)[0]
        on_disk = os.path.join(self.tickets_dir(), ticket["filename"])
        self.assertTrue(os.path.isfile(on_disk))
        response = self.client.post(
            f"/tuning/edit/{self.order_id}/ship-ticket/{ticket['id']}/delete"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(self.order_id), [])
        self.assertFalse(os.path.isfile(on_disk))
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Судовой билет удалён", page)

    def test_missing_file_is_rejected(self):
        response = self.client.post(f"/tuning/edit/{self.order_id}/ship-ticket", data={})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(self.order_id), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("Выберите файл судового билета", page)

    def test_disallowed_extension_is_rejected(self):
        response = self.upload(self.order_id, "virus.exe", b"MZ")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(self.order_id), [])
        page = self.client.get(f"/tuning/edit/{self.order_id}").get_data(as_text=True)
        self.assertIn("PDF, JPG, PNG, WebP, DOC или DOCX", page)

    def test_motor_only_order_has_no_ship_ticket_upload(self):
        page = self.client.get(f"/tuning/edit/{self.motor_order_id}").get_data(as_text=True)
        self.assertNotIn("Судовой билет", page)
        response = self.upload(self.motor_order_id)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(self.motor_order_id), [])

    def test_deleting_the_order_cleans_up_the_ticket_file(self):
        self.upload(self.order_id)
        ticket = self.rows(self.order_id)[0]
        on_disk = os.path.join(self.tickets_dir(), ticket["filename"])
        self.assertTrue(os.path.isfile(on_disk))
        response = self.client.post(f"/tuning/delete/{self.order_id}")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            left = application_module.get_db().execute(
                "SELECT COUNT(*) FROM tuning_order_ship_tickets WHERE order_id = ?", (self.order_id,)
            ).fetchone()[0]
        self.assertEqual(left, 0)
        self.assertFalse(os.path.isfile(on_disk))

    def test_a_ticket_belongs_to_its_own_order_only(self):
        self.upload(self.order_id)
        ticket_id = self.rows(self.order_id)[0]["id"]
        other_order_id = None
        with application_module.app.app_context():
            db = application_module.get_db()
            other_order_id = self._make_order(db, "boat", self.MARK + "-other")
            db.commit()
        try:
            response = self.client.get(f"/tuning/edit/{other_order_id}/ship-ticket/{ticket_id}")
            self.assertEqual(response.status_code, 302)  # not found -> back to that order's card
        finally:
            with application_module.app.app_context():
                db = application_module.get_db()
                application_module._delete_tuning_order_records(db, [other_order_id])
                db.commit()


if __name__ == "__main__":
    unittest.main()
