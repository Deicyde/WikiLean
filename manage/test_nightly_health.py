#!/usr/bin/env python3
"""Tests for manage/nightly_health.py over synthetic site/cache logs.

    python3 manage/test_nightly_health.py
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nightly_health as nh  # noqa: E402

NOW = 1_792_000_000.0  # fixed clock (2026-10)
MS = 1000


def _write(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _outage_logs(cache: Path) -> None:
    """Three review runs: one healthy, then two where every article fails the
    same way (the 2026-09-18 pattern), plus a new-mode run that must be ignored."""
    run_log, decisions = [], []
    t = (NOW - 3 * 86400) * MS
    for i in range(3):
        run_log.append({"slug": f"Ok_{i}", "op": "review", "run_id": "aaaa0001",
                        "tokens": 30000, "elapsed_s": 200.0})
        decisions.append({"ts": t + i, "run_id": "aaaa0001", "mode": "review",
                          "slug": f"Ok_{i}", "outcome": "posted"})
    for n, rid in enumerate(("bbbb0002", "cccc0003")):
        t = (NOW - (2 - n) * 86400) * MS
        for i in range(15):
            slug = f"Head_{i}"
            run_log.append({
                "slug": slug, "op": "review", "run_id": rid, "elapsed_s": 1.5,
                "error": ("RuntimeError: agent_error: Not logged in · Please run "
                          "/login (/Users/jack/.claude sk-ant-oat01-SECRETSECRET)")})
            decisions.append({"ts": t + i, "run_id": rid, "mode": "review",
                              "slug": slug, "outcome": "error"})
    run_log.append({"slug": "New_1", "op": "new", "run_id": "dddd0004",
                    "error": "fetch_failed"})
    decisions.append({"ts": NOW * MS, "run_id": "dddd0004", "mode": "new",
                      "slug": "New_1", "outcome": "error"})
    _write(cache / ".moderate_run.log", run_log)
    _write(cache / ".decisions.jsonl", decisions)


def test_review_runs_groups_errors_and_drops_other_modes():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _outage_logs(cache)
        runs = nh.review_runs(nh.read_jsonl(cache / ".moderate_run.log"),
                              nh.read_jsonl(cache / ".decisions.jsonl"), 5)
    assert [r["run_id"] for r in runs] == ["aaaa0001", "bbbb0002", "cccc0003"], runs
    last = runs[-1]
    assert last["articles"] == 15 and last["errors"] == 15 and last["tokens"] == 0
    (g,) = last["error_groups"]
    assert g["count"] == 15 and g["cause"] == "Claude login", g
    assert g["median_elapsed_s"] == 1.5
    assert "/Users/jack" not in g["error"] and "~/.claude" in g["error"], g["error"]
    assert "SECRET" not in g["error"], g["error"]
    assert len(g["slugs"]) == 3


def test_last_n_runs_only():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _outage_logs(cache)
        runs = nh.review_runs(nh.read_jsonl(cache / ".moderate_run.log"),
                              nh.read_jsonl(cache / ".decisions.jsonl"), 2)
    assert [r["run_id"] for r in runs] == ["bbbb0002", "cccc0003"]


def test_repeated_failures_flags_the_stuck_queue_head():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _outage_logs(cache)
        runs = nh.review_runs(nh.read_jsonl(cache / ".moderate_run.log"),
                              nh.read_jsonl(cache / ".decisions.jsonl"), 5)
    rep = dict(nh.repeated_failures(runs))
    assert len(rep) == 15 and all(n == 2 for n in rep.values()), rep


def test_run_log_without_decisions_falls_back_to_op():
    recs = [{"slug": "X", "op": "review", "run_id": "eeee0005",
             "error": "extract_failed", "elapsed_s": 0.4}]
    (run,) = nh.review_runs(recs, [], 5)
    assert run["started"] == "?" and run["errors"] == 1
    assert run["error_groups"][0]["cause"] == "python3 on the launchd PATH"


def test_likely_cause_table():
    cases = {
        "FileNotFoundError: [Errno 2] No such file or directory: 'python3'":
            "python3 on the launchd PATH",
        "extract_failed": "python3 on the launchd PATH",
        "RuntimeError: agent_error: Working directory does not exist: "
        "~/Desktop/LEAN/mathlib4": "Mathlib checkout path",
        "RuntimeError: agent_error: Command failed with exit code 1":
            "claude CLI launch",
        "RuntimeError: agent_error: Claude Code not found. Install with:":
            "claude CLI launch",
        "RuntimeError: agent_error: Invalid API key · Please run /login":
            "Claude login",
        'agent_error: API Error: 401 {"type":"authentication_error"}': "Claude login",
        "RuntimeError: agent_error: Credit balance is too low":
            "plan or usage limit",
        "agent_error: You've hit your weekly limit · resets Oct 16":
            "plan or usage limit",
        "agent_error: There's an issue with the selected model (claude-opus-4-7)":
            "model id",
        "fetch_failed": "network",
        "get_failed_503": "network",
        "agent1_no_json": "agent output",
        "something new": "unclassified",
    }
    for err, want in cases.items():
        got = nh.likely_cause(err)
        assert got == want, (err, got, want)


def test_clean_keeps_slugs_and_paths_readable():
    s = nh.clean("FileNotFoundError: /Users/jack/Desktop/LEAN/WikiLean/site/cache/"
                 "Generalized_hypergeometric_function_and_more.sections.json")
    assert s.startswith("FileNotFoundError: ~/Desktop/LEAN/WikiLean/site/cache/")
    assert "Generalized_hypergeometric_function_and_more" in s, s
    assert nh.clean("token a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6") == "token [redacted]"
    assert nh.clean("mail jack@example.org now") == "mail [email] now"
    assert len(nh.clean("x" * 500)) == 200


def _drift_logs(cache: Path) -> None:
    """Five nights: the same 4 blocked slugs retried nightly, one re-pin per
    night, already-current and no-latest-revid noops, an error, and one
    attempt outside the window."""
    decisions, report = [], []
    for night in range(5):
        t = (NOW - (night + 1) * 86400) * MS
        decisions.append({"ts": t, "run_id": f"w{night}", "mode": "wp-update",
                          "slug": f"Repin_{night}", "outcome": "posted",
                          "anchors": {"matched": 10, "total": 10}})
        for b in range(4):
            slug = f"Blocked_{b}"
            decisions.append({"ts": t, "run_id": f"w{night}", "mode": "wp-update",
                              "slug": slug, "outcome": "noop",
                              "anchors": {"matched": 8, "total": 10}})
            report.append({"ts": datetime_iso(t / MS), "run_id": f"w{night}",
                           "slug": slug, "dry_run": False})
        decisions.append({"ts": t, "run_id": f"w{night}", "mode": "wp-update",
                          "slug": "Current", "outcome": "noop", "anchors": None})
    decisions.append({"ts": (NOW - 86400) * MS, "run_id": "w0", "mode": "wp-update",
                      "slug": "Broken", "outcome": "error", "anchors": None})
    old = (NOW - 40 * 86400) * MS
    decisions.append({"ts": old, "run_id": "old", "mode": "wp-update",
                      "slug": "Blocked_0", "outcome": "posted",
                      "anchors": {"matched": 10, "total": 10}})
    report.append({"ts": datetime_iso(old / MS), "run_id": "old", "slug": "Old",
                   "dry_run": False})
    report.append({"ts": datetime_iso(NOW - 86400), "run_id": "dry", "slug": "Dry",
                   "dry_run": True})
    _write(cache / ".decisions.jsonl", decisions)
    _write(cache / ".wp_update_report.jsonl", report)


def datetime_iso(epoch_s: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat(timespec="seconds")


def test_drift_trigger_rates_and_queue_head():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _drift_logs(cache)
        d = nh.drift_trigger(nh.read_jsonl(cache / ".decisions.jsonl"),
                             nh.read_jsonl(cache / ".wp_update_report.jsonl"),
                             30, NOW)
    assert d["repinned"] == 5 and d["anchors_failed"] == 20 and d["errors"] == 1, d
    assert abs(d["clear_rate"] - 0.2) < 1e-9, d
    # 5 distinct re-pinned + 4 distinct blocked (the old re-pin of Blocked_0 is
    # outside the window, so it does not count as cleared)
    assert d["distinct_slugs"] == 9 and d["distinct_cleared"] == 5, d
    assert d["never_cleared"] == 4
    assert d["report_rows"] == 20, d
    assert d["queue_head"][0] == ("Blocked_0", 5), d["queue_head"]


def test_report_prints_verdict_and_trigger():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _outage_logs(cache)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = nh.main(["--cache", str(cache), "--runs", "3"])
    out = buf.getvalue()
    assert rc == 0
    assert "failed on every article; likely cause: Claude login" in out, out
    assert "15x  [Claude login; median 1.5s]" in out, out
    assert "P3 trigger    no stage-0 attempts" in out, out
    assert "2 runs  Head_0" in out, out


def test_missing_cache_dir_is_an_error():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = nh.main(["--cache", "/nonexistent/site/cache"])
    assert rc == 2 and "no such directory" in buf.getvalue()


def test_runs_on_python_39_grammar():
    """The script must run on an older host's python3 (macOS system Python)."""
    src = (HERE / "nightly_health.py").read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 9))


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
