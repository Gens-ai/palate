# How Palate works

Two paths matter: **pick** and **ingest**. Both are drawn here because the
plugin's one fatal bug was a comprehension failure rather than a logic error —
the picker compares two sets of tagged records and only one of them was being
hydrated, so every score came back `0.0` with nothing red anywhere. A diagram
with a missing arrow would have shown that in seconds.

## The pick path

```
                  ┌─────────────────────────────────────────┐
                  │  verdicts + reasons  (the user's own)    │
                  └───────────────────┬─────────────────────┘
                                      │
                      storage.verdicts_with_tags()
                      drops polarity-0 rows  ← honesty gate 1
                                      │
            ┌─────────────────────────▼──────────────────────┐
            │  HYDRATE THE USER'S OWN WORKS                  │
            │  _ensure_tags(), VERDICT_TAG_BUDGET = 12       │
            │  ** skip this and the net is empty **          │
            └─────────────────────────┬──────────────────────┘
                                      │
                           picker.rank()
   candidate generation, round-robin across up to PIVOT_LIMIT liked works
   (concatenating pages instead gives pivot 1 the whole pool and the rest none)
                         │
                 picker.tag_net()
                 {tag: Σ polarity × weight}, + who contributed
                                      │
   candidate generation               │
   ┌──────────────────────────┐       │
   │ seed_title → TMDB        │       │
   │   recommendations        │       │
   │ else: pivot on up to 5   │       │
   │   liked works            │       │
   │ else: discover() cold    │       │
   └────────────┬─────────────┘       │
                │ CANDIDATE_POOL = 24 │
                │                     │
   ┌────────────▼─────────────┐       │
   │ HYDRATE CANDIDATES       │       │
   │ DETAIL_BUDGET = 24       │       │
   │ (== pool, deliberately)  │       │
   │ + availability per work  │       │
   └────────────┬─────────────┘       │
                │                     │
                └────────┬────────────┘
                         │
                 picker._trait_score()
          Σ net[tag] × weight ÷ √(tag count)
          no tags → score None, looked_up False  ← honesty gate 2
                         │
                 picker.state_check()
          runtime cap · state-axis caps · availability mode
          unverifiable → `unchecked`, not "satisfied"  ← honesty gate 3
                         │
          ┌──────────────┴──────────────┐
          │                             │
       picks                    shelved_for_another_night
   (taste AND tonight)      (taste, but a named blocker)
```

### Why both budgets equal the pool

A fetch budget smaller than the candidate pool means the best match can be the
item nobody examined. Observed: Minari scored `0.0` on a cold run and `1.719`
once cached, purely from cache warming — so a cold cache produced the *worst*
results rather than merely slower ones. Sizing `DETAIL_BUDGET` to
`CANDIDATE_POOL` removes the class. `tests/live_check.py` asserts no pick is
returned unexamined.

### Trait versus state

| | Axes | Where it is applied |
|---|---|---|
| **Trait** — stable taste | pace, register, ambition, craft, ending, texture | folded into `tag_net`, long-run |
| **State** — tonight only | load, bleakness | `state_check` per request, never learned |

`constants.REASONS` enforces the split in data: the four state-only reasons
(`not_in_the_mood`, `no_time`, `no_access`, `seen_too_recently`) carry
`polarity: 0`, so `_polarity()` returns 0 and `verdicts_with_tags` omits the
row entirely. A reviewer changing one of those zeros changes what the plugin
believes about the user, which is why they live in one table with a comment
rather than being scattered through the picker.

## The ingest path

```
  source.probe(cfg)  ──► parsed records + raw preview, WRITES NOTHING
       │                 (asserted side-effect-free against an empty store)
       │  user confirms
       ▼
  source.fetch(cfg)  ──► canonical records + assumed_units
       │
       ▼
  sources.base.ingest()          ← THE ONLY WRITE PATH
       ├─ plausibility bounds     (constants, owned in one place)
       ├─ dedupe within the batch
       ├─ provenance tag per row
       ├─ protect_user_authored=True by default
       └─ ingest accounting returned, never "added: N"
       ▼
  storage.upsert_work / add_verdict
```

A source answers three questions — `requirements()`, `probe()`, `fetch()` —
and nothing else. It never validates and never writes. That is what makes a
music or food adapter a module and a line in `REGISTRY`, and it is what keeps
a second importer from drifting away from the first on a bound or a safety
flag.

### Cross-source identity

`upsert_work` keys on `(medium, source, source_id)` **and then falls back to
title identity**. Without the fallback, a title logged by hand (no
`source_id`) and the same title arriving from an import (with one) become two
works: history splits and the user-authored guard inspects the wrong row. A
manual stub is promoted to the canonical id once one is known, so tag and
availability lookups work for it later.

## Where the honesty gates live

Scattered across three modules by necessity, listed here so none is quietly
removed:

| Gate | Location | Refuses to |
|---|---|---|
| State reasons teach nothing | `constants.REASONS` polarity 0, `storage._polarity` | learn "I was tired" as "I dislike this" |
| Thin evidence | `taste_profile.build` — 20 verdicts, 3 support, bootstrap CI excluding zero | report an affinity the sample cannot support |
| Unexamined ≠ unmatched | `picker.rank` — `trait_score: None`, `looked_up: False` | claim a comparison that never happened |
| Unverifiable constraint | `picker.state_check` — `unchecked` list | say something "fits" on a dimension never measured |
| Cost is named, not implied | `picker.cost_of` — `cost` on every entry | present a paid rental as if it were free |
| No invented prices | nothing quotes an amount | TMDB gives providers, not prices, so none is guessed |
| One favourite cannot own the answer | `tools._interleave` + `picker._diversify` (`PICKS_PER_PIVOT`) | present six neighbours of a single liked work as if the whole ledger were consulted |
| Guessed units | `sources/*` → `assumed_units` | launder a guess into a reading |
| Foreign file | `sources.base.read_csv_guarded` | echo contents or schema of a non-export |

Each has a test, and the profile gate has two: a planted preference must be
recovered **and** pure-noise tags must come back inconclusive. Only the second
proves the gate works rather than that the code emits a number.

## Testing

| Command | Proves |
|---|---|
| `python -m pytest tests/ -q` | unit behaviour, including the adversarial import arm |
| `python tests/smoke.py` | every tool through the real `register(ctx)`, no network |
| `python tests/live_check.py` | the production path against the live API, and that the pipeline is wired on both sides |

Run the third one before believing a change to the pick path works. The first
two were green throughout the bug described at the top of this page.

## Not built yet

- **Axis scoring from content.** `work_axes` is currently written only by
  `_apply_axis_evidence`, i.e. from the user's own verdict reasons. Nothing
  scores an unseen candidate, so `max_load` and `max_bleakness` report
  `unchecked` in practice. The schema and provenance column already support a
  `model` source; the scoring step is missing.
- **Music and food.** `constants.MEDIA` and the source seam are ready. The
  axes will not transfer: pace and bleakness mean nothing for a restaurant.
  Expect a per-medium axis set sharing one ledger.
