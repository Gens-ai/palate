"""Palate's test suite.

Two arms carry most of the weight:

* The honesty gate (`test_profile_*`): a planted preference must be recovered
  AND pure-noise tags must come back inconclusive. Without the noise arm you
  have only shown the code emits a number, not that it declines to invent one.
* The importer (`test_import_*`): the cases that matter are the rows and files
  that must NOT become records.
"""

import json
import os
import random

import pytest

from collections import Counter

from palate import constants as C
from palate import picker, storage, taste_profile, tmdb
from palate.sources import base as source_base
from palate.sources import letterboxd_csv


# ---------------------------------------------------------------------------
# Ledger basics
# ---------------------------------------------------------------------------

def test_upsert_is_idempotent_and_bounds_are_applied(con):
    a = storage.upsert_work(con, medium="film", title="Burning", year=2018, runtime_min=148)
    b = storage.upsert_work(con, medium="film", title="burning", year=2018)
    assert a == b, "same title and year must resolve to one work"

    wid = storage.upsert_work(con, medium="film", title="Nonsense", year=99, runtime_min=99999)
    row = con.execute("SELECT year, runtime_min FROM works WHERE id=?", (wid,)).fetchone()
    assert row["year"] is None and row["runtime_min"] is None, "out-of-bound values are dropped, not stored"

    with pytest.raises(ValueError):
        storage.upsert_work(con, medium="opera", title="Tosca")


def test_a_hand_logged_title_and_an_imported_one_are_the_same_work(con):
    """Regression: cross-source identity.

    A title entered by hand has no canonical id; the same title from an import
    arrives with one. Keying only on (source, source_id) made these two works,
    which duplicated history and silently disabled the protection against
    overwriting hand-entered verdicts.
    """
    manual = storage.upsert_work(con, medium="film", title="Burning", year=2018)
    imported = storage.upsert_work(con, medium="film", title="burning", year=2018,
                                   source="letterboxd_csv", source_id="https://boxd.it/aaa")
    assert manual == imported
    row = con.execute("SELECT source, source_id FROM works WHERE id=?", (manual,)).fetchone()
    assert row["source_id"] == "https://boxd.it/aaa", "the stub is promoted to the canonical id"
    assert con.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1


def test_unknown_reason_is_refused(con):
    wid = storage.upsert_work(con, medium="film", title="X")
    with pytest.raises(ValueError):
        storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["vibes_were_off"])


def test_state_only_reason_is_recorded_but_not_learned_as_taste(con):
    wid = storage.upsert_work(con, medium="film", title="Jeanne Dielman", year=1975)
    storage.set_tags(con, wid, [("crew", "director:Akerman", 3.0)])
    storage.add_verdict(con, work_id=wid, verdict="skipped", reasons=["not_in_the_mood"])

    assert con.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1, "the verdict is stored"
    assert storage.verdicts_with_tags(con) == [], "but it teaches the picker nothing"


def test_taste_reason_is_learned(con):
    wid = storage.upsert_work(con, medium="film", title="Drive My Car", year=2021)
    storage.set_tags(con, wid, [("crew", "director:Hamaguchi", 3.0)])
    storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"])
    out = storage.verdicts_with_tags(con)
    assert len(out) == 1 and out[0]["polarity"] == 1


def test_verdict_corrects_the_works_axis_score_with_its_own_provenance(con):
    wid = storage.upsert_work(con, medium="film", title="Slow Thing")
    con.execute("INSERT INTO work_axes (work_id, axis, score, provenance, at) VALUES (?,?,?,?,?)",
                (wid, "pace", 0.0, "model", 0.0))
    con.commit()
    storage.add_verdict(con, work_id=wid, verdict="bailed", reasons=["too_slow"],
                        stopped_at_min=40)
    rows = {r["provenance"]: r["score"] for r in con.execute(
        "SELECT provenance, score FROM work_axes WHERE work_id=? AND axis='pace'", (wid,))}
    assert rows["model"] == 0.0, "the original judgement is kept"
    assert rows["user_verdict"] == -1.0, "the user's correction is stored separately"


def test_seen_without_an_opinion_is_presence_not_approval(con):
    wid = storage.upsert_work(con, medium="film", title="Some Film")
    storage.set_tags(con, wid, [("keyword", "whatever", 1.0)])
    storage.add_verdict(con, work_id=wid, verdict="seen")
    assert storage.verdicts_with_tags(con) == []
    assert wid in storage.seen_work_ids(con), "but it is still excluded from future picks"


# ---------------------------------------------------------------------------
# The honesty gate
# ---------------------------------------------------------------------------

def _planted_corpus(n_signal=20, n_rest=40, n_noise=6, seed=7):
    """A corpus with exactly one real effect and several pure-noise tags.

    The noise tags are assigned in balanced stripes across the liked and
    disliked sets, so carrying one is independent of polarity by construction.
    An earlier version handed them out at random, which quietly correlated
    them with the all-liked signal group and made a noise tag look real - the
    fixture was the confound, which is exactly what this arm is for.
    """
    verdicts = []
    for i in range(n_signal):
        verdicts.append({
            "work_id": i, "title": f"signal-{i}", "year": 2000, "medium": "film",
            "polarity": 1, "reasons": ["loved_it"],
            "tags": [("crew", "director:Planted", 3.0)],
        })
    for i in range(n_rest):
        pol = 1 if i % 2 == 0 else -1
        verdicts.append({
            "work_id": 1000 + i, "title": f"rest-{i}", "year": 2000, "medium": "film",
            "polarity": pol, "reasons": ["loved_it" if pol > 0 else "weak_craft"],
            "tags": [],
        })

    liked = [v for v in verdicts if v["polarity"] > 0]
    disliked = [v for v in verdicts if v["polarity"] < 0]
    for i in range(n_noise):
        for group in (liked, disliked):
            for v in group[i::n_noise]:
                v["tags"].append(("keyword", f"noise_{i}", 1.0))

    random.Random(seed).shuffle(verdicts)
    return verdicts


def test_profile_reports_insufficient_evidence_below_the_threshold():
    thin = _planted_corpus(n_signal=3, n_rest=4)
    report = taste_profile.build(thin)
    assert report["sufficient_evidence"] is False
    assert report["established"] == [], "nothing may be reported below the threshold"
    assert str(C.MIN_VERDICTS_FOR_PROFILE) in report["message"], "the threshold is named"


