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

from palate import constants as C
from palate import picker, storage, taste_profile
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


def test_every_reason_declares_a_polarity_and_known_axis():
    for name, spec in C.REASONS.items():
        assert spec["polarity"] in (-1, 0, 1), name
        assert spec["axis"] is None or spec["axis"] in C.AXES + ("length",), name
