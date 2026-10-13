#!/usr/bin/env python3
"""Tests for manage/status.py's live health checks. Every network call
(curl, gh, poll.py, git) goes through status._run, which these tests stub.

    python3 manage/test_status.py
"""
from __future__ import annotations

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


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeNet:
    """Dispatches status._run by command; records every call."""

    def __init__(self, stats=None, local="POLL_DECISION=act", pr=None, ci_log=POLL_LOG):
        self.stats = _stats_html() if stats is None else stats
        self.local = local
        self.pr = pr if pr is not None else {"state": "OPEN", "mergeable": "CONFLICTING",
                                             "createdAt": _iso_days_ago(64)}
        self.ci_log = ci_log
        self.calls = []

    def __call__(self, cmd, timeout=12):
        self.calls.append(cmd)
        if cmd[0] == "curl":
            if "-w" in cmd:
                return "200"
            url = cmd[-1]
            if url.endswith("/stats"):
                return self.stats
            if url.endswith("/assets/brain/current.json"):
                return CURRENT_JSON
            return None
        if cmd[:3] == ["gh", "run", "list"]:
            return json.dumps([{"databaseId": 38435564796, "createdAt": _iso_days_ago(0.1)}])
        if cmd[:3] == ["gh", "run", "view"]:
            return self.ci_log
        if cmd[:3] == ["gh", "pr", "view"]:
            return json.dumps(self.pr) if self.pr != "fail" else None
        if any("poll.py" in c for c in cmd):
            return self.local
        if cmd[0] == "git":
            return ""
        return None


@contextlib.contextmanager
def patched(net, td: Path, digest: dict | None, batch: dict | None):
    saved = {k: getattr(status, k) for k in ("_run", "DIGEST", "BOT_STATE", "LIVE_STATE")}
    status._run = net
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

def test_parse_pipeline_runs_reads_the_stats_table():
    runs = status.parse_pipeline_runs(_stats_html())
    assert runs["review"] == {"runs": 118, "articles": 1776, "errors": 858,
                              "tokens": 26345645}, runs
    assert set(runs) == {"new", "review", "wp-update"}, runs
    assert status.parse_pipeline_runs("<html>maintenance</html>") == {}
    assert status.parse_pipeline_runs(None) == {}


def test_review_health_alerts_on_an_all_error_zero_token_night_and_persists():
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "s.json"
        line, alerts = status.review_health(_stats_html((117, 1761, 843, 26345645)),
                                            "2026-10-12 22:17Z", state)
        assert "baseline saved" in line and alerts == [], (line, alerts)
        line, alerts = status.review_health(_stats_html((118, 1776, 858, 26345645)),
                                            "2026-10-13 19:37Z", state)
        assert len(alerts) == 1, alerts
        a = alerts[0]
        assert "1 run(s) since 2026-10-12 22:17Z" in a, a
        assert "errors equal articles (15/15) and tokens unchanged" in a, a
        # the same totals read again (no new run): the alert must not vanish
        _, again = status.review_health(_stats_html((118, 1776, 858, 26345645)),
                                        "2026-10-13 21:00Z", state)
        assert again == alerts, again
        # a healthy night clears it
        _, healthy = status.review_health(_stats_html((119, 1791, 860, 26745645)),
                                          "2026-10-14 08:00Z", state)
        assert healthy == [], healthy


def test_review_alerts_each_condition_alone():
    prev = {"runs": 10, "articles": 150, "errors": 5, "tokens": 1000, "at": "t0"}
    only_tokens = {"runs": 11, "articles": 165, "errors": 6, "tokens": 1000}
    (a,) = status.review_alerts(prev, only_tokens)
    assert "tokens unchanged" in a and "errors equal" not in a, a
    only_errors = {"runs": 12, "articles": 180, "errors": 35, "tokens": 9000}
    (a,) = status.review_alerts(prev, only_errors)
    assert "errors equal articles (30/30)" in a and "tokens unchanged" not in a, a
    assert status.review_alerts(prev, {**prev}) == []
    assert status.review_alerts(None, only_errors) == []


def test_review_health_without_stats():
    with tempfile.TemporaryDirectory() as td:
        line, alerts = status.review_health(None, "now", Path(td) / "s.json")
    assert "unavailable" in line and alerts == []


def test_decision_ignores_the_echoed_grep_command():
    assert status._decision(POLL_LOG) == "wait"
    assert status._decision("POLL_DECISION=act\n") == "act"
    assert status._decision(None) is None
    assert status._decision("if grep -q 'POLL_DECISION=act' x; then") is None


