"""Bootstrap data for the employee directory.

These values only seed a new database. Once the database exists, positions
are managed from the administrator interface and are never re-synchronised
from this file.
"""

EMPLOYEES = [
    "Даниил Галецкий",
    "Дмитрий Тарусов",
    "Кирилл Бурнасов",
    "Эльмира Бектаева",
    "Платон Жмаев",
    "Михаил Вишневский",
    "Андрей Жаворонков",
    "Арсений Коннов",
    "Марина Кащенко",
    "Юрий Мороз",
    "Игорь Севостьянов",
    "Алексей Чабанов",
    "Андрей Краснюков",
]

INITIAL_EMPLOYEE_POSITIONS = {
    "Даниил Галецкий": ["Тюнингмэн", "Гид-капитан", "Капитан"],
    "Эльмира Бектаева": ["Гид"],
    "Дмитрий Тарусов": ["Тюнингмэн", "Капитан"],
    "Алексей Чабанов": ["Тюнингмэн"],
    "Андрей Краснюков": ["Тюнингмэн"],
    "Андрей Жаворонков": ["Тюнингмэн", "Капитан"],
    "Арсений Коннов": ["Гид"],
    "Платон Жмаев": ["Капитан", "Гид-капитан", "Тюнингмэн"],
    "Кирилл Бурнасов": ["Гид"],
    "Юрий Мороз": ["Тюнингмэн"],
    "Михаил Вишневский": ["Капитан"],
    "Марина Кащенко": ["Менеджер по работе с клиентами"],
    "Игорь Севостьянов": ["Капитан", "Тюнингмэн"],
}

POSITION_MAX_LENGTH = 80
EMPLOYEE_NAME_MAX_LENGTH = 120
EMPLOYEE_LOGIN_MAX_LENGTH = 50
GENERATED_PASSWORD_LENGTH = 14

# The directory keeps runtime positions in the database, but these core
# positions must always remain available when an administrator creates a new
# employee — even if nobody currently holds one of them.
KNOWN_POSITIONS = (
    "Капитан",
    "Гид",
    "Гид-капитан",
    "Тюнингмэн",
    "Менеджер по работе с клиентами",
)

CUSTOMER_MANAGER_POSITION = "Менеджер по работе с клиентами"
