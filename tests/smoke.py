"""End-to-end smoke test through the real register(ctx) path.

Exercises every tool that does not need the network, in a throwaway
HERMES_HOME, and asserts palate_pick degrades politely rather than crashing
when no TMDB key is configured.

    python3 tests/smoke.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="palate-smoke-")
os.environ["HERMES_HOME"] = TMP
# Set deliberately: the plugin must NOT pick this up (admission rule 11).
os.environ["PALATE_TMDB_API_KEY"] = "sentinel-must-not-be-used"

sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import load_plugin  # noqa: E402

palate = load_plugin()


class Ctx:
    """Mirrors the host's registration surface closely enough to exercise it."""

    def __init__(self):
        self.tools, self.hooks, self.skills = {}, {}, []
        self.settings = {}

    def register_tool(self, name=None, toolset=None, schema=None, handler=None, **_kw):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, fn, **_kw):
        self.hooks.setdefault(name, []).append(fn)

    def register_skill(self, name, path):
        self.skills.append((name, str(path)))

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


def call(ctx, _tool, **params):
    _schema, handler = ctx.tools[_tool]
    return json.loads(handler(params))


def main():
    ctx = Ctx()
    palate.register(ctx)
    print(f"registered {len(ctx.tools)} tools, {len(ctx.hooks)} hooks, "
          f"{len(ctx.skills)} skill(s): {[s[0] for s in ctx.skills]}")
    assert len(ctx.tools) == 7, ctx.tools
    assert ctx.hooks == {}, "Palate must register no hooks"

    # --- services ---------------------------------------------------------
    out = call(ctx, "palate_services", action="add", name="Criterion Channel",
               kind="subscription", region="US")
    assert out["ok"], out
    call(ctx, "palate_services", action="add", name="Kanopy", kind="library")
    out = call(ctx, "palate_services", action="list")
    print("services:", [s["name"] for s in out["services"]])
    assert len(out["services"]) == 2

    # --- verdicts, including the trait/state split ------------------------
    out = call(ctx, "palate_verdict", title="Burning", year=2018, verdict="seen",
               reasons=["loved_it", "great_craft"], rating=9)
    assert out["ok"] and out["learned_as_taste"], out
    print("verdict (taste):", out["learned_as_taste"], "->", out["ledger"])

    out = call(ctx, "palate_verdict", title="Stalker", year=1979, verdict="bailed",
               reasons=["too_slow", "not_in_the_mood"], stopped_at_min=40,
               note="tired, not the film's fault")
    print("verdict (mixed):", "taste=", out["learned_as_taste"],
          "tonight=", out["recorded_as_tonight_only"])
    assert out["recorded_as_tonight_only"] == ["not_in_the_mood"], out

    out = call(ctx, "palate_verdict", title="Thing", verdict="seen", reasons=["vibes"])
    assert out["ok"] is False and "Unknown reason" in out["error"], out
    print("bad reason rejected:", out["error"][:60])

    # --- bulk backfill ----------------------------------------------------
    out = call(ctx, "palate_seen", titles=["Drive My Car", "Columbus", "After Yang"])
    assert out["ok"] and len(out["recorded"]) == 3, out
    print("bulk seen:", [r["title"] for r in out["recorded"]])

    # --- profile honesty on a thin ledger ---------------------------------
    out = call(ctx, "palate_profile")
    assert out["sufficient_evidence"] is False and out["established"] == []
    print("profile:", out["message"])

    # --- import: refusal, probe, apply ------------------------------------
    secret = Path(TMP) / "creds.env"
    secret.write_text("API_TOKEN=do-not-echo-this\n", encoding="utf-8")
    out = call(ctx, "palate_import", source="letterboxd_csv", path=str(secret))
    assert out["ok"] is False and out.get("refused")
    assert "do-not-echo-this" not in json.dumps(out)
    print("foreign file refused, nothing leaked:", out["error"][:72])

    csv_path = Path(TMP) / "watched.csv"
    csv_path.write_text(
        "Date,Name,Year,Letterboxd URI,Rating\n"
        "2024-02-01,Poetry,2010,https://boxd.it/p,5\n"
        "2024-02-02,Fallen Leaves,2023,https://boxd.it/f,4\n"
        "2024-02-03,Burning,2018,https://boxd.it/b,3\n",
        encoding="utf-8")

    before = call(ctx, "palate_profile")["ledger"]["works"]
    out = call(ctx, "palate_import", source="letterboxd_csv", path=str(csv_path))
    assert out["mode"] == "probe" and out["records_parsed"] == 3
    assert call(ctx, "palate_profile")["ledger"]["works"] == before, "probe must write nothing"
    print("probe parsed", out["records_parsed"], "rows and wrote nothing")

    out = call(ctx, "palate_import", source="letterboxd_csv", path=str(csv_path), mode="apply")
    acc = out["accounting"]
    print("apply:", {k: acc[k] for k in
                     ("rows_read", "verdicts_inserted", "skipped_existing_user_records")})
    assert acc["skipped_existing_user_records"] >= 1, \
        "Burning was hand-logged above and must not be overwritten"

    # --- pick with no key: must degrade, not crash ------------------------
    out = call(ctx, "palate_pick", max_runtime_min=90, require_free=True)
    assert out["ok"] is False and out.get("needs_setup"), out
    print("pick without a key:", out["error"][:90])

    # --- export -----------------------------------------------------------
    # Deliberately NOT under TMP: TMP is the throwaway HERMES_HOME, and
    # palate_export refuses to write inside the Hermes home. Exporting there
    # is exactly the mistake the guard exists to catch.
    out_dir = tempfile.mkdtemp(prefix="palate-smoke-out-")
    try:
        dest = Path(out_dir) / "export.json"
        out = call(ctx, "palate_export", path=str(dest))
        assert out["ok"] and dest.is_file(), out
        data = json.loads(dest.read_text())
        print("export:", {k: v for k, v in out["counts"].items() if v})
        print("export write checks:", out["write_checks"])
        assert data["format"] == "palate-export" and data["verdicts"]

        # And the guard refuses the paths an injected call would aim at.
        for bad, why in ((str(Path(TMP) / "inside-home.json"), "hermes_home"),
                         (str(Path(out_dir) / "notes.txt"), "suffix")):
            refused = call(ctx, "palate_export", path=bad)
            assert refused["ok"] is False and refused.get("refused"), refused
            assert refused["write_checks"][why] == "rejected", refused
        victim = Path(out_dir) / "someone-elses.json"
        victim.write_text('{"not": "a palate export"}')
        refused = call(ctx, "palate_export", path=str(victim))
        assert refused["ok"] is False
        assert victim.read_text() == '{"not": "a palate export"}', "must be untouched"
        print("export guard: refuses the Hermes home, non-.json, and foreign files")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    print("\nSMOKE OK")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
