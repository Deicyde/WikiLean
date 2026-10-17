#!/usr/bin/env python3
"""Tests for manage/status.py's live health checks. Every network call
(curl, gh, git) goes through status._run or status._run_err, which these
tests stub.

    python3 manage/test_status.py
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status  # noqa: E402

HOUR = 3600.0


def _stats_html(review=(118, 1776, 858, 26345645)) -> str:
    """The /stats Pipeline runs table as wiki/src/pages.ts renders it."""
    def row(kind, runs, arts, errs, toks, cost="$0.00"):
        cells = "".join(f'<td class="wl-stat-num">{v:,}</td>' for v in (runs, arts, errs, toks))
        return (f'<tr><td>{kind}</td>{cells}<td class="wl-stat-num">{cost}</td>'
                f'<td><span class="wl-rq">RQ8</span></td></tr>')
    return ('<h2 class="wl-stats-h">Revisions &amp; patrol</h2><table><tr><td>Revisions: edit</td>'
            '<td class="wl-stat-num">32</td></tr></table>'
            '<h2 class="wl-stats-h">Pipeline runs</h2><div style="overflow-x:auto"><table><thead>'
            '<tr><th>Kind</th><th>Runs</th><th>Articles</th><th>Errors</th><th>Tokens</th>'
            '<th>Cost</th><th>Feeds</th></tr></thead><tbody>'
            + row("new", 7, 355, 302, 1068242, "$71.06")
            + row("review", *review, "$1362.10")
            + row("wp-update", 102, 10103, 224, 0)
            + row("all runs", 227, 12234, 1384, 27413887, "$1433.15")
            + '</tbody></table></div><h2 class="wl-stats-h">AI proposals</h2>')


CURRENT_JSON = json.dumps({
    "schema": "wikilean.release-selector/v2",
    "release_id": "sha256:8305822b0083a086ed75c8d39ba05b319ef70417cc209d87ffe6cab090b3f08e",
    "audited_at": "2026-10-04T17:25:04Z"})

POLL_LOG = (
    "poll\tUNKNOWN STEP\t2026-10-13T17:03:50.7020591Z \x1b[36;1mpython3 bot/poll.py --mathlib /tmp/unused --decide | tee /tmp/decide.txt\x1b[0m\n"
    "poll\tUNKNOWN STEP\t2026-10-13T17:03:50.7020855Z \x1b[36;1mif grep -q 'POLL_DECISION=act' /tmp/decide.txt; then\x1b[0m\n"
    "poll\tUNKNOWN STEP\t2026-10-13T17:03:52.7021760Z POLL_DECISION=wait\n")
POLL_ERROR_LOG = (
    "poll\tDecide\t2026-10-14T01:00:00.1Z POLL_ERROR: gh api repos/x/pulls/42608 failed: HTTP 401\n"
    "poll\tDecide\t2026-10-14T01:00:00.2Z ##[error]Process completed with exit code 1.\n")
APPLY_ERROR_LOG = (
    "poll\tDecide\t2026-10-14T01:00:00.1Z POLL_DECISION=act\n"
    "poll\tPoll tick (apply)\t2026-10-14T01:05:00.1Z POLL_ERROR: #42608 mergeable is still UNKNOWN\n")


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ci_run(run_id: int, conclusion: str, hours_ago: float, minutes: float = 0.4) -> dict:
    """A `gh run list` row for a run queued hours_ago that took minutes."""
    return {"databaseId": run_id, "conclusion": conclusion, "createdAt": _iso_ago(hours_ago),
            "startedAt": _iso_ago(hours_ago), "updatedAt": _iso_ago(hours_ago - minutes / 60)}


def _api_runs(*runs) -> str:
    """GET /api/runs body; each run is (hours_ago, articles, errors, tokens, notes)."""
    now_ms = time.time() * 1000
    return json.dumps({"runs": [
        {"run_id": f"{i:08x}", "kind": "review", "started_at": now_ms - h * 3.6e6 - 6e5,
         "finished_at": now_ms - h * 3.6e6, "articles_processed": a, "errors": e,
         "tokens": t, "notes": n} for i, (h, a, e, t, n) in enumerate(runs)]})


class FakeNet:
    """Dispatches status._run and status._run_err by command; records every call."""

    def __init__(self, **kw):
        self.stats = kw.get("stats", _stats_html())
        self.api_runs = kw.get("api_runs")          # None = endpoint not deployed (404)
        self.ci_runs = kw.get("ci_runs", [{"databaseId": 38435564796, "createdAt": _iso_ago(2),
                                           "conclusion": "success"}])   # None = gh fails
        self.ci_log = kw.get("ci_log", POLL_LOG)    # None = gh fails
        # None = no bot-state branch; "fail" = gh fails; "no-file" = branch without
        # bot_state.json; "branch-fail" = no file, and the branch read fails
        self.ci_state = kw.get("ci_state")
        self.pr = kw.get("pr", {"state": "OPEN", "mergeable": "CONFLICTING",
                                "createdAt": _iso_ago(64 * 24)})
        self.calls = []

    def __call__(self, cmd, timeout=12):
        return self.run_err(cmd, timeout)[0]

    def run_err(self, cmd, timeout=12):
        self.calls.append(cmd)
        if cmd[0] == "curl":
            if "-w" in cmd:
                return "200", ""
            url = cmd[-1]
            if "/api/runs" in url:
                return self.api_runs, ""
            if url.endswith("/stats"):
                return self.stats, ""
            if url.endswith("/assets/brain/current.json"):
                return CURRENT_JSON, ""
            return None, "curl: (22) The requested URL returned error: 404"
        if cmd[:3] == ["gh", "run", "list"]:
            if self.ci_runs is None:
                return None, "HTTP 401: Bad credentials (https://api.github.com/graphql)"
            return json.dumps(self.ci_runs), ""
        if cmd[:3] == ["gh", "run", "view"]:
            return (self.ci_log, "") if self.ci_log is not None else (None, "HTTP 410: Gone")
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/branches/bot-state"):
            if self.ci_state is None:
                return None, "gh: Branch not found (HTTP 404)"
            if self.ci_state == "branch-fail":
                return None, "gh: Server Error (HTTP 502)"
            return "bot-state", ""
        if cmd[:2] == ["gh", "api"]:
            if self.ci_state is None:
                return None, "gh: No commit found for the ref bot-state (HTTP 404)"
            if self.ci_state == "fail":
                return None, "gh: Bad credentials (HTTP 401)"
            if self.ci_state in ("no-file", "branch-fail"):
                return None, "gh: Not Found (HTTP 404)"
            return base64.b64encode(json.dumps(self.ci_state).encode()).decode(), ""
        if cmd[:3] == ["gh", "pr", "view"]:
            return (json.dumps(self.pr), "") if self.pr != "fail" else (None, "HTTP 502")
        if cmd[0] == "git":
            return "", ""
        return None, "unexpected command"


@contextlib.contextmanager
def patched(net, td: Path, digest: dict | None = None, batch: dict | None = None):
    saved = {k: getattr(status, k)
             for k in ("_run", "_run_err", "DIGEST", "BOT_STATE", "LIVE_STATE")}
    status._run = net
    status._run_err = getattr(net, "run_err", lambda cmd, timeout=12: (net(cmd, timeout), ""))
    status.DIGEST = td / "digest.json"
    status.BOT_STATE = td / "bot_state.json"
    status.LIVE_STATE = td / "cache" / ".status_live.json"
    if digest is not None:
        status.DIGEST.write_text(json.dumps(digest))
    if batch is not None:
        status.BOT_STATE.write_text(json.dumps(batch))
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(status, k, v)


def _digest(graph_mtime: float, ann_mtime: float, stored_flag: bool) -> dict:
    return {
        "generated_at": time.time(),
        "coverage": {"pct_formalized": 27.9, "pct_partial": 14.1, "pct_not_formalized": 58.0,
                     "n_articles": 778, "extracted_articles": 0, "extracted_statements": 0},
        "graph": {"n_nodes": 10, "n_edges": 20},
        "freshness": {"annotations_mtime": ann_mtime, "graph_mtime": graph_mtime,
                      "graph_older_than_annotations": stored_flag},
        "pool": {"fresh_candidates": 722, "approx_batches": 80.2, "field_qids_excluded": 0},
        "worklists": {k: {"total": 0, "top": []}
                      for k in ("formalize", "annotate", "coverage_gaps", "pipeline")},
    }


def _snapshot_output(net, digest, batch, live=True) -> str:
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td), digest, batch):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status.snapshot(live=live)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Review health
# ---------------------------------------------------------------------------

def test_parse_pipeline_runs_reads_the_stats_table():
    runs = status.parse_pipeline_runs(_stats_html())
    assert runs["review"] == {"runs": 118, "articles": 1776, "errors": 858,
                              "tokens": 26345645}, runs
    assert set(runs) == {"new", "review", "wp-update"}, runs
    assert status.parse_pipeline_runs("<html>maintenance</html>") == {}
    assert status.parse_pipeline_runs(None) == {}


def test_review_from_runs_alerts_on_a_failing_streak_and_shows_notes():
    now = time.time()
    runs = json.loads(_api_runs(
        (3, 15, 15, 0, "modes=review; 15/15 errors: 15x ~2s Not logged in"),
        (27, 15, 15, 0, None),
        (51, 15, 1, 400000, None)))["runs"]
    lines, alerts = status.review_from_runs(runs, now)
    assert lines[0].startswith("  review     last run 3h ago: 15 articles · 15 errors · 0 tokens"), lines
    assert "notes: modes=review; 15/15 errors" in lines[1], lines
    assert alerts == ["nightly review failing: the last 2 run(s) had errors equal articles "
                      "(15/15) and tokens unchanged; see docs/OPERATIONS.md"], alerts


def test_review_from_runs_healthy_stale_empty_and_zero_article():
    now = time.time()
    healthy = json.loads(_api_runs((3, 15, 2, 400000, None)))["runs"]
    assert status.review_from_runs(healthy, now)[1] == []
    stale = json.loads(_api_runs((50, 15, 0, 400000, None)))["runs"]
    assert status.review_from_runs(stale, now)[1] == ["no review run has finished in 50h"]
    empty = json.loads(_api_runs((3, 0, 0, 0, None)))["runs"]
    assert status.review_from_runs(empty, now)[1] == [], "a run with no jobs is not a failure"
    assert status.review_from_runs([], now)[1] == ["no review run has been reported"]


def test_review_health_delta_alerts_persists_and_clears():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "s.json"
        t0 = time.time() - 30 * HOUR
        lines, alerts = status.review_health(_stats_html((117, 1761, 843, 26345645)), t0, state)
        assert "baseline saved" in lines[0] and alerts == [], (lines, alerts)
        lines, alerts = status.review_health(_stats_html((118, 1776, 858, 26345645)),
                                             t0 + 21 * HOUR, state)
        assert len(alerts) == 1, alerts
        assert "1 run(s) since" in alerts[0], alerts
        assert "errors equal articles (15/15) and tokens unchanged" in alerts[0], alerts
        # the same totals read again (no new run): the alert must not vanish
        _, again = status.review_health(_stats_html((118, 1776, 858, 26345645)),
                                        t0 + 23 * HOUR, state)
        assert again == alerts, again
        # a healthy night clears it
        _, healthy = status.review_health(_stats_html((119, 1791, 860, 26745645)),
                                          t0 + 30 * HOUR, state)
        assert healthy == [], healthy


def test_review_health_alerts_when_review_stops_running():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "s.json"
        t0 = time.time() - 60 * HOUR
        status.review_health(_stats_html(), t0, state)
        _, alerts = status.review_health(_stats_html(), t0 + 40 * HOUR, state)
    assert len(alerts) == 1 and alerts[0].startswith("no new review run since"), alerts


def test_review_health_survives_bad_state_files():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "s.json"
        for bad in ("null", "[]", '"x"', '{"last": 5}', '{"last": {"runs": 1}}',
                    '{"prev": {"runs": true, "articles": 1, "errors": 1, "tokens": 1}}',
                    "{not json"):
            state.write_text(bad)
            lines, alerts = status.review_health(_stats_html(), time.time(), state)
            assert "baseline saved" in lines[0] and alerts == [], (bad, lines, alerts)


def test_review_health_reports_an_unsaved_baseline():
    with tempfile.TemporaryDirectory() as td:
        blocker = Path(td) / "file"
        blocker.write_text("x")
        lines, _ = status.review_health(_stats_html(), time.time(), blocker / "s.json")
    assert "could not save a baseline" in lines[0], lines


def test_review_health_flags_a_markup_change():
    with tempfile.TemporaryDirectory() as td:
        lines, alerts = status.review_health("<html>redesigned</html>", time.time(),
                                             Path(td) / "s.json")
        assert alerts and "parse_pipeline_runs" in alerts[0], alerts
        lines, alerts = status.review_health(None, time.time(), Path(td) / "s.json")
        assert "unavailable" in lines[0] and alerts == []


def test_review_alerts_each_condition_alone():
    prev = {"runs": 10, "articles": 150, "errors": 5, "tokens": 1000, "at": "t0"}
    (a,) = status.review_alerts(prev, {"runs": 11, "articles": 165, "errors": 6, "tokens": 1000})
    assert "tokens unchanged" in a and "errors equal" not in a, a
    (a,) = status.review_alerts(prev, {"runs": 12, "articles": 180, "errors": 35, "tokens": 9000})
    assert "errors equal articles (30/30)" in a and "tokens unchanged" not in a, a
    assert status.review_alerts(prev, {"runs": 11, "articles": 150, "errors": 5,
                                       "tokens": 1000}) == [], "zero-article run"
    assert status.review_alerts(prev, {**prev}) == []
    assert status.review_alerts(None, prev) == []


def test_review_section_prefers_api_runs_and_falls_back_to_stats():
    net = FakeNet(api_runs=_api_runs((3, 15, 15, 0, None)))
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        lines, alerts = status.review_section(time.time())
        assert lines[0].startswith("  review     last run 3h ago"), lines
        assert alerts and "last 1 run(s)" in alerts[0], alerts
        assert not any(c[-1].endswith("/stats") for c in net.calls if c[0] == "curl")
    net = FakeNet()          # /api/runs not deployed
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        lines, _ = status.review_section(time.time())
        assert lines[0].startswith("  review     118 runs · 1,776 articles"), lines
        assert status.LIVE_STATE.exists()


# ---------------------------------------------------------------------------
# Bot gate
# ---------------------------------------------------------------------------

def test_decision_ignores_the_echoed_grep_command():
    assert status._decision(POLL_LOG) == "wait"
    assert status._decision("POLL_DECISION=act\n") == "act"
    assert status._decision(None) is None
    assert status._decision("if grep -q 'POLL_DECISION=act' x; then") is None


def test_ci_gate_reads_the_latest_finished_run_on_main():
    runs = [_ci_run(10, "cancelled", 1, minutes=2), _ci_run(9, "cancelled", 1.5, minutes=24.9),
            {"databaseId": 8, "createdAt": _iso_ago(2), "conclusion": "success"}]
    net = FakeNet(ci_runs=runs)
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        dec, text, alerts = status.ci_gate()
    assert dec == "wait" and text == "CI wait (run 8, 2h ago)" and alerts == [], (dec, text, alerts)
    (listing,) = [c for c in net.calls if c[:3] == ["gh", "run", "list"]]
    assert "--branch main" in " ".join(listing) and "-L 20" in " ".join(listing), listing
    fields = listing[listing.index("--json") + 1].split(",")
    assert {"startedAt", "updatedAt"} <= set(fields), listing


def test_ci_gate_counts_a_long_cancelled_run_as_a_timeout():
    """The job's time limit cancels a tick that runs too long; five of them
    used to fill the listing and leave a silent 'CI ?'."""
    runs = [_ci_run(10, "cancelled", 0.5, minutes=2)]
    runs += [_ci_run(9 - i, "cancelled", 1 + 4 * i, minutes=31 - i) for i in range(5)]
    runs += [_ci_run(4, "success", 22)]
    net = FakeNet(ci_runs=runs, ci_log="poll\tDecide\t2026-10-14T01:00:00.1Z POLL_DECISION=act\n")
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        dec, text, alerts = status.ci_gate()
    assert dec == "act" and text == "CI act (run 9, 1h ago)", (dec, text)
    assert alerts == ["wikidata-poll run 9 timed out (cancelled after 31 min)"], alerts
    assert any(c[:4] == ["gh", "run", "view", "9"] for c in net.calls), net.calls


def test_ci_gate_alerts_when_no_listed_run_finished():
    for runs, why in (([_ci_run(9, "cancelled", 1, minutes=2), _ci_run(8, "skipped", 2)],
                       "the last 2 were all skipped or cancelled early"),
                      ([], "gh listed none")):
        with tempfile.TemporaryDirectory() as td, patched(FakeNet(ci_runs=runs), Path(td)):
            dec, text, alerts = status.ci_gate()
        assert (dec, text) == (None, "CI ?"), (dec, text)
        assert alerts == [f"no finished wikidata-poll run on main ({why}); "
                          f"the gate is unknown"], alerts


def test_ci_gate_alerts_on_poll_error_failure_and_staleness():
    failed = [{"databaseId": 7, "createdAt": _iso_ago(1), "conclusion": "failure"}]
    with tempfile.TemporaryDirectory() as td, patched(
            FakeNet(ci_runs=failed, ci_log=POLL_ERROR_LOG), Path(td)):
        dec, text, alerts = status.ci_gate()
    assert dec == "error" and text.startswith("CI error (run 7"), text
    assert alerts == ["wikidata-poll run 7 reported POLL_ERROR: gh api repos/x/pulls/42608 "
                      "failed: HTTP 401"], alerts
    # a POLL_ERROR from a later step keeps the decision and does not blame decide
    with tempfile.TemporaryDirectory() as td, patched(
            FakeNet(ci_runs=failed, ci_log=APPLY_ERROR_LOG), Path(td)):
        dec, text, alerts = status.ci_gate()
    assert dec == "act" and text.startswith("CI act (run 7"), text
    assert len(alerts) == 1 and "reported POLL_ERROR: #42608 mergeable" in alerts[0], alerts
    with tempfile.TemporaryDirectory() as td, patched(
            FakeNet(ci_runs=failed, ci_log="no decide line"), Path(td)):
        _, text, alerts = status.ci_gate()
    assert text.startswith("CI ? (run 7") and alerts == ["wikidata-poll run 7 ended 'failure'"]
    old = [{"databaseId": 6, "createdAt": _iso_ago(30), "conclusion": "success"}]
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(ci_runs=old), Path(td)):
        _, _, alerts = status.ci_gate()
    assert len(alerts) == 1 and "not finished a run in 30h" in alerts[0], alerts


def test_ci_gate_alerts_when_a_read_fails():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(ci_runs=None), Path(td)):
        dec, text, alerts = status.ci_gate()
    assert (dec, text) == (None, "CI ?"), (dec, text)
    assert alerts == ["could not list wikidata-poll runs (HTTP 401: Bad credentials "
                      "(https://api.github.com/graphql)); the gate is unknown"], alerts
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(ci_log=None), Path(td)):
        dec, text, alerts = status.ci_gate()
    assert dec is None and text.startswith("CI ? (run 38435564796"), text
    assert alerts == ["could not read the log of wikidata-poll run 38435564796 "
                      "(HTTP 410: Gone); its decision is unknown"], alerts


def test_gate_section_shows_ci_only_and_runs_no_local_decide():
    net = FakeNet()
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        lines, alerts = status.gate_section()
    assert lines == ["  gate       CI wait (run 38435564796, 2h ago)"], lines
    assert alerts == []
    assert not any("poll.py" in c for cmd in net.calls for c in cmd), net.calls


# ---------------------------------------------------------------------------
# Batch PR, Brain, graph staleness
# ---------------------------------------------------------------------------

def test_batch_pr_alerts_by_state():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td)):
        lines, alerts = status.batch_pr(42608)
    assert "#42608 OPEN · CONFLICTING · opened 64d ago (state: local)" in lines[0], lines
    assert len(alerts) == 2, alerts
    assert "conflicts with mathlib master" in alerts[0] and "open 64 days" in alerts[1], alerts
    assert "only once it is settled" in alerts[0], "no promise of a rebuild for an unsettled PR"
    labelled = {"state": "OPEN", "mergeable": "UNKNOWN", "createdAt": _iso_ago(72),
                "labels": [{"name": "merge-conflict"}]}
    bors = {"state": "CLOSED", "mergeable": "UNKNOWN", "createdAt": _iso_ago(900),
            "title": "[Merged by Bors] - chore: @[wikidata] tags, batch 7"}
    cases = [(labelled, ["conflicts with mathlib master"]),
             ({"state": "OPEN", "mergeable": "MERGEABLE", "createdAt": _iso_ago(72)}, []),
             ({"state": "MERGED", "mergeable": "UNKNOWN", "createdAt": _iso_ago(900),
               "labels": [{"name": "merge-conflict"}]}, []),
             (bors, []),
             ({"state": "CLOSED", "mergeable": "CONFLICTING", "createdAt": _iso_ago(900),
               "title": "chore: @[wikidata] tags, batch 7",
               "labels": [{"name": "merge-conflict"}]}, ["closed without merging"])]
    for pr, want in cases:
        net = FakeNet(pr=pr)
        with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
            lines, alerts = status.batch_pr(42608)
        assert len(alerts) == len(want) and all(w in a for w, a in zip(want, alerts)), (pr, alerts)
        assert "title" in net.calls[0][net.calls[0].index("--json") + 1].split(","), net.calls
        if pr is bors:
            assert "#42608 MERGED (Bors) · UNKNOWN" in lines[0], lines
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(pr="fail"), Path(td)):
        lines, alerts = status.batch_pr(42608)
    assert lines == ["  batch PR   #42608 ? (could not be read)"], lines
    assert alerts == ["could not read batch PR #42608 (HTTP 502); "
                      "its merge state is unknown"], alerts


def test_live_section_prefers_the_bot_state_branch():
    net = FakeNet(ci_state={"current_pr": 99999, "batch_num": 8})
    with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
        lines, _ = status.live_section({"current_pr": 42608})
    pr_line = [ln for ln in lines if "batch PR" in ln][0]
    assert "#99999" in pr_line and "(state: bot-state branch)" in pr_line, lines
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td)):
        lines, alerts = status.live_section({"current_pr": 42608})
    assert any("#42608" in ln and "(state: local snapshot; no bot-state branch)" in ln
               for ln in lines), lines
    missing = [a for a in alerts if "no bot-state branch" in a]
    assert len(missing) == 1 and "every wikidata-poll run fails at restore" in missing[0], alerts
    assert "bootstrap_state" in missing[0] and "'Restore failed'" in missing[0], missing


def test_live_section_alerts_when_the_bot_state_branch_cannot_be_read():
    for ci_state, why in (("fail", "gh: Bad credentials (HTTP 401)"),
                          ([42608], "bot_state.json is not a JSON object"),
                          ("no-file", "the branch has no bot/state/bot_state.json, so every "
                                      "poller run fails at restore; see bot/README.md, "
                                      "'Restore failed'"),
                          ("branch-fail", "gh: Server Error (HTTP 502)")):
        net = FakeNet(ci_state=ci_state)
        with tempfile.TemporaryDirectory() as td, patched(net, Path(td)):
            lines, alerts = status.live_section({"current_pr": 42608})
        assert "  batch PR   ? (the bot-state branch could not be read)" in lines, lines
        assert f"could not read the poller's state from the bot-state branch ({why}); " \
               f"the batch PR was not checked" in alerts, alerts
        assert not any(c[:3] == ["gh", "pr", "view"] for c in net.calls), \
            "must not fall back to the local snapshot"


def test_brain_release_line():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td)):
        line = status.brain_release()
    assert "release sha256:8305822b0083a086" in line and "audited 2026-10-04T17:25:04Z (" in line, line
    net = FakeNet()
    with tempfile.TemporaryDirectory() as td, patched(
            lambda cmd, timeout=12: None if "current.json" in cmd[-1] else net(cmd, timeout),
            Path(td)):
        assert "unavailable" in status.brain_release()


def test_graph_staleness_tolerates_one_bulk_copy():
    assert not status.graph_older_than_annotations(1787159418.83, 1787159422.83)
    assert status.graph_older_than_annotations(1787159418.83, 1787159418.83 + 3600)


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

def test_offline_snapshot_makes_no_network_calls_and_writes_nothing():
    """The SessionStart hook runs this path: git status only, no state file,
    and a stored strict-< graph flag from a bulk copy no longer alerts."""
    net = FakeNet()
    with tempfile.TemporaryDirectory() as td, patched(
            net, Path(td), _digest(1787159418.83, 1787159422.83, True), {"current_pr": 42608}):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status.snapshot(live=False)
        assert net.calls == [["git", "status", "--porcelain"]], net.calls
        assert not status.LIVE_STATE.exists()
    assert "concept graph is stale" not in buf.getvalue(), buf.getvalue()
    out = _snapshot_output(FakeNet(), _digest(1.0, 7200.0, False), {}, live=False)
    assert "concept graph is stale" in out, out


def test_snapshot_live_prints_every_health_line_and_alert():
    net = FakeNet(api_runs=_api_runs((3, 15, 15, 0, "modes=review; 15/15 errors: x")),
                  ci_runs=[{"databaseId": 7, "createdAt": _iso_ago(1), "conclusion": "failure"}],
                  ci_log=POLL_ERROR_LOG)
    out = _snapshot_output(net, _digest(1.0, 2.0, False), {"current_pr": 42608, "batch_num": 7})
    for want in ("site       https://wikilean.jackmccarthy.org → 200",
                 "gate       CI error (run 7",
                 "review     last run 3h ago: 15 articles · 15 errors · 0 tokens",
                 "notes: modes=review; 15/15 errors: x",
                 "brain      release sha256:8305822b",
                 "batch PR   #42608 OPEN · CONFLICTING",
                 "→ wikidata-poll run 7 reported POLL_ERROR: gh api repos/x/pulls/42608",
                 "→ nightly review failing: the last 1 run(s)",
                 "→ batch PR #42608 conflicts with mathlib master",
                 "→ batch PR #42608 has been open 64 days"):
        assert want in out, (want, out)
    reads = {("gh", "run", "list"), ("gh", "run", "view"), ("gh", "pr", "view")}
    assert all(tuple(c[:3]) in reads or c[:2] == ["gh", "api"]
               for c in net.calls if c[0] == "gh"), "gh is used read-only"


def test_snapshot_without_digest_still_runs_every_live_check():
    out = _snapshot_output(FakeNet(), None, {"current_pr": 42608}, live=True)
    for want in ("no digest yet", "site       ", "gate       CI wait", "review     118 runs",
                 "brain      release", "batch PR   #42608", "→ batch PR #42608 conflicts"):
        assert want in out, (want, out)
    out = _snapshot_output(FakeNet(), None, {}, live=False)
    assert out.strip() == "no digest yet — run `python3 manage/refresh.py` first", out


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
