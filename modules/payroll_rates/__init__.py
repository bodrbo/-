"""Admin-editable payroll pay rates, under Зарплаты -> Ставки. Currently
only "Ставки экскурсий" (₽/hour by crew role: captain/guide/guide_captain)
is implemented — it feeds modules.schedule's auto-close job. "Ставки
тюнинга" is a placeholder for future work."""

from .routes import create_payroll_rates_blueprint
from .schema import init_schema

__all__ = ["create_payroll_rates_blueprint", "init_schema"]
