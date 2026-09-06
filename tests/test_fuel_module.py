import datetime as dt
import unittest

from modules.fleet import fuel_repository, fuel_services
from support import application_module


class FuelModuleIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM boat_fuel_trip_events")
            db.execute("DELETE FROM boat_fuel_transactions")
            db.execute(
                "UPDATE boat_fuel_state SET activated_at = NULL, "
                "activated_by_role = NULL, activated_by_name = NULL, "
                "last_synced_at = NULL"
            )
            db.commit()

        self.original_now = fuel_services.current_datetime
        fuel_services.current_datetime = lambda: dt.datetime(2026, 8, 18, 12, 0)
        self.addCleanup(setattr, fuel_services, "current_datetime", self.original_now)

    def log_in_as_admin(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def activate_boat(self, boat="Бодрый Первый", liters="70"):
        with application_module.app.app_context():
            return fuel_services.record_refill(
                application_module.get_db(),
                boat,
                liters,
                "2026-08-18T08:00",
                True,
                "admin",
                "Администратор теста",
            )

    def completed_group_record(self):
        return {
            "id": 1,
            "activity_id": 777,
            "datetime": "2026-08-18T09:00:00+03:00",
            "seance_length": 3600,
            "attendance": 0,
            "services": [{"id": 14624788, "title": "Групповой рейс"}],
        }

    def test_first_full_refill_activates_known_capacity(self):
        self.assertEqual(
            fuel_services.FUEL_CONFIG,
            {
                "Ларус": {"capacity_liters": 60.0, "group_trip_liters": 12.0},
                "Бодрый Второй": {
                    "capacity_liters": 250.0,
                    "group_trip_liters": 10.0,
                },
                "Бодрый Первый": {
                    "capacity_liters": 100.0,
                    "group_trip_liters": 12.0,
                },
            },
        )
        self.log_in_as_admin()
        response = self.client.post(
            "/fleet/2/fuel/refill",
            data={
                "liters": "70",
                "occurred_at": "2026-08-18T08:00",
                "fill_to_full": "1",
            },
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            summary = fuel_services.fuel_summary(db, "Бодрый Первый")
            transaction = db.execute(
                "SELECT * FROM boat_fuel_transactions WHERE boat = 'Бодрый Первый'"
            ).fetchone()
            self.assertTrue(summary["activated"])
            self.assertEqual(summary["capacity_liters"], 100)
            self.assertEqual(summary["balance_liters"], 100)
            self.assertEqual(transaction["reported_liters"], 70)
            self.assertEqual(transaction["liters_delta"], 100)
            self.assertEqual(transaction["created_by_name"], "Администратор теста")

    def test_group_trip_is_debited_once_and_individual_waits_for_manual_value(self):
        success, _ = self.activate_boat()
        self.assertTrue(success)
        records = [
            {
                "id": 1,
                "activity_id": 777,
                "datetime": "2026-08-18T09:00:00+03:00",
                "seance_length": 3600,
                "attendance": 0,
                "services": [{"id": 14624788, "title": "Групповой рейс"}],
            },
            {
                "id": 2,
                "datetime": "2026-08-18T10:30:00+03:00",
                "seance_length": 3600,
                "custom_color": "8bc34a",
                "services": [{"id": 14624830, "title": "Индивидуальная аренда"}],
            },
            {
                "id": 3,
                "datetime": "2026-08-18T09:30:00+03:00",
                "seance_length": 3600,
                "custom_color": "8bc34a",
                "attendance": -1,
                "services": [{"title": "Не состоялся"}],
            },
        ]

        with application_module.app.app_context():
            db = application_module.get_db()
            first = fuel_services.sync_yclients_records(
                db, records, {777: "8bc34a"}, dt.datetime(2026, 8, 18, 12, 0)
            )
            second = fuel_services.sync_yclients_records(
                db, records, {777: "8bc34a"}, dt.datetime(2026, 8, 18, 12, 5)
            )
            summary = fuel_services.fuel_summary(db, "Бодрый Первый")
            pending = summary["pending_trips"]
            group_transactions = db.execute(
                "SELECT COUNT(*) AS count FROM boat_fuel_transactions "
                "WHERE kind = 'group_consumption'"
            ).fetchone()["count"]
            trip_events = db.execute(
                "SELECT COUNT(*) AS count FROM boat_fuel_trip_events"
            ).fetchone()["count"]

            self.assertEqual(first["automatic"], 1)
            self.assertEqual(second["automatic"], 0)
            self.assertEqual(group_transactions, 1)
            self.assertEqual(trip_events, 2)
            self.assertEqual(summary["balance_liters"], 88)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["service_title"], "Индивидуальная аренда")
            event_id = pending[0]["id"]

        self.log_in_as_admin()
        response = self.client.post(
            f"/fleet/2/fuel/trips/{event_id}/consumption", data={"liters": "7,5"}
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            summary = fuel_services.fuel_summary(
                application_module.get_db(), "Бодрый Первый"
            )
            self.assertEqual(summary["balance_liters"], 80.5)
            self.assertEqual(summary["pending_trips"], [])

    def test_reserve_canisters_are_tracked_and_transferred_to_tank(self):
        success, message = self.activate_boat()
        self.assertTrue(success, message)
        with application_module.app.app_context():
            db = application_module.get_db()
            fuel_services.sync_yclients_records(
                db,
                [self.completed_group_record()],
                {777: "8bc34a"},
                dt.datetime(2026, 8, 18, 12, 0),
            )

        self.log_in_as_admin()
        reserve_response = self.client.post(
            "/fleet/2/fuel/refill",
            data={
                "fuel_operation": "reserve",
                "liters": "30",
                "occurred_at": "2026-08-18T10:30",
            },
        )
        transfer_response = self.client.post(
            "/fleet/2/fuel/refill",
            data={
                "fuel_operation": "reserve_to_tank",
                "liters": "10",
                "occurred_at": "2026-08-18T11:00",
            },
        )

        self.assertEqual(reserve_response.status_code, 302)
        self.assertEqual(transfer_response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            summary = fuel_services.fuel_summary(db, "Бодрый Первый")
            transfer = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_transfer'"
            ).fetchone()
            reserve_refill = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_refill'"
            ).fetchone()
            self.assertEqual(summary["balance_liters"], 98)
            self.assertEqual(summary["reserve_liters"], 20)
            self.assertEqual(summary["total_liters"], 118)
            self.assertEqual(transfer["liters_delta"], 10)
            self.assertEqual(transfer["reserve_delta"], -10)
            transfer_id = transfer["id"]
            removed, removal_message = fuel_services.delete_transaction(
                db,
                "Бодрый Первый",
                reserve_refill["id"],
                "Администратор теста",
            )
            self.assertFalse(removed)
            self.assertIn("резерв станет отрицательным", removal_message)

        with self.client.session_transaction() as session:
            session["team_id"] = 1
            session["team_employee_name"] = "Дмитрий Тарусов"
            session["team_username"] = "captain-test"
        captain_page = self.client.get("/team/?boat_index=2")
        captain_html = captain_page.get_data(as_text=True)
        self.assertEqual(captain_page.status_code, 200)
        self.assertIn("В резерве", captain_html)
        self.assertIn("20.0", captain_html)

        self.log_in_as_admin()
        deleted = self.client.post(
            f"/fleet/2/fuel/transactions/{transfer_id}/delete"
        )
        self.assertEqual(deleted.status_code, 302)
        with application_module.app.app_context():
            summary = fuel_services.fuel_summary(
                application_module.get_db(), "Бодрый Первый"
            )
            self.assertEqual(summary["balance_liters"], 88)
            self.assertEqual(summary["reserve_liters"], 30)

    def test_reserve_transfer_checks_available_reserve_and_tank_space(self):
        success, message = self.activate_boat()
        self.assertTrue(success, message)
        with application_module.app.app_context():
            db = application_module.get_db()
            success, message = fuel_services.record_refill(
                db,
                "Бодрый Первый",
                "20",
                "2026-08-18T09:00",
                False,
                "admin",
                "Администратор теста",
                "reserve",
            )
            self.assertTrue(success, message)
            success, message = fuel_services.record_refill(
                db,
                "Бодрый Первый",
                "10",
                "2026-08-18T10:00",
                False,
                "admin",
                "Администратор теста",
                "reserve_to_tank",
            )

            self.assertFalse(success)
            self.assertIn("в бак помещается", message)
            self.assertEqual(
                fuel_services.fuel_summary(db, "Бодрый Первый")["reserve_liters"],
                20,
            )

    def test_reserve_can_be_transferred_between_boats_and_deleted_as_pair(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            success, message = fuel_services.record_refill(
                db,
                "Бодрый Первый",
                "30",
                "2026-08-18T08:00",
                False,
                "admin",
                "Администратор теста",
                "reserve",
            )
            self.assertTrue(success, message)

        self.log_in_as_admin()
        response = self.client.post(
            "/fleet/2/fuel/refill",
            data={
                "fuel_operation": "reserve_to_boat",
                "destination_boat": "Ларус",
                "liters": "12,5",
                "occurred_at": "2026-08-18T09:00",
            },
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            source = fuel_services.fuel_summary(db, "Бодрый Первый")
            destination = fuel_services.fuel_summary(db, "Ларус")
            outgoing = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_boat_transfer_out'"
            ).fetchone()
            incoming = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_boat_transfer_in'"
            ).fetchone()
            self.assertEqual(source["reserve_liters"], 17.5)
            self.assertEqual(destination["reserve_liters"], 12.5)
            self.assertEqual(outgoing["boat"], "Бодрый Первый")
            self.assertEqual(outgoing["reserve_delta"], -12.5)
            self.assertEqual(incoming["boat"], "Ларус")
            self.assertEqual(incoming["reserve_delta"], 12.5)
            self.assertEqual(
                outgoing["source_ref"][:-4],
                incoming["source_ref"][:-3],
            )

        source_page = self.client.get("/fleet/2").get_data(as_text=True)
        destination_page = self.client.get("/fleet/0").get_data(as_text=True)
        self.assertIn("Передать резерв другому катеру", source_page)
        self.assertIn("Передано на катер «Ларус»", source_page)
        self.assertIn("Получено с катера «Бодрый Первый»", destination_page)

        deleted = self.client.post(
            f"/fleet/2/fuel/transactions/{outgoing['id']}/delete"
        )
        self.assertEqual(deleted.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                fuel_services.fuel_summary(db, "Бодрый Первый")["reserve_liters"],
                30,
            )
            self.assertEqual(
                fuel_services.fuel_summary(db, "Ларус")["reserve_liters"], 0
            )
            deleted_rows = db.execute(
                "SELECT COUNT(*) AS count FROM boat_fuel_transactions "
                "WHERE kind LIKE 'reserve_boat_transfer_%' AND deleted_at IS NOT NULL"
            ).fetchone()["count"]
            self.assertEqual(deleted_rows, 2)

    def test_cross_boat_reserve_transfer_validates_history_and_linked_deletion(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            fuel_services.record_refill(
                db, "Бодрый Первый", "10", "2026-08-18T08:00", False,
                "admin", "Администратор теста", "reserve",
            )
            success, message = fuel_services.transfer_reserve_between_boats(
                db,
                "Бодрый Первый",
                "Ларус",
                "8",
                "2026-08-18T10:00",
                "admin",
                "Администратор теста",
            )
            self.assertTrue(success, message)
            fuel_services.record_refill(
                db, "Бодрый Первый", "10", "2026-08-18T11:00", False,
                "admin", "Администратор теста", "reserve",
            )

            success, message = fuel_services.transfer_reserve_between_boats(
                db,
                "Бодрый Первый",
                "Бодрый Второй",
                "5",
                "2026-08-18T09:00",
                "admin",
                "Администратор теста",
            )
            self.assertFalse(success)
            self.assertIn("задним числом", message)
            self.assertEqual(
                fuel_services.fuel_summary(db, "Бодрый Второй")["reserve_liters"],
                0,
            )

            outgoing = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_boat_transfer_out' AND deleted_at IS NULL"
            ).fetchone()
            incoming = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'reserve_boat_transfer_in' AND deleted_at IS NULL"
            ).fetchone()
            fuel_repository.add_transaction(
                db,
                "Ларус",
                "reserve_transfer",
                6,
                6,
                "2026-08-18 10:30",
                "manual:test-used-transfer",
                "Перелив из резервных канистр",
                "admin",
                "Администратор теста",
                "2026-08-18 10:30",
                reserve_delta=-6,
            )
            db.commit()

            removed, message = fuel_services.delete_transaction(
                db,
                outgoing["boat"],
                outgoing["id"],
                "Администратор теста",
            )
            self.assertFalse(removed)
            self.assertIn("уже использован", message)
            self.assertIsNone(
                fuel_repository.get_transaction(db, outgoing["id"])["deleted_at"]
            )
            self.assertIsNone(
                fuel_repository.get_transaction(db, incoming["id"])["deleted_at"]
            )

    def test_captain_can_transfer_reserve_to_another_boat(self):
        with application_module.app.app_context():
            fuel_services.record_refill(
                application_module.get_db(),
                "Ларус",
                "20",
                "2026-08-18T08:00",
                False,
                "admin",
                "Администратор теста",
                "reserve",
            )
        with self.client.session_transaction() as session:
            session["team_id"] = 1
            session["team_employee_name"] = "Дмитрий Тарусов"
            session["team_username"] = "captain-test"

        response = self.client.post(
            "/team/fuel/refill",
            data={
                "boat_index": "0",
                "fuel_operation": "reserve_to_boat",
                "destination_boat": "Бодрый Второй",
                "liters": "7",
                "occurred_at": "2026-08-18T09:00",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                fuel_services.fuel_summary(db, "Ларус")["reserve_liters"], 13
            )
            self.assertEqual(
                fuel_services.fuel_summary(db, "Бодрый Второй")["reserve_liters"],
                7,
            )

    def test_red_activity_removes_previous_automatic_fuel_debit(self):
        success, _ = self.activate_boat()
        self.assertTrue(success)
        record = self.completed_group_record()

        with application_module.app.app_context():
            db = application_module.get_db()
            first = fuel_services.sync_yclients_records(
                db,
                [record],
                {777: "#8bc34a"},
                dt.datetime(2026, 8, 18, 12, 0),
            )
            cancelled = fuel_services.sync_yclients_records(
                db,
                [record],
                {777: "#f44336"},
                dt.datetime(2026, 8, 18, 12, 5),
            )
            balance_after_cancel = fuel_services.fuel_summary(
                db, "Бодрый Первый"
            )["balance_liters"]
            event_count = db.execute(
                "SELECT COUNT(*) AS count FROM boat_fuel_trip_events"
            ).fetchone()["count"]

            restored = fuel_services.sync_yclients_records(
                db,
                [record],
                {777: "#8bc34a"},
                dt.datetime(2026, 8, 18, 12, 10),
            )
            balance_after_restore = fuel_services.fuel_summary(
                db, "Бодрый Первый"
            )["balance_liters"]

        self.assertEqual(first["automatic"], 1)
        self.assertEqual(cancelled["cancelled"], 1)
        self.assertEqual(balance_after_cancel, 100)
        self.assertEqual(event_count, 0)
        self.assertEqual(restored["automatic"], 1)
        self.assertEqual(balance_after_restore, 88)

    def test_admin_can_remove_trip_from_ledger_without_sync_recreating_it(self):
        success, _ = self.activate_boat()
        self.assertTrue(success)
        record = self.completed_group_record()
        with application_module.app.app_context():
            db = application_module.get_db()
            stats = fuel_services.sync_yclients_records(
                db, [record], {777: "8bc34a"}, dt.datetime(2026, 8, 18, 12, 0)
            )
            transaction = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE kind = 'group_consumption'"
            ).fetchone()
            self.assertEqual(stats["automatic"], 1)
            self.assertEqual(
                fuel_services.fuel_summary(db, "Бодрый Первый")["balance_liters"],
                88,
            )
            transaction_id = transaction["id"]

        response = self.client.post(
            f"/fleet/2/fuel/transactions/{transaction_id}/delete"
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

        self.log_in_as_admin()
        page = self.client.get("/fleet/2")
        self.assertIn(
            f"/fleet/2/fuel/transactions/{transaction_id}/delete".encode(),
            page.data,
        )
        response = self.client.post(
            f"/fleet/2/fuel/transactions/{transaction_id}/delete"
        )
        self.assertEqual(response.status_code, 302)

        with application_module.app.app_context():
            db = application_module.get_db()
            deleted = fuel_repository.get_transaction(db, transaction_id)
            summary = fuel_services.fuel_summary(db, "Бодрый Первый")
            repeated = fuel_services.sync_yclients_records(
                db, [record], {777: "8bc34a"}, dt.datetime(2026, 8, 18, 12, 5)
            )
            active_group_rows = db.execute(
                "SELECT COUNT(*) AS count FROM boat_fuel_transactions "
                "WHERE kind = 'group_consumption' AND deleted_at IS NULL"
            ).fetchone()["count"]

            self.assertEqual(deleted["deleted_at"], "2026-08-18 12:00")
            self.assertEqual(deleted["deleted_by"], "Администратор теста")
            self.assertEqual(summary["balance_liters"], 100)
            self.assertEqual(len(summary["transactions"]), 1)
            self.assertEqual(repeated["automatic"], 0)
            self.assertEqual(active_group_rows, 0)

    def test_initial_calibration_can_only_be_removed_after_later_entries(self):
        success, _ = self.activate_boat()
        self.assertTrue(success)
        with application_module.app.app_context():
            db = application_module.get_db()
            initial = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE boat = 'Бодрый Первый' AND kind = 'calibration'"
            ).fetchone()
            removed, message = fuel_services.delete_transaction(
                db, "Бодрый Первый", initial["id"], "Администратор теста"
            )
            self.assertTrue(removed, message)
            self.assertFalse(
                fuel_services.fuel_summary(db, "Бодрый Первый")["activated"]
            )

            success, _ = fuel_services.record_refill(
                db,
                "Бодрый Первый",
                "80",
                "2026-08-18T08:30",
                True,
                "admin",
                "Администратор теста",
            )
            self.assertTrue(success)
            current_initial = db.execute(
                "SELECT * FROM boat_fuel_transactions "
                "WHERE boat = 'Бодрый Первый' AND deleted_at IS NULL"
            ).fetchone()
            fuel_services.sync_yclients_records(
                db,
                [self.completed_group_record()],
                {777: "8bc34a"},
                dt.datetime(2026, 8, 18, 12, 0),
            )
            removed, message = fuel_services.delete_transaction(
                db, "Бодрый Первый", current_initial["id"], "Администратор теста"
            )

            self.assertFalse(removed)
            self.assertIn("Сначала удалите более поздние записи", message)
            self.assertTrue(
                fuel_services.fuel_summary(db, "Бодрый Первый")["activated"]
            )

    def test_captain_can_record_refill_but_non_captain_cannot(self):
        with self.client.session_transaction() as session:
            session["team_id"] = 1
            session["team_employee_name"] = "Дмитрий Тарусов"
            session["team_username"] = "captain-test"
        response = self.client.post(
            "/team/fuel/refill",
            data={
                "boat_index": "0",
                "liters": "45",
                "occurred_at": "2026-08-18T08:00",
                "fill_to_full": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            summary = fuel_services.fuel_summary(application_module.get_db(), "Ларус")
            self.assertEqual(summary["balance_liters"], 60)
            self.assertEqual(summary["transactions"][0]["created_by_role"], "team")

        response = self.client.get("/team/?boat_index=0")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Топливо на борту", response.get_data(as_text=True))

        with self.client.session_transaction() as session:
            session["team_employee_name"] = "Эльмира Бектаева"
        response = self.client.post(
            "/team/fuel/refill",
            data={
                "boat_index": "1",
                "liters": "100",
                "occurred_at": "2026-08-18T09:00",
                "fill_to_full": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            summary = fuel_services.fuel_summary(
                application_module.get_db(), "Бодрый Второй"
            )
            self.assertFalse(summary["activated"])

    def test_fuel_cron_requires_secret_and_is_safe_to_trigger(self):
        original_secret = application_module.CRON_SECRET
        original_sync = application_module._sync_hourly_yclients
        application_module.CRON_SECRET = "fuel-secret"
        application_module._sync_hourly_yclients = lambda db: {
            "trips": {"imported": 3, "pending": 1},
            "fuel": {"automatic": 2, "pending": 1, "skipped": 0},
        }
        self.addCleanup(setattr, application_module, "CRON_SECRET", original_secret)
        self.addCleanup(
            setattr,
            application_module,
            "_sync_hourly_yclients",
            original_sync,
        )

        response = self.client.get("/internal/cron/sync-fuel?token=wrong")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/internal/cron/sync-fuel?token=fuel-secret")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"3 trips imported", response.data)
        self.assertIn(b"2 automatic", response.data)

    def test_sync_recovers_the_gap_after_an_interruption(self):
        requested_range = []
        original_records = application_module.yclients_get_records
        original_colors = application_module.yclients_get_activity_colors
        application_module.yclients_get_records = lambda start, end: (
            requested_range.append((start, end)) or []
        )
        application_module.yclients_get_activity_colors = lambda activity_ids: {}
        self.addCleanup(
            setattr, application_module, "yclients_get_records", original_records
        )
        self.addCleanup(
            setattr,
            application_module,
            "yclients_get_activity_colors",
            original_colors,
        )

        with application_module.app.app_context():
            db = application_module.get_db()
            success, _ = fuel_services.record_refill(
                db,
                "Бодрый Второй",
                "200",
                "2026-08-10T08:00",
                True,
                "admin",
                "Администратор теста",
            )
            self.assertTrue(success)
            db.execute(
                "UPDATE boat_fuel_state SET last_synced_at = ? WHERE boat = ?",
                ("2026-08-12 11:00", "Бодрый Второй"),
            )
            db.commit()
            application_module._sync_fuel_from_yclients(
                db, dt.datetime(2026, 8, 18, 12, 0)
            )

        self.assertEqual(requested_range, [("2026-08-11", "2026-08-18")])


if __name__ == "__main__":
    unittest.main()
