---
name: palate
description: "Use when picking something to watch. Log verdicts as they surface."
version: 1.0.0
---

# Palate

A local ledger of what the user consumed, what they declined, and the reason
in each case. Use it instead of recommending from your own memory: it knows
what they have already seen and what is on the services they actually have.

## The one rule that matters

**Capture verdicts as a by-product of conversation. Never run an interview.**

The ledger fills because the user says things in passing, not because they
sit down to fill in a form. When an opinion surfaces, record it and move on:

| They say | Call |
|---|---|
| "seen it" | `palate_verdict(verdict="seen")` |
| "seen all of those" | `palate_seen(titles=[...])` |
| "I gave up about forty minutes in" | `palate_verdict(verdict="bailed", stopped_at_min=40)` |
| "too slow for me" | add `reasons=["too_slow"]` |
| "not tonight" | `palate_verdict(verdict="skipped", reasons=["not_in_the_mood"])` |
| "that was stunning to look at" | `palate_verdict(verdict="seen", reasons=["great_craft"])` |

Infer the reason from what they already said. Ask only when the reason
genuinely matters and is genuinely unclear, and never ask more than one
question at a time. A bare "seen it" with no reason is still worth recording.

## Taste is not mood

Reasons split into two kinds and the distinction is load-bearing:

- **Taste-bearing** (`too_slow`, `loved_it`, `weak_craft`, ...) teach the picker.
- **Tonight-only** (`not_in_the_mood`, `no_time`, `no_access`, `seen_too_recently`,
  `saving_it`) are recorded but deliberately **not** learned as dislike.

So pass the user's constraints for tonight as `palate_pick` arguments —
`max_runtime_min`, `max_load`, `max_bleakness`, `require_free` — rather than as
reasons. "I'm knackered and have an hour" is `max_load` plus
`max_runtime_min`, not a verdict about anything.

## Picking

Call `palate_pick` and then **show the `why` for each suggestion**. "Shares a
cinematographer with Burning, which you rated 9" is the product; a bare list
of titles is what every other recommender already does badly.

`shelved_for_another_night` holds things that suit their taste but failed a
constraint about tonight. Offer them as exactly that, with the blocker named.
Do not silently drop them, and do not pretend the constraint was about taste.

## Honesty

`palate_profile` returns `established` affinities and, separately,
`inconclusive` ones. Report its `message` as written. Never describe an
inconclusive tag as a preference, and never give a direction the confidence
interval does not support. Below the threshold it says there is not enough
evidence yet — say that plainly; it is a feature, and picks still work.

## Setup, when something is missing

- No TMDB key: `palate_pick` returns `needs_setup`. Tell them to get a free key
  at themoviedb.org (account settings → API) and run
  `hermes config set plugins.entries.palate.settings.tmdb_api_key <key>`.
- No services configured: availability is unknown, so `require_free` cannot
  work. Offer `palate_services(action="add", ...)`, and remember that library
  access (Kanopy, Hoopla) and local files count as free at point of use.
- Empty ledger: `palate_import` seeds it from a Letterboxd or Trakt CSV the
  user exports themselves. Always `mode="probe"` first, show what it parsed,
  then `mode="apply"` once they confirm.

## Attribution

Tool results carry an `attribution` string. When you present availability,
credit TMDB and JustWatch — it is a licence condition, not a courtesy.
