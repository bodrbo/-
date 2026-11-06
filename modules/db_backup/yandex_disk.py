"""Copies of the backups (and of the uploaded photos) on the owner's Яндекс.Диск.

Only the "app folder" of the OAuth application is ever touched (paths start
with "app:/"), even if the token would allow more. The token is read from the
YANDEX_DISK_TOKEN environment variable and never shown or logged.
"""

import datetime as dt
import json
import os
import socket
import time

from . import services
from .constants import OFFSITE_FILE, OFFSITE_RETRY_MINUTES, PHOTO_FOLDERS

API = "https://cloud-api.yandex.net/v1/disk"
ROOT = "app:/"
REQUEST_TIMEOUT = 30
UPLOAD_TIMEOUT = 600

PROBLEMS = {
    401: "токен Яндекс.Диска недействителен или истёк — выпустите новый и обновите YANDEX_DISK_TOKEN",
    403: "у токена нет прав на запись в папку приложения на Яндекс.Диске",
    507: "на Яндекс.Диске закончилось место",
}


class DiskError(Exception):
    pass


class YandexDisk:
    def __init__(self, token, http=None):
        if http is None:
            import requests
            http = requests
        self.http = http
        self.token = token
        self.headers = {"Authorization": "OAuth " + token}

    def describe(self, error):
        """The kind of error and its text (shortened, without the token)."""
        text = str(error).replace(self.token, "***")
        if len(text) > 200:
            text = text[:200] + "…"
        return "%s: %s" % (error.__class__.__name__, text) if text else error.__class__.__name__

    def _call(self, method, url, expected, **kwargs):
        try:
            response = getattr(self.http, method)(
                url, headers=self.headers, timeout=kwargs.pop("timeout", REQUEST_TIMEOUT), **kwargs
            )
        except Exception as error:
            raise DiskError("Яндекс.Диск недоступен (%s)" % self.describe(error))
        if response.status_code in expected:
            return response
        raise DiskError(PROBLEMS.get(response.status_code, "Яндекс.Диск ответил ошибкой %s" % response.status_code))

    def _make_folders(self, remote):
        parts = remote.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            # 201 created, 409 already there
            self._call("put", API + "/resources", (201, 409), params={"path": ROOT + "/".join(parts[:index])})

    def upload(self, local_path, remote):
        """remote is relative to the app folder, e.g. 'db/workhours-….db.gz'."""
        self._make_folders(remote)
        target = self._call(
            "get", API + "/resources/upload", (200,), params={"path": ROOT + remote, "overwrite": "true"}
        ).json().get("href")
        if not target:
            raise DiskError("Яндекс.Диск не дал адрес для загрузки")
        try:
            with open(local_path, "rb") as handle:
                response = self.http.put(target, data=handle, timeout=UPLOAD_TIMEOUT)
        except OSError as error:
            raise DiskError("не удалось прочитать файл: %s" % self.describe(error))
        except Exception as error:
            raise DiskError("загрузка на Яндекс.Диск прервалась (%s)" % self.describe(error))
        if response.status_code not in (201, 202):
            raise DiskError(PROBLEMS.get(response.status_code, "загрузка не удалась (%s)" % response.status_code))

    def list_names(self, folder):
        response = self._call(
            "get", API + "/resources", (200, 404),
            params={"path": ROOT + folder, "limit": 1000, "fields": "_embedded.items.name,_embedded.items.type"},
        )
        if response.status_code == 404:
            return []
        items = (response.json().get("_embedded") or {}).get("items") or []
        return [item["name"] for item in items if item.get("type") == "file"]

    def delete(self, remote):
        self._call("delete", API + "/resources", (202, 204, 404), params={"path": ROOT + remote, "permanently": "true"})


# ---- what is already on the Disk

