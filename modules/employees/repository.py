"""SQL access for employees, positions and Telegram identities."""


def list_employees(db):
    return db.execute(
        "SELECT employees.*, "
        "(SELECT id FROM team_accounts "
        " WHERE team_accounts.employee_id = employees.id ORDER BY id LIMIT 1) AS account_id, "
        "(SELECT username FROM team_accounts "
        " WHERE team_accounts.employee_id = employees.id ORDER BY id LIMIT 1) AS login "
        "FROM employees WHERE employees.deleted_at IS NULL "
        # the main administrator's schedule record is shown as his own card
        "AND employees.id NOT IN (SELECT schedule_employee_id FROM admin_accounts "
        "                         WHERE schedule_employee_id IS NOT NULL) "
        "ORDER BY employees.name"
    ).fetchall()


def get_employee(db, employee_id):
    return db.execute(
        "SELECT * FROM employees WHERE id = ? AND deleted_at IS NULL", (employee_id,)
    ).fetchone()


def get_employee_by_name(db, name, include_deleted=False):
    query = "SELECT * FROM employees"
    if not include_deleted:
        query += " WHERE deleted_at IS NULL"
    for employee in db.execute(query).fetchall():
        if employee["name"].casefold() == name.casefold():
            return employee
    return None


def list_active_employee_names(db):
    return [
        row["name"]
        for row in db.execute(
            "SELECT name FROM employees WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall()
    ]


def team_username_exists(db, username):
    return db.execute(
        "SELECT 1 FROM team_accounts WHERE lower(username) = lower(?)", (username,)
    ).fetchone() is not None


def create_employee_with_account(
    db,
    existing_employee,
    name,
    username,
    password_hash,
    positions,
    telegram_contact,
    created_at,
):
    """Create or reactivate an employee and provision all current access atomically."""
    try:
        if existing_employee is None:
            cursor = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) VALUES (?, ?, NULL)",
                (name, created_at),
            )
            employee_id = cursor.lastrowid
        else:
            employee_id = existing_employee["id"]
            db.execute(
                "UPDATE employees SET deleted_at = NULL WHERE id = ?", (employee_id,)
            )

        db.execute("DELETE FROM employee_positions WHERE employee_id = ?", (employee_id,))
        db.execute(
            "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
            (employee_id,),
        )
        db.execute(
            "DELETE FROM team_accounts WHERE employee_id = ? OR employee_name = ?",
            (employee_id, name),
        )
        for position in positions:
            db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, ?, ?)",
                (employee_id, position, created_at),
            )

        telegram_chat_id = telegram_contact["chat_id"] if telegram_contact else None
        db.execute(
            "INSERT INTO team_accounts "
            "(employee_id, employee_name, username, password_hash, created_at, telegram_chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                employee_id,
                name,
                username,
                password_hash,
                created_at,
                telegram_chat_id,
            ),
        )
        if telegram_contact is not None:
            db.execute(
                "INSERT INTO employee_telegram_accounts "
                "(employee_id, chat_id, username, display_name, linked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    employee_id,
                    telegram_contact["chat_id"],
                    telegram_contact["username"],
                    telegram_contact["display_name"],
                    created_at,
                ),
            )
        db.commit()
        return employee_id
    except Exception:
        db.rollback()
        raise


def update_team_password(db, employee_id, password_hash):
    cursor = db.execute(
        "UPDATE team_accounts SET password_hash = ? WHERE employee_id = ?",
        (password_hash, employee_id),
    )
    db.commit()
    return cursor.rowcount > 0


def get_team_account(db, employee_id):
    return db.execute(
        "SELECT * FROM team_accounts WHERE employee_id = ? ORDER BY id LIMIT 1",
        (employee_id,),
    ).fetchone()


def create_team_account(
    db, employee_id, employee_name, username, password_hash, created_at
):
    db.execute(
        "INSERT INTO team_accounts "
        "(employee_id, employee_name, username, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (employee_id, employee_name, username, password_hash, created_at),
    )
    db.commit()


def count_open_assignments(db, employee_name):
    query = (
        "SELECT "
        "(SELECT COUNT(*) FROM defect_assignments WHERE employee_name = ? "
        " AND (assignment_status = 'pending' "
        "      OR (assignment_status = 'accepted' AND entry_id IS NULL))) + "
        "(SELECT COUNT(*) FROM tuning_item_assignments WHERE employee_name = ? "
        " AND (assignment_status = 'pending' "
        "      OR (assignment_status = 'accepted' AND entry_id IS NULL))) AS count"
    )
    return db.execute(query, (employee_name, employee_name)).fetchone()["count"]


