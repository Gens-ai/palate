"""The verdict ledger: schema and accessors.

All state lives in <hermes home>/plugin-data/palate/data.db via plugin_db(),
never in the install tree, which `hermes plugins update` git-pulls over.

plugin_db() hands back a NEW connection per call, so every helper here opens
and commits its own. Never hold one open across a call into another helper.
"""

import json
import sqlite3
import time

from . import constants as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS works (
    id          INTEGER PRIMARY KEY,
    medium      TEXT NOT NULL,
    source      TEXT NOT NULL,          -- 'tmdb', 'manual', 'letterboxd_csv', ...
    source_id   TEXT,                   -- NULL for manual entries
    title       TEXT NOT NULL,
    year        INTEGER,
    runtime_min INTEGER,
    added_at    REAL NOT NULL,
    UNIQUE (medium, source, source_id)
);
CREATE INDEX IF NOT EXISTS works_title ON works (medium, title, year);

-- Feature vectors: keywords, crew and genres. The picker runs over these
-- because they need no calibration, unlike the axis scores below.
CREATE TABLE IF NOT EXISTS work_tags (
    work_id INTEGER NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    kind    TEXT NOT NULL,              -- 'keyword' | 'crew' | 'genre'
    value   TEXT NOT NULL,
    weight  REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (work_id, kind, value)
);
CREATE INDEX IF NOT EXISTS work_tags_value ON work_tags (kind, value);

