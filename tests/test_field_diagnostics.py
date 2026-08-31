import unittest

from support import application_module
from modules.field_diagnostics.constants import (
    DIAGNOSTIC_BLOCKS,
    FIELD_DIAGNOSTIC_QUESTIONS,
)


class FieldDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self.cleanup_database()
        self.addCleanup(self.cleanup_database)

    @staticmethod
    def cleanup_database():
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM field_diagnostic_extra_defects")
            db.execute("DELETE FROM field_diagnostic_answers")
            db.execute("DELETE FROM field_diagnostic_sheets")
            db.execute(
                "DELETE FROM tuning_boat_profiles WHERE model_key LIKE 'test field %'"
            )
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Диагност"

    def create_sheet(self, model="Test Field 520", inspection_type="water"):
        return self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": model,
                "owner_name": "Иван Судовладелец",
                "owner_phone": "+7 999 123-45-67",
                "inspection_type": inspection_type,
            },
        )

    def test_routes_require_admin_login(self):
        response = self.client.get("/tuning/diagnostics/field")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_new_sheet_adds_custom_model_to_shared_catalog(self):
        self.login()

        response = self.create_sheet(model="  Test   Field  520  ")

        self.assertEqual(response.status_code, 302)
        self.assertRegex(
            response.headers["Location"], r"/tuning/diagnostics/field/\d+$"
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE model_key = ?",
                ("test field 520",),
            ).fetchone()
            sheet = db.execute("SELECT * FROM field_diagnostic_sheets").fetchone()
            self.assertIsNotNone(profile)
            self.assertEqual(profile["model_name"], "Test Field 520")
            self.assertEqual(sheet["boat_profile_id"], profile["id"])
            self.assertEqual(sheet["boat_model"], "Test Field 520")
            self.assertEqual(sheet["owner_name"], "Иван Судовладелец")
            self.assertEqual(sheet["owner_phone"], "+7 999 123-45-67")
            self.assertEqual(sheet["created_by_name"], "Диагност")
            self.assertIn('"section": "Электрика"', sheet["question_set_json"])

        page = self.client.get(response.headers["Location"])
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Осмотр на воде", html)
        self.assertIn(FIELD_DIAGNOSTIC_QUESTIONS["water"][0]["title"], html)
        self.assertNotIn(FIELD_DIAGNOSTIC_QUESTIONS["land"][0]["title"], html)
        for block_name in DIAGNOSTIC_BLOCKS:
            self.assertIn(block_name, html)

    def test_existing_catalog_model_is_reused_with_canonical_spelling(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_boat_profiles "
                "(model_key, model_name, specifications, created_at, updated_at) "
                "VALUES ('test field existing', 'Test Field Existing', '', "
                "'2026-08-31 10:00', '2026-08-31 10:00')"
            )
            db.commit()

        response = self.create_sheet(model=" TEST   FIELD existing ")

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM tuning_boat_profiles "
                    "WHERE model_key = 'test field existing'"
                ).fetchone()[0],
                1,
            )
            sheet = db.execute("SELECT * FROM field_diagnostic_sheets").fetchone()
            self.assertEqual(sheet["boat_model"], "Test Field Existing")

    def test_invalid_creation_rerenders_modal_without_writes(self):
        self.login()

        response = self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": "",
                "owner_name": "",
                "owner_phone": "",
                "inspection_type": "air",
            },
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Проверьте заполнение", html)
        self.assertIn("Укажите модель лодки", html)
        self.assertIn('field-diagnostic-modal is-open', html)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM field_diagnostic_sheets").fetchone()[0],
                0,
            )

    def test_problem_needs_description_and_answers_are_sequential(self):
        self.login()
        created = self.create_sheet()
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])

        invalid = self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "0", "status": "problem", "comment": ""},
        )
        skipped = self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "2", "status": "ok"},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Опишите обнаруженную неисправность", invalid.get_data(as_text=True))
        self.assertEqual(skipped.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM field_diagnostic_answers").fetchone()[0],
                0,
            )

    def test_completed_inspection_builds_pdf_with_ok_and_problem_results(self):
        self.login()
        created = self.create_sheet(inspection_type="land")
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])
        questions = FIELD_DIAGNOSTIC_QUESTIONS["land"]

        for question_index in range(len(questions)):
            status = "problem" if question_index == 1 else "ok"
            response = self.client.post(
                "/tuning/diagnostics/field/%d/answer" % sheet_id,
                data={
                    "question_index": str(question_index),
                    "status": status,
                    "comment": "Трещина 4 см у крепления" if status == "problem" else "",
                },
            )
            self.assertEqual(response.status_code, 302)

        other_step = self.client.get(sheet_path)
        other_html = other_step.get_data(as_text=True)
        self.assertIn("Прочие неисправности", other_html)
        self.assertIn("Завершить осмотр", other_html)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT status FROM field_diagnostic_sheets WHERE id = ?",
                    (sheet_id,),
                ).fetchone()["status"],
                "in_progress",
            )

        completed = self.client.post(
            "/tuning/diagnostics/field/%d/other" % sheet_id,
            data={
                "other_defect[]": [
                    "Не хватает спасательного жилета",
                    "Повреждён тент",
                ]
            },
        )
        self.assertEqual(completed.status_code, 302)

        result = self.client.get(sheet_path)
        result_html = result.get_data(as_text=True)
        self.assertEqual(result.status_code, 200)
        self.assertIn("Диагностический лист готов", result_html)
        self.assertIn("Трещина 4 см у крепления", result_html)
        self.assertIn("Не хватает спасательного жилета", result_html)
        for block_name in DIAGNOSTIC_BLOCKS:
            self.assertIn(block_name, result_html)

        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
            ).fetchone()
            self.assertEqual(sheet["status"], "completed")
            self.assertIsNotNone(sheet["completed_at"])
            self.assertIsNotNone(sheet["other_completed_at"])
            answers = db.execute(
                "SELECT * FROM field_diagnostic_answers WHERE sheet_id = ? "
                "ORDER BY question_index",
                (sheet_id,),
            ).fetchall()
            self.assertEqual(len(answers), len(questions))
            self.assertEqual(answers[1]["status"], "problem")
            self.assertEqual(answers[1]["comment"], "Трещина 4 см у крепления")
            extra_defects = db.execute(
                "SELECT description FROM field_diagnostic_extra_defects "
                "WHERE sheet_id = ? ORDER BY id",
                (sheet_id,),
            ).fetchall()
            self.assertEqual(
                [row["description"] for row in extra_defects],
                ["Не хватает спасательного жилета", "Повреждён тент"],
            )

        pdf = self.client.get(
            "/tuning/diagnostics/field/%d/diagnostic-sheet.pdf" % sheet_id
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertGreater(len(pdf.data), 5000)
        self.assertIn(
            'inline; filename="Diagnostic-sheet-%d.pdf"' % sheet_id,
            pdf.headers["Content-Disposition"],
        )

    def test_other_block_can_be_completed_without_defects(self):
        self.login()
        created = self.create_sheet(inspection_type="water")
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])
        questions = FIELD_DIAGNOSTIC_QUESTIONS["water"]
        section_order = []
        for question in questions:
            if not section_order or section_order[-1] != question["section"]:
                section_order.append(question["section"])
        self.assertEqual(section_order, list(DIAGNOSTIC_BLOCKS[:3]))
        self.assertNotIn(
            "Прочее", {question["section"] for question in questions}
        )

        for question_index in range(len(questions)):
            self.client.post(
                "/tuning/diagnostics/field/%d/answer" % sheet_id,
                data={"question_index": str(question_index), "status": "ok"},
            )
        response = self.client.post(
            "/tuning/diagnostics/field/%d/other" % sheet_id,
            data={"other_defect[]": ""},
        )

        self.assertEqual(response.status_code, 302)
        result = self.client.get(sheet_path).get_data(as_text=True)
        self.assertIn("Дополнительные неисправности не указаны", result)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_extra_defects "
                    "WHERE sheet_id = ?",
                    (sheet_id,),
                ).fetchone()[0],
                0,
            )

    def test_pdf_is_not_available_until_inspection_is_completed(self):
        self.login()
        created = self.create_sheet()
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])

        response = self.client.get(
            "/tuning/diagnostics/field/%d/diagnostic-sheet.pdf" % sheet_id
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(sheet_path))


if __name__ == "__main__":
    unittest.main()
