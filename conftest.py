"""Root conftest: makes a plain `pytest` work from any checkout directory name.

Nothing here affects the shipped plugin. Hermes imports the plugin through its
own loader and never reads this file.

The problem it solves:

The plugin root carries __init__.py, because that is the plugin entry point.
pytest therefore treats the checkout directory as a test Package and imports
that __init__.py using the DIRECTORY NAME as the module name. In a checkout
named "palate" that happens to work. Clone to "palate-review", "has space" or
"42dir" and the name is not a valid Python identifier, so pytest falls back to
importing the plugin as top-level "__init__" with no parent package, every
relative import inside it raises, and all 42 tests error at setup. Import mode
does not help: pytest imports a Package's __init__.py whatever the mode. Nor
does --rootdir in addopts, because rootdir is fixed in the early-config phase
before addopts are merged.

So load the package once under its real name, then answer to whatever name
pytest asks for.
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

if "palate" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "palate", _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)])
    assert _spec and _spec.loader, f"cannot load the plugin package from {_ROOT}"
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["palate"] = _pkg
    _spec.loader.exec_module(_pkg)

# Aliases for the names pytest may derive for the root Package, so it finds an
# already-imported module instead of re-executing __init__.py out of context.
for _alias in ("__init__", _ROOT.name):
    sys.modules.setdefault(_alias, sys.modules["palate"])
