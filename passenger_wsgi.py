"""WSGI entry point for Beget's Python hosting (Phusion Passenger).

Passenger looks for a module named exactly `passenger_wsgi.py` in the
application's root directory and imports an `application` callable from
it — this just points that at the Flask app object defined in app.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as application
