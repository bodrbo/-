#!/usr/bin/env python3
"""Seeds a fresh database with fictional demo data — orders, tasks, fleet
records, finances — for showing the system to a prospective branch/partner
without touching any real client or business data.

Everything is created through the app's own routes (the same validation
and side effects a real admin action would trigger — auto-created
projects, payroll entries, order totals, etc.), not hand-written SQL, so
the demo data is exactly as consistent as data a real admin would produce.

Safety: refuses to run unless WORKHOURS_DB_PATH is set to a path that
does not already look like the production database, so this can never be
run by accident against real data. Pass --reset to delete an existing
demo database file first — safe to do before every walkthrough to start
from a clean slate.

Usage:
    WORKHOURS_DB_PATH=/path/to/demo.db SECRET_KEY=some-demo-secret \\
        venv/bin/python3 seed_demo_data.py --reset

Afterwards the script prints the login to hand to whoever is viewing the
demo: a normal employee account (position "Администратор") that logs in
at /team/login and is transparently upgraded to a full admin session —
exactly how a real new branch would onboard its own first admin, so this
is also a live rehearsal of that flow, not just a demo shortcut.

IMPORTANT if WORKHOURS_DB_PATH here is the SAME file a live Flask process
uses as its own DB_PATH (i.e. this is a standalone demo deployment, not a
tenant DB provisioned by modules/demo_tenants): app.py bootstraps
ADMIN_ACCOUNTS/INVESTOR_ACCOUNTS (real production names and password
hashes, hardcoded near the top of app.py) into ANY database on every
process start via init_db() — this script's own cleanup only lasts until
that process next restarts, since init_db() runs unconditionally on
import and re-inserts them. Before first deploy of such a standalone demo,
in its own app.py:
  - set INVESTOR_ACCOUNTS = [] (drops two real people's names/hashes)
  - replace ADMIN_ACCOUNTS' password hash with a fresh one via
    generate_password_hash("some-demo-only-password", method="pbkdf2:sha256")
    so the demo's admin login isn't the same credential as production.
(A tenant DB provisioned through modules/demo_tenants doesn't have this
problem — it's built once via init_db(tenant_path) and never touched by
the shared process's own unconditional init_db() again.)
"""
import argparse
import datetime as dt
import os
import sqlite3
import sys

DB_PATH_ENV = "WORKHOURS_DB_PATH"


def _today_offset(days):
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _real_catalog_model_names(appmod, equipment_type, limit):
    """Real tuning_boat_profiles model names (appmod.DB_PATH — the shared/
    main database, not the tenant db this script is populating) so seeded
    orders reference actual catalog cards with real photos/specs — see
    _tuning_catalog_db in app.py. Falls back to [] (caller then uses its
    own made-up names) when the shared catalog can't be read or has too
    few entries — this also keeps a standalone demo deployment (where
    appmod.DB_PATH IS the database being seeded, and so has no catalog yet
    at seed time) working exactly as before."""
    try:
        conn = sqlite3.connect(appmod.DB_PATH)
        rows = conn.execute(
            "SELECT model_name FROM tuning_boat_profiles WHERE equipment_type = ? "
            "ORDER BY model_name COLLATE NOCASE LIMIT ?",
            (equipment_type, limit),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except sqlite3.Error:
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true",
        help="Delete the existing demo database file first.",
    )
    args = parser.parse_args()

    db_path = os.environ.get(DB_PATH_ENV)
    if not db_path:
        sys.exit(
            f"Refusing to run: set {DB_PATH_ENV} to the demo database path "
            "first. This script never touches the default/production path."
        )
    if os.path.basename(db_path) in ("workhours.db",) and "demo" not in db_path.lower():
        sys.exit(
            f"{DB_PATH_ENV}={db_path!r} doesn't look like a demo database "
            "path (no 'demo' in it, default filename). Refusing to run — "
            "point it at a dedicated demo database file."
        )
    if args.reset and os.path.exists(db_path):
        os.remove(db_path)
        print(f"Removed existing {db_path}")

    import app as appmod  # noqa: E402  (import after DB_PATH_ENV is validated — app.py reads it at import time)

    with appmod.app.app_context():
        appmod.init_db()
        db = appmod.get_db()
        credentials = seed(appmod, db, db_path=db_path)

    print("\nГотово. Демо-база засеяна.")
    print("\nВход для показа (полный доступ администратора):")
    print(f"  URL:    /team/login")
    print(f"  Логин:    {credentials['username']}")
    print(f"  Пароль:   {credentials['password']}")
    print(
        "\n(Это обычный логин сотрудника с должностью «Администратор» — "
        "при входе через /team/login система сама поднимает его до "
        "полноценной админ-сессии. Так же будет выглядеть онбординг "
        "настоящего нового филиала.)"
    )
    print(
        "\n!!! ПЕРЕД ПЕРВЫМ ДЕПЛОЕМ демо-репозитория (сделать один раз в "
        "app.py НОВОГО репозитория, не в этом):\n"
        "  1. INVESTOR_ACCOUNTS = []  — иначе реальные ФИО и хэши паролей "
        "инвесторов появятся в базе демо заново при каждом рестарте сервера "
        "(init_db() досеивает их безусловно, чистка этим скриптом не "
        "переживает рестарт).\n"
        "  2. Замените хэш пароля в ADMIN_ACCOUNTS на новый через "
        "generate_password_hash('демо-пароль', method='pbkdf2:sha256') — "
        "иначе служебный вход 'admin' на демо будет открываться тем же "
        "паролем, что и боевой сервер."
    )


