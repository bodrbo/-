import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import unittest

from modules.db_backup import services
from modules.db_backup import yandex_disk
from modules.db_backup.yandex_disk import DiskError, YandexDisk, configured_offsite
from support import application_module

TOKEN = "secret-oauth-token"


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class FakeHttp:
    """Plays the part of requests for the Яндекс.Диск REST API."""

    def __init__(self):
        self.calls = []
        self.files = {}        # remote path -> bytes
        self.folders = set()
        self.fail_with = None  # status code for every call

    def _record(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))

    def put(self, url, **kwargs):
        self._record("put", url, kwargs)
        if self.fail_with:
            return FakeResponse(self.fail_with)
        if url.startswith("https://upload.example/"):
            self.files[url[len("https://upload.example/"):]] = kwargs["data"].read()
            return FakeResponse(201)
        path = kwargs["params"]["path"]
        if path in self.folders:
            return FakeResponse(409)
        self.folders.add(path)
        return FakeResponse(201)

    def get(self, url, **kwargs):
        self._record("get", url, kwargs)
        if self.fail_with:
            return FakeResponse(self.fail_with)
        params = kwargs["params"]
        if url.endswith("/resources/upload"):
            return FakeResponse(200, {"href": "https://upload.example/" + params["path"]})
        folder = params["path"].rstrip("/") + "/"
        items = [{"name": path[len(folder):], "type": "file"} for path in self.files
                 if path.startswith(folder) and "/" not in path[len(folder):]]
        if not items and params["path"].rstrip("/") not in self.folders:
            return FakeResponse(404)
        return FakeResponse(200, {"_embedded": {"items": items}})

    def delete(self, url, **kwargs):
        self._record("delete", url, kwargs)
        if self.fail_with:
            return FakeResponse(self.fail_with)
        self.files.pop(kwargs["params"]["path"], None)
        return FakeResponse(204)


class OfflineHttp(FakeHttp):
    def get(self, url, **kwargs):
        raise ConnectionError("no network, token " + TOKEN)

    put = delete = get


class DiskCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db_path = os.path.join(self.root, "work.db")
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        self.dir = os.path.join(self.root, "backups")
        self.static = os.path.join(self.root, "static")
        os.makedirs(os.path.join(self.static, "work_photos"))
        os.makedirs(os.path.join(self.static, "boat_documents"))
        os.makedirs(os.path.join(self.static, "avatars"))
        self.http = FakeHttp()
        self.offsite = configured_offsite(TOKEN, self.static, self.http)

    def photo(self, folder, name, content=b"photo"):
        path = os.path.join(self.static, folder, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path


class YandexDiskClientTests(DiskCase):
    def test_nothing_is_configured_without_a_token(self):
        self.assertIsNone(configured_offsite("", self.static))
        self.assertIsNone(configured_offsite("   ", self.static))
        self.assertIsNone(configured_offsite(None, self.static))

    def test_an_upload_creates_the_folders_asks_for_an_address_and_sends_the_bytes(self):
        path = self.photo("work_photos", "a.jpg", b"jpeg-bytes")
        YandexDisk(TOKEN, self.http).upload(path, "static/work_photos/a.jpg")
        self.assertEqual(self.http.files["app:/static/work_photos/a.jpg"], b"jpeg-bytes")
        self.assertEqual(self.http.folders, {"app:/static", "app:/static/work_photos"})
        for _method, _url, kwargs in self.http.calls:
            if "headers" in kwargs:
                self.assertEqual(kwargs["headers"], {"Authorization": "OAuth " + TOKEN})
        # only the app folder is ever addressed
        for method, _url, kwargs in self.http.calls:
            if "params" in kwargs:
                self.assertTrue(kwargs["params"]["path"].startswith("app:/"))

    def test_problems_are_explained_and_never_contain_the_token(self):
        disk = YandexDisk(TOKEN, self.http)
        for status, words in ((401, "токен"), (403, "прав"), (507, "место"), (500, "500")):
            self.http.fail_with = status
            with self.assertRaises(DiskError) as caught:
                disk.list_names("db")
            self.assertIn(words, str(caught.exception))
        offline = YandexDisk(TOKEN, OfflineHttp())
        with self.assertRaises(DiskError) as caught:
            offline.list_names("db")
        self.assertNotIn(TOKEN, str(caught.exception))


class OffsiteSyncTests(DiskCase):
    NOW = dt.datetime(2026, 11, 7, 3, 0, 0)

    def run_backup(self, **kwargs):
        return services.run_backup(self.db_path, self.dir, self.NOW, offsite=self.offsite, **kwargs)

    def test_a_copy_and_the_photos_go_to_the_disk_and_are_not_sent_twice(self):
        self.photo("work_photos", "a.jpg")
        self.photo("boat_documents", "b.pdf")
        self.photo("avatars", "me.png")            # not one of the synced folders
        result = self.run_backup()
        self.assertTrue(result["ok"] and result["offsite"]["ok"])
        self.assertEqual(result["offsite"]["sent"], 3)
        self.assertEqual(
            sorted(self.http.files),
            sorted(["app:/db/" + result["name"], "app:/static/work_photos/a.jpg", "app:/static/boat_documents/b.pdf"]),
        )
        before = len(self.http.calls)
        self.assertIsNone(services.run_backup(self.db_path, self.dir, self.NOW + dt.timedelta(hours=1), offsite=self.offsite))
        self.assertEqual(len(self.http.calls), before)   # nothing due, nothing asked

    def test_a_new_or_changed_photo_is_sent_without_making_a_new_copy(self):
        self.run_backup()
        self.photo("work_photos", "new.jpg", b"1")
        later = self.NOW + dt.timedelta(hours=2)
        self.assertTrue(yandex_disk.offsite_due(self.dir, self.static, later))
        result = services.run_backup(self.db_path, self.dir, later, offsite=self.offsite)
        self.assertEqual(result["name"], "")                    # no new local copy
        self.assertEqual(len(services.list_backups(self.dir)), 1)
        self.assertEqual(result["offsite"]["sent"], 1)
        self.assertIn("app:/static/work_photos/new.jpg", self.http.files)

    def test_a_failed_send_is_reported_kept_for_later_and_retried_after_an_hour(self):
        self.http.fail_with = 401
        result = self.run_backup()
        self.assertTrue(result["ok"])                           # the local copy is fine
        self.assertFalse(result["offsite"]["ok"])
        status = yandex_disk.read_offsite_status(self.dir)
        self.assertFalse(status["ok"])
        self.assertIn("токен", status["message"])
        self.assertFalse(yandex_disk.offsite_due(self.dir, self.static, self.NOW + dt.timedelta(minutes=30)))
        self.assertTrue(yandex_disk.offsite_due(self.dir, self.static, self.NOW + dt.timedelta(minutes=90)))
        self.http.fail_with = None
        retry = services.run_backup(self.db_path, self.dir, self.NOW + dt.timedelta(minutes=90), offsite=self.offsite)
        self.assertTrue(retry["offsite"]["ok"])
        self.assertEqual(retry["offsite"]["sent"], 1)
        self.assertTrue(yandex_disk.read_offsite_status(self.dir)["ok"])

    def test_the_time_budget_stops_a_long_send_and_the_rest_follows_next_time(self):
        for number in range(5):
            self.photo("work_photos", "p%s.jpg" % number)
        services.create_backup(self.db_path, self.dir, self.NOW)
        first = yandex_disk.sync(self.offsite.disk, self.dir, self.static, 0, self.NOW)
        self.assertTrue(first["ok"])
        self.assertEqual(first["sent"], 0)
        self.assertIn("осталось", first["message"])
        second = yandex_disk.sync(self.offsite.disk, self.dir, self.static, 600, self.NOW, max_files=3)
        self.assertEqual(second["sent"], 3)
        third = yandex_disk.sync(self.offsite.disk, self.dir, self.static, 600, self.NOW)
        self.assertEqual(third["sent"], 3)                      # 6 files in all: 1 copy + 5 photos
        self.assertFalse(yandex_disk.offsite_due(self.dir, self.static, self.NOW))

    def test_old_copies_on_the_disk_follow_the_retention_rules(self):
        start = dt.datetime(2026, 11, 7, 3, 0, 0)
        for offset in range(0, 60):
            name = services.backup_name(start - dt.timedelta(days=offset))
            self.http.files["app:/db/" + name] = b"x"
        self.http.folders.add("app:/db")
        services.create_backup(self.db_path, self.dir, start + dt.timedelta(days=1))
        result = yandex_disk.sync(self.offsite.disk, self.dir, self.static, 600, start + dt.timedelta(days=1))
        self.assertTrue(result["ok"])
        self.assertIn("удалено старых копий", result["message"])
        left = [name[len("app:/db/"):] for name in self.http.files if name.startswith("app:/db/")]
        self.assertLess(len(left), 60)
        # the newest copies are all still there
        for offset in range(0, 10):
            self.assertIn(services.backup_name(start - dt.timedelta(days=offset)), left)

    def admin_client(self):
        client = application_module.app.test_client()
        with client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        return client

    def use_offsite(self, offsite):
        original = application_module.BACKUP_OFFSITE
        application_module.BACKUP_OFFSITE = offsite
        self.addCleanup(setattr, application_module, "BACKUP_OFFSITE", original)
        self.addCleanup(shutil.rmtree, application_module.BACKUP_DIR, True)
        shutil.rmtree(application_module.BACKUP_DIR, ignore_errors=True)

    def test_the_page_explains_how_to_connect_the_disk_when_there_is_no_token(self):
        self.use_offsite(None)
        page = self.admin_client().get("/settings/backups").get_data(as_text=True)
        self.assertIn("YANDEX_DISK_TOKEN", page)
        self.assertNotIn("Подключено:", page)

    def test_the_manual_run_sends_to_the_disk_and_the_page_shows_the_result(self):
        self.use_offsite(self.offsite)
        client = self.admin_client()
        page = client.post("/settings/backups/run", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Копия создана", page)
        self.assertIn("Подключено:", page)
        self.assertIn("Последняя отправка", page)
        self.assertNotIn("YANDEX_DISK_TOKEN", page)
        self.assertEqual([n for n in self.http.files if n.startswith("app:/db/workhours-")].__len__(), 1)
        self.assertNotIn(TOKEN, page)

    def test_a_disk_error_is_shown_on_the_page_but_the_local_copy_stays(self):
        self.http.fail_with = 401
        self.use_offsite(self.offsite)
        page = self.admin_client().post("/settings/backups/run", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Копия создана", page)
        self.assertIn("На Яндекс.Диск отправить не удалось", page)
        self.assertIn("токен", page)
        self.assertEqual(len(services.list_backups(application_module.BACKUP_DIR)), 1)
        self.assertNotIn(TOKEN, page)

    def test_the_cron_route_reports_a_disk_error(self):
        self.http.fail_with = 403
        self.use_offsite(self.offsite)
        original = application_module.CRON_SECRET
        application_module.CRON_SECRET = "cron-secret-disk"
        self.addCleanup(setattr, application_module, "CRON_SECRET", original)
        response = application_module.app.test_client().get("/internal/cron/backup?token=cron-secret-disk&force=1")
        self.assertEqual(response.status_code, 500)
        self.assertIn("yandex disk error", response.get_data(as_text=True))


class OfflineSyncTests(DiskCase):
    def test_no_network_is_a_reported_failure_not_a_crash(self):
        offsite = configured_offsite(TOKEN, self.static, OfflineHttp())
        result = services.run_backup(self.db_path, self.dir, dt.datetime(2026, 11, 7, 3, 0, 0), offsite=offsite)
        self.assertTrue(result["ok"])
        self.assertFalse(result["offsite"]["ok"])
        self.assertNotIn(TOKEN, result["offsite"]["message"])
        self.assertNotIn(TOKEN, open(os.path.join(self.dir, "offsite.json"), encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
