"""Letterboxd CSV export.

Letterboxd's API has been in closed beta for years, so every "Letterboxd
integration" in the wild is a scraper. The export route sidesteps that
entirely: the user downloads their own data from
letterboxd.com/settings/data/ and this reads the file. No auth, no API, no
scraping, nothing to break when the site changes.

Handles watched.csv, ratings.csv and diary.csv, which share a column shape.
"""

from . import base

NAME = "letterboxd_csv"
PROVIDES = ("title", "year", "rating", "at", "rewatch")

# Star ratings are 0.5-5.0; Palate stores 1-10.
STAR_TO_TEN = 2.0


def requirements():
    return {
        "path": "Path to watched.csv, ratings.csv or diary.csv from your Letterboxd "
                "data export (letterboxd.com/settings/data/).",
    }


def _parse(cfg):
    rows, columns = base.read_csv_guarded(
        cfg["path"],
        require_all=("Name",),
        require_any=("Letterboxd URI", "Year"),
        label="Letterboxd")

    records, assumed = [], []
    for row in rows:
        title = row.get("name", "")
        if not title:
            continue
        rating = None
        raw = row.get("rating", "")
        if raw:
            try:
                stars = float(raw)
                if 0.0 < stars <= 5.0:
                    rating = int(round(stars * STAR_TO_TEN))
                elif 5.0 < stars <= 10.0:
                    # Already a /10 value: some third-party exporters do this.
                    rating = int(round(stars))
                    assumed.append({"title": title, "value": raw,
                                    "assumption": "read as a /10 rating, not stars"})
            except ValueError:
                pass

        at = base.parse_date_epoch(row.get("watched date") or row.get("date") or "")
        records.append({
            "title": title,
            "year": base.parse_year(row.get("year", "")),
            "medium": "film",
            "verdict": "seen",
            "rating": rating,
            "at": at,
            "source_id": row.get("letterboxd uri") or None,
            "note": None,
        })
    return records, columns, assumed


def probe(cfg):
    """Parse and report, writing nothing. Asserted side-effect-free by the tests."""
    records, columns, assumed = _parse(cfg)
    return {
        "source": NAME,
        "columns_recognised": [c for c in columns if c.lower() in
                               ("name", "year", "rating", "date", "watched date",
                                "letterboxd uri", "rewatch", "tags")],
        "records_parsed": len(records),
        "preview": records[:5],
        "assumed_units": assumed,
        "wrote_nothing": True,
    }


def fetch(cfg):
    records, _columns, assumed = _parse(cfg)
    return records, assumed
