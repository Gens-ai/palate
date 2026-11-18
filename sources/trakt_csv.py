"""Trakt CSV export.

Trakt has a real API, but using it costs an OAuth app, a token refresh path
and a vendor that can revoke access. A one-time CSV read gets the same history
with none of that, which is why this is an import rather than a sync: the
local ledger is the source of truth, and Trakt is just a way to seed it.

Column names vary between Trakt's own export and the third-party exporters
people actually use, so aliases are accepted and anything ambiguous is
reported rather than guessed at silently.
"""

from . import base

NAME = "trakt_csv"
PROVIDES = ("title", "year", "rating", "at", "medium")

TITLE_KEYS = ("title", "show title", "movie title", "name")
YEAR_KEYS = ("year", "released", "show year")
DATE_KEYS = ("watched_at", "watched at", "last_watched_at", "rated_at", "rated at", "date")
RATING_KEYS = ("rating", "my rating", "user rating")
TYPE_KEYS = ("type", "media type", "kind")


def requirements():
    return {
        "path": "Path to a Trakt CSV export (trakt.tv/settings/data, or any of the "
                "community exporters).",
    }


def _first(row, keys):
    for k in keys:
        v = row.get(k)
        if v:
            return v
    return ""


def _parse(cfg):
    rows, columns = base.read_csv_guarded(
        cfg["path"],
        require_all=(),
        require_any=TITLE_KEYS,
        label="Trakt")

    records, assumed = [], []
    for row in rows:
        title = _first(row, TITLE_KEYS)
        if not title:
            continue

        medium = "film"
        kind = _first(row, TYPE_KEYS).lower()
        if kind in ("show", "episode", "season", "tv"):
            medium = "tv"
        elif not kind and _first(row, ("show title", "show year")):
            medium = "tv"

        rating = None
        raw = _first(row, RATING_KEYS)
        if raw:
            try:
                n = float(raw)
                if 0 < n <= 10:
                    rating = int(round(n))
                elif 0 < n <= 5:
                    rating = int(round(n * 2))
                    assumed.append({"title": title, "value": raw,
                                    "assumption": "read as a /5 rating and doubled"})
            except ValueError:
                pass

        records.append({
            "title": title,
            "year": base.parse_year(_first(row, YEAR_KEYS)),
            "medium": medium,
            "verdict": "seen",
            "rating": rating,
            "at": base.parse_date_epoch(_first(row, DATE_KEYS)),
            "source_id": row.get("trakt_id") or row.get("imdb_id") or None,
        })
    return records, columns, assumed


def probe(cfg):
    records, columns, assumed = _parse(cfg)
    recognised = [c for c in columns
                  if c.lower() in set(TITLE_KEYS + YEAR_KEYS + DATE_KEYS + RATING_KEYS + TYPE_KEYS)]
    return {
        "source": NAME,
        "columns_recognised": recognised,
        "records_parsed": len(records),
        "preview": records[:5],
        "assumed_units": assumed,
        "wrote_nothing": True,
    }


def fetch(cfg):
    records, _columns, assumed = _parse(cfg)
    return records, assumed
