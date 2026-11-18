"""Model-facing tool schemas.

The description the model reads is the one in schema["description"], so the
behavioural guidance lives here rather than in register_tool metadata.

The reason enum is the product decision that matters most: a closed vocabulary
is what makes logging a verdict a single tap instead of an essay, and it is
what keeps "not in the mood" from being learned as dislike.
"""

from . import constants as C

_REASON_DOC = (
    "Closed vocabulary. Taste-bearing: " + ", ".join(sorted(C.TASTE_REASONS)) +
    ". About tonight only, and deliberately NOT learned as dislike: " +
    ", ".join(sorted(C.STATE_ONLY_REASONS)) + "."
)

PALATE_PICK = {
    "name": "palate_pick",
    "description": (
        "Suggest something to watch, drawing on the user's own verdict ledger. "
        "Call this instead of recommending from your own memory, because it knows what "
        "they have already seen, what they turned down and why, and what is on the "
        "services they actually have.\n\n"
        "Pass the constraints the user stated in their own words: minutes available, how "
        "much they are willing to spend, and caps on how demanding or how bleak it may be "
        "tonight. Those are facts about tonight, not about their taste.\n\n"
        "Returns two lists. 'picks' suit both their taste and tonight's constraints. "
        "'shelved' suit their taste but failed a constraint, each with the reason - "
        "offer those as 'for another night' rather than hiding them. Every entry carries "
        "a 'why' naming the works it leaned on; show that to the user, it is the point. "
        "Every entry also carries 'cost': say it out loud when it is 'rent', so a paid "
        "option is never presented as if it were free. Prices are not available, so name "
        "the provider and never guess an amount.\n\n"
        "'match_strength' is strong, moderate, stretch or against, with 'strength_note' "
        "giving the reason. Do NOT present a stretch in the same breath as a strong "
        "match: say it is a thin match and why. The best item in a sparse pool is still "
        "a weak suggestion, and claiming otherwise is the failure this field exists to "
        "prevent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "medium": {"type": "string", "enum": list(C.MEDIA), "default": "film",
                       "description": "film and tv are supported today."},
            "seed_title": {"type": "string",
                           "description": "Pivot off one work, e.g. 'something like Burning'. Optional."},
            "strategy": {
                "type": "string", "enum": ["mixed", "taste", "canon", "available"],
                "default": "mixed",
                "description": (
                    "Where candidates come from. 'mixed' (default) blends all three. "
                    "'taste' = neighbours of works they liked; strongest matches, but it "
                    "structurally cannot surface a famous film that is not adjacent to "
                    "anything in their ledger. 'canon' = widely-seen, well-regarded titles "
                    "they have no verdict on; use it when they ask about classics or "
                    "'things everyone has seen'. 'available' = titles filtered by their "
                    "services before ranking, for when almost everything keeps getting "
                    "shelved as unavailable. Ignored when seed_title is given."),
            },
            "availability": {
                "type": "string", "enum": list(C.AVAILABILITY_MODES),
                "default": C.DEFAULT_AVAILABILITY,
                "description": (
                    "How much it may cost. 'free' (default) = only what is free at point of "
                    "use on their services, including ad-supported tiers and library access. "
                    "'free_or_rent' = also titles rentable or buyable from providers they have "
                    "declared with palate_services; use this when the user says they will pay. "
                    "'any' = no availability filter, for pure taste exploration. With no "
                    "services declared, availability cannot be checked and is reported as "
                    "unchecked rather than treated as satisfied."),
            },
            "max_runtime_min": {"type": "integer",
                                "description": "Hard cap in minutes if the user named a time budget."},
            "max_load": {"type": "number", "minimum": -2, "maximum": 2,
                         "description": "Cap on how much attention it may demand, -2 easy to +2 demanding. "
                                        "Use when the user says they are tired or distracted."},
            "max_bleakness": {"type": "number", "minimum": -2, "maximum": 2,
                              "description": "Cap on how bleak it may be, -2 warm to +2 devastating."},
            "require_free": {"type": "boolean",
                             "description": "Deprecated alias for availability='free'. Prefer availability."},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
        },
    },
}

