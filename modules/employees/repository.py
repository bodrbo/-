"""SQL access for employees, positions and Telegram identities."""


def list_employees(db):
    return db.execute(
        "SELECT employees.*, "
        "(SELECT username FROM team_accounts "
        " WHERE team_accounts.employee_name = employees.name ORDER BY id LIMIT 1) AS login "
        "FROM employees ORDER BY employees.name"
    ).fetchall()


def get_employee(db, employee_id):
    return db.execute(
        "SELECT * FROM employees WHERE id = ?", (employee_id,)
    ).fetchone()


def list_employee_positions(db):
    return db.execute(
        "SELECT * FROM employee_positions ORDER BY position, id"
    ).fetchall()


def list_known_positions(db):
    return [
        row["position"]
        for row in db.execute(
            "SELECT DISTINCT position FROM employee_positions ORDER BY position"
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
        "UPDATE team_accounts SET telegram_chat_id = ? WHERE employee_name = ?",
        (contact["chat_id"], employee["name"]),
    )
    db.commit()


def unlink_telegram_account(db, employee):
    db.execute(
        "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
        (employee["id"],),
    )
    db.execute(
        "UPDATE team_accounts SET telegram_chat_id = NULL WHERE employee_name = ?",
        (employee["name"],),
    )
    db.commit()