def deactivate_employee(db, employee, deleted_at, today):
    """Remove current access while retaining name-based operational history."""
    try:
        db.execute(
            "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
            (employee["id"],),
        )
        db.execute(
            "DELETE FROM employee_positions WHERE employee_id = ?",
            (employee["id"],),
        )
        db.execute(
            "DELETE FROM team_accounts WHERE employee_id = ? OR employee_name = ?",
            (employee["id"], employee["name"]),
        )
        db.execute(
            "DELETE FROM captain_shifts WHERE employee_id = ? AND shift_date >= ?",
            (employee["id"], today),
        )
        db.execute(
            "UPDATE employees SET deleted_at = ? WHERE id = ?",
            (deleted_at, employee["id"]),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


def list_employee_positions(db):
    return db.execute(
        "SELECT * FROM employee_positions ORDER BY position, id"
    ).fetchall()


def list_known_positions(db):
    return [
        row["position"]
        for row in db.execute(
            "SELECT DISTINCT employee_positions.position FROM employee_positions "
            "JOIN employees ON employees.id = employee_positions.employee_id "
            "WHERE employees.deleted_at IS NULL ORDER BY employee_positions.position"
        ).fetchall()
    ]


def get_position(db, employee_id, position_id):
    return db.execute(
        "SELECT * FROM employee_positions WHERE id = ? AND employee_id = ?",
        (position_id, employee_id),
    ).fetchone()


def add_position(db, employee_id, position, created_at):
    db.execute(
        "INSERT OR IGNORE INTO employee_positions (employee_id, position, created_at) "
        "VALUES (?, ?, ?)",
        (employee_id, position, created_at),
    )
    db.commit()


def delete_position(db, employee_id, position_id):
    db.execute(
        "DELETE FROM employee_positions WHERE id = ? AND employee_id = ?",
        (position_id, employee_id),
    )
    db.commit()


def list_telegram_links(db):
    return db.execute(
        "SELECT employee_telegram_accounts.*, telegram_contacts.last_message_at "
        "FROM employee_telegram_accounts "
        "LEFT JOIN telegram_contacts "
        "ON telegram_contacts.chat_id = employee_telegram_accounts.chat_id"
    ).fetchall()


def get_telegram_link(db, employee_id):
    return db.execute(
        "SELECT employee_telegram_accounts.*, telegram_contacts.last_message_at "
        "FROM employee_telegram_accounts "
        "LEFT JOIN telegram_contacts "
        "ON telegram_contacts.chat_id = employee_telegram_accounts.chat_id "
        "WHERE employee_telegram_accounts.employee_id = ?",
        (employee_id,),
    ).fetchone()


def get_telegram_chat_id_by_employee_name(db, employee_name):
    row = db.execute(
        "SELECT employee_telegram_accounts.chat_id "
        "FROM employee_telegram_accounts "
        "JOIN employees ON employees.id = employee_telegram_accounts.employee_id "
        "WHERE employees.name = ?",
        (employee_name,),
    ).fetchone()
    if row is not None:
        return row["chat_id"]

    # Compatibility for installations that have not yet restarted through
    # the migration which copies old links into employee_telegram_accounts.
    legacy = db.execute(
        "SELECT telegram_chat_id FROM team_accounts "
        "WHERE employee_name = ? AND telegram_chat_id IS NOT NULL",
        (employee_name,),
    ).fetchone()
    return legacy["telegram_chat_id"] if legacy is not None else None


def list_telegram_contacts(db):
    return db.execute(
        "SELECT telegram_contacts.*, "
        "employee_telegram_accounts.employee_id AS linked_employee_id, "
        "employees.name AS linked_employee_name "
        "FROM telegram_contacts "
        "LEFT JOIN employee_telegram_accounts "
        "ON employee_telegram_accounts.chat_id = telegram_contacts.chat_id "
        "LEFT JOIN employees ON employees.id = employee_telegram_accounts.employee_id "
        "ORDER BY telegram_contacts.updated_at DESC, telegram_contacts.chat_id"
    ).fetchall()


def get_telegram_contact(db, chat_id):
    return db.execute(
        "SELECT * FROM telegram_contacts WHERE chat_id = ?", (chat_id,)
    ).fetchone()


def upsert_telegram_contact(db, contact, updated_at):
    existing = get_telegram_contact(db, contact["chat_id"])
    values = (
        contact["username"] or None,
        contact["display_name"] or None,
        contact["last_text"] or None,
        contact["last_message_at"],
        updated_at,
        contact["chat_id"],
    )
    if existing is None:
        db.execute(
            "INSERT INTO telegram_contacts "
            "(username, display_name, last_text, last_message_at, updated_at, chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            values,
        )
    else:
        db.execute(
            "UPDATE telegram_contacts SET username = ?, display_name = ?, last_text = ?, "
            "last_message_at = ?, updated_at = ? WHERE chat_id = ?",
            values,
        )
    # Keep the employee-facing snapshot current when a Telegram user changes
    # their username or display name after the account was linked.
    db.execute(
        "UPDATE employee_telegram_accounts SET username = ?, display_name = ? "
        "WHERE chat_id = ?",
        (contact["username"] or None, contact["display_name"] or None, contact["chat_id"]),
    )


def commit(db):
    db.commit()


def find_link_by_chat_id(db, chat_id):
    return db.execute(
        "SELECT employee_telegram_accounts.*, employees.name AS employee_name "
        "FROM employee_telegram_accounts "
        "JOIN employees ON employees.id = employee_telegram_accounts.employee_id "
        "WHERE employee_telegram_accounts.chat_id = ?",
        (chat_id,),
    ).fetchone()


def link_telegram_account(db, employee, contact, linked_at):
    existing = db.execute(
        "SELECT id FROM employee_telegram_accounts WHERE employee_id = ?",
        (employee["id"],),
    ).fetchone()
    values = (
        contact["chat_id"],
        contact["username"],
        contact["display_name"],
        linked_at,
    )
    if existing is None:
        db.execute(
            "INSERT INTO employee_telegram_accounts "
            "(employee_id, chat_id, username, display_name, linked_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (employee["id"], *values),
        )
    else:
        db.execute(
            "UPDATE employee_telegram_accounts SET chat_id = ?, username = ?, "
            "display_name = ?, linked_at = ? WHERE employee_id = ?",
            (*values, employee["id"]),
        )
    db.execute(
        "UPDATE team_accounts SET telegram_chat_id = ? "
        "WHERE employee_id = ? OR employee_name = ?",
        (contact["chat_id"], employee["id"], employee["name"]),
    )
    db.commit()


def unlink_telegram_account(db, employee):
    db.execute(
        "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
        (employee["id"],),
    )
    db.execute(
        "UPDATE team_accounts SET telegram_chat_id = NULL "
        "WHERE employee_id = ? OR employee_name = ?",
        (employee["id"], employee["name"]),
    )
    db.commit()


def list_candidates(db):
    return db.execute(
        "SELECT * FROM candidates ORDER BY created_at DESC, id DESC"
    ).fetchall()


def get_candidate(db, candidate_id):
    return db.execute(
        "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()


def create_candidate(db, name, phone, note, timestamp):
    cursor = db.execute(
        "INSERT INTO candidates (name, phone, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, phone, note, timestamp, timestamp),
    )
    db.commit()
    return cursor.lastrowid


def delete_candidate(db, candidate_id):
    cursor = db.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
    db.commit()
    return cursor.rowcount > 0


# Every place that keeps an employee's name as plain text (history is joined by
# name, not by id). Renaming an employee must rewrite all of them or the
# person's payroll, tasks, shifts and paid-week marks would silently detach.
NAME_COLUMNS = (
    ("entries", "employee"),
    ("payments", "employee"),
    ("tbank_payout_registries", "employee"),
    ("boat_checklists", "employee_name"),
    ("boat_defects", "employee_name"),
    ("defect_assignments", "employee_name"),
    ("offline_operations", "employee_name"),
    ("schedule_assignments", "employee_name"),
    ("supply_locker_drops", "employee_name"),
    ("supply_requests", "employee_name"),
    ("supply_writeoffs", "employee_name"),
    ("tuning_item_assignments", "employee_name"),
    ("tuning_schedule_tasks", "employee_name"),
    ("boat_fuel_transactions", "created_by_name"),
    ("boat_fuel_state", "activated_by_name"),
    ("field_diagnostic_sheets", "created_by_name"),
)


def _table_has_column(db, table, column):
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})").fetchall())


