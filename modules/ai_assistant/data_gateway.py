"""Read-only execution boundary for AI data tools."""

import contextlib
import json
import pathlib
import sqlite3
import time


DEFAULT_QUERY_TIMEOUT_SECONDS = 3.0
MAX_RESULT_BYTES = 120000


class DataGatewayError(ValueError):
    pass


def _database_path(source_connection):
    rows = source_connection.execute("PRAGMA database_list").fetchall()
    for row in rows:
        name = row[1]
        path = row[2]
        if name == "main" and path:
            return pathlib.Path(path).resolve()
    raise DataGatewayError("AI не удалось открыть безопасное подключение к данным.")


def _read_only_authorizer(action, _arg1, _arg2, _database, _trigger):
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


@contextlib.contextmanager
def read_only_connection(source_connection, timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS):
    database_path = _database_path(source_connection)
    uri = database_path.as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    connection.set_progress_handler(
        lambda: 1 if time.monotonic() > deadline else 0,
        10000,
    )
    connection.set_authorizer(_read_only_authorizer)
    try:
        yield connection
    finally:
        connection.set_progress_handler(None, 0)
        connection.set_authorizer(None)
        connection.close()


def run_read_only(source_connection, operation):
    try:
        with read_only_connection(source_connection) as connection:
            result = operation(connection)
    except DataGatewayError:
        raise
    except sqlite3.DatabaseError as error:
        message = str(error).casefold()
        if "interrupt" in message:
            raise DataGatewayError("Запрос к данным превысил допустимое время.") from error
        raise DataGatewayError("Безопасный запрос к данным не выполнен.") from error
    serialized = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
    if len(serialized) > MAX_RESULT_BYTES:
        raise DataGatewayError("Результат запроса слишком большой. Уточните период или фильтры.")
    return result
