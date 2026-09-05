"""Role-aware logical data catalog exposed to the AI assistant.

The catalog deliberately describes business datasets instead of physical
SQLite tables.  Passwords, API tokens and other infrastructure data never
become discoverable through this interface.
"""


CATALOG_VERSION = 1


DATASETS = (
    {
        "id": "schedule",
        "name": "Расписание экскурсий",
        "description": "Созданные рейсы без персональных данных туристов.",
        "roles": ("admin", "manager"),
        "scope": "all",
        "date_basis": {
            "field": "starts_at",
            "label": "Дата и время начала рейса",
        },
        "metrics": (
            ("trips", "Количество рейсов", "integer"),
            ("planned_revenue_rub", "Плановая выручка", "currency"),
            ("guests", "Количество гостей", "integer"),
            ("unassigned_trips", "Рейсы без назначенного экипажа", "integer"),
        ),
        "dimensions": (
            ("day", "День"),
            ("month", "Месяц"),
            ("boat", "Катер"),
            ("service", "Услуга"),
            ("kind", "Вид рейса"),
        ),
        "filters": ("date_from", "date_to", "boat"),
        "tools": ("get_schedule_summary", "get_bar_chart"),
    },
    {
        "id": "tuning_orders",
        "name": "Тюнинг-заказы",
        "description": "Заказы, оплаты и задолженность тюнинг-центра.",
        "roles": ("admin",),
        "scope": "all",
        "date_basis": {
            "field": "order_date",
            "label": "Дата заказа из интерфейса, не техническая дата импорта",
        },
        "metrics": (
            ("orders", "Количество заказов", "integer"),
            ("orders_total_rub", "Полная стоимость заказов", "currency"),
            ("payments_received_rub", "Полученные оплаты", "currency"),
            ("outstanding_rub", "Текущая задолженность", "currency"),
        ),
        "dimensions": (
            ("day", "День"),
            ("month", "Месяц"),
            ("status", "Статус заказа"),
            ("sale_channel", "Канал продаж"),
            ("equipment_type", "Тип техники"),
        ),
        "filters": ("date_from", "date_to", "status"),
        "tools": ("get_tuning_summary", "get_bar_chart"),
    },
    {
        "id": "excursion_clients",
        "name": "Клиенты экскурсий",
        "description": "Обезличенные агрегаты экскурсионной клиентской базы.",
        "roles": ("admin", "manager"),
        "scope": "aggregates_only",
        "date_basis": None,
        "metrics": (("clients", "Количество клиентов", "integer"),),
        "dimensions": (
            ("status", "Статус клиента"),
            ("acquisition_channel", "Канал привлечения"),
        ),
        "filters": (),
        "tools": ("get_clients_summary", "get_bar_chart"),
    },
    {
        "id": "tuning_clients",
        "name": "Клиенты тюнинга",
        "description": "Обезличенные агрегаты клиентской базы тюнинг-центра.",
        "roles": ("admin",),
        "scope": "aggregates_only",
        "date_basis": None,
        "metrics": (("clients", "Количество клиентов", "integer"),),
        "dimensions": (
            ("status", "Статус клиента"),
            ("acquisition_channel", "Канал привлечения"),
        ),
        "filters": (),
        "tools": ("get_clients_summary", "get_bar_chart"),
    },
    {
        "id": "payroll",
        "name": "Зарплаты и начисления",
        "description": "Начисления сотрудникам по датам и видам работ.",
        "roles": ("admin", "manager", "captain", "employee"),
        "scope": "own_for_employee",
        "date_basis": {"field": "work_date", "label": "Дата выполнения работы"},
        "metrics": (
            ("entries", "Количество начислений", "integer"),
            ("amount_rub", "Сумма начислений", "currency"),
        ),
        "dimensions": (
            ("day", "День"),
            ("month", "Месяц"),
            ("employee", "Сотрудник — только администратору"),
            ("work_type", "Вид работы"),
        ),
        "filters": ("date_from", "date_to", "employee_name"),
        "tools": ("get_payroll_summary", "get_bar_chart"),
    },
    {
        "id": "tasks",
        "name": "Порученные задачи",
        "description": "Количество ожидающих и принятых задач по флоту и тюнингу.",
        "roles": ("admin", "manager", "captain", "employee"),
        "scope": "own_for_employee",
        "date_basis": None,
        "metrics": (
            ("pending", "Ожидают принятия", "integer"),
            ("accepted", "Приняты в работу", "integer"),
        ),
        "dimensions": (("area", "Флот или тюнинг"),),
        "filters": ("employee_name",),
        "tools": ("get_tasks_summary",),
    },
    {
        "id": "fleet",
        "name": "Флот",
        "description": "Остатки топлива и текущие неисправности по катерам.",
        "roles": ("admin", "captain"),
        "scope": "all",
        "date_basis": None,
        "metrics": (
            ("tank_liters", "Топливо в баке", "liters"),
            ("reserve_liters", "Топливо в резерве", "liters"),
            ("defects", "Текущие неисправности", "integer"),
        ),
        "dimensions": (("boat", "Катер"), ("defect_status", "Статус неисправности")),
        "filters": ("boat",),
        "tools": ("get_fleet_status", "get_bar_chart"),
    },
)


