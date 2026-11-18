"""The source/core seam for importers.

A source locates and parses. The core validates, bounds, dedupes, tags
provenance and writes — once, here — so a second adapter cannot drift away
from the first on a plausibility bound or forget a safety flag.

Security posture: an import tool takes a path from whoever is driving the
agent and reports what it found, which is an arbitrary file read with content
echo unless it is closed deliberately. So a file must present positive
evidence of the expected format *before* anything from it is echoed back, and
a file that fails that test is refused with none of its contents and none of
its schema in the result.
"""

import csv
import os
import time

from .. import constants as C
from .. import storage


class FormatRefused(ValueError):
    """The file is not an export of the expected kind. Carries no file content."""


def read_csv_guarded(path, *, require_all=(), require_any=(), label="this format",
                     max_rows=20000):
    """Open a CSV only if its header proves it is the expected export.

    Returns (rows, columns). Raises FormatRefused naming the requirement and
    showing nothing from the file when the header does not qualify.
    """
    path = os.path.expanduser(str(path))
    if not os.path.isfile(path):
        raise FormatRefused(f"No file at that path. Expected a {label} CSV export.")
    if os.path.getsize(path) > 64 * 1024 * 1024:
        raise FormatRefused("File is larger than 64 MB; that is not an export of this kind.")

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            columns = [c.strip() for c in (reader.fieldnames or [])]
            lowered = {c.lower() for c in columns}

            missing_all = [c for c in require_all if c.lower() not in lowered]
            has_any = (not require_any) or any(c.lower() in lowered for c in require_any)
            if missing_all or not has_any:
                # Deliberately no column names, no first line, no sample.
                need = ", ".join(require_all) or "(none)"
                opt = " or ".join(require_any) or "(none)"
                raise FormatRefused(
                    f"That file is not a {label} export, so none of its contents are shown. "
                    f"A {label} export needs the column(s) {need}, plus one of: {opt}."
                )

            rows = []
            for i, row in enumerate(reader):
                if i >= max_rows:
                    break
                rows.append({(k or "").strip().lower(): (v or "").strip()
                             for k, v in row.items()})
    except UnicodeDecodeError:
        raise FormatRefused(f"File is not text, so it is not a {label} CSV export.") from None
    except csv.Error as exc:
        raise FormatRefused(f"File is not readable as CSV ({exc.__class__.__name__}).") from None
    return rows, columns


# ---------------------------------------------------------------------------
# The single write path. Every source funnels through this.
# ---------------------------------------------------------------------------

def ingest(con, records, *, source_name, protect_user_authored=True):
    """Validate, bound, dedupe and store. Returns the ingest accounting.

    The accounting is reported rather than summarised to 'added: N', so the
    user can see that nothing of theirs vanished.
    """
    acc = {
        "source": source_name,
        "rows_read": len(records),
        "no_usable_payload": 0,
        "rejected_bounds": 0,
        "works_created_or_matched": 0,
        "verdicts_inserted": 0,
        "skipped_existing_user_records": 0,
        "assumed": [],
        "questionable": [],
    }
    seen_keys = set()
    for rec in records:
        title = (rec.get("title") or "").strip()
        if not title:
            acc["no_usable_payload"] += 1
            continue

        medium = rec.get("medium") or "film"
        if medium not in C.MEDIA:
            acc["rejected_bounds"] += 1
            continue

        year = rec.get("year")
        key = (medium, title.lower(), year)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        try:
            work_id = storage.upsert_work(
                con, medium=medium, title=title, year=year,
                runtime_min=rec.get("runtime_min"),
                source=rec.get("work_source") or source_name,
                source_id=rec.get("source_id"))
        except ValueError:
            acc["rejected_bounds"] += 1
            continue
        acc["works_created_or_matched"] += 1

        rating = rec.get("rating")
        if rating is not None and not (C.RATING_MIN <= rating <= C.RATING_MAX):
            acc["assumed"].append({
                "title": title,
                "assumption": f"rating {rating} is outside {C.RATING_MIN}-{C.RATING_MAX} and was dropped",
            })
            rating = None

        try:
            _vid, status = storage.add_verdict(
                con, work_id=work_id,
                verdict=rec.get("verdict") or "seen",
                reasons=rec.get("reasons") or (),
                rating=rating,
                note=rec.get("note"),
                at=rec.get("at"),
                source=source_name,
                protect_user_authored=protect_user_authored)
        except ValueError:
            acc["rejected_bounds"] += 1
            continue

        if status == "inserted":
            acc["verdicts_inserted"] += 1
        else:
            acc["skipped_existing_user_records"] += 1
    return acc


def parse_year(value):
    value = (value or "").strip()
    if len(value) >= 4 and value[:4].isdigit():
        n = int(value[:4])
        if C.YEAR_MIN_BOUND <= n <= C.YEAR_MAX_BOUND:
            return n
    return None


def parse_date_epoch(value):
    """Accepts YYYY-MM-DD and ISO timestamps. Returns epoch seconds or None."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return time.mktime(time.strptime(value[:len(time.strftime(fmt))], fmt))
        except (ValueError, OverflowError):
            continue
    try:
        return time.mktime(time.strptime(value[:10], "%Y-%m-%d"))
    except (ValueError, OverflowError):
        return None
