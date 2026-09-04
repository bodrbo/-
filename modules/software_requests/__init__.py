"""Internal software improvement requests submitted by staff."""

from .routes import create_blueprint
from .schema import init_schema

__all__ = ["create_blueprint", "init_schema"]
