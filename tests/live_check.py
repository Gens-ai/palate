"""Live check: drives Palate against the real TMDB API.

Committed deliberately. Green gates and a passing unit suite cannot see a
half-wired pipeline: a test that calls set_tags() before asserting the ranker
proves the ranker and says nothing about whether the production path ever
populates tags. The first version of this plugin hydrated candidates but not
the user's own works, so every score was 0.0 and every explanation degraded to
a plausible "no overlap" string with nothing red anywhere. This script is what
caught it, so it stays in the tree.

Usage:

    python tests/live_check.py

Reads the TMDB key from Hermes config, or from PALATE_TMDB_API_KEY for a local
run. (The shipped plugin reads only the config route -- admission rule 11.
This is a test harness, not shipped code.) Everything happens in a throwaway
HERMES_HOME, so the real ledger is never touched, and the key is never printed.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="palate-live-")
os.environ["HERMES_HOME"] = TMP
sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def read_key():
    """Hermes config first, env second. Never printed, never returned upward."""
    try:
        import yaml
        cfg = yaml.safe_load(Path("~/.hermes/config.yaml").expanduser().read_text()) or {}
        node = (cfg.get("plugins", {}).get("entries", {})
                   .get("palate", {}).get("settings", {}) or {})
        if node.get("tmdb_api_key"):
            return str(node["tmdb_api_key"]), "config.yaml"
    except Exception as exc:  # noqa: BLE001
        print(f"  (config read failed: {exc.__class__.__name__})")
    key = os.environ.get("PALATE_TMDB_API_KEY")
    return (key, "environment") if key else (None, None)


KEY, WHERE = read_key()
if not KEY:
    print("SKIP: no TMDB key in plugins.entries.palate.settings.tmdb_api_key "
          "or PALATE_TMDB_API_KEY.")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)
print(f"key loaded from {WHERE}: {len(KEY)} chars")

import palate  # noqa: E402
from palate import storage, tmdb  # noqa: E402


class Ctx:
    """Mirrors the host's registration surface closely enough to exercise it."""

    def __init__(self):
        self.tools, self.skills = {}, []
        self.settings = {"tmdb_api_key": KEY}

    def register_tool(self, name=None, schema=None, handler=None, **_kw):
        self.tools[name] = handler

    def register_hook(self, name, fn, **_kw):
        raise AssertionError("Palate must register no hooks")

    def register_skill(self, name, path):
        self.skills.append(name)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def call(ctx, _tool, **params):
    return json.loads(ctx.tools[_tool](params))


SEED = [
    ("Burning", 2018, "seen", ["loved_it", "great_craft"], 9),
    ("Drive My Car", 2021, "seen", ["stayed_with_me"], 9),
    ("Columbus", 2017, "seen", ["great_craft"], 8),
    ("Past Lives", 2023, "seen", ["loved_it"], 8),
    ("Stalker", 1979, "bailed", ["too_slow", "not_in_the_mood"], None),
    ("Transformers", 2007, "seen", ["weak_craft"], 3),
]


def main():
    ctx = Ctx()
    palate.register(ctx)
    print(f"registered {len(ctx.tools)} tools, 0 hooks, skills={ctx.skills}")

    print("\n[1] TMDB reachability")
    hits = tmdb.search(KEY, "film", "Burning", 2018)
    det = tmdb.details(KEY, "film", hits[0]["tmdb_id"])
    print(f"    {hits[0]['title']} ({hits[0]['year']}) id={hits[0]['tmdb_id']} "
          f"runtime={det['runtime_min']}")
    print(f"    {len(det['keywords'])} keywords, {len(det['crew'])} crew "
          f"-> {len(tmdb.tags_from_details(det))} tag rows")

    print("\n[2] availability (JustWatch data via TMDB)")
    av = tmdb.availability(KEY, "film", hits[0]["tmdb_id"], "US")
    for bucket in ("flatrate", "free", "ads", "rent"):
        if av.get(bucket):
            print(f"    {bucket}: {av[bucket][:3]}")

    print("\n[3] seeding a ledger through the plugin's own tools")
    for name, kind in [("Criterion Channel", "subscription"), ("Kanopy", "library"),
                       ("Max", "subscription")]:
        call(ctx, "palate_services", action="add", name=name, kind=kind, region="US")
    for title, year, verdict, reasons, rating in SEED:
        out = call(ctx, "palate_verdict", title=title, year=year, verdict=verdict,
                   reasons=reasons, rating=rating)
        assert out.get("ok"), out
        print(f"    {title}: taste={out['learned_as_taste']} "
              f"tonight={out['recorded_as_tonight_only']}")

    # THE CHECK THAT MATTERS. Print the intermediate the algorithm consumes,
    # not just the final answer: feature rows next to verdict count is what
    # exposes a one-sided pipeline instantly.
    print("\n[4] pipeline intermediates (a one-sided pipeline shows up here)")
    con = storage.connect()
    counts = storage.counts(con)
    verdicts = storage.verdicts_with_tags(con, medium="film")
    own_tagged = sum(1 for v in verdicts if v["tags"])
    print(f"    taste-bearing verdicts: {len(verdicts)}")
    print(f"    of those, carrying tags: {own_tagged}   <-- 0 here means the "
          f"user's own side was never hydrated")
    print(f"    ledger: {counts}")

    print("\n[5] palate_pick, cold cache: 100 minutes, free on my services")
    t0 = time.time()
    out = call(ctx, "palate_pick", medium="film", max_runtime_min=100,
               require_free=True, limit=5)
    cold = time.time() - t0
    assert out.get("ok"), out
    print(f"    strategy: {out['candidate_strategy']}  ({cold:.1f}s)")
    unexamined = [p for p in out["picks"] if not p["looked_up"]]
    for p in out["picks"]:
        print(f"    > {p['title']} ({p['year']}) {p['runtime_min']} min  "
              f"score={p['trait_score']}  free_on={p['free_on'] or '-'}")
        print(f"      why: {p['why']}")
        if p["unchecked"]:
            print(f"      unchecked: {p['unchecked']}")
    for s in out["shelved_for_another_night"][:3]:
        print(f"    shelved: {s['title']} -> {s['tonight_blockers']}")

    assert not unexamined, (
        f"{len(unexamined)} pick(s) were never looked up; size DETAIL_BUDGET "
        "to CANDIDATE_POOL so the best match is not the one nobody examined")

    print("\n[6] warm cache: materially better results here means [5] was partial")
    t0 = time.time()
    warm = call(ctx, "palate_pick", medium="film", max_runtime_min=100,
                require_free=True, limit=5)
    print(f"    {time.time() - t0:.1f}s; top: "
          f"{[(p['title'], p['trait_score']) for p in warm['picks'][:3]]}")

    print("\n[7] no constraints, to prove the shelving was the constraint")
    for p in call(ctx, "palate_pick", medium="film", limit=3)["picks"]:
        print(f"    > {p['title']} ({p['year']}) {p['runtime_min']} min "
              f"score={p['trait_score']}")

    print("\n[8] profile honesty on a thin ledger")
    prof = call(ctx, "palate_profile")
    print(f"    {prof['message']}")
    assert prof["established"] == [], "nothing may be established below the threshold"

    scored = [p for p in warm["picks"] if (p["trait_score"] or 0) > 0]
    print(f"\nLIVE OK - {len(scored)} of {len(warm['picks'])} picks carry a real score")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
