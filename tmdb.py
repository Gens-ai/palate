"""TMDB client: stdlib only, so the plugin ships with no Python dependencies.

TMDB is the one network dependency. It serves the film corpus (search,
discover, keywords, credits) and, through /watch/providers, streaming
availability licensed from JustWatch.

Attribution is a licence condition, not a courtesy: TMDB must be credited for
the data and JustWatch must be credited by name for availability. Both are
stated in the README and echoed in tool output that uses them.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.themoviedb.org/3"
UA = "palate-hermes-plugin/0.1 (+https://github.com/Gens-ai/palate)"
TIMEOUT = 12
ATTRIBUTION = "Data from TMDB (not endorsed or certified by TMDB); availability from JustWatch."

# Availability moves; metadata effectively does not.
AVAILABILITY_TTL = 24 * 3600


class TmdbError(RuntimeError):
    pass


class NotConfigured(TmdbError):
    pass


def _get(api_key, path, **params):
    if not api_key:
        raise NotConfigured(
            "No TMDB API key. Get a free one at themoviedb.org (account settings > API), then: "
            "hermes config set plugins.entries.palate.settings.tmdb_api_key <key>"
        )
    params = {k: v for k, v in params.items() if v is not None}
    params["api_key"] = api_key
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise NotConfigured("TMDB rejected the API key (HTTP 401). Check the configured value.") from exc
        raise TmdbError(f"TMDB HTTP {exc.code} on {path}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TmdbError(f"TMDB unreachable: {exc}") from exc
    except ValueError as exc:
        raise TmdbError(f"TMDB returned something that is not JSON: {exc}") from exc


MEDIUM_TO_PATH = {"film": "movie", "tv": "tv"}

# TMDB keywords that describe a work's provenance or production format rather
# than the experience of watching it. They are common enough to pollute every
# explanation and they say nothing about taste: "based on novel or book" is
# shared by Coraline, The NeverEnding Story and tens of thousands of others.
KEYWORD_STOPLIST = frozenset({
    "based on novel or book", "based on short story", "based on comic",
    "based on play or musical", "based on video game", "based on manga",
    "based on young adult novel", "based on children's book",
    "woman director", "live action remake", "remake", "reboot",
    "duringcreditsstinger", "aftercreditsstinger", "3d", "imax",
})

# Crew roles carry craft; a sixth-billed actor carries coincidence. Cast is
# kept because a shared lead is a weak signal, but weighted far below crew:
# rarity weighting would otherwise let any shared bit-part actor outrank a
# shared director, which is how a kids' cartoon surfaced off a Julianne Moore
# voice role.
CAST_LIMIT = 3
CAST_WEIGHT = 0.2


def search(api_key, medium, query, year=None):
    kind = MEDIUM_TO_PATH.get(medium)
    if not kind:
        raise TmdbError(f"TMDB has no corpus for medium {medium!r}")
    key = "year" if kind == "movie" else "first_air_date_year"
    data = _get(api_key, f"/search/{kind}", query=query, **{key: year})
    return [_slim(kind, r) for r in (data.get("results") or [])][:8]


def _slim(kind, r):
    date = r.get("release_date") or r.get("first_air_date") or ""
    return {
        "tmdb_id": r.get("id"),
        "title": r.get("title") or r.get("name"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "overview": (r.get("overview") or "")[:400],
        "popularity": r.get("popularity"),
        "vote_average": r.get("vote_average"),
        "medium": "film" if kind == "movie" else "tv",
    }


def details(api_key, medium, tmdb_id):
    """Full record plus the two things the picker runs on: keywords and crew."""
    kind = MEDIUM_TO_PATH[medium]
    data = _get(api_key, f"/{kind}/{tmdb_id}", append_to_response="keywords,credits")

    kw_block = data.get("keywords") or {}
    keywords = [k.get("name") for k in (kw_block.get("keywords") or kw_block.get("results") or []) if k.get("name")]

    credits = data.get("credits") or {}
    crew = []
    for member in credits.get("crew") or []:
        job = (member.get("job") or "").lower()
        if job in ("director", "director of photography", "original music composer", "writer", "screenplay"):
            if member.get("name"):
                crew.append((job, member["name"]))
    cast = [c["name"] for c in (credits.get("cast") or [])[:CAST_LIMIT] if c.get("name")]

    runtime = data.get("runtime")
    if runtime is None:
        eps = data.get("episode_run_time") or []
        runtime = eps[0] if eps else None

    date = data.get("release_date") or data.get("first_air_date") or ""
    return {
        "tmdb_id": data.get("id"),
        "medium": medium,
        "title": data.get("title") or data.get("name"),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "runtime_min": runtime,
        "overview": (data.get("overview") or "")[:600],
        "genres": [g["name"] for g in (data.get("genres") or []) if g.get("name")],
        "keywords": keywords[:40],
        "crew": crew,
        "cast": cast,
    }


def recommendations(api_key, medium, tmdb_id, page=1):
    kind = MEDIUM_TO_PATH[medium]
    data = _get(api_key, f"/{kind}/{tmdb_id}/recommendations", page=page)
    return [_slim(kind, r) for r in (data.get("results") or [])]


def discover(api_key, medium, *, max_runtime=None, min_year=None, max_year=None,
             with_keywords=None, with_people=None, page=1, sort_by="vote_count.desc",
             min_votes=50):
    kind = MEDIUM_TO_PATH[medium]
    params = {
        "sort_by": sort_by,
        "page": page,
        "vote_count.gte": min_votes,
        "with_runtime.lte": max_runtime,
        "with_keywords": "|".join(str(k) for k in with_keywords) if with_keywords else None,
        "with_people": "|".join(str(p) for p in with_people) if with_people else None,
    }
    if kind == "movie":
        params["primary_release_date.gte"] = f"{min_year}-01-01" if min_year else None
        params["primary_release_date.lte"] = f"{max_year}-12-31" if max_year else None
    data = _get(api_key, f"/discover/{kind}", **params)
    return [_slim(kind, r) for r in (data.get("results") or [])]


def tags_from_details(det):
    """Flatten a details() record into the (kind, value, weight) rows the picker uses.

    Crew is weighted above keywords on purpose: a shared director or
    cinematographer is a far stronger taste signal than a shared plot tag, and
    it comes free in the same response. Cast sits below both, and structural
    keywords are dropped entirely rather than discounted.
    """
    tags = []
    for job, name in det.get("crew") or []:
        weight = 3.0 if job == "director" else 2.0
        tags.append(("crew", f"{job}:{name}", weight))
    for name in (det.get("cast") or [])[:CAST_LIMIT]:
        tags.append(("crew", f"cast:{name}", CAST_WEIGHT))
    for kw in det.get("keywords") or []:
        if kw.strip().lower() in KEYWORD_STOPLIST:
            continue
        tags.append(("keyword", kw, 1.0))
    for g in det.get("genres") or []:
        tags.append(("genre", g, 0.5))
    return tags


# ---------------------------------------------------------------------------
# Availability (JustWatch data, served by TMDB)
# ---------------------------------------------------------------------------

def availability(api_key, medium, tmdb_id, region="US"):
    kind = MEDIUM_TO_PATH[medium]
    data = _get(api_key, f"/{kind}/{tmdb_id}/watch/providers")
    block = (data.get("results") or {}).get(region.upper()) or {}
    return {
        "region": region.upper(),
        "flatrate": [p.get("provider_name") for p in block.get("flatrate") or []],
        "free": [p.get("provider_name") for p in block.get("free") or []],
        "ads": [p.get("provider_name") for p in block.get("ads") or []],
        "rent": [p.get("provider_name") for p in block.get("rent") or []],
        "buy": [p.get("provider_name") for p in block.get("buy") or []],
        "link": block.get("link"),
    }


def cached_availability(con, api_key, work_id, medium, tmdb_id, region="US"):
    row = con.execute(
        "SELECT payload, fetched_at FROM availability_cache WHERE work_id=? AND region=?",
        (work_id, region.upper()),
    ).fetchone()
    if row and (time.time() - row["fetched_at"]) < AVAILABILITY_TTL:
        try:
            return json.loads(row["payload"]), True
        except ValueError:
            pass
    fresh = availability(api_key, medium, tmdb_id, region)
    con.execute(
        "INSERT OR REPLACE INTO availability_cache (work_id, region, payload, fetched_at) "
        "VALUES (?,?,?,?)", (work_id, region.upper(), json.dumps(fresh), time.time()))
    con.commit()
    return fresh, False


def free_on(avail, service_names):
    """Which of the user's own services carry this at no marginal cost."""
    owned = {s.lower() for s in service_names}
    hits = []
    for bucket in ("flatrate", "free", "ads"):
        for name in avail.get(bucket) or []:
            if name and name.lower() in owned:
                hits.append(name)
    return sorted(set(hits))
