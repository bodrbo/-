#!/usr/bin/env python3
"""Import the full YCLIENTS directory into excursion clients."""

import datetime as dt
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module
from modules.clients.yclients import sync_clients


def main():
    if not application_module.yclients_configured():
        raise SystemExit(
            "YCLIENTS_PARTNER_TOKEN, YCLIENTS_USER_TOKEN и "
            "YCLIENTS_COMPANY_ID должны быть заданы в .env."
        )
    with application_module.app.app_context():
        stats = sync_clients(
            application_module.get_db(),
            dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            application_module.YCLIENTS_API_BASE,
            application_module.YCLIENTS_COMPANY_ID,
            application_module.YCLIENTS_PARTNER_TOKEN,
            application_module.YCLIENTS_USER_TOKEN,
        )
    print(
        "Импорт завершён: "
        f"получено {stats['received']}, создано {stats['created']}, "
        f"обновлено {stats['updated']}, связано {stats['linked']}, "
        f"пропущено {stats['skipped']}."
    )


if __name__ == "__main__":
    main()
