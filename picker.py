"""Picker v1: nearest neighbour over crew and keywords, with the trait/state split.

Deliberately the unglamorous algorithm. It needs no calibration, works from
about twenty verdicts, and every result can be explained by naming the works
it leaned on. Axis scoring is layered on top only where axis data exists,
because an uncalibrated judgement should not outvote the user's own history.

The split that matters: a trait score says "this suits your taste", a state
check says "this suits tonight". They are reported separately, so something
you would love on a free Sunday is shelved rather than silently dropped when
you have forty minutes and no patience.
"""

from . import constants as C
from .taste_profile import tag_key


def tag_net(verdicts):
    """Net affinity per tag across the verdict log, plus who contributed it."""
    net, who = {}, {}
    for v in verdicts:
        for kind, value, weight in v["tags"]:
            key = tag_key(kind, value)
            net[key] = net.get(key, 0.0) + v["polarity"] * weight
            if v["polarity"] > 0:
                who.setdefault(key, []).append(v["title"])
    return net, who


def _trait_score(cand_tags, net, who):
    if not cand_tags:
        return 0.0, []
    total, contrib = 0.0, []
    for kind, value, weight in cand_tags:
        key = tag_key(kind, value)
        if key in net and net[key]:
            c = net[key] * weight
            total += c
            contrib.append((key, c, who.get(key, [])[:3]))
    # Normalise by tag count: a work carrying forty keywords should not beat a
    # sparse one just by having more chances to match.
    total /= max(1.0, len(cand_tags) ** 0.5)
    contrib.sort(key=lambda t: -abs(t[1]))
    return total, contrib[:4]


def _axis_map(con, work_id):
    """Best-available score per axis, preferring the user's own corrections."""
    out = {}
    rows = con.execute(
        "SELECT axis, score, provenance FROM work_axes WHERE work_id=?", (work_id,))
    for r in rows:
        axis, score, prov = r["axis"], r["score"], r["provenance"]
        if axis not in out or prov == "user_verdict":
            out[axis] = (score, prov)
    return out


def state_check(cand, state, axes):
    """Hard constraints about tonight. Returns (ok, failures, caveats).

    Caveats are the constraints that could not be checked because the data is
    missing. They are reported rather than silently passed: claiming something
    "fits tonight" when the runtime is unknown is the same dishonesty as
    inventing a correlation.
    """
    fails, caveats = [], []
    max_runtime = state.get("max_runtime_min")
    if max_runtime:
        runtime = cand.get("runtime_min")
        if runtime and runtime > max_runtime:
            fails.append(f"runs {runtime} min, you have {max_runtime}")
        elif not runtime:
            caveats.append("runtime unknown, so the time cap was not checked")

    for axis in C.STATE_AXES:
        cap = state.get(f"max_{axis}")
        if cap is None:
            continue
        known = axes.get(axis)
        if known is None:
            caveats.append(f"no {axis} score yet, so that cap was not checked")
        elif known[0] > cap:
            fails.append(f"{axis} scored {known[0]:+.0f} against your cap of {cap:+.0f}")

    if state.get("require_free") and not cand.get("free_on"):
        where = cand.get("rent_on") or []
        fails.append("not free on your services" + (f" (rental on {', '.join(where[:2])})" if where else ""))
    return (not fails), fails, caveats


def explain(contrib):
    """Plain sentence naming what the pick leaned on."""
    if not contrib:
        return "nothing in your ledger overlaps with it yet, so this is a cold guess"
    bits = []
    for key, weight, titles in contrib[:3]:
        kind, _, value = key.partition(":")
        label = value if kind != "crew" else value.replace(":", " ")
        verb = "shares" if weight > 0 else "shares (and you disliked)"
        if titles:
            bits.append(f"{verb} {label} with {', '.join(titles[:2])}")
        else:
            bits.append(f"{verb} {label}")
    return "; ".join(bits)


NOT_LOOKED_UP = ("not looked up yet, so it is unranked rather than unmatched; "
                 "ask again and it will be scored properly")


def rank(con, candidates, verdicts, state=None, seen_ids=(), limit=5):
    """Rank candidates. Returns (picks, shelved).

    picks    suit taste AND tonight
    shelved  suit taste but fail a constraint about tonight, with the reason
    """
    state = state or {}
    net, who = tag_net(verdicts)
    scored = []
    for cand in candidates:
        if cand.get("work_id") in seen_ids:
            continue
        tags = cand.get("tags") or []
        # A work whose metadata was never fetched has no tags, which is NOT the
        # same as having no overlap. Scoring it 0.0 and saying "nothing
        # overlaps" claims a comparison that never happened, and it lets an
        # unexamined title outrank a genuinely matched one.
        looked_up = bool(tags)
        trait, contrib = _trait_score(tags, net, who) if looked_up else (0.0, [])
        axes = _axis_map(con, cand["work_id"]) if cand.get("work_id") else {}
        ok, fails, caveats = state_check(cand, state, axes)
        scored.append({
            "title": cand.get("title"),
            "year": cand.get("year"),
            "medium": cand.get("medium"),
            "runtime_min": cand.get("runtime_min"),
            "work_id": cand.get("work_id"),
            "tmdb_id": cand.get("tmdb_id"),
            "trait_score": round(trait, 3) if looked_up else None,
            "looked_up": looked_up,
            "why": explain(contrib) if looked_up else NOT_LOOKED_UP,
            "free_on": cand.get("free_on") or [],
            "rent_on": cand.get("rent_on") or [],
            "fits_tonight": ok,
            "tonight_blockers": fails,
            "unchecked": caveats,
            "axes": {a: round(v[0], 2) for a, v in axes.items()},
        })
    # Examined candidates first, best score first; unexamined ones trail.
    scored.sort(key=lambda d: (not d["looked_up"], -(d["trait_score"] or 0.0)))
    picks = [s for s in scored if s["fits_tonight"]][:limit]
    shelved = [s for s in scored if not s["fits_tonight"]][:limit]
    return picks, shelved