def test_gate_line_reports_local_timeout_and_ci_decision():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td), None, None):
        line, alerts = status.gate_line(None)
        assert line.startswith("gate: local timeout/error · CI wait (run 38435564796"), line
        assert alerts == []
        line, alerts = status.gate_line("POLL_DECISION=act")
        assert line.startswith("gate: local act · CI wait"), line
        assert len(alerts) == 1 and "MATHLIB_FORK_PAT" in alerts[0], alerts
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(ci_log=None), Path(td), None, None):
        line, _ = status.gate_line("POLL_DECISION=wait")
        assert line.startswith("gate: local wait · CI ? (run"), line


def test_batch_pr_alerts_on_conflict_and_age():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td), None, None):
        line, alerts = status.batch_pr(42608)
    assert "#42608 OPEN · CONFLICTING · opened 64d ago" in line, line
    assert len(alerts) == 2, alerts
    assert "conflicts with mathlib master" in alerts[0] and "open 64 days" in alerts[1], alerts
    labelled = {"state": "OPEN", "mergeable": "UNKNOWN", "createdAt": _iso_days_ago(3),
                "labels": [{"name": "merge-conflict"}, {"name": "LLM-generated"}]}
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(pr=labelled), Path(td), None, None):
        _, alerts = status.batch_pr(42608)
    assert len(alerts) == 1 and "conflicts with mathlib master" in alerts[0], alerts
    fresh = {"state": "OPEN", "mergeable": "MERGEABLE", "createdAt": _iso_days_ago(3)}
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(pr=fresh), Path(td), None, None):
        _, alerts = status.batch_pr(42608)
    assert alerts == [], alerts
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(pr="fail"), Path(td), None, None):
        line, alerts = status.batch_pr(42608)
    assert "gh unavailable" in line and alerts == []


def test_brain_release_line():
    with tempfile.TemporaryDirectory() as td, patched(FakeNet(), Path(td), None, None):
        line = status.brain_release()
    assert "release sha256:8305822b0083a086" in line and "audited 2026-10-04T17:25:04Z (" in line, line
    net = FakeNet()
    net_call = net.__call__
    with tempfile.TemporaryDirectory() as td, patched(
            lambda cmd, timeout=12: None if "current.json" in cmd[-1] else net_call(cmd, timeout),
            Path(td), None, None):
        assert "unavailable" in status.brain_release()


def test_graph_staleness_tolerates_one_bulk_copy():
    assert not status.graph_older_than_annotations(1787159418.83, 1787159422.83)
    assert status.graph_older_than_annotations(1787159418.83, 1787159418.83 + 3600)


def test_snapshot_ignores_a_stored_stale_flag_from_a_bulk_copy():
    """Digests written before this rule store the strict-< flag; status must
    recompute from the mtimes instead of trusting it."""
    out = _snapshot_output(FakeNet(), _digest(1787159418.83, 1787159422.83, True), {}, live=False)
    assert "concept graph is stale" not in out, out
    out = _snapshot_output(FakeNet(), _digest(1.0, 7200.0, False), {}, live=False)
    assert "concept graph is stale" in out, out


def test_snapshot_live_prints_every_health_line_and_alert():
    batch = {"current_pr": 42608, "batch_num": 7}
    with tempfile.TemporaryDirectory() as td:
        net = FakeNet(stats=_stats_html((117, 1761, 843, 26345645)))
        with patched(net, Path(td), _digest(1.0, 2.0, False), batch):
            with contextlib.redirect_stdout(io.StringIO()):
                status.snapshot(live=True)  # baseline night
            net.stats = _stats_html((118, 1776, 858, 26345645))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                status.snapshot(live=True)
    out = buf.getvalue()
    for want in ("gate: local act · CI wait (run 38435564796",
                 "review     118 runs · 1,776 articles · 858 errors · 26,345,645 tokens",
                 "brain      release sha256:8305822b",
                 "batch PR   #42608 OPEN · CONFLICTING",
                 "→ bot gate: CI decided 'wait'",
                 "→ nightly review failing: 1 run(s) since",
                 "→ batch PR #42608 conflicts with mathlib master",
                 "→ batch PR #42608 has been open 64 days"):
        assert want in out, (want, out)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in net.calls), "read-only only"


def test_snapshot_without_digest_still_runs_live_checks():
    out = _snapshot_output(FakeNet(), None, {"current_pr": 42608}, live=True)
    assert "no digest yet" in out and "brain      release" in out, out
    assert "batch PR   #42608" in out and "→ batch PR #42608 conflicts" in out, out
    out = _snapshot_output(FakeNet(), None, {}, live=False)
    assert out.strip().startswith("no digest yet") and "brain" not in out, out


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
