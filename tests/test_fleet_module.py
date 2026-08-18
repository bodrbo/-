import io
import os
import unittest

from support import TEST_DIRECTORY, application_module


class FleetModuleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()
        cls.original_static_folder = application_module.app.static_folder
        application_module.app.static_folder = os.path.join(
            TEST_DIRECTORY.name, "static"
        )
        os.makedirs(application_module.app.static_folder, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        application_module.app.static_folder = cls.original_static_folder

    def setUp(self):
        self.client = application_module.app.test_client()

    def log_in_as_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def create_defect(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            cursor = db.execute(
                "INSERT INTO boat_defects "
                "(boat, description, employee_name, status, reported_at, updated_at) "
                "VALUES (?, ?, ?, 'new', ?, ?)",
                (
                    "Ларус",
                    "Тестовая неисправность",
                    "Дмитрий Тарусов",
                    "2026-08-18 10:00",
                    "2026-08-18 10:00",
                ),
            )
            db.commit()
            return cursor.lastrowid

    def test_fleet_routes_are_registered_by_blueprint(self):
        fleet_rules = {
            rule.endpoint: rule.rule
            for rule in application_module.app.url_map.iter_rules()
            if rule.rule.startswith("/fleet")
        }
        self.assertEqual(fleet_rules["fleet.index"], "/fleet")
        self.assertEqual(fleet_rules["fleet.boat_detail"], "/fleet/<int:boat_index>")
        self.assertEqual(
            fleet_rules["fleet.delete_defect"],
            "/fleet/<int:boat_index>/defects/<int:defect_id>/delete",
        )
        self.assertNotIn("fleet_index", fleet_rules)

    def test_fleet_requires_admin_session(self):
        response = self.client.get("/fleet")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

    def test_admin_can_open_fleet_and_boat(self):
        self.log_in_as_admin()
        response = self.client.get("/fleet")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Флот".encode(), response.data)

        response = self.client.get("/fleet/0")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ларус".encode(), response.data)

    def test_defect_case_and_plan_use_shared_fleet_services(self):
        defect_id = self.create_defect()
        self.log_in_as_admin()

        response = self.client.get(f"/fleet/0/defects/{defect_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Удалить неисправность".encode(), response.data)

        response = self.client.post(
            f"/fleet/0/defects/{defect_id}",
            data={"anamnesis": "Шум", "diagnosis": "Износ реле"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/fleet/0/defects/{defect_id}/plan",
            data={"description": "Заменить реле"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/fleet/0/defects/{defect_id}/status",
            data={"status": "monitoring"},
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            defect = db.execute(
                "SELECT * FROM boat_defects WHERE id = ?", (defect_id,)
            ).fetchone()
            plan_item = db.execute(
                "SELECT * FROM defect_work_plan_items WHERE defect_id = ?", (defect_id,)
            ).fetchone()
            self.assertEqual(defect["anamnesis"], "Шум")
            self.assertEqual(defect["diagnosis"], "Износ реле")
            self.assertEqual(defect["status"], "monitoring")
            self.assertEqual(plan_item["description"], "Заменить реле")

        response = self.client.post(
            f"/fleet/0/defects/{defect_id}/plan/{plan_item['id']}/status",
            data={"status": "done"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/fleet/0/defects/{defect_id}/assign",
            data={
                "employee_name": "Дмитрий Тарусов",
                "rate": "1500",
                "norm_hours": "2",
            },
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            plan_status = db.execute(
                "SELECT status FROM defect_work_plan_items WHERE id = ?", (plan_item["id"],)
            ).fetchone()["status"]
            assignment = db.execute(
                "SELECT * FROM defect_assignments WHERE defect_id = ?", (defect_id,)
            ).fetchone()
            self.assertEqual(plan_status, "done")
            self.assertEqual(assignment["employee_name"], "Дмитрий Тарусов")
            self.assertEqual(assignment["rate"], 1500)

    def test_admin_can_delete_defect_with_children_but_keeps_payroll_entry(self):
        defect_id = self.create_defect()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO defect_work_plan_items "
                "(defect_id, description, status, created_at, updated_at) "
                "VALUES (?, 'Проверить реле', 'pending', ?, ?)",
                (defect_id, "2026-08-18 10:05", "2026-08-18 10:05"),
            )
            entry_cursor = db.execute(
                "INSERT INTO entries "
                "(employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES ('Дмитрий Тарусов', 'Ремонт', 1500, 1, 1500, ?, ?)",
                ("2026-08-18", "2026-08-18 11:00"),
            )
            entry_id = entry_cursor.lastrowid
            db.execute(
                "INSERT INTO defect_assignments "
                "(defect_id, employee_name, rate, norm_hours, assignment_status, "
                "assigned_at, entry_id) VALUES (?, 'Дмитрий Тарусов', 1500, 1, "
                "'accepted', ?, ?)",
                (defect_id, "2026-08-18 10:10", entry_id),
            )
            db.commit()

        response = self.client.post(f"/fleet/0/defects/{defect_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

        self.log_in_as_admin()
        response = self.client.post(f"/fleet/1/defects/{defect_id}/delete")
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            self.assertIsNotNone(
                application_module.get_db()
                .execute("SELECT 1 FROM boat_defects WHERE id = ?", (defect_id,))
                .fetchone()
            )

        response = self.client.get("/fleet/0")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'action="/fleet/0/defects/{defect_id}/delete"'.encode(), response.data
        )

        response = self.client.post(f"/fleet/0/defects/{defect_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/fleet/0"))

        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertIsNone(
                db.execute("SELECT 1 FROM boat_defects WHERE id = ?", (defect_id,)).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM defect_work_plan_items WHERE defect_id = ?", (defect_id,)
                ).fetchone()
            )
            self.assertIsNone(
                db.execute(
                    "SELECT 1 FROM defect_assignments WHERE defect_id = ?", (defect_id,)
                ).fetchone()
            )
            self.assertIsNotNone(
                db.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone()
            )

    def test_document_upload_download_and_delete(self):
        self.log_in_as_admin()
        response = self.client.post(
            "/fleet/0/documents",
            data={
                "title": "Судовой билет",
                "document": (io.BytesIO(b"test document"), "ticket.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            document = db.execute(
                "SELECT * FROM boat_documents WHERE title = ?", ("Судовой билет",)
            ).fetchone()
            self.assertIsNotNone(document)
            document_id = document["id"]
            stored_path = os.path.join(
                application_module.app.static_folder,
                "boat_documents",
                document["filename"],
            )
            self.assertTrue(os.path.exists(stored_path))

        response = self.client.get(f"/fleet/0/documents/{document_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"test document")
        response.close()

        response = self.client.post(f"/fleet/0/documents/{document_id}/delete")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(os.path.exists(stored_path))

    def test_team_defect_page_reuses_fleet_service(self):
        defect_id = self.create_defect()
        with self.client.session_transaction() as session:
            session["team_id"] = 1
            session["team_employee_name"] = "Дмитрий Тарусов"

        response = self.client.get(f"/team/defects/{defect_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Тестовая неисправность".encode(), response.data)
        self.assertNotIn("Удалить неисправность".encode(), response.data)


if __name__ == "__main__":
    unittest.main()
