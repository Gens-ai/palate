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

import math

from . import constants as C
from .taste_profile import tag_key

# Below this many tagged works the ledger is too small to estimate how common
# a tag is, so weighting would be noise dressed as correction. Fall back to
# flat weights and say nothing.
MIN_DOCS_FOR_IDF = 8

# Ceiling on the rarity multiplier. A tag seen twice in a 30-work corpus is
# not demonstrably rarer than one seen twice in a 3000-work corpus, but an
# uncapped log would claim it is by a factor of five. Capping keeps rarity a
# tie-breaker between tag kinds rather than the dominant term, and stops the
# multiplier growing without bound as the ledger fills.
IDF_CAP = 2.0


def tag_idf(con):
    """Inverse document frequency per tag, over every work Palate has tagged.

    Without this, a keyword carried by tens of thousands of films ("based on
    novel or book", "friendship") counts the same as a shared director, and it
    wins on sheer frequency: it appears in more explanations and drags generic
    candidates up the ranking. The corpus here is the works already in the
    ledger -- the user's own plus every candidate ever hydrated -- which is a
    sample of exactly the neighbourhood being searched.

    A tag present on every work gets 0, not a negative: it must contribute
    nothing rather than invert the sign of the contribution.
    """
    total = con.execute("SELECT COUNT(DISTINCT work_id) FROM work_tags").fetchone()[0]
    if total < MIN_DOCS_FOR_IDF:
        return {}, total
    idf = {}
    for row in con.execute(
            "SELECT kind, value, COUNT(DISTINCT work_id) AS n FROM work_tags "
            "GROUP BY kind, value"):
        idf[tag_key(row["kind"], row["value"])] = min(
            IDF_CAP, max(0.0, math.log(total / (1 + row["n"]))))
    return idf, total


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


def _trait_score(cand_tags, net, who, idf=None):
    """Returns (score, top contributions, how many tags actually overlapped).

    The overlap count is returned separately because the score alone cannot
    distinguish a genuine match from the best of a thin pool: two films can
    both score 2.0, one off a shared director and one off two generic genres.
    """
    if not cand_tags:
        return 0.0, [], 0
    idf = idf or {}
    total, contrib = 0.0, []
    for kind, value, weight in cand_tags:
        key = tag_key(kind, value)
        if key in net and net[key]:
            # Rarity multiplier: a shared director should not be outvoted by
            # three shared genres. Defaults to 1.0 when the corpus is too
            # small to estimate rarity.
            c = net[key] * weight * idf.get(key, 1.0)
            if c:
                total += c
                contrib.append((key, c, who.get(key, [])[:3]))
    overlap = len(contrib)
    # Normalise by tag count: a work carrying forty keywords should not beat a
    # sparse one just by having more chances to match.
    total /= max(1.0, len(cand_tags) ** 0.5)
    contrib.sort(key=lambda t: -abs(t[1]))
    return total, contrib[:4], overlap


# Thresholds for match strength, expressed RELATIVE to the best score in the
# same answer rather than as absolute numbers. An absolute cutoff would be a
# number invented from nothing: scores are not calibrated and their magnitude
# moves with ledger size, rarity weighting and tag density.
MODERATE_RATIO = 0.60
STRETCH_RATIO = 0.25
# Below this many overlapping tags, nothing is called a strong match however
# well it scores, because one lucky rare tag is not a pattern.
MIN_OVERLAP_FOR_STRONG = 3


