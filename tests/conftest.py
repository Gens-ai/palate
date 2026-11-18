"""Test harness: a throwaway HERMES_HOME, so nothing here can touch real records.

Set before the plugin is imported, because plugin_db() resolves the home on
every call and the first call would otherwise land in the live profile.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_HOME = tempfile.mkdtemp(prefix="palate-test-home-")
os.environ["HERMES_HOME"] = _TMP_HOME

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import load_plugin  # noqa: E402

# Registers the plugin as `palate` from its own directory, whatever that
# directory is called. See _bootstrap.py.
load_plugin()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP_HOME, ignore_errors=True)


@pytest.fixture()
def con():
    """A clean ledger per test."""
    data = Path(_TMP_HOME) / "plugin-data" / "palate"
    if data.is_dir():
        for f in data.glob("data.db*"):
            f.unlink(missing_ok=True)
    from palate import storage
    c = storage.connect()
    yield c
    c.close()


@pytest.fixture()
def tmp_home():
    return _TMP_HOME
