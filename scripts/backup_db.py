#!/usr/bin/env python3
"""Takes a checked, compressed copy of the database right now (over SSH or by hand).

    venv/bin/python scripts/backup_db.py            # the database next to this project
    venv/bin/python scripts/backup_db.py --list     # only show the copies

WORKHOURS_DB_PATH and BACKUP_DIR are honoured like in the application.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modules.db_backup import services  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list the copies and exit")
    args = parser.parse_args()

    db_path = os.environ.get("WORKHOURS_DB_PATH") or os.path.join(ROOT, "workhours.db")
    backup_dir = os.environ.get("BACKUP_DIR") or services.default_backup_dir(db_path)

    if args.list:
        for item in services.list_backups(backup_dir):
            print("%s  %6.1f MB  %s" % (item["moment"].strftime("%Y-%m-%d %H:%M"), item["size"] / 1048576.0, item["name"]))
        return 0

    result = services.run_backup(db_path, backup_dir, force=True)
    if result is None:
        print("Другая копия уже создаётся — повторите через минуту.")
        return 1
    if not result["ok"]:
        print("Ошибка: %s" % result["message"])
        return 1
    print("Готово: %s (%.1f МБ), папка %s" % (result["name"], result["size"] / 1048576.0, backup_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
