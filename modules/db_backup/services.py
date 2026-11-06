"""Checked, compressed copies of the SQLite database and their retention.

A copy is taken with SQLite's own backup API, so it is consistent even while
the site is writing; it is checked (integrity_check, same tables as the
source) before it is kept, and only then compressed under its final name.
Nothing here imports the application: the same functions serve the web page,
the cron route, the background trigger and scripts/backup_db.py.
"""

import datetime as dt
import gzip
import json
import os
import re
import shutil
import sqlite3
import threading
import time

from .constants import (
    BACKUP_PREFIX, BACKUP_SUFFIX, CHECK_EVERY_SECONDS, DUE_HOURS, KEEP_MONTHS,
    KEEP_RECENT, KEEP_WEEKS, LOCK_FILE, NIGHT_HOURS, OVERDUE_HOURS,
    RETRY_AFTER_FAILURE_MINUTES, STALE_LOCK_MINUTES, STATUS_FILE,
)

NAME_PATTERN = re.compile(
    "^" + re.escape(BACKUP_PREFIX) + r"(\d{8}-\d{6})" + re.escape(BACKUP_SUFFIX) + "$"
)
STAMP_FORMAT = "%Y%m%d-%H%M%S"
TEMP_PREFIX = ".work-"
PARTIAL_SUFFIX = ".partial"


class BackupError(Exception):
    pass


