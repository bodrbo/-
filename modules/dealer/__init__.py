"""Дилерский центр: selling boats and motors (not services) — orders built
like the tuning-center orders, but the lines are units taken from the boat
and motor catalogs, with a price list, payments and a status flow."""

from .routes import create_dealer_blueprint
from .schema import init_schema

__all__ = ["create_dealer_blueprint", "init_schema"]