PALATE_VERDICT = {
    "name": "palate_verdict",
    "description": (
        "Record one verdict with its reasons. This is how the ledger fills, so call it "
        "whenever the user reveals an opinion in passing - 'seen it', 'I bailed on that', "
        "'not tonight', 'that was great' - without making them fill in a form.\n\n"
        "Infer the verdict and the reasons from what they already said. Only ask if the "
        "reason genuinely matters and is genuinely unclear. Reasons are optional: a bare "
        "'seen it' is still worth recording.\n\n" + _REASON_DOC
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "year": {"type": "integer"},
            "medium": {"type": "string", "enum": list(C.MEDIA), "default": "film"},
            "verdict": {"type": "string", "enum": list(C.VERDICTS),
                        "description": "seen = finished it; bailed = started and stopped; "
                                       "skipped = declined without starting; queued = saved for later."},
            "reasons": {"type": "array", "items": {"type": "string", "enum": sorted(C.REASONS)},
                        "description": _REASON_DOC},
            "rating": {"type": "integer", "minimum": C.RATING_MIN, "maximum": C.RATING_MAX},
            "stopped_at_min": {"type": "integer",
                               "description": "Where they bailed, in minutes. Strong signal - record it when known."},
            "where_watched": {"type": "string", "description": "Service or place, if mentioned."},
            "note": {"type": "string", "description": "Their own words, verbatim. Optional."},
        },
        "required": ["title", "verdict"],
    },
}

PALATE_SEEN = {
    "name": "palate_seen",
    "description": (
        "Bulk-record several titles as already seen, with no reasons attached. For fast "
        "backfill when the user lists things off, or to mark a batch of suggestions as "
        "'seen those'. Use palate_verdict instead whenever a reason or rating is available, "
        "since presence without an opinion teaches the picker very little."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "titles": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "medium": {"type": "string", "enum": list(C.MEDIA), "default": "film"},
        },
        "required": ["titles"],
    },
}

PALATE_SERVICES = {
    "name": "palate_services",
    "description": (
        "List or edit which services the user can actually watch on. This is what makes "
        "'free tonight' meaningful, and it covers more than subscriptions: free "
        "ad-supported tiers, library access through Kanopy or Hoopla, things they own, "
        "and local files.\n\n"
        "It also records which providers they are willing to PAY per title on: add a "
        "provider with kind='rent' or kind='buy' and palate_pick will offer titles from "
        "it when called with availability='free_or_rent'. Add only providers the user "
        "named; someone may accept Amazon rentals and refuse everything else. Prices are "
        "not available from the data, so never imply an amount.\n\n"
        "Region matters, because availability is region-locked. Use provider names as "
        "TMDB and JustWatch spell them, e.g. 'Amazon Video', 'Apple TV Store', "
        "'Tubi TV', 'The Roku Channel', 'HBO Max'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list"},
            "name": {"type": "string", "description": "Provider name as TMDB/JustWatch spell it."},
            "kind": {"type": "string", "enum": list(C.SERVICE_KINDS),
                     "description": ("subscription, free_ads, library, owned and local are free at "
                                     "point of use. rent and buy cost money per title and only "
                                     "count when availability='free_or_rent'.")},
            "region": {"type": "string", "description": "ISO country code, e.g. US, GB. Default US."},
        },
    },
}

PALATE_PROFILE = {
    "name": "palate_profile",
    "description": (
        "Report what the ledger can and cannot yet support about the user's taste. "
        "Returns established affinities (each with its support count and confidence "
        "interval) and, separately, tags that were seen but where the direction is not "
        "established.\n\n"
        "Below the evidence threshold it says so plainly instead of guessing. Report that "
        "honestly to the user rather than dressing up a thin result - the thresholds exist "
        "so this is a measurement and not a horoscope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "medium": {"type": "string", "enum": list(C.MEDIA),
                       "description": "Restrict to one medium. Omit for everything."},
        },
    },
}

PALATE_IMPORT = {
    "name": "palate_import",
    "description": (
        "Seed the ledger from a CSV the user exported themselves from Letterboxd or Trakt. "
        "No account, no API, no scraping - they download their own data and this reads the file.\n\n"
        "Always run mode='probe' first and show the user what it parsed; probe writes "
        "nothing. Only run mode='apply' once they have confirmed. Imported records never "
        "overwrite verdicts the user entered by hand; those come back as "
        "skipped_existing_user_records."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": ["letterboxd_csv", "trakt_csv"]},
            "path": {"type": "string", "description": "Path to the exported CSV."},
            "mode": {"type": "string", "enum": ["probe", "apply"], "default": "probe"},
        },
        "required": ["source", "path"],
    },
}

PALATE_EXPORT = {
    "name": "palate_export",
    "description": (
        "Write the entire ledger to a JSON file: works, verdicts, reasons, axis scores with "
        "their provenance, services and notes. The data is the user's, so it has to be able "
        "to leave. Mention the path in your reply."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "Destination file. Defaults to ~/palate-export-<date>.json."},
        },
    },
}

ALL = [PALATE_PICK, PALATE_VERDICT, PALATE_SEEN, PALATE_SERVICES,
       PALATE_PROFILE, PALATE_IMPORT, PALATE_EXPORT]
