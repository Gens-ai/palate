"""Root conftest: makes a plain `pytest` work from any checkout directory name.

Nothing here affects the shipped plugin. Hermes imports the plugin through its
own loader and never reads this file. See tests/_bootstrap.py for why it is
needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

from _bootstrap import load_plugin  # noqa: E402

load_plugin()
