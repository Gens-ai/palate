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
DETAIL_BUDGET = CANDIDATE_POOL
# Separate budget for the user's own verdict works, which must be tagged or
# there is nothing for candidates to be similar TO.
VERDICT_TAG_BUDGET = 12


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

def palate_pick(params, **_kwargs):
    medium = params.get("medium") or "film"
    limit = int(params.get("limit") or 5)
    state = {
        "max_runtime_min": params.get("max_runtime_min"),
        "max_load": params.get("max_load"),
        "max_bleakness": params.get("max_bleakness"),
        "require_free": bool(params.get("require_free")),
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
        service_names = [s["name"] for s in services]
        region = storage.region(con)

        note = " ".join(str(params.get(k)) for k in ("seed_title",) if params.get(k))
        if note:
            storage.add_state_note(con, note)

        # ---- candidate generation -------------------------------------------------
        candidates, how = [], ""
        seed = params.get("seed_title")
        if seed:
            hits = tmdb.search(api_key, medium, seed)
            if not hits:
                return _err(f"TMDB has nothing matching {seed!r} as {medium}.")
            candidates = tmdb.recommendations(api_key, medium, hits[0]["tmdb_id"])
            how = f"similar to {hits[0]['title']}"
        else:
            liked = [v for v in verdicts if v["polarity"] > 0]
            pivots = []
            for v in liked[-8:]:
                row = con.execute("SELECT source, source_id FROM works WHERE id=?",
                                  (v["work_id"],)).fetchone()
                if row and row["source"] == "tmdb" and row["source_id"]:
                    pivots.append((row["source_id"], v["title"]))
            for tmdb_id, _title in pivots[:5]:
                try:
                    candidates.extend(tmdb.recommendations(api_key, medium, tmdb_id))
                except tmdb.TmdbError:
                    continue
            if candidates:
                how = f"built from {len(pivots[:5])} works you rated well"
            else:
                candidates = tmdb.discover(
                    api_key, medium, max_runtime=state["max_runtime_min"])
                how = "cold start: popular titles, since the ledger has nothing to lean on yet"

        # dedupe, drop anything already in the ledger as seen
        uniq, by_id = [], set()
        for c in candidates:
            if c["tmdb_id"] in by_id:
                continue
            by_id.add(c["tmdb_id"])
            uniq.append(c)
        candidates = uniq[:CANDIDATE_POOL]

        # ---- hydrate ------------------------------------------------------------
        fetched, enriched = 0, []
        for c in candidates:
            wid = storage.upsert_work(con, medium=medium, title=c["title"], year=c["year"],
                                      source="tmdb", source_id=c["tmdb_id"])
            if wid in seen:
                continue
            tags, did_fetch = _ensure_tags(con, api_key, wid, medium, c["tmdb_id"],
                                           allow_fetch=fetched < DETAIL_BUDGET)
            fetched += 1 if did_fetch else 0
            row = con.execute("SELECT runtime_min FROM works WHERE id=?", (wid,)).fetchone()

            free_on, rent_on = [], []
            if service_names:
                try:
                    avail, _cached = tmdb.cached_availability(
                        con, api_key, wid, medium, c["tmdb_id"], region)
                    free_on = tmdb.free_on(avail, service_names)
                    rent_on = (avail.get("rent") or [])[:3]
                except tmdb.TmdbError:
                    pass

            enriched.append({**c, "work_id": wid, "tags": tags,
                             "runtime_min": row["runtime_min"] if row else None,
                             "free_on": free_on, "rent_on": rent_on})

        picks, shelved = picker.rank(con, enriched, verdicts, state=state,
                                     seen_ids=seen, limit=limit)
        prof = taste_profile.build(verdicts)
        _idf, corpus = picker.tag_idf(con)
        return _ok({
            "ok": True,
            "candidate_strategy": how,
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


def palate_export(params, **_kwargs):
    try:
        path = params.get("path") or os.path.expanduser(
            f"~/palate-export-{time.strftime('%Y%m%d')}.json")
        path = os.path.expanduser(path)
        con = storage.connect()
        data = storage.export_all(con)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
        return _ok({"ok": True, "path": path,
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