def match_strength(score, best, overlap):
    """How much weight this pick deserves, with the reason, or None if unknown.

    Returns (label, note). The honesty problem this solves: a pick scoring 2.0
    was being printed in the same shape as one scoring 9.8, so the best of a
    thin pool read as a recommendation.
    """
    if score is None:
        return None, None
    if score <= 0:
        return "against", ("it shares more with what you disliked than what you liked, "
                           "so it is listed for completeness")
    ratio = (score / best) if best and best > 0 else 1.0
    if overlap < MIN_OVERLAP_FOR_STRONG:
        return "stretch", (
            f"a stretch: only {overlap} tag{'s' if overlap != 1 else ''} overlap"
            f"{'' if overlap != 1 else 's'} with your ledger"
            + (f", and it scores {ratio:.0%} of the top match" if ratio < 0.99 else ""))
    if ratio >= MODERATE_RATIO:
        return "strong", f"a strong match on {overlap} overlapping tags"
    if ratio >= STRETCH_RATIO:
        return "moderate", (f"a moderate match: {overlap} tags overlap, "
                            f"scoring {ratio:.0%} of the top match")
    return "stretch", (f"a stretch: scores {ratio:.0%} of the top match "
                       f"on {overlap} overlapping tags")


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

    mode = state.get("availability") or C.DEFAULT_AVAILABILITY
    if mode != "any":
        if not state.get("has_services"):
            # Nothing to filter against. Saying "fits" would claim a check that
            # never happened, so report it instead of quietly passing.
            caveats.append("no services declared, so availability was not checked")
        elif not cand.get("free_on"):
            rentable = cand.get("rent_on_yours") or []
            if mode == "free":
                where = cand.get("rent_on") or []
                fails.append(
                    "not free on your services"
                    + (f" (rental on {', '.join(where[:2])})" if where else ""))
            elif not rentable:
                where = cand.get("rent_on") or []
                fails.append(
                    "not free, and not on a rental provider you accept"
                    + (f" (rents from {', '.join(where[:2])})" if where else ""))
    return (not fails), fails, caveats


def cost_of(cand, state):
    """What this would cost the user, named rather than implied."""
    if cand.get("free_on"):
        return "free"
    if cand.get("rent_on_yours"):
        return "rent"
    if not state.get("has_services"):
        return "unknown"
    return "unavailable"


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

# At most this many picks may trace back to the same liked work. Six films that
# are all neighbours of one favourite is a narrower answer than the ledger can
# actually support, and it reads as the picker having one idea.
PICKS_PER_PIVOT = 2


def _diversify(ordered, limit, cap=PICKS_PER_PIVOT):
    """Best `limit` items, but no more than `cap` tracing to one pivot.

    Any shortfall is filled from what the cap held back, so diversity is a
    preference between eligible candidates and never a reason to return fewer
    picks than the user asked for. Candidates with no pivot attribution (a
    seeded search, or the cold-start path) share one bucket and are capped
    together, which is correct: they came from a single source too.
    """
    chosen, held, counts = [], [], {}
    for item in ordered:
        if len(chosen) >= limit:
            break
        src = (item.get("from_pivots") or [None])[0]
        if counts.get(src, 0) >= cap:
            held.append(item)
            continue
        counts[src] = counts.get(src, 0) + 1
        chosen.append(item)
    for item in held:
        if len(chosen) >= limit:
            break
        chosen.append(item)
    return chosen


def rank(con, candidates, verdicts, state=None, seen_ids=(), limit=5):
    """Rank candidates. Returns (picks, shelved).

    picks    suit taste AND tonight
    shelved  suit taste but fail a constraint about tonight, with the reason
    """
    state = state or {}
    net, who = tag_net(verdicts)
    idf, corpus = tag_idf(con)
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
        trait, contrib, overlap = (_trait_score(tags, net, who, idf) if looked_up
                                   else (0.0, [], 0))
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
            "overlap_tags": overlap if looked_up else None,
            "from_pivots": cand.get("from_pivots") or [],
            "why": explain(contrib) if looked_up else NOT_LOOKED_UP,
            "free_on": cand.get("free_on") or [],
            "rent_on_yours": cand.get("rent_on_yours") or [],
            "rent_on": cand.get("rent_on") or [],
            "cost": cost_of(cand, state),
            "fits_tonight": ok,
            "tonight_blockers": fails,
            "unchecked": caveats,
            "rarity_weighted": bool(idf),
            "axes": {a: round(v[0], 2) for a, v in axes.items()},
        })
    # Examined candidates first, best score first; unexamined ones trail.
    scored.sort(key=lambda d: (not d["looked_up"], -(d["trait_score"] or 0.0)))
    # Strength is relative, so it can only be assigned once the whole answer is
    # known. Without it a 2.0 and a 9.8 are printed in the same shape and the
    # best of a thin pool reads as a recommendation.
    examined = [s for s in scored if s["looked_up"]]
    best = max((s["trait_score"] or 0.0 for s in examined), default=0.0)
    for s in scored:
        label, note = match_strength(s["trait_score"], best, s["overlap_tags"] or 0)
        s["match_strength"] = label
        s["strength_note"] = note
    picks = _diversify([s for s in scored if s["fits_tonight"]], limit)
    shelved = [s for s in scored if not s["fits_tonight"]][:limit]
    return picks, shelved
