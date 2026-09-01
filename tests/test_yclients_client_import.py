import unittest

from support import application_module
from modules.clients.yclients import fetch_clients, import_clients


class FakeResponse:
    def __init__(self, data, total_count, status_code=200):
        self.status_code = status_code
        self._body = {
            "success": status_code == 200,
            "data": data,
            "meta": {"total_count": total_count},
        }

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, headers, params, timeout):
        self.calls.append((url, headers, params, timeout))
        return self.pages[params["page"] - 1]


class YclientsClientImportTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM client_segments")
            db.execute("DELETE FROM clients")
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM client_segments")
            db.execute("DELETE FROM clients")
            db.commit()

    def test_fetches_all_pages_of_two_hundred(self):
        first_page = [{"id": index, "display_name": f"Клиент {index}"}
                      for index in range(1, 201)]
        second_page = [{"id": 201, "display_name": "Последний клиент"}]
        session = FakeSession([
            FakeResponse(first_page, 201),
            FakeResponse(second_page, 201),
        ])

        rows = fetch_clients(
            "https://api.example.test/api/v1", "1", "partner", "user",
            http_session=session,
        )

        self.assertEqual(len(rows), 201)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][2], {"page": 1, "count": 200})

    def test_import_accepts_missing_and_duplicate_phones_and_is_idempotent(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            existing_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Старое имя', '', '+79990000001', 'legacy-import', "
                "'2026-09-01 08:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'tuning', '2026-09-01 08:00')",
                (existing_id,),
            )
            rows = [
                {"id": 1001, "display_name": "Новое имя", "phone": "+79990000001"},
                {"id": 1002, "display_name": "Без телефона", "phone": ""},
                {"id": 1003, "display_name": "Дубль телефона", "phone": "+79990000001"},
            ]
            first = import_clients(db, rows, "2026-09-01 12:00")
            second = import_clients(db, rows, "2026-09-01 12:30")
            db.commit()
            clients = db.execute(
                "SELECT * FROM clients ORDER BY yclients_client_id"
            ).fetchall()
            segments = db.execute(
                "SELECT client_id, segment FROM client_segments"
            ).fetchall()

        self.assertEqual(first["received"], 3)
        self.assertEqual(first["linked"], 1)
        self.assertEqual(first["created"], 2)
        self.assertEqual(second["created"], 0)
        self.assertEqual(len(clients), 3)
        self.assertEqual(clients[0]["id"], existing_id)
        self.assertEqual(clients[1]["phone"], "")
        self.assertEqual(clients[0]["phone"], clients[2]["phone"])
        self.assertEqual(
            {row["segment"] for row in segments}, {"tuning", "excursion"}
        )

    def test_schema_allows_more_than_one_client_without_phone(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            for index in (1, 2):
                db.execute(
                    "INSERT INTO clients "
                    "(client_name, boat_model, phone, token, created_at) "
                    "VALUES (?, '', '', ?, '2026-09-01 12:00')",
                    (f"Без телефона {index}", f"no-phone-{index}"),
                )
            db.commit()
            count = db.execute(
                "SELECT COUNT(*) FROM clients WHERE phone = ''"
            ).fetchone()[0]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
