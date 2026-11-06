import datetime as dt
import gzip
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from modules.db_backup import services
from modules.db_backup.constants import KEEP_MONTHS, KEEP_RECENT, KEEP_WEEKS
from support import application_module


def make_database(path, rows=3):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, title TEXT)")
    connection.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT)")
    for number in range(rows):
        connection.execute("INSERT INTO orders (title) VALUES (?)", ("заказ %s" % number,))
    connection.commit()
    connection.close()


def touch_backup(directory, moment):
    path = os.path.join(directory, services.backup_name(moment))
    with open(path, "wb") as handle:
        handle.write(b"x")
    return os.path.basename(path)


class BackupTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db_path = os.path.join(self.root, "work.db")
        self.dir = os.path.join(self.root, "backups")
        make_database(self.db_path)


class CreateBackupTests(BackupTestCase):
    def test_a_copy_is_a_complete_gzipped_database_and_leaves_nothing_behind(self):
        moment = dt.datetime(2026, 11, 7, 3, 15, 0)
        made = services.create_backup(self.db_path, self.dir, moment)
        self.assertEqual(made["name"], "workhours-20261107-031500.db.gz")
        self.assertEqual(os.listdir(self.dir), [made["name"]])
        restored = os.path.join(self.root, "restored.db")
        with gzip.open(os.path.join(self.dir, made["name"]), "rb") as packed, open(restored, "wb") as plain:
            shutil.copyfileobj(packed, plain)
        connection = sqlite3.connect(restored)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 3)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        connection.close()

    def test_the_copy_and_its_folder_are_private(self):
        made = services.create_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        self.assertEqual(os.stat(os.path.join(self.dir, made["name"])).st_mode & 0o077, 0)
        self.assertEqual(os.stat(self.dir).st_mode & 0o077, 0)

    def test_a_missing_database_is_an_error_and_is_not_created(self):
        missing = os.path.join(self.root, "nope.db")
        with self.assertRaises(services.BackupError):
            services.create_backup(missing, self.dir)
        self.assertFalse(os.path.exists(missing))

    def test_a_file_that_is_not_a_database_is_an_error_and_keeps_no_copy(self):
        broken = os.path.join(self.root, "broken.db")
        with open(broken, "wb") as handle:
            handle.write(b"this is not sqlite" * 100)
        with self.assertRaises(services.BackupError):
            services.create_backup(broken, self.dir)
        self.assertEqual(services.list_backups(self.dir), [])
        self.assertEqual([n for n in os.listdir(self.dir)], [])

    def test_a_copy_taken_while_another_connection_writes_is_consistent(self):
        writer = sqlite3.connect(self.db_path)
        writer.execute("INSERT INTO orders (title) VALUES ('во время копии')")
        writer.commit()
        made = services.create_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        writer.close()
        restored = os.path.join(self.root, "r.db")
        with gzip.open(os.path.join(self.dir, made["name"]), "rb") as packed, open(restored, "wb") as plain:
            shutil.copyfileobj(packed, plain)
        self.assertEqual(sqlite3.connect(restored).execute("SELECT COUNT(*) FROM orders").fetchone()[0], 4)


class RetentionTests(BackupTestCase):
    def test_recent_weekly_and_monthly_copies_are_kept_and_the_rest_removed(self):
        os.makedirs(self.dir)
        start = dt.datetime(2026, 11, 7, 3, 0, 0)
        names = [touch_backup(self.dir, start - dt.timedelta(days=offset)) for offset in range(0, 400)]
        foreign = os.path.join(self.dir, "notes.txt")
        with open(foreign, "w") as handle:
            handle.write("keep me")
        removed = services.prune(self.dir)
        left = services.list_backups(self.dir)
        self.assertEqual(removed, len(names) - len(left))
        self.assertTrue(os.path.exists(foreign))
        kept = set(item["name"] for item in left)
        self.assertEqual(kept, services.names_to_keep(services.list_backups(self.dir)) | kept)
        # the newest KEEP_RECENT copies are all there, the newest of all included
        for offset in range(KEEP_RECENT):
            self.assertIn(services.backup_name(start - dt.timedelta(days=offset)), kept)
        # one a week for KEEP_WEEKS weeks and one a month for KEEP_MONTHS months, not more
        self.assertLessEqual(len(left), KEEP_RECENT + KEEP_WEEKS + KEEP_MONTHS)
        self.assertGreater(len(left), KEEP_RECENT)
        # a copy from 400 days ago is gone
        self.assertNotIn(services.backup_name(start - dt.timedelta(days=399)), kept)

    def test_the_only_copy_is_never_removed(self):
        os.makedirs(self.dir)
        name = touch_backup(self.dir, dt.datetime(2020, 1, 1, 3, 0, 0))
        self.assertEqual(services.prune(self.dir), 0)
        self.assertEqual([item["name"] for item in services.list_backups(self.dir)], [name])