def seed(appmod, db, db_path=None, client=None):
    """db is a connection to the target database (already schema-migrated
    via appmod.init_db()); db_path is that same database's file path, used
    only to point a fresh test client's session at it. Pass an existing
    client (already scoped to the right tenant session, e.g. from the demo
    tenant provisioning flow) to reuse it instead of creating one here."""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    # The bootstrap admin/investor rows from ADMIN_ACCOUNTS/INVESTOR_ACCOUNTS
    # (real production names and password hashes, hardcoded in app.py and
    # created by init_db() in ANY fresh database) have no place in a demo
    # that might end up in front of an outside colleague — wipe them before
    # seeding fresh, demo-only accounts.
    db.execute("DELETE FROM investors")
    db.execute("DELETE FROM admin_accounts")
    db.commit()

    # Prefer real catalog model names (real photos/specs, see
    # _tuning_catalog_db in app.py) over made-up ones, so a demo tenant's
    # seeded orders point at actual catalog cards — falling back to the
    # made-up names for whichever slots the real catalog can't fill.
    fallback_boat_models = [
        "RIB Тюнинг-Про 780", "Wellboat Крым 620",
        "Каютный катер Норд 850", "Лодка ПВХ 380",
    ]
    boat_models = (
        _real_catalog_model_names(appmod, "boat", len(fallback_boat_models))
        + fallback_boat_models
    )[:len(fallback_boat_models)]
    real_motor_models = _real_catalog_model_names(appmod, "motor", 1)
    motor_model = real_motor_models[0] if real_motor_models else "Мотор Yamaha 115"

    if client is None:
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            # Grants a full admin session without needing any admin_accounts
            # row at all (see _active_admin_account's demo-tenant bypass in
            # app.py) — works even though the row above was just deleted.
            sess["demo_tenant_id"] = -1
            sess["demo_tenant_name"] = "Demo seed"
            # Bypasses the module-visibility gate (demo_tenant_module_gate
            # in app.py) — seeding writes across every module regardless
            # of which ones the tenant will end up showing.
            sess["demo_tenant_seeding"] = True
            if db_path:
                sess["demo_tenant_db_path"] = db_path

    def post(path, data, expect_redirect=True):
        resp = client.post(path, data=data)
        if expect_redirect and resp.status_code not in (301, 302, 303, 200, 204):
            raise RuntimeError(f"POST {path} failed: {resp.status_code}\n{resp.get_data(as_text=True)[:2000]}")
        return resp

    # ---- Сотрудники -------------------------------------------------
    def add_employee(name, position):
        post("/employees", {"name": name, "positions": position})
        with client.session_transaction() as sess:
            creds = sess.get("employee_credentials")
        if not creds or creds["employee_name"] != name:
            raise RuntimeError(f"employee creation for {name} did not return credentials")
        return creds

    admin_creds = add_employee("Демо Администратор", "Администратор")
    tuner_1 = add_employee("Иван Тюнингов", "Тюнингмэн")
    tuner_2 = add_employee("Пётр Механиков", "Тюнингмэн")

    # ---- Тюнинг-центр: заказы в разных статусах ----------------------
    def add_order(
        client_name, boat_model, deadline_offset_days, items,
        phone="+79001234567", equipment_type="boat",
    ):
        data = {
            "client_name": client_name,
            "equipment_type": equipment_type,
            "boat_model": boat_model if equipment_type == "boat" else "",
            "motor_model": boat_model if equipment_type == "motor" else "",
            "boat_registration_number": "",
            "phone": phone,
            "sale_channel": "direct",
            "order_date": _today_offset(-5),
            "deadline_date": _today_offset(deadline_offset_days),
            "discount_type": "percent",
            "discount_value": "0",
            "work_name[]": [w for w, _, _ in items],
            "cost_price[]": [str(c) for _, c, _ in items],
            "multiplier[]": [str(m) for _, _, m in items],
        }
        post("/tuning/add", data)
        order = db.execute(
            "SELECT * FROM tuning_orders WHERE client_name = ? ORDER BY id DESC LIMIT 1",
            (client_name,),
        ).fetchone()
        items_rows = db.execute(
            "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order["id"],)
        ).fetchall()
        return order, items_rows

    def assign(order_id, item_id, employee_name, rate, hours, comment=""):
        post(f"/tuning/{order_id}/item/{item_id}/assign", {
            "employee_name[]": employee_name, "rate[]": str(rate),
            "norm_hours[]": str(hours), "comment": comment,
        })
        return db.execute(
            "SELECT * FROM tuning_item_assignments WHERE item_id = ? "
            "AND employee_name = ? ORDER BY id DESC LIMIT 1",
            (item_id, employee_name),
        ).fetchone()

    def set_assignment_status(assignment_id, status):
        post(f"/tuning/assignments/{assignment_id}/status", {"status": status})

    def set_order_status(order_id, status):
        post(f"/tuning/{order_id}/status", {"status": status})

    # Order 1 — В работе, срок ещё не наступил, одна задача уже выполнена
    # и оплачена, вторая в работе — показывает начисление гонорара и
    # разные статусы задач в одной работе.
    order_a, items_a = add_order(
        "Смирнов Алексей", boat_models[0], deadline_offset_days=5,
        items=[("Полировка корпуса", 15000, 2), ("Замена уплотнений", 8000, 1.8)],
        phone="+79001112233",
    )
    set_order_status(order_a["id"], "in_progress")
    # Rate x hours here is the employee's payout, deducted from the item's
    # price (cost_price x multiplier) as project expense once "done" — kept
    # well below price so every qualifying project stays profitable in
    # Аналитика rather than showing a demotivating loss.
    assignment_1 = assign(order_a["id"], items_a[0]["id"], tuner_1["employee_name"], 900, 8, "Полный цикл полировки")
    set_assignment_status(assignment_1["id"], "accepted")
    set_assignment_status(assignment_1["id"], "in_progress")
    set_assignment_status(assignment_1["id"], "done")  # начисляет гонорар
    assignment_2 = assign(order_a["id"], items_a[1]["id"], tuner_2["employee_name"], 700, 4)
    set_assignment_status(assignment_2["id"], "accepted")
    set_assignment_status(assignment_2["id"], "in_progress")

    # Order 2 — В работе, срок просрочен (демонстрирует подсветку и
    # тултип «Заказ был просрочен»), задача ещё не начата.
    order_b, items_b = add_order(
        "Козлова Мария", boat_models[1], deadline_offset_days=-3,
        items=[("Ремонт транца", 20000, 2.2)],
        phone="+79007654321",
    )
    set_order_status(order_b["id"], "in_progress")
    assignment_3 = assign(order_b["id"], items_b[0]["id"], tuner_1["employee_name"], 900, 10)
    set_assignment_status(assignment_3["id"], "accepted")

    # Order 3 — Выполнен, передан: срок был соблюдён (completed_at раньше
    # deadline_date), задача выполнена и оплачена.
    order_c, items_c = add_order(
        "ООО «Паруса Балтики»", boat_models[2], deadline_offset_days=2,
        items=[("Установка навигации", 35000, 1.6)],
        phone="+78121112233",
    )
    set_order_status(order_c["id"], "in_progress")
    assignment_4 = assign(order_c["id"], items_c[0]["id"], tuner_2["employee_name"], 1200, 12)
    set_assignment_status(assignment_4["id"], "accepted")
    set_assignment_status(assignment_4["id"], "done")
    set_order_status(order_c["id"], "done")
    set_order_status(order_c["id"], "handed_over")

    # Order 4 — Отменён: проверяет, что Аналитика его не считает.
    order_d, items_d = add_order(
        "Тестовый Клиент", boat_models[3], deadline_offset_days=10,
        items=[("Диагностика", 3000, 1.5)],
        phone="+79009998877",
    )
    set_order_status(order_d["id"], "cancelled")

    # Order 5 — просто новая заявка, без движения: показывает воронку.
    add_order(
        "Фёдоров Никита", motor_model, deadline_offset_days=14,
        items=[("Расчёт стоимости работ", 0, 0)],
        phone="+79005554433", equipment_type="motor",
    )

    # ---- Финансы: транзакции по проектам (для Аналитики) -------------
    def add_transaction(operation_date, direction, amount, counterparty, purpose, project_id):
        post("/analytics/transactions/manual-add", {
            "operation_date": operation_date, "direction": direction,
            "amount": str(amount), "counterparty_name": counterparty, "purpose": purpose,
        })
        row = db.execute(
            "SELECT id FROM bank_transactions WHERE counterparty_name = ? "
            "ORDER BY id DESC LIMIT 1", (counterparty,),
        ).fetchone()
        post("/analytics/transactions/project", {
            "transaction_id": str(row["id"]), "target": f"p{project_id}",
        })

    project_a = db.execute(
        "SELECT id FROM projects WHERE tuning_order_id = ?", (order_a["id"],)
    ).fetchone()["id"]
    project_b = db.execute(
        "SELECT id FROM projects WHERE tuning_order_id = ?", (order_b["id"],)
    ).fetchone()["id"]
    project_c = db.execute(
        "SELECT id FROM projects WHERE tuning_order_id = ?", (order_c["id"],)
    ).fetchone()["id"]
    project_d = db.execute(
        "SELECT id FROM projects WHERE tuning_order_id = ?", (order_d["id"],)
    ).fetchone()["id"]
    today = _today_offset(0)
    add_transaction(today, "in", 46000, "Смирнов Алексей", "Предоплата по заказу", project_a)
    add_transaction(today, "in", 25000, "Козлова Мария", "Предоплата по заказу", project_b)
    add_transaction(today, "in", 91600, "ООО «Паруса Балтики»", "Оплата заказа полностью", project_c)
    # Отменённый заказ: транзакция есть, но проект не должен попасть в
    # подсчёт Аналитики — так проверяется фильтр по статусу заказа.
    add_transaction(today, "in", 4500, "Тестовый Клиент", "Оплата диагностики", project_d)

    # ---- Флот: пара неисправностей для demo пагинации/фильтра --------
    tenant_boats = appmod.fleet_boats_for_db(db)
    boat_name = tenant_boats[0]["name"]
    boat_index = 0
    post(f"/fleet/{boat_index}/defects", {"description": "Скрип в районе транца при полном ходу"})
    post(f"/fleet/{boat_index}/defects", {"description": "Замена анода на нижней части двигателя"})

    # ---- Клиентская база ----------------------------------------------
    for name, phone, email in [
        ("Смирнов Алексей", "+79001112233", "smirnov@example.com"),
        ("Козлова Мария", "+79007654321", "kozlova@example.com"),
        ("ООО «Паруса Балтики»", "+78121112233", "parusa@example.com"),
    ]:
        post("/admin/clients/create", {
            "client_name": name, "phone": phone, "email": email,
            "section": "tuning", "relationship": "client",
        })

    return admin_creds


if __name__ == "__main__":
    main()