def test_profile_recovers_a_planted_signal():
    report = taste_profile.build(_planted_corpus())
    assert report["sufficient_evidence"] is True
    established = {e["tag"] for e in report["established"]}
    assert "crew:director:Planted" in established, "the real effect must be found"
    found = next(e for e in report["established"] if e["tag"] == "crew:director:Planted")
    assert found["direction"] == "likes"
    assert found["ci"][0] > 0, "the interval must exclude zero"


def test_profile_declines_to_invent_an_effect_for_noise():
    """The arm that proves the gate works rather than that it emits numbers."""
    report = taste_profile.build(_planted_corpus())
    established = {e["tag"] for e in report["established"]}
    leaked = [t for t in established if t.startswith("keyword:noise_")]
    assert not leaked, f"noise reported as established affinity: {leaked}"


def test_profile_is_deterministic():
    corpus = _planted_corpus()
    a = taste_profile.build(corpus)
    b = taste_profile.build(corpus)
    assert a["established"] == b["established"]


# ---------------------------------------------------------------------------
# Picker
# ---------------------------------------------------------------------------

def _seed_liked_director(con):
    wid = storage.upsert_work(con, medium="film", title="Burning", year=2018,
                              source="tmdb", source_id="99991")
    storage.set_tags(con, wid, [("crew", "director:Lee Chang-dong", 3.0),
                                ("keyword", "slow burn", 1.0)])
    storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"], rating=9)
    return wid


def test_picker_prefers_and_explains_a_shared_crew_member(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)

    match = storage.upsert_work(con, medium="film", title="Poetry", year=2010,
                                source="tmdb", source_id="88881", runtime_min=139)
    storage.set_tags(con, match, [("crew", "director:Lee Chang-dong", 3.0)])
    other = storage.upsert_work(con, medium="film", title="Loud Thing", year=2015,
                                source="tmdb", source_id="88882", runtime_min=100)
    storage.set_tags(con, other, [("crew", "director:Someone Else", 3.0)])

    candidates = [
        {"work_id": match, "title": "Poetry", "year": 2010, "medium": "film",
         "runtime_min": 139, "tags": storage.tags_for(con, match)},
        {"work_id": other, "title": "Loud Thing", "year": 2015, "medium": "film",
         "runtime_min": 100, "tags": storage.tags_for(con, other)},
    ]
    picks, _shelved = picker.rank(con, candidates, verdicts)
    assert picks[0]["title"] == "Poetry"
    assert "Lee Chang-dong" in picks[0]["why"] and "Burning" in picks[0]["why"]


