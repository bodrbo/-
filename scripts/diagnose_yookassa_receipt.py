#!/usr/bin/env python3
"""Read-only check of where a ЮKassa payment's fiscal receipt can be found.

For a payment id it prints what ЮKassa itself knows: the payment's
`receipt_registration` state and the receipts listed by
GET /receipts?payment_id=... (fiscal_document_number, fiscal_storage_number,
fiscal_attribute, registered_at, fiscal_provider_id) — the data the schedule
receipt PDF is built from. With no id it uses the latest succeeded paid link
of the schedule. Makes no writes.

Usage:
    python3 scripts/diagnose_yookassa_receipt.py [yookassa_payment_id]
"""

import json
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module  # noqa: E402


def main():
    payment_id = sys.argv[1] if len(sys.argv) > 1 else None
    with application_module.app.app_context():
        if payment_id is None:
            row = application_module.get_db().execute(
                "SELECT yookassa_payment_id FROM schedule_yookassa_payments "
                "WHERE status = 'succeeded' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("Нет оплаченных ссылок в расписании — передайте id платежа.")
                return 1
            payment_id = row["yookassa_payment_id"]
        request = application_module._yookassa_request
        payment = request("GET", f"/payments/{payment_id}")
        print(f"Платёж {payment_id}: status={payment.get('status')}, "
              f"receipt_registration={payment.get('receipt_registration')}, "
              f"amount={payment.get('amount')}")
        receipts = request("GET", "/receipts", params={"payment_id": payment_id})
        print("Чеки, найденные по payment_id:")
        print(json.dumps(receipts, ensure_ascii=False, indent=2))
        picked = application_module.schedule_services._pick_payment_receipt(
            receipts.get("items") or []
        )
        info = picked and application_module._fiscal_info_from_yookassa_receipt(
            picked, (payment.get("amount") or {}).get("value") or 0
        )
        print("QR-строка для чека:", info["qr"] if info else "— (фискальных данных ещё нет)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
