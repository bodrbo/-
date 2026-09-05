"""Limits and defaults for the internal AI assistant."""

DEFAULT_MODEL = "gpt-5.6-luna"

MESSAGE_MAX_LENGTH = 4000
CONVERSATION_TITLE_MAX_LENGTH = 80
HISTORY_MESSAGE_LIMIT = 16
CONVERSATION_LIST_LIMIT = 30
MAX_OUTPUT_TOKENS = 1600
MAX_TOOL_ROUNDS = 4
MAX_TOOL_RESULT_LENGTH = 12000
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_CONCURRENT_REQUESTS = 5
DEFAULT_USER_REQUESTS_PER_MINUTE = 10

GUIDE_TOPICS = {
    "overview": "Обзор системы",
    "schedule": "Расписание и экскурсии",
    "clients": "Клиенты",
    "fleet": "Флот",
    "tuning": "Тюнинг-центр",
    "payroll": "Зарплаты",
    "analytics": "Аналитика",
    "employees": "Сотрудники и уведомления",
    "offline": "Офлайн-режим",
}
