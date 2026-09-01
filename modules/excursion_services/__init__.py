"""Excursion product catalog exposed to the admin UI and schedule."""

from .routes import create_blueprint
from .schema import init_schema

__all__ = ["create_blueprint", "init_schema"]
