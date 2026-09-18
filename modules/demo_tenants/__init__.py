"""Demo-tenant provisioning: lets a platform admin create branded,
module-scoped demo accounts for prospective companies, each backed by its
own isolated SQLite database (same schema, no shared data)."""

from .routes import create_blueprint
from .constants import DEMO_MODULES, DEMO_TOUR_STEPS

__all__ = ["create_blueprint", "DEMO_MODULES", "DEMO_TOUR_STEPS"]