def _read_state(backup_dir):
    try:
        with open(os.path.join(backup_dir, OFFSITE_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("files", {})
    return data


def _write_state(backup_dir, state):
    path = os.path.join(backup_dir, OFFSITE_FILE)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def read_offsite_status(backup_dir):
    """What the settings page shows: {'at', 'ok', 'message', 'files'} or None."""
    state = _read_state(backup_dir)
    if "at" not in state:
        return None
    return {"at": state["at"], "ok": state.get("ok", False), "message": state.get("message", ""),
            "files": len(state["files"])}


def _photo_files(static_dir):
    """[(key, path, signature)] of the uploaded photos and documents."""
    found = []
    for folder in PHOTO_FOLDERS:
        base = os.path.join(static_dir, folder)
        for current, _dirs, names in os.walk(base):
            for name in sorted(names):
                if name.startswith("."):
                    continue
                path = os.path.join(current, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                key = "static/" + os.path.relpath(path, static_dir).replace(os.sep, "/")
                found.append((key, path, [info.st_size, int(info.st_mtime)]))
    return found


def _pending(state, backup_dir, static_dir):
    pending = []
    for item in reversed(services.list_backups(backup_dir)):   # oldest first
        key = "db/" + item["name"]
        if key not in state["files"]:
            pending.append((key, os.path.join(backup_dir, item["name"]), [item["size"], 0]))
    for key, path, signature in _photo_files(static_dir):
        if state["files"].get(key) != signature:
            pending.append((key, path, signature))
    return pending


def offsite_due(backup_dir, static_dir, now=None):
    """Something is not on the Disk yet (and a failure is not retried at once)."""
    now = now or dt.datetime.now()
    state = _read_state(backup_dir)
    if state.get("ok") is False and state.get("at"):
        try:
            last = dt.datetime.strptime(state["at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            last = None
        if last and now - last < dt.timedelta(minutes=OFFSITE_RETRY_MINUTES):
            return False
    return bool(_pending(state, backup_dir, static_dir))


def sync(disk, backup_dir, static_dir, budget_seconds, now=None, max_files=None):
    """Sends what is missing (database copies first, then photos) until the time
    budget is used up, then applies the retention rules to the copies on the Disk.
    Returns {'ok', 'message', 'sent'}."""
    now = now or dt.datetime.now()
    state = _read_state(backup_dir)
    started = time.time()
    sent = 0
    left = 0
    try:
        queue = _pending(state, backup_dir, static_dir)
        for index, (key, path, signature) in enumerate(queue):
            if time.time() - started > budget_seconds or (max_files is not None and sent >= max_files):
                left = len(queue) - index
                break
            disk.upload(path, key)
            state["files"][key] = signature
            sent += 1
        removed = _prune_remote(disk, backup_dir, state)
    except DiskError as error:
        state.update(at=now.strftime("%Y-%m-%d %H:%M:%S"), ok=False, message=str(error))
        _write_state(backup_dir, state)
        return {"ok": False, "message": str(error), "sent": sent}
    message = "отправлено файлов: %s" % sent
    if left:
        message += ", осталось: %s (продолжится автоматически)" % left
    if removed:
        message += ", удалено старых копий: %s" % removed
    state.update(at=now.strftime("%Y-%m-%d %H:%M:%S"), ok=True, message=message)
    _write_state(backup_dir, state)
    return {"ok": True, "message": message, "sent": sent}


def _prune_remote(disk, backup_dir, state):
    """The copies on the Disk follow the same retention rules as the local ones."""
    listed = []
    for name in disk.list_names("db"):
        moment = services.parse_name(name)
        if moment is not None:
            listed.append({"name": name, "moment": moment})
    listed.sort(key=lambda item: item["moment"], reverse=True)
    keep = services.names_to_keep(listed)
    removed = 0
    for item in listed:
        if item["name"] not in keep:
            disk.delete("db/" + item["name"])
            state["files"].pop("db/" + item["name"], None)
            removed += 1
    return removed


def diagnose(disk, resolver=None):
    """A connection check made from the web process itself, step by step (the
    server that serves the site is not always the one you reach over SSH).
    Uploads a 5-byte file to <app folder>/_diagnostic/ and deletes it again.
    Returns [{'ok', 'title', 'detail'}]."""
    resolver = resolver or socket.getaddrinfo
    results = []

    def step(title, action):
        try:
            detail = action() or ""
            results.append({"ok": True, "title": title, "detail": detail})
            return True
        except DiskError as error:
            results.append({"ok": False, "title": title, "detail": str(error)})
        except Exception as error:
            results.append({"ok": False, "title": title, "detail": disk.describe(error)})
        return False

    def resolve(host):
        return lambda: ", ".join(sorted({item[4][0] for item in resolver(host, 443)}))

    holder = {}

    def info():
        return "ответ %s" % disk._call("get", API, (200,)).status_code

    def upload_address():
        params = {"path": ROOT + "_diagnostic/test.txt", "overwrite": "true"}
        disk._make_folders("_diagnostic/test.txt")
        response = disk._call("get", API + "/resources/upload", (200,), params=params)
        holder["href"] = response.json().get("href")
        if not holder["href"]:
            raise DiskError("Яндекс.Диск не дал адрес для загрузки")
        holder["host"] = holder["href"].split("/")[2]
        return "сервер загрузки %s" % holder["host"]

    def put_file():
        try:
            response = disk.http.put(holder["href"], data=b"hello", timeout=60)
        except Exception as error:
            raise DiskError(disk.describe(error))
        if response.status_code not in (201, 202):
            raise DiskError("ответ %s" % response.status_code)
        return "ответ %s" % response.status_code

    def cleanup():
        return "ответ %s" % disk._call(
            "delete", API + "/resources", (202, 204, 404),
            params={"path": ROOT + "_diagnostic", "permanently": "true"},
        ).status_code

    step("Адрес cloud-api.yandex.net находится", resolve("cloud-api.yandex.net"))
    if not step("Запрос к API Диска с вашим токеном", info):
        return results
    if not step("Получение адреса для загрузки", upload_address):
        return results
    step("Адрес сервера загрузки находится", resolve(holder["host"]))
    step("Загрузка тестового файла", put_file)
    step("Удаление тестовой папки", cleanup)
    return results


class Offsite:
    """What services.run_backup needs: is something waiting, and send it."""

    def __init__(self, disk, static_dir):
        self.disk = disk
        self.static_dir = static_dir

    def due(self, backup_dir, now=None):
        return offsite_due(backup_dir, self.static_dir, now)

    def sync(self, backup_dir, budget_seconds, now=None):
        return sync(self.disk, backup_dir, self.static_dir, budget_seconds, now)

    def diagnose(self):
        return diagnose(self.disk)


def configured_offsite(token, static_dir, http=None):
    """None until a token is set (YANDEX_DISK_TOKEN)."""
    token = (token or "").strip()
    if not token:
        return None
    return Offsite(YandexDisk(token, http), static_dir)
