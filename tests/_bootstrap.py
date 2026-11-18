"""Load the plugin as the package `palate`, whatever the checkout is called.

One place, used by the root conftest.py, tests/conftest.py, smoke.py and
live_check.py, because the same flaw bit two entry points independently.

Putting the plugin's PARENT directory on sys.path works only in a checkout
literally named "palate": clone to "palate-review" and `import palate` fails.
And pytest has a second version of the same problem, since the plugin root
carries __init__.py, so pytest treats the checkout as a test Package and
imports that __init__.py using the DIRECTORY NAME as the module name, falling
back to top-level "__init__" when the name is not a valid Python identifier,
which breaks every relative import inside it.

Loading by file location with an explicit package name removes the dependency
on the folder's name entirely.
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    """Import and return the plugin package, registering it as `palate`."""
    # plugins.plugin_storage / hermes_constants live in the Hermes install tree.
    hermes_tree = os.path.expanduser("~/.hermes/hermes-agent")
    if hermes_tree not in sys.path:
        sys.path.insert(0, hermes_tree)

    if "palate" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "palate", ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)])
        assert spec and spec.loader, f"cannot load the plugin package from {ROOT}"
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["palate"] = pkg
        spec.loader.exec_module(pkg)

    # Aliases for the names pytest may derive for the root Package, so it
    # finds an already-imported module instead of re-executing __init__.py
    # out of context.
    for alias in ("__init__", ROOT.name):
        sys.modules.setdefault(alias, sys.modules["palate"])

    return sys.modules["palate"]
