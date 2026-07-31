"""WSGI entry point for Beget's Python hosting (Phusion Passenger).

Passenger looks for a module named exactly `passenger_wsgi.py` in the
application's root directory and imports an `application` callable from
it — this just points that at the Flask app object defined in app.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


DOTENV_DEBUG = {}


def _load_dotenv():
    """Beget's shared hosting has no environment-variables UI reachable by
    Passenger, so secrets (YOOKASSA_*, etc.) live in a .env file next to
    this one instead — load it into os.environ before app.py reads them.
    Real environment variables (if ever set some other way) still win."""
    this_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(this_dir, ".env")
    DOTENV_DEBUG["this_dir"] = this_dir
    DOTENV_DEBUG["cwd"] = os.getcwd()
    DOTENV_DEBUG["env_path"] = env_path
    DOTENV_DEBUG["exists"] = os.path.exists(env_path)
    if not DOTENV_DEBUG["exists"]:
        return
    loaded = []
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
            loaded.append(key)
    DOTENV_DEBUG["loaded_keys"] = loaded


_load_dotenv()

from app import app as application

application.config["DOTENV_DEBUG"] = dict(DOTENV_DEBUG)
