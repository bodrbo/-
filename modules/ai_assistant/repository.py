"""Persistence helpers for the internal AI assistant."""


def list_conversations(db, owner_type, owner_id, limit):
    return db.execute(
        "SELECT id, title, created_at, updated_at FROM ai_conversations "
        "WHERE owner_type = ? AND owner_id = ? "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        (owner_type, owner_id, limit),
    ).fetchall()


def get_conversation(db, conversation_id, owner_type, owner_id):
    return db.execute(
        "SELECT * FROM ai_conversations "
        "WHERE id = ? AND owner_type = ? AND owner_id = ?",
        (conversation_id, owner_type, owner_id),
    ).fetchone()


def create_conversation(db, owner_type, owner_id, owner_name, title, timestamp):
    cursor = db.execute(
        "INSERT INTO ai_conversations "
        "(owner_type, owner_id, owner_name, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (owner_type, owner_id, owner_name, title, timestamp, timestamp),
    )
    db.commit()
    return cursor.lastrowid


def update_conversation(db, conversation_id, title, timestamp):
    db.execute(
        "UPDATE ai_conversations SET title = ?, updated_at = ? WHERE id = ?",
        (title, timestamp, conversation_id),
    )


def touch_conversation(db, conversation_id, timestamp):
    db.execute(
        "UPDATE ai_conversations SET updated_at = ? WHERE id = ?",
        (timestamp, conversation_id),
    )


def delete_conversation(db, conversation_id, owner_type, owner_id):
    conversation = get_conversation(db, conversation_id, owner_type, owner_id)
    if conversation is None:
        return False
    with db:
        db.execute(
            "DELETE FROM ai_tool_runs WHERE conversation_id = ?",
            (conversation_id,),
        )
        db.execute(
            "DELETE FROM ai_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        db.execute(
            "DELETE FROM ai_conversations WHERE id = ?",
            (conversation_id,),
        )
    return True


def list_messages(db, conversation_id, limit=None):
    if limit is None:
        return db.execute(
            "SELECT * FROM ai_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    rows = db.execute(
        "SELECT * FROM ai_messages WHERE conversation_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return list(reversed(rows))


def add_message(
    db,
    conversation_id,
    role,
    content,
    timestamp,
    model=None,
    input_tokens=0,
    output_tokens=0,
):
    cursor = db.execute(
        "INSERT INTO ai_messages "
        "(conversation_id, role, content, model, input_tokens, output_tokens, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            role,
            content,
            model,
            int(input_tokens or 0),
            int(output_tokens or 0),
            timestamp,
        ),
    )
    return cursor.lastrowid


def add_tool_run(db, conversation_id, tool_name, arguments_json, result_json, timestamp):
    db.execute(
        "INSERT INTO ai_tool_runs "
        "(conversation_id, tool_name, arguments_json, result_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, tool_name, arguments_json, result_json, timestamp),
    )


def count_recent_user_messages(db, owner_type, owner_id, since_timestamp):
    row = db.execute(
        "SELECT COUNT(*) AS total FROM ai_messages m "
        "JOIN ai_conversations c ON c.id = m.conversation_id "
        "WHERE c.owner_type = ? AND c.owner_id = ? "
        "AND m.role = 'user' AND m.created_at >= ?",
        (owner_type, owner_id, since_timestamp),
    ).fetchone()
    return int(row["total"] if row else 0)