DATASET_IDS = tuple(dataset["id"] for dataset in DATASETS)


def _role(user):
    if user.get("owner_type") == "admin":
        return "admin"
    positions = {str(value).casefold() for value in user.get("positions", [])}
    if "менеджер по работе с клиентами" in positions:
        return "manager"
    if positions & {"капитан", "гид-капитан", "капитан-механик"}:
        return "captain"
    return "employee"


def _public_dataset(dataset, role):
    scope = dataset["scope"]
    if dataset["id"] in ("payroll", "tasks"):
        scope = "all" if role == "admin" else "own_only"
    dimensions = [
        {"id": value, "label": label}
        for value, label in dataset["dimensions"]
        if not (value == "employee" and role != "admin")
    ]
    filters = [
        value for value in dataset["filters"]
        if not (value == "employee_name" and role != "admin")
    ]
    return {
        "id": dataset["id"],
        "name": dataset["name"],
        "description": dataset["description"],
        "access_scope": scope,
        "date_basis": dataset["date_basis"],
        "metrics": [
            {"id": value, "label": label, "format": value_format}
            for value, label, value_format in dataset["metrics"]
        ],
        "dimensions": dimensions,
        "filters": filters,
        "available_tools": list(dataset["tools"]),
        "personal_data": "excluded",
        "write_access": False,
    }


def visible_dataset_ids(user):
    role = _role(user)
    return tuple(dataset["id"] for dataset in DATASETS if role in dataset["roles"])


def visible_chart_subjects(user):
    dataset_ids = set(visible_dataset_ids(user))
    mapping = (
        ("schedule", {"schedule"}),
        ("tuning", {"tuning_orders"}),
        ("clients", {"excursion_clients", "tuning_clients"}),
        ("payroll", {"payroll"}),
        ("fleet", {"fleet"}),
    )
    return tuple(subject for subject, datasets in mapping if dataset_ids & datasets)


def catalog_for_user(user, dataset_id=None):
    role = _role(user)
    allowed_ids = set(visible_dataset_ids(user))
    visible = [dataset for dataset in DATASETS if dataset["id"] in allowed_ids]
    if dataset_id:
        visible = [dataset for dataset in visible if dataset["id"] == dataset_id]
        if not visible:
            raise ValueError("Набор данных не найден или недоступен для вашей роли.")
    return {
        "catalog_version": CATALOG_VERSION,
        "role": role,
        "datasets": [_public_dataset(dataset, role) for dataset in visible],
        "safety": {
            "connection": "read_only",
            "personal_data": "excluded_by_default",
            "credentials_and_tokens": "never_available",
            "raw_sql": "not_available",
        },
    }