def rename_employee_everywhere(db, employee_id, old_name, new_name):
    """Rename an employee and rewrite their name in all name-keyed history.
    Does not commit — the caller wraps it with the rest of the update."""
    db.execute("UPDATE employees SET name = ? WHERE id = ?", (new_name, employee_id))
    db.execute(
        "UPDATE team_accounts SET employee_name = ? WHERE employee_id = ?",
        (new_name, employee_id),
    )
    db.execute(
        "UPDATE admin_accounts SET admin_name = ? WHERE employee_id = ?",
        (new_name, employee_id),
    )
    if _table_has_column(db, "software_requests", "author_name"):
        db.execute(
            "UPDATE software_requests SET author_name = ? WHERE author_employee_id = ?",
            (new_name, employee_id),
        )
    for table, column in NAME_COLUMNS:
        if _table_has_column(db, table, column):
            db.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new_name, old_name))


def login_in_use(db, username, exclude_team_account_id=None, exclude_admin_id=None):
    """Logins are looked up across several account tables (the administrator
    login page tries admin_accounts first, then team_accounts), so a login
    must be unique across all of them, ignoring case."""
    row = db.execute(
        "SELECT id FROM team_accounts WHERE lower(username) = lower(?) AND id != ?",
        (username, exclude_team_account_id or 0),
    ).fetchone()
    if row is not None:
        return True
    row = db.execute(
        "SELECT id FROM admin_accounts WHERE lower(username) = lower(?) "
        "AND employee_id IS NULL AND id != ?",
        (username, exclude_admin_id or 0),
    ).fetchone()
    if row is not None:
        return True
    return db.execute(
        "SELECT 1 FROM investors WHERE lower(username) = lower(?)", (username,)
    ).fetchone() is not None