CREATE TABLE IF NOT EXISTS verdicts (
    id          INTEGER PRIMARY KEY,
    work_id     INTEGER NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    verdict     TEXT NOT NULL,          -- see constants.VERDICTS
    rating      INTEGER,                -- optional 1-10
    at          REAL NOT NULL,
    stopped_at_min INTEGER,             -- where they bailed, if they did
    where_watched  TEXT,                -- service name, or NULL
    note        TEXT,                   -- free text, always optional
    source      TEXT NOT NULL DEFAULT 'manual',
    user_authored INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS verdicts_work ON verdicts (work_id);

CREATE TABLE IF NOT EXISTS verdict_reasons (
    verdict_id INTEGER NOT NULL REFERENCES verdicts (id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,           -- closed vocabulary, constants.REASONS
    PRIMARY KEY (verdict_id, reason)
);

-- Axis scores carry their provenance, so a model judgement and a correction
-- derived from the user's own verdict stay distinguishable.
CREATE TABLE IF NOT EXISTS work_axes (
    work_id    INTEGER NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    axis       TEXT NOT NULL,
    score      REAL NOT NULL,
    provenance TEXT NOT NULL,
    at         REAL NOT NULL,
    PRIMARY KEY (work_id, axis, provenance)
);

CREATE TABLE IF NOT EXISTS services (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL UNIQUE,
    kind   TEXT NOT NULL,               -- constants.SERVICE_KINDS
    region TEXT NOT NULL DEFAULT 'US',
    active INTEGER NOT NULL DEFAULT 1
);

-- What the user said they wanted, at the moment they asked. Kept separate
-- from verdicts because mood is not taste.
CREATE TABLE IF NOT EXISTS state_notes (
    id   INTEGER PRIMARY KEY,
    at   REAL NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability_cache (
    work_id   INTEGER NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    region    TEXT NOT NULL,
    payload   TEXT NOT NULL,            -- raw TMDB watch/providers result
    fetched_at REAL NOT NULL,
    PRIMARY KEY (work_id, region)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Open the plugin's own database, applying the schema on first use."""
    from plugins.plugin_storage import plugin_db  # imported late: keeps this module standalone-importable

    con = plugin_db(C.PLUGIN_NAME)
    con.executescript(SCHEMA)
    con.commit()
    con.row_factory = sqlite3.Row
    _invalidate_stale_tags(con)
    return con


def _invalidate_stale_tags(con):
    """Drop the tag cache when the tagging rules have changed under it.

    The cache exists so a work is only hydrated once, which also means a
    change to weights or the keyword stoplist would never reach a ledger that
    already has tags. Clearing is safe: tags are derived data and refetch on
    the next pick. Verdicts, reasons and services are untouched.
    """
    try:
        row = con.execute("SELECT value FROM settings WHERE key='tag_rules_version'").fetchone()
        stored = int(json.loads(row["value"])) if row else 0
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        stored = 0
    if stored >= C.TAG_RULES_VERSION:
        return
    con.execute("DELETE FROM work_tags")
    con.execute("INSERT INTO settings (key, value) VALUES ('tag_rules_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(C.TAG_RULES_VERSION),))
    con.commit()


# ---------------------------------------------------------------------------
# Works
# ---------------------------------------------------------------------------

def _clean_int(value, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def upsert_work(con, *, medium, title, source="manual", source_id=None,
                year=None, runtime_min=None):
    """Insert or find a work. Returns its row id.

    Plausibility bounds come from constants and are applied here, once, so
    every ingest route agrees on what a valid year or runtime is.
    """
    if medium not in C.MEDIA:
        raise ValueError(f"unknown medium {medium!r}")
    title = (title or "").strip()
    if not title:
        raise ValueError("title is required")
    year = _clean_int(year, C.YEAR_MIN_BOUND, C.YEAR_MAX_BOUND)
    runtime_min = _clean_int(runtime_min, C.RUNTIME_MIN_BOUND, C.RUNTIME_MAX_BOUND)

    if source_id is not None:
        row = con.execute(
            "SELECT id FROM works WHERE medium=? AND source=? AND source_id=?",
            (medium, source, str(source_id)),
        ).fetchone()
    else:
        row = None

    # Fall back to title identity across sources. Without this, a title the user
    # logged by hand (source='manual', no source_id) and the same title arriving
    # from an import (with a source_id) become two works, the duplicate carries
    # no history, and the protection against overwriting hand-entered verdicts
    # never engages because it is looking at the wrong row.
    if row is None:
        if year is not None:
            row = con.execute(
                "SELECT id FROM works WHERE medium=? AND lower(title)=lower(?) "
                "AND (year=? OR year IS NULL)", (medium, title, year),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT id FROM works WHERE medium=? AND lower(title)=lower(?)",
                (medium, title),
            ).fetchone()

    if row:
        wid = row["id"]
        # fill in blanks a later, richer source supplied
        con.execute(
            "UPDATE works SET year=COALESCE(year,?), runtime_min=COALESCE(runtime_min,?) "
            "WHERE id=?", (year, runtime_min, wid),
        )
        # Promote a manual stub to a canonical id once one is known, so tag and
        # availability lookups work for it later. Ignored if it would collide.
        if source_id is not None:
            try:
                con.execute(
                    "UPDATE works SET source=?, source_id=? WHERE id=? AND source_id IS NULL",
                    (source, str(source_id), wid))
            except sqlite3.IntegrityError:
                pass
        con.commit()
        return wid

    cur = con.execute(
        "INSERT INTO works (medium, source, source_id, title, year, runtime_min, added_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (medium, source, None if source_id is None else str(source_id),
         title, year, runtime_min, time.time()),
    )
    con.commit()
    return cur.lastrowid


def set_tags(con, work_id, tags):
    """tags: iterable of (kind, value, weight)."""
    con.executemany(
        "INSERT OR REPLACE INTO work_tags (work_id, kind, value, weight) VALUES (?,?,?,?)",
        [(work_id, k, str(v)[:120], float(w)) for k, v, w in tags],
    )
    con.commit()


def tags_for(con, work_id):
    return [(r["kind"], r["value"], r["weight"]) for r in con.execute(
        "SELECT kind, value, weight FROM work_tags WHERE work_id=?", (work_id,))]


def find_work(con, medium, title, year=None):
    sql = "SELECT * FROM works WHERE medium=? AND lower(title)=lower(?)"
    args = [medium, title.strip()]
    if year:
        sql += " AND year=?"
        args.append(int(year))
    return con.execute(sql + " LIMIT 1", args).fetchone()


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def add_verdict(con, *, work_id, verdict, reasons=(), rating=None, note=None,
                where_watched=None, stopped_at_min=None, source="manual",
                at=None, protect_user_authored=True):
    """Record a verdict. Returns (verdict_id, status).

    status is 'inserted' or 'skipped_existing'. An automated source never
    overwrites something the user entered by hand: the importer has to opt out
    of that protection, not remember to opt in.
    """
    if verdict not in C.VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    bad = [r for r in reasons if r not in C.REASONS]
    if bad:
        raise ValueError(f"unknown reason(s): {', '.join(sorted(bad))}")
    rating = _clean_int(rating, C.RATING_MIN, C.RATING_MAX)
    user_authored = 1 if source == "manual" else 0

    if protect_user_authored and source != "manual":
        clash = con.execute(
            "SELECT id FROM verdicts WHERE work_id=? AND user_authored=1 LIMIT 1",
            (work_id,),
        ).fetchone()
        if clash:
            return clash["id"], "skipped_existing"

    cur = con.execute(
        "INSERT INTO verdicts (work_id, verdict, rating, at, stopped_at_min, "
        "where_watched, note, source, user_authored) VALUES (?,?,?,?,?,?,?,?,?)",
        (work_id, verdict, rating, float(at) if at else time.time(),
         _clean_int(stopped_at_min, 0, C.RUNTIME_MAX_BOUND),
         where_watched, note, source, user_authored),
    )
    vid = cur.lastrowid
    con.executemany(
        "INSERT OR IGNORE INTO verdict_reasons (verdict_id, reason) VALUES (?,?)",
        [(vid, r) for r in dict.fromkeys(reasons)],
    )
    con.commit()
    _apply_axis_evidence(con, work_id, reasons)
    return vid, "inserted"


def _apply_axis_evidence(con, work_id, reasons):
    """Let a verdict correct the work's axis scores, not just the profile.

    "Too slow" about something scored mid-pace is a labelled error on the
    work's row. Stored under its own provenance so it never masquerades as a
    judgement made from the synopsis.
    """
    now = time.time()
    rows = []
    for r in reasons:
        spec = C.REASONS.get(r) or {}
        axis, direction = spec.get("axis"), spec.get("direction", 0)
        if axis and direction:
            rows.append((work_id, axis, float(direction), "user_verdict", now))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO work_axes (work_id, axis, score, provenance, at) "
            "VALUES (?,?,?,?,?)", rows)
        con.commit()


def verdicts_with_tags(con, medium=None):
    """Every taste-bearing verdict, with the work's tags attached.

    State-only reasons are excluded from polarity, which is the trait/state
    split doing its job: a film you skipped because you were tired is not
    evidence that you dislike it.
    """
    sql = ("SELECT v.id, v.work_id, v.verdict, v.rating, w.medium, w.title, w.year "
           "FROM verdicts v JOIN works w ON w.id = v.work_id")
    args = []
    if medium:
        sql += " WHERE w.medium=?"
        args.append(medium)
    out = []
    for r in con.execute(sql, args):
        reasons = [x["reason"] for x in con.execute(
            "SELECT reason FROM verdict_reasons WHERE verdict_id=?", (r["id"],))]
        pol = _polarity(r["verdict"], r["rating"], reasons)
        if pol == 0:
            continue
        out.append({
            "work_id": r["work_id"], "title": r["title"], "year": r["year"],
            "medium": r["medium"], "polarity": pol, "reasons": reasons,
            "rating": r["rating"], "verdict_id": r["id"],
            "tags": tags_for(con, r["work_id"]),
        })
    return out


def _polarity(verdict, rating, reasons):
    """+1 liked, -1 disliked, 0 says nothing about taste."""
    taste = [C.REASONS[r]["polarity"] for r in reasons if r in C.REASONS and C.REASONS[r]["polarity"]]
    if taste:
        return 1 if sum(taste) > 0 else -1
    if rating is not None:
        if rating >= 7:
            return 1
        if rating <= 4:
            return -1
        return 0
    if verdict == "bailed":
        return -1
    # "seen" with no rating and no reason is presence, not approval.
    return 0


def seen_work_ids(con, medium=None):
    sql = ("SELECT DISTINCT v.work_id FROM verdicts v JOIN works w ON w.id=v.work_id "
           "WHERE v.verdict IN ('seen','bailed')")
    args = []
    if medium:
        sql += " AND w.medium=?"
        args.append(medium)
    return {r[0] for r in con.execute(sql, args)}


def counts(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    return {
        "works": q("SELECT COUNT(*) FROM works"),
        "verdicts": q("SELECT COUNT(*) FROM verdicts"),
        "taste_bearing_verdicts": len(verdicts_with_tags(con)),
        "services": q("SELECT COUNT(*) FROM services WHERE active=1"),
        "tagged_works": q("SELECT COUNT(DISTINCT work_id) FROM work_tags"),
    }


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def set_service(con, name, kind, region="US", active=True):
    if kind not in C.SERVICE_KINDS:
        raise ValueError(f"unknown service kind {kind!r}; expected one of {', '.join(C.SERVICE_KINDS)}")
    con.execute(
        "INSERT INTO services (name, kind, region, active) VALUES (?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, region=excluded.region, "
        "active=excluded.active",
        (name.strip(), kind, region.upper()[:4], 1 if active else 0))
    con.commit()


def list_services(con, active_only=True):
    sql = "SELECT name, kind, region, active FROM services"
    if active_only:
        sql += " WHERE active=1"
    return [dict(r) for r in con.execute(sql + " ORDER BY kind, name")]


def region(con, default="US"):
    rows = list_services(con)
    return rows[0]["region"] if rows else default


# ---------------------------------------------------------------------------
# Settings / state notes / export
# ---------------------------------------------------------------------------

def put_setting(con, key, value):
    con.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))
    con.commit()


def get_setting(con, key, default=None):
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def add_state_note(con, note):
    con.execute("INSERT INTO state_notes (at, note) VALUES (?,?)", (time.time(), note[:500]))
    con.commit()


def export_all(con):
    """Everything, as plain JSON. The ledger outlives the plugin or it is not yours."""
    def rows(sql):
        return [dict(r) for r in con.execute(sql)]
    return {
        "format": "palate-export",
        "version": 1,
        "exported_at": time.time(),
        "works": rows("SELECT * FROM works"),
        "work_tags": rows("SELECT * FROM work_tags"),
        "work_axes": rows("SELECT * FROM work_axes"),
        "verdicts": rows("SELECT * FROM verdicts"),
        "verdict_reasons": rows("SELECT * FROM verdict_reasons"),
        "services": rows("SELECT * FROM services"),
        "state_notes": rows("SELECT * FROM state_notes"),
    }
