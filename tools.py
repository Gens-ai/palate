"""Tool handlers. Each takes (params, **kwargs) and returns a JSON string."""

import json
import os
import time

from . import constants as C
from . import picker
from . import sources
from . import storage
from . import taste_profile
from . import tmdb
from .sources import base as source_base

# Set by register(); lets handlers read plugins.entries.palate.settings.*
_get_config = None

# Every candidate in the pool gets examined on the first pass: a budget smaller
# than the pool means the best match can be the one nobody looked at, which
# makes a cold cache the worst experience rather than just a slower one.
# Tags and availability are cached, so repeat picks cost almost nothing.
CANDIDATE_POOL = 24
# Kept equal to the pool: the detail budget must cover every candidate or the
# best match can be the one nobody examined. Both now scale together via
# pool_size, computed per call from the number of generators in play.
DETAIL_BUDGET = CANDIDATE_POOL
# Each generator needs a real share, or round-robin fairness starts discarding
# the best match: with a fixed pool of 24 and 8 groups every group gets 3
# slots, and a 9.7-scoring match sitting at position 4 of its pivot's page is
# dropped in favour of spread. So the pool grows with the number of groups,
# bounded so a pick cannot become unboundedly slow on a cold cache.
PER_GROUP_MIN = 4
MAX_POOL = 48
# Separate budget for the user's own verdict works, which must be tagged or
# there is nothing for candidates to be similar TO.
VERDICT_TAG_BUDGET = 12
# How many liked works may seed the pool. One TMDB recommendations call per
# pivot, and the pool is split evenly between them, so this is both a cost
# ceiling and the width of the search.
PIVOT_LIMIT = 8
# Canon generation: widely-seen, well-regarded titles, used to reach films that
# no neighbour-of-what-you-liked search can ever surface.
CANON_MIN_VOTES = 3000
CANON_MIN_RATING = 7.5
# A share of the pool reserved for availability-first candidates, so access is
# a filter of first resort rather than only a filter of last resort.
AVAIL_FIRST_SHARE = 0.34


def _interleave(groups):
    """Round-robin merge, so every pivot actually reaches the pool.

    Concatenating the groups and truncating gives the first pivot the whole
    pool: TMDB returns a full page of about 20 per pivot against a pool of 24,
    so pivot 1 took ~20 slots, pivot 2 took the remainder and every pivot
    after that got NOTHING. The ledger looked like it was searching six
    favourites and was really searching one, which is invisible from the
    output because the picks it returns are all legitimately well scored.
    Taking one candidate from each pivot in turn costs the same calls and
    spends the pool evenly.
    """
    out = []
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g):
                out.append(g[i])
    return out


def _ok(payload):
    return json.dumps(payload, default=str)


def _err(message, **extra):
    return json.dumps({"ok": False, "error": message, **extra})


def _api_key():
    """Read the TMDB key through Hermes's own config route, and only that route.

    Deliberately no os.environ fallback. Admission rule 11 names bare
    os.environ as the wrong way to reach a credential, because under profile
    multiplexing it can hand back another profile's key; and an undeclared env
    read contradicts the empty requires_env this plugin ships. The config route
    already works, so the fallback was convenience buying a capability.
    """
    if _get_config is None:
        return None
    return _get_config("tmdb_api_key")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _ensure_tags(con, api_key, work_id, medium, tmdb_id, allow_fetch=True):
    """Tags for a work, fetching from TMDB only when we do not already have them."""
    tags = storage.tags_for(con, work_id)
    if tags or not allow_fetch or not tmdb_id:
        return tags, False
    det = tmdb.details(api_key, medium, tmdb_id)
    rows = tmdb.tags_from_details(det)
    if rows:
        storage.set_tags(con, work_id, rows)
    if det.get("runtime_min"):
        con.execute("UPDATE works SET runtime_min=COALESCE(runtime_min,?) WHERE id=?",
                    (det["runtime_min"], work_id))
        con.commit()
    return storage.tags_for(con, work_id), True


