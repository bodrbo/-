#!/usr/bin/env python3
"""Creates ONE test ЮKassa invoice (1 RUB, valid 1 day, nothing is charged
unless somebody pays it) and prints the API's answer — to see whether invoices
are enabled for the shop and how the request/response look. The schedule's
payment links use the same request shape (see
modules.schedule.services.create_participant_payment).

Usage:
    python3 scripts/diagnose_yookassa_invoice.py [full|cart-fiscal|minimal]

  full         payment_data.receipt with vat_code etc. (what the app sends)
  cart-fiscal  fiscal fields on the cart item instead
  minimal      no receipt data at all
"""

import datetime as dt
import json
import os
import secrets
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
    variant = sys.argv[1] if len(sys.argv) > 1 else "full"
    with application_module.app.app_context():
        vat = application_module._current_yookassa_excursion_vat_code(application_module.get_db())
    amount = {"value": "1.00", "currency": "RUB"}
    fiscal = {"vat_code": vat, "payment_subject": "service", "payment_mode": "full_payment"}
    cart_item = {"description": "Тестовый счёт", "price": amount, "quantity": 1.000}
    payment_data = {"amount": amount, "capture": True, "description": "Тестовый счёт"}
    if variant == "full":
        payment_data["receipt"] = {"items": [{
            "description": "Тестовый счёт", "quantity": 1, "amount": amount,
            "measure": "piece", **fiscal,
        }]}
    elif variant == "cart-fiscal":
        cart_item.update(fiscal)
    body = {
        "payment_data": payment_data, "cart": [cart_item],
        "delivery_method_data": {"type": "self"}, "locale": "ru_RU",
        "expires_at": (dt.datetime.utcnow() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "description": "Тестовый счёт",
    }
    print("Запрос:", json.dumps(body, ensure_ascii=False, indent=2))
    try:
        answer = application_module._yookassa_request(
            "POST", "/invoices", json_body=body, idempotence_key=secrets.token_hex(16)
        )
    except Exception as error:
        print("\nОШИБКА:", error)
        return 1
    print("\nОтвет:", json.dumps(answer, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
