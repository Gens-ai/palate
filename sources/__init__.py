"""Source registry. Adding a bridge is a module and a line in REGISTRY."""

from . import base, letterboxd_csv, trakt_csv

REGISTRY = {
    letterboxd_csv.NAME: letterboxd_csv,
    trakt_csv.NAME: trakt_csv,
}


def get(name):
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown source {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        ) from None


def describe():
    return {
        name: {"provides": mod.PROVIDES, "requirements": mod.requirements()}
        for name, mod in sorted(REGISTRY.items())
    }


__all__ = ["REGISTRY", "get", "describe", "base"]