def default_backup_dir(db_path):
    """Next to the database, outside the static/ folder that the site serves."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")


def backup_name(moment):
    return BACKUP_PREFIX + moment.strftime(STAMP_FORMAT) + BACKUP_SUFFIX


def parse_name(name):
    match = NAME_PATTERN.match(name or "")
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), STAMP_FORMAT)
    except ValueError:
        return None


def list_backups(backup_dir):
    """Existing copies, newest first: [{'name', 'moment', 'size'}]."""
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return []
    found = []
    for name in names:
        moment = parse_name(name)
        if moment is None:
            continue
        try:
            size = os.path.getsize(os.path.join(backup_dir, name))
        except OSError:
            continue
        found.append({"name": name, "moment": moment, "size": size})
    found.sort(key=lambda item: item["moment"], reverse=True)
    return found


# ---- status of the last attempt

def read_status(backup_dir):
    try:
        with open(os.path.join(backup_dir, STATUS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_status(backup_dir, moment, ok, message, name=""):
    path = os.path.join(backup_dir, STATUS_FILE)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"at": moment.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "message": message, "name": name}, handle)
        os.replace(temporary, path)
    except OSError:
        pass


def _status_moment(status):
    try:
        return dt.datetime.strptime(status["at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return None


# ---- making a copy

def _table_count(connection):
    return connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]


def _copy_database(source, target_path):
    if hasattr(source, "backup"):
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
        return
    # old Python: hold the write lock while the file is copied (the journal
    # is not in WAL mode, so the file on disk is complete between commits)
    source.execute("BEGIN IMMEDIATE")
    try:
        shutil.copyfile(source_path_of(source), target_path)
    finally:
        source.rollback()


def source_path_of(connection):
    return connection.execute("PRAGMA database_list").fetchone()[2]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _clean_leftovers(backup_dir):
    """Half-made files of a run that was cut short (the web process was
    restarted in the middle)."""
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return
    for name in names:
        if not (name.startswith(TEMP_PREFIX) or name.endswith(PARTIAL_SUFFIX)):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if time.time() - os.path.getmtime(path) > 3600:
                os.remove(path)
        except OSError:
            pass


def create_backup(db_path, backup_dir, now=None):
    """Takes, checks and stores one copy. Raises BackupError."""
    now = now or dt.datetime.now()
    if not os.path.isfile(db_path):
        raise BackupError("файл базы данных не найден")
    os.makedirs(backup_dir, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o700)
    except OSError:
        pass
    _clean_leftovers(backup_dir)

    name = backup_name(now)
    final = os.path.join(backup_dir, name)
    work = os.path.join(backup_dir, TEMP_PREFIX + now.strftime(STAMP_FORMAT) + ".db")
    partial = final + PARTIAL_SUFFIX
    try:
        source = sqlite3.connect(db_path, timeout=60)
        try:
            _copy_database(source, work)
            source_tables = _table_count(source)
        except sqlite3.Error as error:
            raise BackupError("не удалось скопировать базу: %s" % error)
        finally:
            source.close()

        copy = sqlite3.connect(work)
        try:
            verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
            copy_tables = _table_count(copy)
        except sqlite3.Error as error:
            raise BackupError("копия не открывается: %s" % error)
        finally:
            copy.close()
        if verdict != "ok":
            raise BackupError("копия не прошла проверку целостности: %s" % verdict)
        if copy_tables != source_tables:
            raise BackupError("в копии %s таблиц вместо %s" % (copy_tables, source_tables))

        with open(work, "rb") as plain, gzip.open(partial, "wb", compresslevel=6) as packed:
            shutil.copyfileobj(plain, packed)
        try:
            os.chmod(partial, 0o600)
        except OSError:
            pass
        os.replace(partial, final)
    except OSError as error:
        raise BackupError("не удалось сохранить копию: %s" % error)
    finally:
        _remove(work)
        _remove(partial)
    return {"name": name, "size": os.path.getsize(final), "tables": source_tables}


# ---- retention

def names_to_keep(backups):
    """Names to keep out of list_backups(): the newest KEEP_RECENT copies, the
    newest copy of each of the last KEEP_WEEKS weeks and KEEP_MONTHS months."""
    keep = set(item["name"] for item in backups[:KEEP_RECENT])
    weeks, months = {}, {}
    for item in backups:  # newest first, so the first one seen is the newest of its week/month
        moment = item["moment"]
        weeks.setdefault(tuple(moment.isocalendar()[:2]), item["name"])
        months.setdefault((moment.year, moment.month), item["name"])
    for bucket, limit in ((weeks, KEEP_WEEKS), (months, KEEP_MONTHS)):
        for key in sorted(bucket, reverse=True)[:limit]:
            keep.add(bucket[key])
    return keep


def prune(backup_dir):
    """Deletes copies outside the retention rules; returns how many."""
    backups = list_backups(backup_dir)
    keep = names_to_keep(backups)
    removed = 0
    for item in backups:
        if item["name"] not in keep:
            _remove(os.path.join(backup_dir, item["name"]))
            removed += 1
    return removed


# ---- schedule

def is_due(backup_dir, now=None):
    now = now or dt.datetime.now()
    status = read_status(backup_dir)
    if status and not status.get("ok"):
        last_attempt = _status_moment(status)
        if last_attempt and now - last_attempt < dt.timedelta(minutes=RETRY_AFTER_FAILURE_MINUTES):
            return False
    backups = list_backups(backup_dir)
    if not backups:
        return True
    age = now - backups[0]["moment"]
    if age >= dt.timedelta(hours=OVERDUE_HOURS):
        return True
    return age >= dt.timedelta(hours=DUE_HOURS) and now.hour in NIGHT_HOURS


def _acquire_lock(backup_dir):
    path = os.path.join(backup_dir, LOCK_FILE)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                continue
            if age < STALE_LOCK_MINUTES * 60:
                return False
            _remove(path)
        except OSError:
            return False
    return False


def run_backup(db_path, backup_dir, now=None, force=False):
    """One run: a copy, then retention. Without force it does nothing unless a
    copy is due. Returns {'ok', 'message', 'name'} or None when nothing was done
    (not due, or another run is in progress)."""
    now = now or dt.datetime.now()
    os.makedirs(backup_dir, exist_ok=True)
    if not force and not is_due(backup_dir, now):
        return None
    if not _acquire_lock(backup_dir):
        return None
    try:
        try:
            made = create_backup(db_path, backup_dir, now)
        except BackupError as error:
            _write_status(backup_dir, now, False, str(error))
            return {"ok": False, "message": str(error), "name": ""}
        removed = prune(backup_dir)
        message = "копия сохранена"
        if removed:
            message += ", удалено старых: %s" % removed
        _write_status(backup_dir, now, True, message, made["name"])
        return {"ok": True, "message": message, "name": made["name"], "size": made["size"]}
    finally:
        _remove(os.path.join(backup_dir, LOCK_FILE))


# ---- the web process starts a due run in the background

_check_state = {"checked_at": 0.0, "thread": None}
_check_guard = threading.Lock()


def maybe_start_background(db_path, backup_dir):
    """Cheap to call on every request: at most every CHECK_EVERY_SECONDS it
    looks at the schedule, and a due run goes to a daemon thread. Returns True
    when a run was started."""
    with _check_guard:
        moment = time.time()
        if moment - _check_state["checked_at"] < CHECK_EVERY_SECONDS:
            return False
        _check_state["checked_at"] = moment
        running = _check_state["thread"]
        if running is not None and running.is_alive():
            return False
        try:
            if not is_due(backup_dir):
                return False
        except OSError:
            return False
        worker = threading.Thread(target=_background_run, args=(db_path, backup_dir), daemon=True)
        _check_state["thread"] = worker
        worker.start()
        return True


def _background_run(db_path, backup_dir):
    try:
        run_backup(db_path, backup_dir)
    except Exception:  # a backup problem must never reach a visitor
        pass
