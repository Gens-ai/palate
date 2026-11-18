"""Palate — your own record of what you watched, what you turned down, and why.

Three commitments, in order of importance:

  1. The ledger is yours. It is a local SQLite file, it exports to plain JSON,
     and nothing about your viewing leaves the machine. The only network calls
     are anonymous metadata lookups to TMDB for titles you ask about.
  2. Reasons beat ratings. A star says you disliked something; "too slow",
     "bailed at forty minutes", "not in the mood" say why, and only the last
     of those is about tonight rather than about you.
  3. It declines to guess. Below the evidence threshold the profile reports
     insufficient evidence and names the threshold, instead of inventing a
     preference from four data points.

Surfaces used: ctx.register_tool, ctx.register_skill. No hooks: Palate never
changes how any other session behaves, and only runs when it is called.
"""

from pathlib import Path

from . import schemas as _schemas
from . import tools as _tools

__version__ = "0.3.0"


def register(ctx):
    # Let the handlers read plugins.entries.palate.settings.*
    try:
        _tools._get_config = ctx.get_config
    except AttributeError:
        _tools._get_config = lambda _key, _default=None: None

    for schema in _schemas.ALL:
        handler = _tools.HANDLERS[schema["name"]]
        ctx.register_tool(
            name=schema["name"],
            toolset="palate",
            schema=schema,
            handler=handler,
        )

    skills_dir = Path(__file__).parent / "skills"
    if skills_dir.is_dir():
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                try:
                    ctx.register_skill(child.name, skill_md)
                except Exception:  # noqa: BLE001 - a skill that will not load must not break the plugin
                    pass