class ScheduleTests(BackupTestCase):
    def test_a_first_copy_is_due_at_once(self):
        self.assertTrue(services.is_due(self.dir, dt.datetime(2026, 11, 7, 14, 0, 0)))

    def test_after_a_copy_the_next_waits_for_the_night(self):
        os.makedirs(self.dir)
        touch_backup(self.dir, dt.datetime(2026, 11, 7, 3, 10, 0))
        self.assertFalse(services.is_due(self.dir, dt.datetime(2026, 11, 7, 15, 0, 0)))
        self.assertFalse(services.is_due(self.dir, dt.datetime(2026, 11, 8, 1, 0, 0)))   # night has not come yet
        self.assertFalse(services.is_due(self.dir, dt.datetime(2026, 11, 8, 6, 0, 0)))   # 26h50m, but not night yet
        self.assertTrue(services.is_due(self.dir, dt.datetime(2026, 11, 8, 3, 0, 0)))    # 23h50m, night

    def test_an_overdue_copy_is_made_at_any_hour(self):
        os.makedirs(self.dir)
        touch_backup(self.dir, dt.datetime(2026, 11, 7, 3, 10, 0))
        self.assertTrue(services.is_due(self.dir, dt.datetime(2026, 11, 8, 14, 0, 0)))   # 35h

    def test_run_backup_without_force_does_nothing_when_not_due(self):
        first = services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        self.assertTrue(first["ok"])
        self.assertIsNone(services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 30, 0)))
        self.assertEqual(len(services.list_backups(self.dir)), 1)
        forced = services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 31, 0), force=True)
        self.assertTrue(forced["ok"])
        self.assertEqual(len(services.list_backups(self.dir)), 2)

    def test_the_status_records_the_last_attempt(self):
        services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        status = services.read_status(self.dir)
        self.assertTrue(status["ok"])
        self.assertEqual(status["at"], "2026-11-07 03:00:00")
        self.assertEqual(status["name"], "workhours-20261107-030000.db.gz")

    def test_a_failure_is_recorded_and_retried_only_after_a_while(self):
        missing = os.path.join(self.root, "gone.db")
        result = services.run_backup(missing, self.dir, dt.datetime(2026, 11, 7, 14, 0, 0))
        self.assertFalse(result["ok"])
        self.assertFalse(services.read_status(self.dir)["ok"])
        self.assertIsNone(services.run_backup(missing, self.dir, dt.datetime(2026, 11, 7, 14, 20, 0)))
        self.assertTrue(services.is_due(self.dir, dt.datetime(2026, 11, 7, 15, 30, 0)))

    def test_a_run_in_progress_blocks_another_and_a_dead_one_does_not(self):
        os.makedirs(self.dir)
        lock = os.path.join(self.dir, ".lock")
        with open(lock, "w"):
            pass
        self.assertIsNone(services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0)))
        old = time.time() - 3 * 3600
        os.utime(lock, (old, old))
        result = services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(lock))

    def test_leftovers_of_a_cut_short_run_are_cleaned_up(self):
        os.makedirs(self.dir)
        leftover = os.path.join(self.dir, ".work-20261101-030000.db")
        with open(leftover, "wb") as handle:
            handle.write(b"half")
        old = time.time() - 3 * 3600
        os.utime(leftover, (old, old))
        services.create_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0))
        self.assertFalse(os.path.exists(leftover))

    def test_the_background_start_checks_rarely_and_starts_only_a_due_run(self):
        services._check_state.update(checked_at=0.0, thread=None)
        self.assertTrue(services.maybe_start_background(self.db_path, self.dir))
        services._check_state["thread"].join(30)
        self.assertEqual(len(services.list_backups(self.dir)), 1)
        # too soon to look again, and nothing is due anyway
        self.assertFalse(services.maybe_start_background(self.db_path, self.dir))
        services._check_state["checked_at"] = 0.0
        self.assertFalse(services.maybe_start_background(self.db_path, self.dir))
        self.assertEqual(len(services.list_backups(self.dir)), 1)


class BackupPageTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self.backup_dir = application_module.BACKUP_DIR
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.backup_dir, True)

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def test_the_page_needs_an_admin(self):
        for path in ("/settings/backups", "/settings/backups/workhours-20261107-030000.db.gz"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("login", response.headers["Location"])
        self.assertEqual(self.client.post("/settings/backups/run").status_code, 302)
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_the_page_warns_when_there_is_no_copy_then_makes_one_and_lets_it_download(self):
        self.login()
        page = self.client.get("/settings/backups").get_data(as_text=True)
        self.assertIn("Свежей копии нет", page)
        self.assertIn("Резервные копии", page)

        response = self.client.post("/settings/backups/run", follow_redirects=True)
        page = response.get_data(as_text=True)
        self.assertIn("Копия создана", page)
        self.assertNotIn("Свежей копии нет", page)
        backups = services.list_backups(self.backup_dir)
        self.assertEqual(len(backups), 1)
        name = backups[0]["name"]
        self.assertIn(f'href="/settings/backups/{name}"', page)

        download = self.client.get(f"/settings/backups/{name}")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        body = download.get_data()
        download.close()
        self.assertEqual(body[:2], b"\x1f\x8b")   # gzip

    def test_sizes_are_shown_in_kilobytes_when_small(self):
        from modules.db_backup.routes import _size_text
        self.assertEqual(_size_text(10), "1 КБ")
        self.assertEqual(_size_text(300 * 1024), "300 КБ")
        self.assertEqual(_size_text(5869009), "5.6 МБ")

    def test_only_listed_copies_can_be_downloaded(self):
        self.login()
        for name in ("workhours-20261107-030000.db.gz", "../app.py", "status.json", "x"):
            self.assertEqual(self.client.get(f"/settings/backups/{name}").status_code, 404, name)

    def test_the_settings_pages_link_to_the_backups(self):
        self.login()
        self.assertIn('href="/settings/backups"', self.client.get("/settings").get_data(as_text=True))
        self.assertIn('href="/settings/backups"', self.client.get("/settings/software-requests").get_data(as_text=True))

    def test_the_cron_route_is_protected_and_only_copies_when_due(self):
        original = application_module.CRON_SECRET
        application_module.CRON_SECRET = "cron-secret-for-backups"
        self.addCleanup(setattr, application_module, "CRON_SECRET", original)
        self.assertEqual(self.client.get("/internal/cron/backup").status_code, 403)
        self.assertEqual(self.client.get("/internal/cron/backup?token=wrong").status_code, 403)
        first = self.client.get("/internal/cron/backup?token=cron-secret-for-backups")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_data(as_text=True).startswith("ok: workhours-"))
        again = self.client.get("/internal/cron/backup?token=cron-secret-for-backups")
        self.assertEqual(again.get_data(as_text=True), "ok: nothing to do")
        forced = self.client.get("/internal/cron/backup?token=cron-secret-for-backups&force=1")
        self.assertTrue(forced.get_data(as_text=True).startswith("ok: workhours-"))

    def test_the_cron_route_is_off_without_a_secret(self):
        original = application_module.CRON_SECRET
        application_module.CRON_SECRET = None
        self.addCleanup(setattr, application_module, "CRON_SECRET", original)
        self.assertEqual(self.client.get("/internal/cron/backup?token=").status_code, 403)

    def test_requests_do_not_start_backups_under_test(self):
        services._check_state.update(checked_at=0.0, thread=None)
        self.client.get("/login")
        self.assertIsNone(services._check_state["thread"])
        self.assertFalse(os.path.exists(self.backup_dir))


if __name__ == "__main__":
    unittest.main()