def _resolve(con, api_key, medium, title, year=None):
    """Find or create a work, preferring a TMDB match so tags are available."""
    row = storage.find_work(con, medium, title, year)
    if row:
        return row["id"], row["source_id"], dict(row)
    hits = []
    try:
        hits = tmdb.search(api_key, medium, title, year)
    except tmdb.TmdbError:
        pass
    if hits:
        h = hits[0]
        wid = storage.upsert_work(con, medium=medium, title=h["title"], year=h["year"],
                                  source="tmdb", source_id=h["tmdb_id"])
        return wid, str(h["tmdb_id"]), h
    wid = storage.upsert_work(con, medium=medium, title=title, year=year, source="manual")
    return wid, None, {"title": title, "year": year}


# ---------------------------------------------------------------------------
# palate_pick
# ---------------------------------------------------------------------------

def _resolve_availability(params):
    """Single owner of the availability mapping.

    `require_free` predates the three-state mode; honour it so an older caller
    keeps its constraint instead of silently losing it, and fall back to the
    default on anything unrecognised rather than filtering by a typo.
    """
    mode = params.get("availability")
    if mode in C.AVAILABILITY_MODES:
        return mode
    return "free" if params.get("require_free") else C.DEFAULT_AVAILABILITY


def palate_pick(params, **_kwargs):
    medium = params.get("medium") or "film"
    limit = int(params.get("limit") or 5)
    mode = _resolve_availability(params)
    state = {
        "max_runtime_min": params.get("max_runtime_min"),
        "max_load": params.get("max_load"),
        "max_bleakness": params.get("max_bleakness"),
        "availability": mode,
    }
    try:
        api_key = _api_key()
        con = storage.connect()
        verdicts = storage.verdicts_with_tags(con, medium=medium)
        seen = storage.seen_work_ids(con, medium=medium)

        # Hydrate tags for the user's OWN works first. Without this the net is
        # empty, every candidate scores 0.0 and every explanation degrades to
        # "cold guess" while looking like a working recommender: the verdicts
        # are there, the candidates are tagged, and the one side that actually
        # needs tags never got them. Cached, so this cost falls away.
        hydrated = 0
        for v in verdicts:
            if v["tags"] or hydrated >= VERDICT_TAG_BUDGET:
                continue
            row = con.execute("SELECT source_id FROM works WHERE id=?",
                              (v["work_id"],)).fetchone()
            if not (row and row["source_id"]):
                continue
            try:
                _tags, did_fetch = _ensure_tags(con, api_key, v["work_id"], medium,
                                                row["source_id"])
                hydrated += 1 if did_fetch else 0
            except tmdb.TmdbError:
                continue
        if hydrated:
            verdicts = storage.verdicts_with_tags(con, medium=medium)

        services = storage.list_services(con)
        free_names = [s["name"] for s in services if s["kind"] in C.FREE_AT_POINT_OF_USE]
        paid_names = [s["name"] for s in services if s["kind"] in C.PAID_PER_TITLE]
        service_names = [s["name"] for s in services]
        state["has_services"] = bool(free_names or paid_names)
        region = storage.region(con)

        note = " ".join(str(params.get(k)) for k in ("seed_title",) if params.get(k))
        if note:
            storage.add_state_note(con, note)

        # ---- candidate generation -------------------------------------------------
        # Initialised before the branch: the seeded path does not set it, and
        # the result block below reads it unconditionally.
        unmatched_services = []
        pool_size = CANDIDATE_POOL
        pivots_used, pivots_skipped = [], []
        candidates, how = [], ""
        seed = params.get("seed_title")
        if seed:
            hits = tmdb.search(api_key, medium, seed)
            if not hits:
                return _err(f"TMDB has nothing matching {seed!r} as {medium}.")
            candidates = tmdb.recommendations(api_key, medium, hits[0]["tmdb_id"])
            how = f"similar to {hits[0]['title']}"
        else:
            strategy = params.get("strategy") or "mixed"
            groups, notes = [], []

            if strategy in ("taste", "mixed"):
                liked = [v for v in verdicts if v["polarity"] > 0]
                # Order pivots by how strong the evidence is, NOT by recency.
                # A plain [-N:] slice silently evicts the oldest favourites, so
                # logging a seventh film dropped the pivot carrying the single
                # best match in the ledger and the loss was invisible: the
                # answer still looked well scored, just without its best item.
                # loved_it outranks a bare rating, and ties break on recency.
                def _pivot_rank(v):
                    loved = 1 if "loved_it" in (v.get("reasons") or []) else 0
                    return (-loved, -(v.get("rating") or 0), -(v.get("verdict_id") or 0))

                liked.sort(key=_pivot_rank)
                pivots, skipped = [], []
                for v in liked:
                    row = con.execute("SELECT source, source_id FROM works WHERE id=?",
                                      (v["work_id"],)).fetchone()
                    if not (row and row["source"] == "tmdb" and row["source_id"]):
                        continue
                    if len(pivots) < PIVOT_LIMIT:
                        pivots.append((row["source_id"], v["title"]))
                    else:
                        skipped.append(v["title"])
                pivots_used = [t for _, t in pivots]
                pivots_skipped = skipped
                for tmdb_id, title in pivots:
                    try:
                        recs = tmdb.recommendations(api_key, medium, tmdb_id)
                    except tmdb.TmdbError:
                        continue
                    for r in recs:
                        r["from_pivot"] = title
                    if recs:
                        groups.append(recs)
                if groups:
                    notes.append(f"round-robin across {len(groups)} works you rated well")

            if strategy in ("canon", "mixed"):
                # Reaches what similarity structurally cannot: a widely-seen
                # classic is not a neighbour of anything in a small ledger, so
                # without this generator it can never appear at all, however
                # available it is.
                try:
                    canon = tmdb.discover(
                        api_key, medium, min_votes=CANON_MIN_VOTES,
                        min_rating=CANON_MIN_RATING, sort_by="vote_count.desc",
                        max_runtime=state["max_runtime_min"])
                    for r in canon:
                        r["from_pivot"] = "widely seen"
                    if canon:
                        groups.append(canon)
                        notes.append("plus widely-seen titles you have no verdict on")
                except tmdb.TmdbError:
                    pass

            if strategy in ("available", "mixed") and free_names:
                # Availability as a filter of FIRST resort. The similarity
                # generator produces candidates and access is only checked
                # afterwards, so with few services most of the pool is
                # discarded; asking TMDB to filter server-side spends the pool
                # on things that can actually be watched.
                try:
                    ids, unmatched = tmdb.provider_ids(api_key, medium, free_names,
                                                       watch_region=region)
                    if ids:
                        avail = tmdb.discover(
                            api_key, medium, with_providers=list(ids.values()),
                            watch_region=region, monetization="flatrate|free|ads",
                            min_votes=200, sort_by="vote_count.desc",
                            max_runtime=state["max_runtime_min"])
                        for r in avail:
                            r["from_pivot"] = "free on your services"
                        if avail:
                            groups.append(avail)
                            notes.append(f"plus titles free on {len(ids)} of your services")
                    if unmatched:
                        unmatched_services = unmatched
                except tmdb.TmdbError:
                    pass

            candidates = _interleave(groups)
            if candidates:
                how = ", ".join(notes) + ", each contributing to the pool"
                pool_size = min(MAX_POOL, max(CANDIDATE_POOL,
                                              PER_GROUP_MIN * len(groups)))
            else:
                candidates = tmdb.discover(
                    api_key, medium, max_runtime=state["max_runtime_min"])
                how = "cold start: popular titles, since the ledger has nothing to lean on yet"

        # dedupe, drop anything already in the ledger as seen. Keep every pivot
        # that suggested a title: being recommended by three favourites at once
        # is a stronger signal than being recommended by one, and it is also
        # what the diversity cap needs to avoid six variations of one film.
        uniq, by_id = [], {}
        for c in candidates:
            pivot = c.pop("from_pivot", None)
            prev = by_id.get(c["tmdb_id"])
            if prev is not None:
                if pivot and pivot not in prev["from_pivots"]:
                    prev["from_pivots"].append(pivot)
                continue
            c["from_pivots"] = [pivot] if pivot else []
            by_id[c["tmdb_id"]] = c
            uniq.append(c)
        candidates = uniq[:pool_size]

        # ---- hydrate ------------------------------------------------------------
        fetched, enriched = 0, []
        for c in candidates:
            wid = storage.upsert_work(con, medium=medium, title=c["title"], year=c["year"],
                                      source="tmdb", source_id=c["tmdb_id"])
            if wid in seen:
                continue
            tags, did_fetch = _ensure_tags(con, api_key, wid, medium, c["tmdb_id"],
                                           allow_fetch=fetched < pool_size)
            fetched += 1 if did_fetch else 0
            row = con.execute("SELECT runtime_min FROM works WHERE id=?", (wid,)).fetchone()

            free_on, rent_on, rent_on_yours = [], [], []
            if service_names:
                try:
                    avail, _cached = tmdb.cached_availability(
                        con, api_key, wid, medium, c["tmdb_id"], region)
                    free_on = tmdb.free_on(avail, free_names)
                    rent_on_yours = tmdb.paid_on(avail, paid_names)
                    rent_on = (avail.get("rent") or [])[:3]
                except tmdb.TmdbError:
                    pass

            enriched.append({**c, "work_id": wid, "tags": tags,
                             "runtime_min": row["runtime_min"] if row else None,
                             "free_on": free_on, "rent_on": rent_on,
                             "rent_on_yours": rent_on_yours})

        picks, shelved = picker.rank(con, enriched, verdicts, state=state,
                                     seen_ids=seen, limit=limit)
        prof = taste_profile.build(verdicts)
        _idf, corpus = picker.tag_idf(con)
        return _ok({
            "ok": True,
            "candidate_strategy": how,
            "pivots": {
                "used": pivots_used or None,
                "not_consulted": pivots_skipped or None,
                "limit": PIVOT_LIMIT,
            } if (pivots_used or pivots_skipped) else None,
            "picks": picks,
            "shelved_for_another_night": shelved,
            "ledger": storage.counts(con),
            "scoring": {
                "rarity_weighted": bool(_idf),
                "tagged_corpus": corpus,
                "note": ("Common tags are discounted by how many works carry them, so a "
                         "shared director outweighs a shared genre."
                         if _idf else
                         f"Flat tag weights: only {corpus} tagged works, "
                         f"{picker.MIN_DOCS_FOR_IDF} needed before rarity can be estimated."),
            },
            "profile_status": prof["message"],
            "availability": {
                "mode": mode,
                "free_services": free_names or None,
                "rental_services_you_accept": paid_names or None,
                "unrecognised_services": unmatched_services or None,
                "note": ("Prices are not available: TMDB reports which providers rent a "
                         "title but not what they charge, so a rental is named, never priced."
                         if mode != "free" else
                         "Only things free at point of use. Pass availability='free_or_rent' "
                         "to include rentals from providers the user has declared."),
            },
            "services_configured": service_names or None,
            "region": region,
            "attribution": tmdb.ATTRIBUTION,
            "guidance": ("Show each pick's 'why'. If 'shelved_for_another_night' is "
                         "non-empty, offer those as other-night options with their blocker. "
                         "Record whatever the user says back with palate_verdict."),
        })
    except tmdb.NotConfigured as exc:
        return _err(str(exc), needs_setup=True)
    except tmdb.TmdbError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool must not take the turn down
        return _err(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# palate_verdict / palate_seen
# ---------------------------------------------------------------------------

def palate_verdict(params, **_kwargs):
    try:
        title = (params.get("title") or "").strip()
        verdict = params.get("verdict")
        if not title or not verdict:
            return _err("title and verdict are both required.")
        reasons = list(params.get("reasons") or [])
        bad = [r for r in reasons if r not in C.REASONS]
        if bad:
            return _err(f"Unknown reason(s): {', '.join(bad)}.",
                        allowed=sorted(C.REASONS))
        medium = params.get("medium") or "film"
        con = storage.connect()
        wid, _sid, info = _resolve(con, _api_key(), medium, title, params.get("year"))
        vid, status = storage.add_verdict(
            con, work_id=wid, verdict=verdict, reasons=reasons,
            rating=params.get("rating"), note=params.get("note"),
            where_watched=params.get("where_watched"),
            stopped_at_min=params.get("stopped_at_min"), source="manual")

        state_only = [r for r in reasons if C.REASONS[r]["polarity"] == 0]
        taste = [r for r in reasons if C.REASONS[r]["polarity"] != 0]
        return _ok({
            "ok": True, "verdict_id": vid, "status": status,
            "work": {"id": wid, "title": info.get("title", title), "year": info.get("year")},
            "learned_as_taste": taste or None,
            "recorded_as_tonight_only": state_only or None,
            "ledger": storage.counts(con),
            "note": ("Reasons about tonight are stored but deliberately not learned as "
                     "dislike." if state_only else None),
        })
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


def palate_seen(params, **_kwargs):
    try:
        titles = [t.strip() for t in (params.get("titles") or []) if str(t).strip()]
        if not titles:
            return _err("titles is required and must be non-empty.")
        medium = params.get("medium") or "film"
        con, api_key = storage.connect(), _api_key()
        done = []
        for t in titles[:100]:
            wid, _sid, info = _resolve(con, api_key, medium, t)
            _vid, status = storage.add_verdict(con, work_id=wid, verdict="seen",
                                              source="manual")
            done.append({"title": info.get("title", t), "year": info.get("year"),
                         "status": status})
        return _ok({
            "ok": True, "recorded": done, "ledger": storage.counts(con),
            "note": ("Recorded as seen with no opinion attached. A rating or a reason "
                     "teaches the picker far more - ask only if it is natural to."),
        })
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# palate_services
# ---------------------------------------------------------------------------

def palate_services(params, **_kwargs):
    try:
        action = params.get("action") or "list"
        con = storage.connect()
        if action == "list":
            return _ok({"ok": True, "services": storage.list_services(con, active_only=False),
                        "kinds": list(C.SERVICE_KINDS),
                        "note": ("Library access (Kanopy, Hoopla via a library card) and local "
                                 "files count as free at point of use.")})
        name = (params.get("name") or "").strip()
        if not name:
            return _err("name is required to add or remove a service.")
        if action == "add":
            kind = params.get("kind")
            if kind not in C.SERVICE_KINDS:
                return _err(f"kind must be one of: {', '.join(C.SERVICE_KINDS)}")
            storage.set_service(con, name, kind, params.get("region") or "US", True)
        else:
            storage.set_service(con, name, params.get("kind") or "subscription",
                                params.get("region") or "US", False)
        return _ok({"ok": True, "services": storage.list_services(con, active_only=False)})
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# palate_profile
# ---------------------------------------------------------------------------

def palate_profile(params, **_kwargs):
    try:
        con = storage.connect()
        verdicts = storage.verdicts_with_tags(con, medium=params.get("medium"))
        report = taste_profile.build(verdicts)
        report.update({"ok": True, "ledger": storage.counts(con)})
        report["guidance"] = (
            "Report 'message' as written. Do not describe inconclusive tags as "
            "preferences, and do not infer a direction the interval does not support."
        )
        return _ok(report)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------------------
# palate_import / palate_export
# ---------------------------------------------------------------------------

def palate_import(params, **_kwargs):
    try:
        name = params.get("source")
        path = params.get("path")
        mode = params.get("mode") or "probe"
        if not path:
            return _err("path is required.")
        try:
            mod = sources.get(name)
        except ValueError as exc:
            return _err(str(exc))

        cfg = {"path": path}
        if mode == "probe":
            return _ok({"ok": True, "mode": "probe", **mod.probe(cfg),
                        "next": "Show this to the user, then call again with mode='apply' "
                                "once they confirm."})

        records, assumed = mod.fetch(cfg)
        con = storage.connect()
        acc = source_base.ingest(con, records, source_name=mod.NAME,
                                 protect_user_authored=True)
        acc["assumed"].extend(assumed)
        return _ok({"ok": True, "mode": "apply", "accounting": acc,
                    "ledger": storage.counts(con),
                    "note": ("Imported rows never overwrite verdicts you entered by hand; "
                             "those are counted as skipped_existing_user_records.")})
    except source_base.FormatRefused as exc:
        return _err(str(exc), refused=True)
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


EXPORT_FORMAT = "palate-export"


def _file_safety():
    """Hermes' own write/read denylist, if reachable.

    Imported lazily and defensively, but a failure here must make the guard
    STRICTER rather than quietly removing a check, so the caller is told
    whether the denylist actually ran.
    """
    try:
        from agent import file_safety  # noqa: PLC0415
        return file_safety
    except Exception:  # noqa: BLE001
        return None


def _hermes_home():
    """Realpath of the Hermes home, or None if it cannot be resolved.

    Uses Hermes' own resolver rather than the raw env var: one gateway can
    serve several profiles and keeps only the launch profile in HERMES_HOME.
    """
    try:
        import hermes_constants  # noqa: PLC0415
        return os.path.realpath(str(hermes_constants.get_hermes_home()))
    except Exception:  # noqa: BLE001
        raw = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        try:
            return os.path.realpath(raw)
        except Exception:  # noqa: BLE001
            return None


def _looks_like_palate_export(path):
    """True only if the file on disk is already one of our own exports."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            head = json.load(fh)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(head, dict) and head.get("format") == EXPORT_FORMAT


def _export_write_guard(path):
    """Return (error, checks) for writing an export to `path`.

    palate_export wrote to whatever path it was handed. A prompt-injected call
    could therefore overwrite any user-writable file - a shell rc file,
    ~/.ssh/authorized_keys, Hermes' own config.yaml - with export JSON, and it
    would bypass the denylist Hermes' own file tools enforce. The path is a
    model-supplied argument, so it is untrusted input.

    Three gates: Hermes' denylist, a .json suffix, and no clobbering anything
    that is not already a palate export.
    """
    checks = {"denylist": "not available", "hermes_home": "ok",
              "suffix": "ok", "clobber": "ok"}
    fs = _file_safety()
    if fs is not None:
        checks["denylist"] = "checked"
        try:
            if fs.is_write_denied(path):
                return (fs.get_write_denied_error(path)
                        or f"Write denied: '{path}' is a protected system or credential "
                           f"file."), checks
        except Exception:  # noqa: BLE001
            # A denylist that errors is not a denylist that passed.
            checks["denylist"] = "errored, treated as deny"
            return (f"Write refused: could not verify that '{path}' is safe to write."), checks

    # The denylist protects specific known files, so a NEW file inside the
    # Hermes home (~/.hermes/credentials.json, say) passes it. An export has
    # no business being written there at all: the plugin's own storage lives
    # in the Hermes home and is managed by storage.py, never by this tool.
    home = _hermes_home()
    if home:
        try:
            if os.path.commonpath([os.path.realpath(path), home]) == home:
                checks["hermes_home"] = "rejected"
                return (f"Write refused: '{path}' is inside the Hermes home. Exports go "
                        f"somewhere you chose, not into Hermes' own directory."), checks
        except ValueError:
            pass  # different drives on Windows; nothing to compare

    if not path.lower().endswith(".json"):
        checks["suffix"] = "rejected"
        return (f"Write refused: '{path}' does not end in .json. An export is JSON, and "
                f"refusing other suffixes keeps this tool from being aimed at a config "
                f"or script file."), checks

    if os.path.exists(path):
        if os.path.isdir(path):
            checks["clobber"] = "rejected"
            return f"Write refused: '{path}' is a directory.", checks
        if not _looks_like_palate_export(path):
            checks["clobber"] = "rejected"
            return (f"Write refused: '{path}' already exists and is not a palate export. "
                    f"Pick a new filename, or delete that file yourself first."), checks
        checks["clobber"] = "overwriting an earlier palate export"
    return None, checks


def palate_export(params, **_kwargs):
    try:
        path = params.get("path") or os.path.expanduser(
            f"~/palate-export-{time.strftime('%Y%m%d')}.json")
        path = os.path.expanduser(path)
        error, checks = _export_write_guard(path)
        if error:
            return _err(error, refused=True, write_checks=checks)
        con = storage.connect()
        data = storage.export_all(con)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        return _ok({"ok": True, "path": path, "write_checks": checks,
                    "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)}})
    except Exception as exc:  # noqa: BLE001
        return _err(f"{exc.__class__.__name__}: {exc}")


HANDLERS = {
    "palate_pick": palate_pick,
    "palate_verdict": palate_verdict,
    "palate_seen": palate_seen,
    "palate_services": palate_services,
    "palate_profile": palate_profile,
    "palate_import": palate_import,
    "palate_export": palate_export,
}
