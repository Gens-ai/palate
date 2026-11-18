"""Single source of truth for Palate's vocabularies, axes and bounds.

Every other module imports from here. Duplicating any of these values into an
importer or a tool is how the two copies drift apart and a record starts
importing by one route and failing by another.
"""

PLUGIN_NAME = "palate"

# Media Palate can hold a verdict on. Film and TV have adapters today; music
# and food are in the schema so a later source does not need a migration.
MEDIA = ("film", "tv", "music", "food")

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

VERDICTS = (
    "seen",     # consumed it to the end
    "bailed",   # started and stopped
    "skipped",  # declined without starting
    "queued",   # explicitly saved for later
)

# ---------------------------------------------------------------------------
# Reasons: a closed vocabulary, so logging a verdict is a tap and not an essay.
#
# polarity   -1 dislike, +1 like, 0 == says nothing about taste
# axis       which axis this reason is evidence about (None == no evidence)
# direction  which way this work sits on that axis, from the user's reaction
#
# The polarity-0 rows are the whole trait/state split, enforced in data rather
# than in prose: "not in the mood" and "no access" are facts about tonight, so
# they must never move the taste profile. A recommender that learns from them
# concludes you dislike films you simply could not watch.
# ---------------------------------------------------------------------------

REASONS = {
    # negative, and evidence about taste
    "too_slow":          {"polarity": -1, "axis": "pace",      "direction": -1},
    "too_frantic":       {"polarity": -1, "axis": "pace",      "direction": +1},
    "too_long":          {"polarity": -1, "axis": "length",    "direction": +1},
    "too_bleak":         {"polarity": -1, "axis": "bleakness", "direction": +1},
    "too_demanding":     {"polarity": -1, "axis": "load",      "direction": +1},
    "too_slight":        {"polarity": -1, "axis": "ambition",  "direction": -1},
    "wrong_register":    {"polarity": -1, "axis": "register",  "direction": 0},
    "weak_craft":        {"polarity": -1, "axis": "craft",     "direction": -1},
    "ending_let_down":   {"polarity": -1, "axis": "ending",    "direction": 0},
    # positive, and evidence about taste
    "loved_it":          {"polarity": +1, "axis": None,        "direction": 0},
    "great_craft":       {"polarity": +1, "axis": "craft",     "direction": +1},
    "stayed_with_me":    {"polarity": +1, "axis": None,        "direction": 0},
    "comfort":           {"polarity": +1, "axis": "load",      "direction": -1},
    # state only: true tonight, silent about taste
    "not_in_the_mood":   {"polarity": 0,  "axis": None,        "direction": 0},
    "seen_too_recently": {"polarity": 0,  "axis": None,        "direction": 0},
    "no_access":         {"polarity": 0,  "axis": None,        "direction": 0},
    "saving_it":         {"polarity": 0,  "axis": None,        "direction": 0},
    "no_time":           {"polarity": 0,  "axis": None,        "direction": 0},
}

STATE_ONLY_REASONS = tuple(k for k, v in REASONS.items() if v["polarity"] == 0)
TASTE_REASONS = tuple(k for k, v in REASONS.items() if v["polarity"] != 0)

# ---------------------------------------------------------------------------
# Axes. Genre is a marketing category; these describe the experience.
#
# TRAIT axes are stable preferences. STATE axes change hour to hour, which is
# why they are filtered against tonight's request and never folded into the
# long-run profile.
# ---------------------------------------------------------------------------

AXES = ("pace", "bleakness", "load", "register", "ambition", "craft", "ending", "texture")
STATE_AXES = ("load", "bleakness")
TRAIT_AXES = tuple(a for a in AXES if a not in STATE_AXES)

AXIS_SCALE = (-2.0, 2.0)  # every axis score lives here, or it is rejected

# Where an axis score came from. Stored per row: a judgement and a correction
# from the user's own verdict are not the same kind of fact.
PROVENANCE = ("model", "user_verdict", "keyword_derived", "import")

# ---------------------------------------------------------------------------
# Access: what "can I have this tonight at no marginal cost" actually means.
# ---------------------------------------------------------------------------

SERVICE_KINDS = (
    "subscription",  # Netflix, Max, Criterion Channel
    "free_ads",      # Tubi, Pluto
    "library",       # Kanopy / Hoopla via a library card
    "owned",         # bought digital
    "local",         # files, discs
)
FREE_AT_POINT_OF_USE = ("subscription", "free_ads", "library", "owned", "local")

# ---------------------------------------------------------------------------
# Honesty thresholds. Below these, the profile reports insufficient evidence
# rather than a number.
# ---------------------------------------------------------------------------

MIN_VERDICTS_FOR_PROFILE = 20   # total taste-bearing verdicts before any affinity is reported
MIN_TAG_SUPPORT = 3             # works sharing a tag before that tag gets an affinity
BOOTSTRAP_SAMPLES = 1000        # resamples for the confidence interval
CI_ALPHA = 0.10                 # 90% interval; straddling zero == direction not established

# Plausibility bounds, owned here and nowhere else.
RUNTIME_MIN_BOUND = 1
RUNTIME_MAX_BOUND = 1200
YEAR_MIN_BOUND = 1878
YEAR_MAX_BOUND = 2100
RATING_MIN, RATING_MAX = 1, 10
