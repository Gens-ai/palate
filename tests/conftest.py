"""Test harness: a throwaway HERMES_HOME, so nothing here can touch real records.

Set before the plugin is imported, because plugin_db() resolves the home on
every call and the first call would otherwise land in the live profile.
"""

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_HOME = tempfile.mkdtemp(prefix="palate-test-home-")
os.environ["HERMES_HOME"] = _TMP_HOME

# plugins.plugin_storage / hermes_constants live in the Hermes install tree.
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))

# Register the plugin as the package name `palate` from its own directory,
# whatever that directory is called. Putting the parent on sys.path instead
# would make the tests pass only in a checkout literally named "palate" and
# fail to collect in a clone named anything else, which is what catalog CI
# and any contributor would hit.
_ROOT = Path(__file__).resolve().parents[1]
if "palate" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "palate", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    assert _spec and _spec.loader, f"cannot load the plugin package from {_ROOT}"
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["palate"] = _pkg
    _spec.loader.exec_module(_pkg)


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
