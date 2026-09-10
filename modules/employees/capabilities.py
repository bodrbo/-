"""Role-driven capabilities for the shared employee cabinet.

The cabinet is intentionally composed from capabilities instead of choosing
one fixed dashboard per employee.  An employee may hold several positions;
changing those positions in the administrator interface therefore changes
the visible modules on the next request without recreating their account.
"""


INCOME = "income"
TASKS = "tasks"
SUPPLY = "supply"
FLEET = "fleet"
DOCUMENTS = "documents"
SCHEDULE = "schedule"

BASE_CAPABILITIES = frozenset({INCOME})

POSITION_CAPABILITIES = {
    "Капитан": frozenset({TASKS, SUPPLY, FLEET, DOCUMENTS, SCHEDULE}),
    "Гид": frozenset({SCHEDULE}),
    "Гид-капитан": frozenset({SCHEDULE}),
    "Тюнингмэн": frozenset({TASKS, SUPPLY}),
}


def dashboard_capabilities(position_names):
    """Return the union of cabinet capabilities for current DB positions."""
    capabilities = set(BASE_CAPABILITIES)
    positions = {
        str(position or "").strip().casefold()
        for position in position_names
        if str(position or "").strip()
    }
    for position, position_capabilities in POSITION_CAPABILITIES.items():
        if position.casefold() in positions:
            capabilities.update(position_capabilities)
    return frozenset(capabilities)


def dashboard_title(position_names):
    """Choose the most specific familiar title for a multi-position cabinet."""
    positions = {
        str(position or "").strip().casefold()
        for position in position_names
        if str(position or "").strip()
    }
    if "Капитан".casefold() in positions:
        return "Кабинет капитана"
    if "Тюнингмэн".casefold() in positions:
        return "Кабинет тюнингмэна"
    return "Личный кабинет сотрудника"
