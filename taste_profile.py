"""Taste profile with an honesty gate.

Any tool that reports "you like X" over a handful of records is a horoscope.
So every affinity here must clear three bars before it is reported at all:

  1. enough taste-bearing verdicts overall (constants.MIN_VERDICTS_FOR_PROFILE)
  2. enough works sharing the tag        (constants.MIN_TAG_SUPPORT)
  3. a bootstrap confidence interval that does not straddle zero

Anything short of that is returned as an explicit insufficient-evidence
verdict naming the threshold, never as a number. A false affinity is worse
than no affinity, because the user acts on it.
"""

import random

from . import constants as C


def tag_key(kind, value):
    return f"{kind}:{value}"


def _collect(verdicts):
    """-> (samples, tag_support) where samples is [(polarity, {tag_key: weight})]."""
    samples, support = [], {}
    for v in verdicts:
        tags = {tag_key(k, val): w for k, val, w in v["tags"]}
        samples.append((v["polarity"], tags))
        for t in tags:
            support[t] = support.get(t, 0) + 1
    return samples, support


def _point(samples, tag):
    """Like-rate among works carrying the tag, minus the overall like-rate.

    Positive means the tag is associated with liking something beyond the
    user's baseline enthusiasm, which is the only version of this number that
    means anything: someone who likes 90% of what they watch is not telling
    you about a tag when they like a film carrying it.
    """
    with_tag = [p for p, tags in samples if tag in tags]
    if not with_tag:
        return None
    base = sum(1 for p, _ in samples if p > 0) / len(samples)
    rate = sum(1 for p in with_tag if p > 0) / len(with_tag)
    return rate - base


def _bootstrap_ci(samples, tag, n=C.BOOTSTRAP_SAMPLES, alpha=C.CI_ALPHA, rng=None):
    rng = rng or random.Random(1234567)
    k = len(samples)
    estimates = []
    for _ in range(n):
        resample = [samples[rng.randrange(k)] for _ in range(k)]
        est = _point(resample, tag)
        if est is not None:
            estimates.append(est)
    if len(estimates) < n * 0.5:
        return None  # the tag vanished from most resamples: too rare to speak about
    estimates.sort()
    lo = estimates[int((alpha / 2) * len(estimates))]
    hi = estimates[min(len(estimates) - 1, int((1 - alpha / 2) * len(estimates)))]
    return lo, hi


def build(verdicts, *, min_support=C.MIN_TAG_SUPPORT, seed=1234567):
    """Returns a report dict. Never raises on thin data; says so instead."""
    samples, support = _collect(verdicts)
    total = len(samples)
    report = {
        "taste_bearing_verdicts": total,
        "threshold": C.MIN_VERDICTS_FOR_PROFILE,
        "established": [],
        "inconclusive": [],
        "sufficient_evidence": total >= C.MIN_VERDICTS_FOR_PROFILE,
    }
    if total < C.MIN_VERDICTS_FOR_PROFILE:
        report["message"] = (
            f"Not enough evidence yet: {total} taste-bearing verdicts, "
            f"{C.MIN_VERDICTS_FOR_PROFILE} needed before any affinity is reported. "
            "Picks still work; they just lean on similarity rather than a profile."
        )
        return report

    rng = random.Random(seed)
    for tag, n in sorted(support.items(), key=lambda kv: -kv[1]):
        if n < min_support:
            report["inconclusive"].append(
                {"tag": tag, "support": n, "why": f"only {n} works carry it; {min_support} needed"})
            continue
        point = _point(samples, tag)
        ci = _bootstrap_ci(samples, tag, alpha=C.CI_ALPHA, rng=rng)
        if ci is None:
            report["inconclusive"].append({"tag": tag, "support": n, "why": "too rare to resample"})
            continue
        lo, hi = ci
        if lo <= 0.0 <= hi:
            report["inconclusive"].append({
                "tag": tag, "support": n, "point": round(point, 3),
                "ci": [round(lo, 3), round(hi, 3)],
                "why": "interval straddles zero; direction not established"})
        else:
            report["established"].append({
                "tag": tag, "support": n, "point": round(point, 3),
                "ci": [round(lo, 3), round(hi, 3)],
                "direction": "likes" if point > 0 else "avoids"})

    report["established"].sort(key=lambda d: -abs(d["point"]))
    report["message"] = (
        f"{len(report['established'])} established affinities from {total} verdicts; "
        f"{len(report['inconclusive'])} tags seen but not established."
    )
    return report
