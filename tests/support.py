"""Shared isolated application environment for integration tests."""

import atexit
import os
import tempfile


TEST_DIRECTORY = tempfile.TemporaryDirectory()
atexit.register(TEST_DIRECTORY.cleanup)
os.environ.setdefault("SECRET_KEY", "application-test-key")
os.environ["WORKHOURS_DB_PATH"] = os.path.join(TEST_DIRECTORY.name, "application-tests.db")

import app as application_module

application_module.app.config.update(TESTING=True, SECRET_KEY="application-test-key")
