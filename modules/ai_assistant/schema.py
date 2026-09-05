"""SQLite schema for conversations, messages and tool audit records."""


def init_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_type TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            visualizations_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    message_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ai_messages)").fetchall()
    }
    if "visualizations_json" not in message_columns:
        conn.execute(
            "ALTER TABLE ai_messages ADD COLUMN "
            "visualizations_json TEXT NOT NULL DEFAULT '[]'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_tool_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_conversations_owner_updated "
        "ON ai_conversations(owner_type, owner_id, updated_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_messages_conversation "
        "ON ai_messages(conversation_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_tool_runs_conversation "
        "ON ai_tool_runs(conversation_id, id)"
    )