def test_picker_shelves_rather_than_drops_when_tonight_does_not_fit(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    long_one = storage.upsert_work(con, medium="film", title="Poetry", year=2010,
                                   source="tmdb", source_id="88881", runtime_min=139)
    storage.set_tags(con, long_one, [("crew", "director:Lee Chang-dong", 3.0)])

    candidates = [{"work_id": long_one, "title": "Poetry", "year": 2010, "medium": "film",
                   "runtime_min": 139, "tags": storage.tags_for(con, long_one)}]
    picks, shelved = picker.rank(con, candidates, verdicts, state={"max_runtime_min": 90})
    assert picks == []
    assert len(shelved) == 1
    assert "139 min" in shelved[0]["tonight_blockers"][0]
    assert shelved[0]["trait_score"] > 0, "still a good match for their taste"


def test_picker_excludes_what_they_have_already_seen(con):
    seen_id = _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    candidates = [{"work_id": seen_id, "title": "Burning", "year": 2018, "medium": "film",
                   "runtime_min": 148, "tags": storage.tags_for(con, seen_id)}]
    picks, shelved = picker.rank(con, candidates, verdicts,
                                 seen_ids=storage.seen_work_ids(con))
    assert picks == [] and shelved == []


def test_picker_respects_a_state_axis_cap_from_the_users_own_correction(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    demanding = storage.upsert_work(con, medium="film", title="Hard Watch",
                                    source="tmdb", source_id="7777", runtime_min=95)
    storage.set_tags(con, demanding, [("crew", "director:Lee Chang-dong", 3.0)])
    con.execute("INSERT INTO work_axes (work_id, axis, score, provenance, at) VALUES (?,?,?,?,?)",
                (demanding, "load", 2.0, "model", 0.0))
    con.commit()

    candidates = [{"work_id": demanding, "title": "Hard Watch", "medium": "film",
                   "runtime_min": 95, "tags": storage.tags_for(con, demanding)}]
    picks, shelved = picker.rank(con, candidates, verdicts, state={"max_load": 0})
    assert picks == [] and len(shelved) == 1
    assert "load" in shelved[0]["tonight_blockers"][0]


def test_picker_says_cold_guess_when_it_looked_and_found_no_overlap(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    stranger = storage.upsert_work(con, medium="film", title="Unrelated",
                                   source="tmdb", source_id="4242", runtime_min=90)
    storage.set_tags(con, stranger, [("crew", "director:Nobody You Know", 3.0),
                                     ("keyword", "unrelated thing", 1.0)])
    candidates = [{"work_id": stranger, "title": "Unrelated", "medium": "film",
                   "runtime_min": 90, "tags": storage.tags_for(con, stranger)}]
    picks, _ = picker.rank(con, candidates, verdicts)
    assert picks[0]["looked_up"] is True
    assert picks[0]["trait_score"] == 0.0
    assert "cold guess" in picks[0]["why"]


def test_an_unexamined_candidate_is_not_claimed_to_be_unmatched(con):
    """Regression: 'never looked up' must not be reported as 'no overlap'.

    Found live. When the per-pick detail budget ran out, untagged candidates
    scored 0.0 and were explained as "nothing in your ledger overlaps", which
    claims a comparison that never happened - and let an unexamined title
    outrank a genuinely matched one.
    """
    matched_id = _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)

    good = storage.upsert_work(con, medium="film", title="Poetry", year=2010,
                               source="tmdb", source_id="88881", runtime_min=90)
    storage.set_tags(con, good, [("crew", "director:Lee Chang-dong", 3.0)])
    unknown = storage.upsert_work(con, medium="film", title="Never Fetched",
                                  source="tmdb", source_id="55555", runtime_min=90)

    candidates = [
        {"work_id": unknown, "title": "Never Fetched", "medium": "film",
         "runtime_min": 90, "tags": []},
        {"work_id": good, "title": "Poetry", "year": 2010, "medium": "film",
         "runtime_min": 90, "tags": storage.tags_for(con, good)},
    ]
    picks, _ = picker.rank(con, candidates, verdicts)

    assert picks[0]["title"] == "Poetry", "an examined match must outrank an unexamined title"
    unexamined = next(p for p in picks if p["title"] == "Never Fetched")
    assert unexamined["looked_up"] is False
    assert unexamined["trait_score"] is None, "no score may be asserted for it"
    assert "overlap" not in unexamined["why"] and "not looked up" in unexamined["why"]
    assert matched_id not in [p["work_id"] for p in picks]


def test_an_unchecked_constraint_is_reported_rather_than_passed(con):
    """A film with no known runtime must not silently 'fit' a time cap."""
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    no_runtime = storage.upsert_work(con, medium="film", title="Runtime Unknown",
                                     source="tmdb", source_id="66666")
    storage.set_tags(con, no_runtime, [("crew", "director:Lee Chang-dong", 3.0)])
    candidates = [{"work_id": no_runtime, "title": "Runtime Unknown", "medium": "film",
                   "runtime_min": None, "tags": storage.tags_for(con, no_runtime)}]
    picks, _ = picker.rank(con, candidates, verdicts, state={"max_runtime_min": 90})
    assert picks[0]["fits_tonight"] is True
    assert any("runtime unknown" in c for c in picks[0]["unchecked"]), picks[0]["unchecked"]


# ---------------------------------------------------------------------------
# Importer: the rows and files that must NOT become records
# ---------------------------------------------------------------------------

LETTERBOXD_CSV = (
    "Date,Name,Year,Letterboxd URI,Rating\n"
    "2024-02-01,Burning,2018,https://boxd.it/aaa,4.5\n"
    "2024-02-02,Poetry,2010,https://boxd.it/bbb,5\n"
    "2024-02-03,Bad Year,notayear,https://boxd.it/ccc,\n"
    ",,,,\n"
)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_import_refuses_a_file_that_is_not_an_export_and_leaks_nothing(tmp_path):
    secret = "AWS_SECRET_ACCESS_KEY=hunter2placeholder\nDB_PASSWORD=swordfishplaceholder\n"
    path = _write(tmp_path, "credentials.env", secret)
    with pytest.raises(source_base.FormatRefused) as exc:
        letterboxd_csv.probe({"path": path})
    msg = str(exc.value)
    assert "hunter2placeholder" not in msg and "swordfishplaceholder" not in msg
    assert "AWS_SECRET_ACCESS_KEY" not in msg and "DB_PASSWORD" not in msg
    assert "Letterboxd" in msg, "the refusal names the requirement instead"


def test_import_refuses_a_foreign_csv_without_listing_its_columns(tmp_path):
    path = _write(tmp_path, "payroll.csv", "employee_ssn,salary_usd\n123-45-6789,91000\n")
    with pytest.raises(source_base.FormatRefused) as exc:
        letterboxd_csv.probe({"path": path})
    msg = str(exc.value)
    assert "employee_ssn" not in msg and "salary_usd" not in msg and "123-45-6789" not in msg


def test_probe_writes_nothing(con, tmp_path):
    path = _write(tmp_path, "watched.csv", LETTERBOXD_CSV)
    out = letterboxd_csv.probe({"path": path})
    assert out["records_parsed"] == 3
    assert storage.counts(con)["works"] == 0, "probe must not touch the store"
    assert out["wrote_nothing"] is True


def test_import_apply_then_reapply_adds_nothing_the_second_time(con, tmp_path):
    path = _write(tmp_path, "watched.csv", LETTERBOXD_CSV)
    records, _assumed = letterboxd_csv.fetch({"path": path})
    first = source_base.ingest(con, records, source_name="letterboxd_csv")
    assert first["verdicts_inserted"] == 3
    before = storage.counts(con)

    records2, _ = letterboxd_csv.fetch({"path": path})
    source_base.ingest(con, records2, source_name="letterboxd_csv")
    after = storage.counts(con)
    assert after["works"] == before["works"], "re-import must not duplicate works"


def test_import_never_overwrites_a_verdict_the_user_entered_by_hand(con, tmp_path):
    wid = storage.upsert_work(con, medium="film", title="Burning", year=2018)
    storage.add_verdict(con, work_id=wid, verdict="bailed", reasons=["too_slow"],
                        note="mine", source="manual")

    path = _write(tmp_path, "watched.csv", LETTERBOXD_CSV)
    records, _ = letterboxd_csv.fetch({"path": path})
    acc = source_base.ingest(con, records, source_name="letterboxd_csv")

    assert acc["skipped_existing_user_records"] >= 1
    rows = con.execute(
        "SELECT verdict, note FROM verdicts WHERE work_id=? ORDER BY id", (wid,)).fetchall()
    assert len(rows) == 1 and rows[0]["note"] == "mine", "the hand-entered record survives intact"


def test_ambiguous_rating_scale_is_reported_not_laundered(tmp_path):
    csv_text = ("Date,Name,Year,Letterboxd URI,Rating\n"
                "2024-01-01,Star Rated,2000,https://boxd.it/x,4.5\n"
                "2024-01-02,Ten Rated,2001,https://boxd.it/y,8\n")
    path = _write(tmp_path, "ratings.csv", csv_text)
    records, assumed = letterboxd_csv.fetch({"path": path})

    by_title = {r["title"]: r for r in records}
    assert by_title["Star Rated"]["rating"] == 9, "4.5 stars doubles to 9/10"
    assert by_title["Ten Rated"]["rating"] == 8

    titles_assumed = {a["title"] for a in assumed}
    assert "Ten Rated" in titles_assumed, "the guess must be surfaced"
    assert "Star Rated" not in titles_assumed, "an unambiguous value must not be"


def test_ingest_accounting_reconciles(con, tmp_path):
    path = _write(tmp_path, "watched.csv", LETTERBOXD_CSV)
    records, _ = letterboxd_csv.fetch({"path": path})
    acc = source_base.ingest(con, records, source_name="letterboxd_csv")
    assert acc["rows_read"] == 3
    assert acc["no_usable_payload"] + acc["verdicts_inserted"] + \
           acc["skipped_existing_user_records"] + acc["rejected_bounds"] == acc["rows_read"]


# ---------------------------------------------------------------------------
# Services and export
# ---------------------------------------------------------------------------

def test_service_kinds_are_validated(con):
    storage.set_service(con, "Kanopy", "library", "US")
    with pytest.raises(ValueError):
        storage.set_service(con, "Something", "borrowed_from_a_mate")
    assert [s["name"] for s in storage.list_services(con)] == ["Kanopy"]


def test_export_contains_the_whole_ledger(con, tmp_path):
    wid = storage.upsert_work(con, medium="film", title="Burning", year=2018)
    storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"], rating=9)
    storage.set_service(con, "Mubi", "subscription")
    data = storage.export_all(con)

    assert data["format"] == "palate-export"
    assert len(data["works"]) == 1 and len(data["verdicts"]) == 1
    assert data["verdict_reasons"][0]["reason"] == "loved_it"
    assert len(data["services"]) == 1
    json.dumps(data, default=str)  # must be serialisable


def _cand(wid, con, free_on=(), rent_on_yours=(), rent_on=()):
    return {"work_id": wid, "title": "Thing", "medium": "film", "runtime_min": 100,
            "tags": storage.tags_for(con, wid), "free_on": list(free_on),
            "rent_on_yours": list(rent_on_yours), "rent_on": list(rent_on)}


def test_a_rental_is_offered_only_when_the_user_accepts_that_provider(con):
    """Per-provider consent: Amazon rentals may be fine and Apple's may not."""
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    wid = storage.upsert_work(con, medium="film", title="Rentable", source="tmdb",
                              source_id="RR", runtime_min=100)
    storage.set_tags(con, wid, [("crew", "director:Lee Chang-dong", 3.0)])
    storage.set_service(con, "Amazon Video", "rent")

    accepted = _cand(wid, con, rent_on_yours=["Amazon Video"], rent_on=["Amazon Video"])
    picks, shelved = picker.rank(con, [accepted], verdicts,
                                 state={"availability": "free_or_rent", "has_services": True})
    assert picks and picks[0]["cost"] == "rent"
    assert picks[0]["rent_on_yours"] == ["Amazon Video"]
    assert shelved == []

    # Same title, but it only rents from a storefront the user did not declare.
    refused = _cand(wid, con, rent_on=["Apple TV Store"])
    picks, shelved = picker.rank(con, [refused], verdicts,
                                 state={"availability": "free_or_rent", "has_services": True})
    assert picks == [] and len(shelved) == 1
    assert "not on a rental provider you accept" in shelved[0]["tonight_blockers"][0]
    assert "Apple TV Store" in shelved[0]["tonight_blockers"][0]


def test_free_mode_rejects_a_rental_the_user_would_otherwise_accept(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    wid = storage.upsert_work(con, medium="film", title="Rentable", source="tmdb",
                              source_id="RR", runtime_min=100)
    storage.set_tags(con, wid, [("crew", "director:Lee Chang-dong", 3.0)])
    cand = _cand(wid, con, rent_on_yours=["Amazon Video"], rent_on=["Amazon Video"])
    picks, shelved = picker.rank(con, [cand], verdicts,
                                 state={"availability": "free", "has_services": True})
    assert picks == []
    assert "not free on your services" in shelved[0]["tonight_blockers"][0]


def test_the_buckets_do_not_cross(con):
    """A rent provider must not make a title look free, and vice versa."""
    av = {"flatrate": ["Hulu"], "free": ["Tubi TV"], "ads": [],
          "rent": ["Amazon Video"], "buy": ["Apple TV Store"]}
    assert tmdb.free_on(av, ["Hulu", "Tubi TV"]) == ["Hulu", "Tubi TV"]
    assert tmdb.free_on(av, ["Amazon Video"]) == [], \
        "a rental storefront must never register as free"
    assert tmdb.paid_on(av, ["Amazon Video", "Apple TV Store"]) == \
        ["Amazon Video", "Apple TV Store"]
    assert tmdb.paid_on(av, ["Hulu"]) == [], \
        "a subscription must never satisfy a rental request"


def test_availability_with_no_services_is_unchecked_not_satisfied(con):
    """The honesty rule: an unverifiable constraint is reported, not passed."""
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    wid = storage.upsert_work(con, medium="film", title="Unknown Access",
                              source="tmdb", source_id="UA", runtime_min=100)
    storage.set_tags(con, wid, [("crew", "director:Lee Chang-dong", 3.0)])
    picks, _ = picker.rank(con, [_cand(wid, con)], verdicts,
                           state={"availability": "free", "has_services": False})
    assert picks[0]["fits_tonight"] is True
    assert picks[0]["cost"] == "unknown"
    assert any("no services declared" in c for c in picks[0]["unchecked"])


def test_any_mode_applies_no_availability_filter(con):
    _seed_liked_director(con)
    verdicts = storage.verdicts_with_tags(con)
    wid = storage.upsert_work(con, medium="film", title="Nowhere", source="tmdb",
                              source_id="NW", runtime_min=100)
    storage.set_tags(con, wid, [("crew", "director:Lee Chang-dong", 3.0)])
    picks, _ = picker.rank(con, [_cand(wid, con)], verdicts,
                           state={"availability": "any", "has_services": True})
    assert picks[0]["fits_tonight"] is True
    assert picks[0]["cost"] == "unavailable", "still reported honestly as costing something"


def test_rent_and_buy_are_valid_service_kinds(con):
    storage.set_service(con, "Amazon Video", "rent")
    storage.set_service(con, "Apple TV Store", "buy")
    kinds = {s["name"]: s["kind"] for s in storage.list_services(con)}
    assert kinds == {"Amazon Video": "rent", "Apple TV Store": "buy"}
    assert "rent" in C.SERVICE_KINDS and "rent" not in C.FREE_AT_POINT_OF_USE


def test_the_deprecated_require_free_flag_still_constrains(con):
    """An older caller must not silently lose its constraint."""
    from palate import tools

    mode = tools._resolve_availability({"require_free": True})
    assert mode == "free"
    assert tools._resolve_availability({}) == C.DEFAULT_AVAILABILITY
    assert tools._resolve_availability({"availability": "any"}) == "any"
    assert tools._resolve_availability({"availability": "nonsense"}) == C.DEFAULT_AVAILABILITY


def test_interleave_gives_every_pivot_a_share_of_the_pool(con):
    """The bug this replaced: concatenation handed pivot 1 the whole pool.

    TMDB returns a full page per pivot against a pool of 24, so extend() then
    truncate gave pivot 1 about 20 slots, pivot 2 the remainder, and every
    pivot after that nothing at all.
    """
    from palate import tools

    groups = [[f"{name}-{i}" for i in range(20)]
              for name in ("west", "anim", "noir", "docu", "scifi", "comedy")]
    pool = tools._interleave(groups)[:24]
    reached = {item.split("-")[0] for item in pool}
    assert reached == {"west", "anim", "noir", "docu", "scifi", "comedy"}, \
        f"only {reached} reached the pool"
    per_source = Counter(item.split("-")[0] for item in pool)
    assert set(per_source.values()) == {4}, f"uneven split: {per_source}"

    # And the old behaviour, for contrast.
    naive = [x for g in groups for x in g][:24]
    assert {i.split("-")[0] for i in naive} == {"west", "anim"}


def test_interleave_tolerates_uneven_and_empty_groups(con):
    from palate import tools

    assert tools._interleave([]) == []
    assert tools._interleave([[], []]) == []
    assert tools._interleave([["a"], ["b", "c", "d"]]) == ["a", "b", "c", "d"]


def test_no_more_than_two_picks_trace_to_one_favourite(con):
    ordered = [{"title": f"west{i}", "from_pivots": ["Across the Great Divide"]}
               for i in range(5)]
    ordered += [{"title": "anim1", "from_pivots": ["Coraline"]},
                {"title": "noir1", "from_pivots": ["The Big Lebowski"]}]
    picks = picker._diversify(ordered, limit=4)
    counts = Counter(p["from_pivots"][0] for p in picks)
    assert counts["Across the Great Divide"] == 2
    assert len(picks) == 4
    assert {"anim1", "noir1"} <= {p["title"] for p in picks}


def test_the_diversity_cap_never_returns_fewer_picks_than_asked(con):
    """A preference between eligible candidates, not a reason to answer less."""
    ordered = [{"title": f"west{i}", "from_pivots": ["Across the Great Divide"]}
               for i in range(6)]
    picks = picker._diversify(ordered, limit=5)
    assert len(picks) == 5, "the cap must be filled from what it held back"
    assert [p["title"] for p in picks] == [f"west{i}" for i in range(5)], \
        "and the held-back items must come back in score order"


def test_a_candidate_several_favourites_recommend_keeps_all_of_them(con):
    """Being recommended by three favourites is a stronger signal than one."""
    ordered = [{"title": "both", "from_pivots": ["Coraline", "Ink"]}]
    assert picker._diversify(ordered, limit=3)[0]["from_pivots"] == ["Coraline", "Ink"]


def test_a_weak_match_is_labelled_relative_to_the_best_one(con):
    """The failure: 2.0 and 9.8 printed in the same shape.

    Thresholds are relative on purpose. Scores are uncalibrated and their
    magnitude moves with ledger size and rarity weighting, so an absolute
    cutoff would be a number invented from nothing.
    """
    strong, note = picker.match_strength(9.8, 9.8, overlap=5)
    assert strong == "strong" and "5 overlapping tags" in note

    weak, note = picker.match_strength(2.0, 9.8, overlap=4)
    assert weak == "stretch", "20% of the best score is not a recommendation"
    assert "20%" in note and "4 overlapping tags" in note

    mid, note = picker.match_strength(4.0, 9.8, overlap=4)
    assert mid == "moderate" and "41%" in note

    # 6.0/9.8 is 61%, just above the 60% boundary, so it is strong. Pinned
    # deliberately: the boundary is a judgement and should fail loudly if moved.
    assert picker.match_strength(6.0, 9.8, overlap=4)[0] == "strong"
    assert picker.match_strength(5.8, 9.8, overlap=4)[0] == "moderate"


def test_one_lucky_rare_tag_is_never_a_strong_match(con):
    """Top of the pool on a single overlap is still a thin suggestion."""
    label, note = picker.match_strength(9.8, 9.8, overlap=1)
    assert label == "stretch"
    assert "only 1 tag overlaps" in note


def test_a_net_negative_candidate_says_so(con):
    label, note = picker.match_strength(-3.0, 9.8, overlap=4)
    assert label == "against"
    assert "disliked" in note


def test_match_strength_is_unknown_for_an_unexamined_candidate(con):
    assert picker.match_strength(None, 9.8, overlap=0) == (None, None)


def test_rank_attaches_strength_relative_to_the_whole_answer(con):
    # A liked work carrying several tags, so a candidate can genuinely overlap
    # on more than one. The shared _seed_liked_director fixture tags only two,
    # which would correctly cap everything at "stretch" and prove nothing.
    liked = storage.upsert_work(con, medium="film", title="Burning", year=2018,
                                source="tmdb", source_id="99991")
    storage.set_tags(con, liked, [("crew", "director:Lee Chang-dong", 3.0),
                                  ("keyword", "candid", 1.0),
                                  ("keyword", "rural", 1.0),
                                  ("genre", "Drama", 1.0)])
    storage.add_verdict(con, work_id=liked, verdict="seen", reasons=["loved_it"], rating=9)
    verdicts = storage.verdicts_with_tags(con)
    cands = []
    for name, tags in (("Strong", [("crew", "director:Lee Chang-dong", 3.0),
                                   ("keyword", "candid", 1.0),
                                   ("keyword", "rural", 1.0),
                                   ("genre", "Drama", 1.0)]),
                       ("Thin", [("genre", "Drama", 1.0)])):
        wid = storage.upsert_work(con, medium="film", title=name, source="tmdb",
                                  source_id=name, runtime_min=100)
        storage.set_tags(con, wid, tags)
        cands.append({"work_id": wid, "title": name, "medium": "film",
                      "runtime_min": 100, "tags": storage.tags_for(con, wid),
                      "free_on": ["Hulu"]})
    picks, _ = picker.rank(con, cands, verdicts,
                           state={"availability": "any", "has_services": True}, limit=5)
    by_title = {p["title"]: p for p in picks}
    assert by_title["Strong"]["match_strength"] == "strong"
    assert by_title["Thin"]["match_strength"] == "stretch"
    assert by_title["Thin"]["overlap_tags"] < by_title["Strong"]["overlap_tags"]
    assert by_title["Thin"]["strength_note"]


def test_discover_refuses_a_half_availability_request(con, monkeypatch):
    """Providers without a region are silently ignored by TMDB.

    Sending that half request would filter on nothing while the output claimed
    availability had been applied.
    """
    seen = {}

    def fake_get(api_key, path, **params):
        seen.update(params)
        return {"results": []}

    monkeypatch.setattr(tmdb, "_get", fake_get)

    tmdb.discover("k", "film", with_providers=[15, 386])
    assert seen["with_watch_providers"] is None, "no region, so no provider filter"
    assert seen["watch_region"] is None

    tmdb.discover("k", "film", with_providers=[15, 386], watch_region="US",
                  monetization="flatrate|free|ads")
    assert seen["with_watch_providers"] == "15|386"
    assert seen["watch_region"] == "US"
    assert seen["with_watch_monetization_types"] == "flatrate|free|ads"


def test_provider_ids_matches_exactly_and_reports_what_it_could_not_match(con, monkeypatch):
    """A fuzzy match would filter on the wrong service and still look fine."""
    monkeypatch.setattr(tmdb, "_get", lambda api_key, path, **p: {"results": [
        {"provider_name": "Tubi TV", "provider_id": 73},
        {"provider_name": "HBO Max", "provider_id": 1899},
        {"provider_name": "Hulu", "provider_id": 15},
    ]})
    found, unmatched = tmdb.provider_ids("k", "film", ["hulu", "TUBI TV", "Nonesuch"])
    assert found == {"Hulu": 15, "Tubi TV": 73}, "case-insensitive but exact"
    assert unmatched == ["nonesuch"]
    assert tmdb.provider_ids("k", "film", []) == ({}, [])


def test_the_strongest_pivot_is_never_evicted_by_a_newer_one(con):
    """The bug: pivots[-N:] kept the most RECENT favourites.

    Logging a seventh liked film dropped the oldest pivot, which happened to
    carry the single best match in the ledger. The answer still looked well
    scored, just quietly without its best item.
    """
    from palate import tools

    liked = []
    for i, (title, reasons, rating) in enumerate([
            ("Oldest Loved", ["loved_it"], None),
            ("Mid Rated", [], 8),
            ("Newest Loved", ["loved_it"], None),
            ("Newest Mild", [], 7)]):
        liked.append({"title": title, "reasons": reasons, "rating": rating,
                      "verdict_id": i + 1, "polarity": 1})

    def rank(v):
        loved = 1 if "loved_it" in (v.get("reasons") or []) else 0
        return (-loved, -(v.get("rating") or 0), -(v.get("verdict_id") or 0))

    ordered = [v["title"] for v in sorted(liked, key=rank)]
    assert ordered[:2] == ["Newest Loved", "Oldest Loved"], ordered
    assert "Oldest Loved" in ordered[:2], \
        "an explicit loved_it must outrank a newer mild verdict"
    # And the old behaviour, for contrast: recency alone drops it.
    by_recency = [v["title"] for v in sorted(liked, key=lambda v: -v["verdict_id"])]
    assert by_recency[-1] == "Oldest Loved"
    assert tools.PIVOT_LIMIT >= 8, "the limit must cover a normal favourites list"


def test_the_pool_grows_with_the_number_of_generators(con):
    """A fixed pool divided by many groups starves each one."""
    from palate import tools

    for groups, expected_min in ((2, tools.CANDIDATE_POOL),
                                 (8, 8 * tools.PER_GROUP_MIN),
                                 (40, tools.MAX_POOL)):
        size = min(tools.MAX_POOL,
                   max(tools.CANDIDATE_POOL, tools.PER_GROUP_MIN * groups))
        assert size >= min(expected_min, tools.MAX_POOL)
        assert size <= tools.MAX_POOL, "but it stays bounded"
    assert tools.PER_GROUP_MIN * 8 > tools.CANDIDATE_POOL, \
        "with 8 groups the base pool would give each fewer than PER_GROUP_MIN"


def test_export_refuses_the_paths_a_prompt_injection_would_aim_at(con, tmp_path, monkeypatch):
    """palate_export took an arbitrary path from a model-supplied argument.

    An injected call could overwrite a shell rc, ~/.ssh/authorized_keys or
    Hermes' own config with export JSON, bypassing the denylist Hermes' file
    tools enforce. Each gate is asserted separately so a regression names
    which one broke.
    """
    from palate import tools

    # Gate 1: Hermes' own denylist, stubbed so the test does not depend on
    # which paths this machine happens to protect.
    class FakeFS:
        @staticmethod
        def is_write_denied(path):
            return "authorized_keys" in path or path.endswith("config.yaml")

        @staticmethod
        def get_write_denied_error(path, **_kw):
            return f"Write denied: '{path}' is a protected system/credential file."

    monkeypatch.setattr(tools, "_file_safety", lambda: FakeFS)
    for target in (os.path.expanduser("~/.ssh/authorized_keys"),
                   str(tmp_path / "config.yaml")):
        err, checks = tools._export_write_guard(target)
        assert err and "denied" in err.lower(), target
        assert checks["denylist"] == "checked"

    # Gate 2: suffix. A .json requirement keeps the tool from being aimed at
    # a script or rc file even where the denylist has no opinion.
    for target in (os.path.expanduser("~/.bashrc"),
                   str(tmp_path / "run.sh"),
                   str(tmp_path / "notes")):
        err, checks = tools._export_write_guard(target)
        assert err and ".json" in err, target
        assert checks["suffix"] == "rejected"

    # Gate 3: no clobbering a file that is not already our own export.
    victim = tmp_path / "important.json"
    victim.write_text(json.dumps({"some": "other tool's data"}), encoding="utf-8")
    err, checks = tools._export_write_guard(str(victim))
    assert err and "already exists" in err
    assert checks["clobber"] == "rejected"

    # An earlier palate export MAY be overwritten; that is the normal case.
    victim.write_text(json.dumps({"format": "palate-export", "version": 1}), encoding="utf-8")
    err, checks = tools._export_write_guard(str(victim))
    assert err is None
    assert "earlier palate export" in checks["clobber"]

    # A fresh .json path is fine.
    assert tools._export_write_guard(str(tmp_path / "new.json"))[0] is None


def test_export_refuses_to_write_inside_the_hermes_home(con, tmp_home, tmp_path):
    """The denylist protects known files, so a NEW file there would pass it.

    An export has no business inside Hermes' own directory: the plugin's
    storage lives there and is managed by storage.py, not by this tool.
    """
    from palate import tools

    for target in ("credentials.json", "config.json", "plugin-data/anything.json"):
        err, checks = tools._export_write_guard(os.path.join(tmp_home, target))
        assert err and "Hermes home" in err, target
        assert checks["hermes_home"] == "rejected"

    # Outside it, the same filename is allowed.
    assert tools._export_write_guard(str(tmp_path / "credentials.json"))[0] is None


def test_a_denylist_that_cannot_be_consulted_fails_closed(con, tmp_path, monkeypatch):
    """Losing the check must make the guard stricter, never weaker."""
    from palate import tools

    class Exploding:
        @staticmethod
        def is_write_denied(path):
            raise RuntimeError("denylist unavailable")

    monkeypatch.setattr(tools, "_file_safety", lambda: Exploding)
    err, checks = tools._export_write_guard(str(tmp_path / "fine.json"))
    assert err and "could not verify" in err
    assert checks["denylist"] == "errored, treated as deny"

    # With the module absent entirely, the remaining gates still apply.
    monkeypatch.setattr(tools, "_file_safety", lambda: None)
    assert tools._export_write_guard(os.path.expanduser("~/.bashrc"))[0]
    err, checks = tools._export_write_guard(str(tmp_path / "fine.json"))
    assert err is None and checks["denylist"] == "not available", \
        "and the result says the denylist did not run, rather than implying it passed"


def test_export_through_the_tool_refuses_and_writes_nothing(con, tmp_path):
    from palate import tools

    victim = tmp_path / "keep.txt"
    victim.write_text("original contents", encoding="utf-8")
    out = json.loads(tools.palate_export({"path": str(victim)}))
    assert out["ok"] is False and out.get("refused") is True
    assert victim.read_text(encoding="utf-8") == "original contents", \
        "the victim file must be untouched"

    good = tmp_path / "export.json"
    out = json.loads(tools.palate_export({"path": str(good)}))
    assert out["ok"] is True and good.is_file()
    assert out["write_checks"]["suffix"] == "ok"


def test_import_consults_the_read_denylist(con, tmp_path, monkeypatch):
    """An injected import could otherwise be aimed at a secret-bearing file."""
    secret = tmp_path / ".env"
    secret.write_text("API_KEY=should-never-be-read\n", encoding="utf-8")

    import agent.file_safety as real_fs

    def deny(path):
        if str(path).endswith(".env"):
            raise ValueError(f"Access denied: {path} is a secret-bearing environment file.")

    monkeypatch.setattr(real_fs, "raise_if_read_blocked", deny)
    with pytest.raises(source_base.FormatRefused) as caught:
        source_base.read_csv_guarded(str(secret), require_all=("name",), label="Letterboxd")
    assert "Access denied" in str(caught.value)
    assert "should-never-be-read" not in str(caught.value), \
        "a refusal must carry no content from the file"


def test_a_tagging_rules_change_invalidates_the_cache_but_keeps_the_ledger(con):
    """Tags are cached per work, so a rules change must clear them.

    Otherwise new installs get the new weighting while an existing ledger
    keeps the old tags forever: _ensure_tags skips anything already tagged,
    and nothing anywhere reports that the cache is stale.
    """
    wid = storage.upsert_work(con, medium="film", title="Burning", year=2018)
    storage.set_tags(con, wid, [("keyword", "based on novel or book", 1.0),
                                ("crew", "cast:Someone", 0.75)])
    storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"], rating=9)
    storage.set_service(con, "Mubi", "subscription")
    con.execute("UPDATE settings SET value='1' WHERE key='tag_rules_version'")
    con.commit()
    assert storage.tags_for(con, wid), "tags present under the old rules"

    fresh = storage.connect()          # simulates the next process after an upgrade
    try:
        assert storage.tags_for(fresh, wid) == [], "stale tags must be dropped"
        assert fresh.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1, \
            "verdicts must survive"
        assert fresh.execute(
            "SELECT COUNT(*) FROM verdict_reasons").fetchone()[0] == 1, "reasons must survive"
        assert len(storage.list_services(fresh)) == 1, "services must survive"
        assert storage.get_setting(fresh, "tag_rules_version") == C.TAG_RULES_VERSION

        again = storage.connect()
        assert storage.get_setting(again, "tag_rules_version") == C.TAG_RULES_VERSION, \
            "and it must not re-clear on every open"
        again.close()
    finally:
        fresh.close()


def test_structural_keywords_and_bit_part_cast_are_kept_out_of_the_signal():
    """Provenance keywords are dropped; cast is weighted far below crew.

    Found on first real use: "based on novel or book" appeared in three of
    five explanations, and rarity weighting promoted a kids' cartoon because
    it shared a voice actor with The Big Lebowski.
    """
    from palate import tmdb

    det = {
        "crew": [("director", "Rare Auteur"), ("director of photography", "A DP")],
        "cast": ["Lead", "Second", "Third", "Fourth", "Fifth", "Sixth"],
        "keywords": ["based on novel or book", "woman director", "duringcreditsstinger",
                     "slow burn", "one location"],
        "genres": ["Drama"],
    }
    rows = tmdb.tags_from_details(det)
    by_kind = {}
    for kind, value, weight in rows:
        by_kind.setdefault(kind, {})[value] = weight

    keywords = set(by_kind["keyword"])
    assert keywords == {"slow burn", "one location"}, \
        f"structural keywords leaked into the signal: {keywords}"

    cast = {k: v for k, v in by_kind["crew"].items() if k.startswith("cast:")}
    assert len(cast) == tmdb.CAST_LIMIT == 3, "only the top-billed few are kept"
    assert all(v == tmdb.CAST_WEIGHT for v in cast.values())
    assert by_kind["crew"]["director:Rare Auteur"] > 10 * tmdb.CAST_WEIGHT, \
        "a shared director must dominate a shared actor"


def test_the_rarity_multiplier_is_capped(con):
    """An uncapped log lets a twice-seen tag claim unbounded informativeness."""
    for i in range(40):
        wid = storage.upsert_work(con, medium="film", title=f"W {i}", year=2000,
                                  source="tmdb", source_id=f"C{i}")
        tags = [("genre", "Drama", 0.5)]
        if i < 2:
            tags.append(("crew", "cast:Someone Rare", 0.2))
        storage.set_tags(con, wid, tags)
    idf, corpus = picker.tag_idf(con)
    import math
    assert math.log(corpus / 3) > picker.IDF_CAP, "the test corpus must exceed the cap"
    assert idf["crew:cast:Someone Rare"] == picker.IDF_CAP


def test_a_ubiquitous_tag_is_discounted_against_a_rare_one(con):
    """Rarity weighting: a shared director must outweigh a shared genre.

    Found on first real use. Without it, "based on novel or book" and "Drama"
    appeared in four of six explanations and dragged generic candidates up the
    ranking -- a keyword carried by tens of thousands of films counted the
    same as a shared crew member.
    """
    # A corpus where "Drama" is on everything and the director is on two.
    liked = []
    for i in range(10):
        wid = storage.upsert_work(con, medium="film", title=f"Liked {i}", year=2000 + i,
                                  source="tmdb", source_id=f"L{i}")
        tags = [("genre", "Drama", 0.5)]
        if i < 2:
            tags.append(("crew", "director:Rare Auteur", 3.0))
        storage.set_tags(con, wid, tags)
        storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"])
        liked.append(wid)
    verdicts = storage.verdicts_with_tags(con)

    generic = storage.upsert_work(con, medium="film", title="Generic Drama",
                                  source="tmdb", source_id="G1", runtime_min=100)
    storage.set_tags(con, generic, [("genre", "Drama", 0.5)])
    rare = storage.upsert_work(con, medium="film", title="Auteur Film",
                               source="tmdb", source_id="R1", runtime_min=100)
    storage.set_tags(con, rare, [("genre", "Drama", 0.5),
                                 ("crew", "director:Rare Auteur", 3.0)])

    idf, corpus = picker.tag_idf(con)
    assert corpus >= picker.MIN_DOCS_FOR_IDF, "corpus must be large enough to weight"
    assert idf["genre:Drama"] < idf["crew:director:Rare Auteur"]

    candidates = [
        {"work_id": generic, "title": "Generic Drama", "medium": "film",
         "runtime_min": 100, "tags": storage.tags_for(con, generic)},
        {"work_id": rare, "title": "Auteur Film", "medium": "film",
         "runtime_min": 100, "tags": storage.tags_for(con, rare)},
    ]
    picks, _ = picker.rank(con, candidates, verdicts)
    assert picks[0]["title"] == "Auteur Film"
    assert picks[0]["rarity_weighted"] is True
    assert "Rare Auteur" in picks[0]["why"], picks[0]["why"]


def test_a_tag_on_every_work_contributes_nothing_rather_than_inverting(con):
    """idf must floor at 0: a negative multiplier would flip the sign."""
    for i in range(10):
        wid = storage.upsert_work(con, medium="film", title=f"W {i}", year=2000 + i,
                                  source="tmdb", source_id=f"W{i}")
        storage.set_tags(con, wid, [("genre", "Drama", 0.5)])
        storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"])
    idf, _corpus = picker.tag_idf(con)
    assert idf["genre:Drama"] == 0.0

    verdicts = storage.verdicts_with_tags(con)
    only_generic = storage.upsert_work(con, medium="film", title="Only Drama",
                                       source="tmdb", source_id="OD", runtime_min=90)
    storage.set_tags(con, only_generic, [("genre", "Drama", 0.5)])
    picks, _ = picker.rank(con, [{
        "work_id": only_generic, "title": "Only Drama", "medium": "film",
        "runtime_min": 90, "tags": storage.tags_for(con, only_generic)}], verdicts)
    assert picks[0]["trait_score"] == 0.0, "a universal tag must add nothing"
    assert "cold guess" in picks[0]["why"], "and must not be offered as a reason"


def test_rarity_weighting_is_declined_on_a_corpus_too_small_to_estimate_it(con):
    """Below the threshold, fall back to flat weights and say so."""
    wid = storage.upsert_work(con, medium="film", title="Lonely", year=2001,
                              source="tmdb", source_id="X1")
    storage.set_tags(con, wid, [("genre", "Drama", 0.5)])
    storage.add_verdict(con, work_id=wid, verdict="seen", reasons=["loved_it"])
    idf, corpus = picker.tag_idf(con)
    assert idf == {} and corpus < picker.MIN_DOCS_FOR_IDF

    verdicts = storage.verdicts_with_tags(con)
    cand = storage.upsert_work(con, medium="film", title="Other", source="tmdb",
                               source_id="X2", runtime_min=90)
    storage.set_tags(con, cand, [("genre", "Drama", 0.5)])
    picks, _ = picker.rank(con, [{
        "work_id": cand, "title": "Other", "medium": "film", "runtime_min": 90,
        "tags": storage.tags_for(con, cand)}], verdicts)
    assert picks[0]["rarity_weighted"] is False
    assert picks[0]["trait_score"] > 0, "flat weighting still scores the overlap"


def test_the_key_is_never_read_from_the_environment(monkeypatch):
    """Regression for admission rule 11.

    An os.environ fallback can hand back another profile's key under
    multiplexing, and an undeclared env read contradicts the empty
    requires_env this plugin ships. The config route is the only route.
    """
    from palate import tools

    monkeypatch.setenv("PALATE_TMDB_API_KEY", "sentinel-must-not-be-used")
    monkeypatch.setattr(tools, "_get_config", lambda _key, _default=None: None)
    assert tools._api_key() is None, "the environment must not supply the key"

    monkeypatch.setattr(tools, "_get_config", lambda key, _default=None:
                        "from-config" if key == "tmdb_api_key" else None)
    assert tools._api_key() == "from-config"


def test_every_reason_declares_a_polarity_and_known_axis():
    for name, spec in C.REASONS.items():
        assert spec["polarity"] in (-1, 0, 1), name
        assert spec["axis"] is None or spec["axis"] in C.AXES + ("length",), name
