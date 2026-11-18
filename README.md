# Palate

**It records why you turned something down, which is the half nobody keeps.**

Every other recommender knows what you watched and what you starred. Palate
also knows that you bailed on *Stalker* at minute forty because you were
tired, that you skipped *Shoah* twice because it was a Tuesday, and that the
one thing you reliably love is whatever Roger Deakins pointed a camera at. It
keeps that in a SQLite file on your machine, and it picks from it.

```
You:     I've got about 90 minutes and no brain. Something on my services.

Palate:  Columbus (2017) · 104 min · Criterion Channel
         shares cinematographer Elisha Christian with Burning, which you rated 9

         Shelved for another night:
         Burning (2018) · 148 min — runs 148 min, you have 90
         Fallen Leaves (2023) — not free on your services (rental on Apple TV)
```

That second block is deliberate. Something can be right for your taste and
wrong for tonight, and collapsing those two into one number is why "because
you watched X" suggestions feel like they were written by someone who has
never met you.

## Install

[Install in Hermes](hermes://plugin/install?repo=Gens-ai/palate&enable=1)

```
hermes plugins install palate
hermes plugins enable palate
```

Then one free key, which is the only setup there is:

```
hermes config set plugins.entries.palate.settings.tmdb_api_key <your key>
```

Get it at [themoviedb.org](https://www.themoviedb.org/settings/api) — account
settings → API, no cost, no approval wait. There is no Palate account, because
there is no Palate service.

## Contents

- [Reasons, not stars](#reasons-not-stars)
- [Taste is not mood](#taste-is-not-mood)
- [What you can actually watch tonight](#what-you-can-actually-watch-tonight)
- [It declines to guess](#it-declines-to-guess)
- [Seeding it from Letterboxd or Trakt](#seeding-it-from-letterboxd-or-trakt)
- [Tools](#tools)
- [Why it's called Palate](#why-its-called-palate)
- [Disclosure](#disclosure)

## Reasons, not stars

A rating compresses a whole evening into one number and throws away the part
that predicts anything. Palate stores a verdict plus a reason from a closed
vocabulary, which means logging one is a tap rather than an essay:

| Verdict | Means |
|---|---|
| `seen` | finished it |
| `bailed` | started and stopped — `stopped_at_min` is the strongest signal in the ledger |
| `skipped` | declined without starting, which no streaming service records at all |
| `queued` | saved deliberately |

Reasons are things like `too_slow`, `too_bleak`, `weak_craft`, `great_craft`,
`comfort`, `stayed_with_me`. You never type them. You say "gave up about forty
minutes in, too slow for me" and the agent records it while answering you.

That is also how the ledger fills from nothing. There is no setup chore and no
import you have to do first: every suggestion you reject is a labelled data
point, so a cold start is a by-product of use rather than a wall.

## Taste is not mood

Four of the reasons — `not_in_the_mood`, `no_time`, `no_access`,
`seen_too_recently` — are recorded and then **deliberately not learned**. They
are facts about one evening. A recommender that treats "I was tired" as "I
dislike this" teaches itself that you hate every film you were ever too tired
to start, and that error compounds for as long as you keep using it.

The same split runs through the picker. Pace, register, ambition and craft are
treated as stable traits. How much attention something demands and how bleak it
is are treated as tonight's constraints, passed per request. So a film can
score highly for you and still be shelved, with the reason named.

## What you can actually watch tonight

Availability comes from TMDB's `/watch/providers`, which is JustWatch data under
licence — so no scraper, and nothing to repair when a site changes its markup.

What counts as watchable is broader than a subscription list, and this is where
commercial recommenders quietly fail you:

| Kind | Example | Costs |
|---|---|---|
| `subscription` | Netflix, Max, Criterion Channel | nothing more |
| `free_ads` | Tubi, The Roku Channel, Plex, Xumo | nothing |
| `library` | Kanopy or Hoopla on a library card — an enormous free catalogue almost nothing surfaces | nothing |
| `owned` | films you already bought | nothing |
| `local` | files and discs | nothing |
| `rent` | a storefront you're willing to rent from | per title |
| `buy` | a storefront you're willing to buy from | per title |

Rentals are **per provider, opt-in**. Declaring Amazon Video as `rent` does not
sign you up for every storefront on earth, because wanting Amazon rentals and
refusing the rest is a perfectly normal position. Ask for picks three ways:

```
availability="free"           only what costs nothing more  (default)
availability="free_or_rent"   also rentals from providers you accept
availability="any"            ignore access entirely, explore taste
```

Every suggestion carries its cost tier, named out loud, so a paid option can
never be dressed up as a free one.

**What it cannot tell you is the price.** TMDB reports *which* providers rent a
title and not what they charge, so you get "rentable on Amazon Video" and never
"$3.99". JustWatch has the prices; TMDB doesn't pass them through, and scraping
JustWatch directly is exactly the fragility this plugin exists to avoid. So
there is no "rentals under five dollars" filter, and there won't be an invented
one.

## It declines to guess

`palate_profile` reports an affinity only when it clears three bars: twenty
taste-bearing verdicts overall, at least three works carrying the tag, and a
bootstrap confidence interval that does not straddle zero. Everything else
comes back as `inconclusive`, with the reason.

Below the threshold it says so and names the number. This is tested from both
directions: a planted preference must be recovered, and pure-noise tags must
come back inconclusive. A recommender that will not say "not enough evidence
yet" is a horoscope, and the noise arm is the only thing that proves the
difference.

Axis scores carry their provenance too, so a judgement made from a synopsis and
a correction derived from your own verdict stay distinguishable. When you say
"too slow" about something scored mid-pace, that is recorded as a labelled
error on the film, not just a note about you.

## Seeding it from Letterboxd or Trakt

Optional, and it needs no auth, no API and no scraper. Letterboxd's API has
been in closed beta for years, so every "Letterboxd integration" in the wild is
a scraper; the export route sidesteps that completely.

Download your own data — `letterboxd.com/settings/data/` or
`trakt.tv/settings/data` — and point Palate at the CSV. It probes first and
writes nothing until you confirm, reports its full ingest accounting rather
than "added: 412", and never overwrites a verdict you entered by hand.

The import path is treated as an untrusted surface, because a tool that takes a
path and echoes what it found is an arbitrary file read unless it is closed
deliberately. A file that does not present the expected columns is refused with
none of its contents and none of its schema in the result.

## Tools

| Tool | Does |
|---|---|
| `palate_pick` | Suggests, with constraints for tonight; returns picks and shelved separately |
| `palate_verdict` | Records one verdict with its reasons |
| `palate_seen` | Bulk backfill when you list titles off |
| `palate_services` | What you can watch on, and in which region |
| `palate_profile` | What the ledger can and cannot support, honestly |
| `palate_import` | Letterboxd or Trakt CSV, probe then apply |
| `palate_export` | The whole ledger as JSON |

**Palate registers no hooks.** Nothing happens in any session unless a tool is
called, and installing it does not change how anything else behaves. A bundled
skill teaches the agent to log verdicts as they surface in conversation instead
of interviewing you.

How the pieces connect — the pick path, the ingest seam, and where each
honesty gate lives — is in [docs/pipeline.md](docs/pipeline.md).

Film and TV work today. `music` and `food` already exist in the schema and the
source seam, because the valuable part — a ledger of verdicts and reasons, with
mood held separately from taste — is not specific to film. Those adapters are
not written yet, and the axes will not transfer between media when they are.

## Why it's called Palate

A palate is the instrument that does the matching, and it is also the thing
that changes as the ledger fills. "Developing a palate" is exactly the process
here: the longer you keep reasons, the better the instrument gets. It is one of
the few words that works unchanged for food, wine, music and film, which is
where this is going.

It is not a palette. People will type that. It is fine.

## Disclosure

**Network.** One host: `api.themoviedb.org`. Palate sends it the titles and
TMDB ids of things you ask about, plus discovery filters such as a runtime cap,
in order to search, read keywords and credits, and look up availability. It
never sends your verdicts, your reasons, your ratings, your services or any
other part of your ledger anywhere. There is no Palate server, no account, no
telemetry, and no usage reporting.

**Credentials.** Your TMDB key is read only from
`plugins.entries.palate.settings.tmdb_api_key`, through Hermes's own config
route. Palate reads no environment variables, no `.env` files and no vendor
token files, and it does not store or copy the key itself.

**Files read.** Only the single CSV path you pass to `palate_import`, and only
after the header proves it is a Letterboxd or Trakt export; a file that fails
that test is refused without any of its contents or column names appearing in
the result. Nothing else on disk is read, and Hermes's own `state.db` is never
opened.

**Files written.** Its own database at
`<hermes home>/plugin-data/palate/data.db`, holding works, verdicts, reasons,
axis scores, your service list, cached TMDB metadata and cached availability.
`palate_export` writes a JSON file where you tell it to. Nothing is written to
the install tree.

**Process.** No hooks, no middleware, no background threads, no scheduled jobs,
no shell commands, no subprocesses. Pure Python standard library, no
dependencies to install.

**Attribution.** This product uses the TMDB API but is not endorsed or certified
by TMDB. Streaming availability is provided by JustWatch.

## Licence

MIT.
