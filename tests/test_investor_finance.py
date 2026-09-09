import io
import unittest
import zipfile
from xml.sax.saxutils import escape

from support import application_module


def _legacy_workbook_bytes(title="БОДРЫЙ ПЕРВЫЙ - доходы и расходы"):
    cells = {
        "A1": title,
        "D1": 45849,
        "D2": 360,
        "D4": 5,
        "F1": "10%-39%",
        "F2": 0.5,
        "F3": 1177250,
        "C7": 45923,
        "D7": 148553,
        "A15": 46202,
        "B15": 10000,
        "C15": "Аренда 2 часа",
        "D15": -2200,
        "E15": "Гиду и капитану",
        "A16": 46202,
        "D16": "--768",
        "E16": "Бензин",
        "A17": 46203,
        "D17": -1000,
        "E17": "Стоянка",
    }
    rows = {}
    for reference, value in cells.items():
        row_number = int("".join(character for character in reference if character.isdigit()))
        if isinstance(value, str):
            node = (
                f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
            )
        else:
            node = f'<c r="{reference}"><v>{value}</v></c>'
        rows.setdefault(row_number, []).append(node)
    sheet_rows = "".join(
        f'<row r="{row}">{"".join(nodes)}</row>' for row, nodes in sorted(rows.items())
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{sheet_rows}</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Лист1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return output.getvalue()


class InvestorFinanceTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self._clear_data()

    def tearDown(self):
        self._clear_data()

    @staticmethod
    def _clear_data():
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM investor_distributions WHERE boat = 'Бодрый Первый'")
            db.execute("DELETE FROM investor_ledger_entries WHERE boat = 'Бодрый Первый'")
            db.execute("DELETE FROM investor_asset_profiles WHERE boat = 'Бодрый Первый'")
            db.execute("DELETE FROM trips WHERE boat = 'Бодрый Первый'")
            db.commit()

    def _login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def _login_investor(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["investor_id"] = 1
            session["investor_name"] = "Андрей Жаворонков"

    def _upload(self, payload=None, filename="report.xlsx"):
        self._login_admin()
        return self.client.post(
            "/trips/investor-history/import",
            data={
                "boat": "Бодрый Первый",
                "investor_workbook": (
                    io.BytesIO(payload if payload is not None else _legacy_workbook_bytes()),
                    filename,
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def test_import_maps_terms_ledger_and_payouts(self):
        response = self._upload()
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Операций: 4. Выплат: 1.", html)
        self.assertIn("двойной минус исправлен", html)
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM investor_asset_profiles WHERE boat = 'Бодрый Первый'"
            ).fetchone()
            entries = db.execute(
                "SELECT entry_kind, description, amount FROM investor_ledger_entries "
                "WHERE boat = 'Бодрый Первый' ORDER BY source_row, id"
            ).fetchall()
            payout = db.execute(
                "SELECT payout_date, amount FROM investor_distributions "
                "WHERE boat = 'Бодрый Первый'"
            ).fetchone()

        self.assertEqual(profile["investment_date"], "2025-07-11")
        self.assertEqual(profile["history_through_date"], "2026-06-30")
        self.assertEqual(profile["investment_amount"], 1177250)
        self.assertEqual(profile["investor_share"], 0.5)
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[2]["description"], "Бензин")
        self.assertEqual(entries[2]["amount"], 768)
        self.assertEqual(payout["payout_date"], "2025-09-23")
        self.assertEqual(payout["amount"], 148553)

    def test_reimport_replaces_legacy_rows_without_duplicates(self):
        self._upload()
        self._upload()

        with application_module.app.app_context():
            db = application_module.get_db()
            entry_count = db.execute(
                "SELECT COUNT(*) AS count FROM investor_ledger_entries "
                "WHERE boat = 'Бодрый Первый'"
            ).fetchone()["count"]
            payout_count = db.execute(
                "SELECT COUNT(*) AS count FROM investor_distributions "
                "WHERE boat = 'Бодрый Первый'"
            ).fetchone()["count"]
        self.assertEqual(entry_count, 4)
        self.assertEqual(payout_count, 1)

    def test_dashboard_combines_history_only_with_trips_after_cutoff(self):
        self._upload()
        with application_module.app.app_context():
            db = application_module.get_db()
            now = "2026-07-01 12:00"
            statement = (
                "INSERT INTO trips (boat, trip_date, trip_time, work_type, revenue, sale_channel, "
                "commission_pct, commission_amount, labor_cost, fuel_cost, mooring_cost, extra_total, "
                "remainder, investor_payout, my_share, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            db.execute(statement, (
                "Бодрый Первый", "2026-06-30", "00:00", "Старый дубль", 100000.0,
                "direct", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100000.0, 50000.0,
                50000.0, now,
            ))
            db.execute(statement, (
                "Бодрый Первый", "2026-07-01", "00:00", "Новый рейс", 2000.0,
                "direct", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 2000.0, 1000.0,
                1000.0, now,
            ))
            db.commit()

        self._login_investor()
        response = self.client.get("/investor/?month=all")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        normalized_html = html.replace("\u00a0", " ")
        self.assertIn("Аренда 2 часа", html)
        self.assertIn("Новый рейс", html)
        self.assertNotIn("Старый дубль", html)
        self.assertIn("4 016", normalized_html)
        self.assertIn("148 553", normalized_html)

    def test_invalid_workbook_is_rejected_without_erasing_existing_history(self):
        self._upload()
        response = self._upload(_legacy_workbook_bytes("ДРУГОЙ ОТЧЁТ"))
        html = response.get_data(as_text=True)

        self.assertIn("Это не отчёт", html)
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM investor_ledger_entries "
                "WHERE boat = 'Бодрый Первый'"
            ).fetchone()["count"]
        self.assertEqual(count, 4)


if __name__ == "__main__":
    unittest.main()