def apply_employee_update(db, employee, new_name, account, username, password_hash, timestamp):
    """One transaction for name / login / password of an employee. `account`
    is the existing team_accounts row or None (then a cabinet is created from
    username + password_hash)."""
    try:
        if new_name != employee["name"]:
            rename_employee_everywhere(db, employee["id"], employee["name"], new_name)
        if account is not None:
            if username and username != account["username"]:
                db.execute(
                    "UPDATE team_accounts SET username = ? WHERE id = ?", (username, account["id"])
                )
            if password_hash:
                db.execute(
                    "UPDATE team_accounts SET password_hash = ? WHERE id = ?",
                    (password_hash, account["id"]),
                )
                db.execute(
                    "UPDATE admin_accounts SET password_hash = ? WHERE employee_id = ?",
                    (password_hash, employee["id"]),
                )
        elif username and password_hash:
            db.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (employee["id"], new_name, username, password_hash, timestamp),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise


def list_legacy_admins(db):
    """The main administrator login(s): admin_accounts rows that are not
    bridged from an employee."""
    return db.execute(
        "SELECT id, admin_name, username, schedule_employee_id FROM admin_accounts "
        "WHERE employee_id IS NULL ORDER BY id"
    ).fetchall()


def get_legacy_admin(db, admin_id):
    return db.execute(
        "SELECT id, admin_name, username, schedule_employee_id FROM admin_accounts "
        "WHERE id = ? AND employee_id IS NULL",
        (admin_id,),
    ).fetchone()


def create_admin_schedule_employee(db, admin_id, name, position, timestamp):
    """The employees record the main administrator is scheduled under; it
    holds the position «Администратор» (so both schedules accept him) and has
    no personal cabinet — he keeps logging in through admin_accounts."""
    try:
        employee_id = db.execute(
            "INSERT INTO employees (name, created_at, deleted_at) VALUES (?, ?, NULL)",
            (name, timestamp),
        ).lastrowid
        db.execute(
            "INSERT INTO employee_positions (employee_id, position, created_at) VALUES (?, ?, ?)",
            (employee_id, position, timestamp),
        )
        db.execute(
            "UPDATE admin_accounts SET schedule_employee_id = ? WHERE id = ?", (employee_id, admin_id)
        )
        db.commit()
        return employee_id
    except Exception:
        db.rollback()
        raise


def update_legacy_admin(db, admin_id, name, username, password_hash):
    try:
        admin = db.execute(
            "SELECT admin_name, schedule_employee_id FROM admin_accounts WHERE id = ?", (admin_id,)
        ).fetchone()
        if admin is not None and admin["schedule_employee_id"] and admin["admin_name"] != name:
            # keep the schedule record (and all history keyed by its name) in step
            rename_employee_everywhere(db, admin["schedule_employee_id"], admin["admin_name"], name)
        db.execute(
            "UPDATE admin_accounts SET admin_name = ?, username = ? WHERE id = ?",
            (name, username, admin_id),
        )
        if password_hash:
            db.execute(
                "UPDATE admin_accounts SET password_hash = ? WHERE id = ?",
                (password_hash, admin_id),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
