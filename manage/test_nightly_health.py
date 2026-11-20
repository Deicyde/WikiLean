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
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nightly_health as nh  # noqa: E402

NOW = 1_792_000_000.0  # fixed clock (2026-10)
MS = 1000


def _at_now():
    """nh.main() takes its drift window from the real clock; pin it to NOW."""
    return mock.patch.object(nh.time, "time", return_value=NOW)


def _write(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _outage_logs(cache: Path) -> None:
    """Three review runs: one healthy, then two where every article fails the
    same way (the 2026-09-18 pattern), plus a new-mode run that must be ignored.
    The failing articles' elapsed_s grows with their wait for one of the two
    concurrency slots, as annotate_one records it."""
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
                "slug": slug, "op": "review", "run_id": rid,
                "elapsed_s": 1.5 + 3.0 * (i // 2),
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
    assert g["min_elapsed_s"] == 1.5, g
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
    assert run["error_groups"][0]["cause"] == "python3 on the job's PATH"


def test_likely_cause_table():
    cases = {
        "FileNotFoundError: [Errno 2] No such file or directory: 'python3'":
            "python3 on the job's PATH",
        "extract_failed": "python3 on the job's PATH",
        "RuntimeError: agent_error: Working directory does not exist: "
        "~/Desktop/LEAN/mathlib4": "Mathlib checkout path",
        "RuntimeError: agent_error: Command failed with exit code 1":
            "claude CLI",
        "RuntimeError: agent_error: Claude Code not found. Install with:":
            "claude CLI",
        "RuntimeError: agent_error: Claude Code returned an error result: success":
            "claude CLI",
        "RuntimeError: agent_error: Control request timeout: initialize":
            "claude CLI startup timeout",
        "RuntimeError: agent_error: API Error: Stream idle timeout - partial "
        "response received": "API stream timeout",
        "RuntimeError: agent_error: Invalid API key · Please run /login":
            "Claude login",
        "RuntimeError: agent_error: Not logged in · Please run /login": "Claude login",
        'agent_error: API Error: 401 {"type":"authentication_error"}': "Claude login",
        "RuntimeError: agent_error: Failed to authenticate. API Error: 401 OAuth "
        "access token has expired": "Claude login",
        "RuntimeError: agent_error: Credit balance is too low": "API-key credential",
        "agent_error: You've hit your weekly limit · resets Oct 16":
            "plan or usage limit",
        "agent_error: You've hit your limit · resets 7:30am (America/New_York)":
            "plan or usage limit",
        "agent_error: There's an issue with the selected model (claude-opus-4-7)":
            "model id",
        'agent_error: API Error: 404 {"type":"not_found_error","message":"model"}':
            "model id",
        "rate_limited_429": "Worker write",
        "network_post_failed": "Worker write",
        "post_503: {}": "Worker write",
        "fetch_failed": "Wikipedia fetch",
        "get_failed_503": "Worker read",
        "agent1_no_json": "agent output",
        # 'moderate' and 'generate' contain 'rate' but are not limits
        "job_crashed: KeyError: 'moderate generate'": "unclassified",
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'site/cache/X.123.sections.json'": "unclassified",
        "something new": "unclassified",
    }
    for err, want in cases.items():
        got = nh.likely_cause(err)
        assert got == want, (err, got, want)


def test_cause_uses_the_untruncated_error():
    recs = [{"slug": "X", "op": "review", "run_id": "ffff0006",
             "error": "agent_error: API Error: 400 " + "x" * 300 + " selected model gone"}]
    (run,) = nh.review_runs(recs, [], 5)
    assert run["error_groups"][0]["cause"] == "model id", run


def test_all_mode_runs_keep_their_review_articles():
    """`moderate.py all` writes wp-update decisions first under the same
    run_id; the review recs still count (their op says review)."""
    decisions = [{"ts": 1, "run_id": "abcd0007", "mode": "wp-update", "slug": "W",
                  "outcome": "noop"},
                 {"ts": 2, "run_id": "abcd0007", "mode": "review", "slug": "R1",
                  "outcome": "error"},
                 {"ts": 3, "run_id": "abcd0007", "mode": "review", "slug": "R2",
                  "outcome": "error"}]
    recs = [{"slug": "R1", "op": "review", "run_id": "abcd0007", "error": "extract_failed"},
            {"slug": "R2", "run_id": "abcd0007", "error": "job_crashed: boom"}]
    (run,) = nh.review_runs(recs, decisions, 5)
    assert run["articles"] == 2 and run["errors"] == 2, run
    assert nh.review_runs(recs, decisions, 0) == []


def test_review_runs_leave_out_dry_runs():
    recs = [{"slug": "A", "op": "review", "run_id": "real0001", "error": "x"},
            {"slug": "B", "op": "review", "run_id": "dryd0002", "error": "x"},
            {"slug": "C", "op": "review", "run_id": "dryf0003", "error": "x", "dry_run": True}]
    decisions = [{"ts": 1, "run_id": "dryd0002", "mode": "review", "slug": "B",
                  "outcome": "dry-run"}]
    runs = nh.review_runs(recs, decisions, 5)
    assert [r["run_id"] for r in runs] == ["real0001"], runs


def test_extract_and_fetch_failures_are_host_wide_only_on_every_article():
    some = [{"slug": "A", "op": "review", "run_id": "some0001", "error": "fetch_failed"},
            {"slug": "B", "op": "review", "run_id": "some0001"},
            {"slug": "C", "op": "review", "run_id": "some0001", "error": "extract_failed"}]
    every = [{"slug": s, "op": "review", "run_id": "evry0002", "error": "fetch_failed"}
             for s in "ABC"]
    (run_some, run_every) = nh.review_runs(some + every, [], 5)
    assert {g["cause"] for g in run_some["error_groups"]} == {"article-specific"}, run_some
    assert [g["cause"] for g in run_every["error_groups"]] == ["Wikipedia fetch"], run_every


def test_each_stage_counts_only_the_articles_that_reached_it():
    """A python3 outage hits every article that gets past the fetch; articles
    that failed earlier must not make it look article-specific."""
    recs = [{"slug": "A", "op": "review", "run_id": "mixd0001", "error": "get_failed_404"},
            {"slug": "B", "op": "review", "run_id": "mixd0001", "error": "fetch_failed"},
            {"slug": "C", "op": "review", "run_id": "mixd0001", "error": "extract_failed"},
            {"slug": "D", "op": "review", "run_id": "mixd0001", "error": "extract_failed"}]
    (run,) = nh.review_runs(recs, [], 5)
    causes = {g["error"]: g["cause"] for g in run["error_groups"]}
    assert causes["extract_failed"] == "python3 on the job's PATH", causes
    assert causes["fetch_failed"] == "article-specific", causes  # C and D got past the fetch


def test_full_disk_access_is_its_own_cause():
    assert nh.likely_cause("PermissionError: [Errno 1] Operation not permitted: "
                           "'/Users/x/Desktop/mathlib4'") == "Full Disk Access"
    assert nh.likely_cause("bash: line 1: exit 126") == "Full Disk Access"
    assert nh.likely_cause("RuntimeError: agent_error: Command failed with exit code 126") == "Full Disk Access"
    assert nh.likely_cause("PermissionError: Permission denied") == "claude CLI"


def test_report_flags_a_sample_from_a_full_old_100_row_page():
    """Only a run of exactly 100 (a full old page) was truncated; smaller runs
    took the whole drifted set."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _drift_logs(cache)
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache), "--runs", "0"])
        assert "lower bound" not in out.getvalue(), out.getvalue()
        t = (NOW - 3600) * MS
        decisions = nh.read_jsonl(cache / ".decisions.jsonl")
        decisions += [{"ts": t, "run_id": "page", "mode": "wp-update", "slug": f"P{i}",
                       "outcome": "noop", "anchors": None} for i in range(100)]
        _write(cache / ".decisions.jsonl", decisions)
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache), "--runs", "0"])
    assert "read the clear rate as a lower bound" in out.getvalue(), out.getvalue()


def test_clean_keeps_slugs_and_paths_readable():
    s = nh.clean("FileNotFoundError: /Users/jack/Desktop/LEAN/WikiLean/site/cache/"
                 "Generalized_hypergeometric_function_and_more.sections.json")
    assert s.startswith("FileNotFoundError: ~/Desktop/LEAN/WikiLean/site/cache/")
    assert "Generalized_hypergeometric_function_and_more" in s, s
    assert nh.clean("token a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6") == "token [redacted]"
    assert nh.clean("mail jack@example.org now") == "mail [email] now"
    assert nh.clean("claude-agent-sdk@0.1.3") == "claude-agent-sdk@0.1.3"
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


def test_drift_trigger_drops_dry_runs():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _drift_logs(cache)
        decisions = nh.read_jsonl(cache / ".decisions.jsonl")
        report = nh.read_jsonl(cache / ".wp_update_report.jsonl")
        t = (NOW - 3600) * MS
        # a dry run whose needs-work lines look real, flagged only by its report rows
        decisions += [{"ts": t, "run_id": "dry1", "mode": "wp-update", "slug": f"D{i}",
                       "outcome": "noop", "anchors": {"matched": 1, "total": 9}}
                      for i in range(30)]
        report += [{"ts": datetime_iso(NOW - 3600), "run_id": "dry1", "slug": f"D{i}",
                    "dry_run": True} for i in range(30)]
        # a dry run flagged by a would-repin line
        decisions += [{"ts": t, "run_id": "dry2", "mode": "wp-update", "slug": "E",
                       "outcome": "dry-run"},
                      {"ts": t, "run_id": "dry2", "mode": "wp-update", "slug": "F",
                       "outcome": "noop", "anchors": {"matched": 1, "total": 9}}]
        d = nh.drift_trigger(decisions, report, 30, NOW)
    assert d["anchors_failed"] == 20 and d["distinct_slugs"] == 9, d


def test_report_warns_when_a_wp_update_run_nears_the_cap():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _drift_logs(cache)
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache), "--runs", "0"])
        assert "queue size" not in out.getvalue(), out.getvalue()
        t = (NOW - 3600) * MS
        decisions = nh.read_jsonl(cache / ".decisions.jsonl")
        decisions += [{"ts": t, "run_id": "big", "mode": "wp-update", "slug": f"S{i}",
                       "outcome": "noop", "anchors": None} for i in range(800)]
        _write(cache / ".decisions.jsonl", decisions)
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache), "--runs", "0"])
    assert "WARNING: a wp-update run took 800 articles" in out.getvalue(), out.getvalue()


def test_report_withholds_the_trigger_on_a_small_sample():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _drift_logs(cache)
        buf = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(buf):
            nh.main(["--cache", str(cache), "--runs", "0"])
    assert "too few drifted slugs to judge (9 < 20)" in buf.getvalue(), buf.getvalue()


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
    assert "15x  [Claude login; fastest 1.5s]" in out, out
    assert "P3 trigger    no stage-0 attempts" in out, out
    assert "2 runs  Head_0" in out, out


def test_report_infers_nothing_from_slow_failures():
    """A slow failure may be queue wait, not agent work: no quota claim."""
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        _write(cache / ".moderate_run.log", [
            {"slug": f"S{i}", "op": "review", "run_id": "abab0008",
             "elapsed_s": 90.0 + 60.0 * (i // 2),
             "error": "RuntimeError: agent_error: Control request timeout: initialize"}
            for i in range(4)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            nh.main(["--cache", str(cache)])
    out = buf.getvalue()
    assert "4x  [claude CLI startup timeout; fastest 90.0s]" in out, out
    assert "quota" not in out.lower(), out


def test_missing_cache_dir_is_an_error():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = nh.main(["--cache", "/nonexistent/site/cache"])
    assert rc == 2 and "no such directory" in buf.getvalue()


CURL_404 = "curl: (22) The requested URL returned error: 404"


def _builder_output(libs) -> list:
    """site/build_library_decls.py's lines as they land in the moderate log:
    stderr's FETCH FAILED lines at once, stdout's when the builder exits.
    libs: [(key, own decls, failure reason or None)]."""
    err = [f"  {k}: FETCH FAILED ({r})" for k, _, r in libs if r is not None]
    out = []
    for k, own, r in libs:
        if r is not None:
            out.append(f"  {k}: keeping {own} decls from the previous blob")
        out.append(f"  {k}: {own} own decls")
    total = sum(own for _, own, _ in libs)
    return err + out + [f"wrote library_decls.json: {total} decls across "
                        f"{len(libs)} libraries (1.6 MB)"]


def _moderate_log(cron: Path, day: int, section, after=()) -> None:
    """One night's moderate log, shaped like site/ops/nightly-moderate.sh's, with
    decoy builder-like lines in the sections either side. section=None leaves
    the decl fabric step out (WIKILEAN_LIBDECLS_REFRESH=0)."""
    stamp = f"202611{day:02d}T032004"
    lines = [f"=== WikiLean nightly moderation {stamp} ===",
             "PY=/usr/bin/python3  wp=1000 review=15 conc=2 budget=700000", "",
             "--- refresh /decl reverse citations (KV declcites:v1) ---",
             "  physlib: 0 own decls", ""]
    if section is not None:
        lines += [nh.DECL_SECTION, *section, "",
                  " ⛅️ wrangler 4.107.0 (update available 4.158.0)",
                  "Resource location: remote ",
                  'Writing the contents of /Users/jack/x/site/out/library_decls.json to the '
                  'key "libdecls:v1" on namespace binding: "RENDER_CACHE" (id: "abc").',
                  *after, ""]
    lines += ["--- refresh golden fixtures (site/out, local only) ---",
              "  cslib: 0 own decls", "(golden fixtures: 3 re-rendered, 0 failed)", "",
              f"=== done {stamp[:9]}045512 ==="]
    cron.mkdir(parents=True, exist_ok=True)
    (cron / f"moderate-{stamp}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


HEALTHY = [("cslib", 6139, None), ("physlib", 12739, None),
           ("formal-conjectures", 13823, None)]


def _fc(own, reason):
    return [("cslib", 6139, None), ("physlib", 12739, None),
            ("formal-conjectures", own, reason)]


def test_decl_section_reads_only_its_own_lines():
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 9, _builder_output(_fc(0, CURL_404)))
        (log,) = cron.iterdir()
        sec = nh.decl_section(log.read_text(encoding="utf-8"))
    assert sec["libraries"] == {
        "formal-conjectures": {"own": 0, "kept": 0, "reason": CURL_404},
        "cslib": {"own": 6139, "kept": None, "reason": None},
        "physlib": {"own": 12739, "kept": None, "reason": None},
    }, sec
    assert not sec["build_failed"] and sec["kv_put"] is None, sec
    assert nh.decl_section("=== WikiLean nightly moderation ===\n--- review batch ---\n") is None


def test_decl_section_waits_for_the_builder_to_finish():
    """Read mid-run, a section has only the FETCH FAILED lines (stderr) so far;
    it counts once the builder's last line or the wrapper's build-failed line is in."""
    head = f"=== WikiLean nightly moderation 20261120T032004 ===\n{nh.DECL_SECTION}\n"
    running = head + f"  formal-conjectures: FETCH FAILED ({CURL_404})\n"
    assert nh.decl_section(running) is None
    assert nh.decl_section(running + "(library-decls build failed — keeping the last KV copy)\n"
                           )["build_failed"]
    done = running + "\n".join(_builder_output(_fc(0, CURL_404))[1:]) + "\n"
    assert nh.decl_section(done)["libraries"]["formal-conjectures"]["own"] == 0


def test_decl_fabric_flags_an_enabled_library_at_zero_on_the_first_night():
    """A fetch that succeeds but keeps no decl (module_roots wrong, say) is
    flagged at once; the empty blob looks like any other success."""
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 9, _builder_output(HEALTHY))
        _moderate_log(cron, 10, _builder_output(_fc(0, None)))
        fab = nh.decl_fabric(cron)
    assert fab["log"] == "moderate-20261110T032004.log" and fab["refreshes"] == 2, fab
    assert [lib["key"] for lib in fab["libraries"]] == ["cslib", "formal-conjectures", "physlib"]
    (w,) = fab["warnings"]
    assert w.startswith("formal-conjectures has 0 own decls"), w


def test_decl_fabric_flags_a_fetch_that_failed_three_refreshes_running():
    """One or two failed nights are listed, not flagged: the builder keeps the
    last good blob. The run starts after the last night the fetch worked."""
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 8, _builder_output(_fc(13823, "JSONDecodeError")))
        _moderate_log(cron, 9, _builder_output(HEALTHY))
        for day in (10, 11):
            _moderate_log(cron, day, _builder_output(_fc(13823, "JSONDecodeError")))
        fab = nh.decl_fabric(cron)
        fc = next(lib for lib in fab["libraries"] if lib["key"] == "formal-conjectures")
        assert fc["failed"] and fc["run"] == 2 and fc["kept"] == 13823, fc
        assert fab["warnings"] == [], fab["warnings"]
        _moderate_log(cron, 12, _builder_output(_fc(13823, "JSONDecodeError")))
        fab = nh.decl_fabric(cron)
    (w,) = fab["warnings"]
    assert w == ("formal-conjectures: fetch failed in each of the last 3 refreshes, since "
                 "2026-11-10 (JSONDecodeError); the 13,823 decls it serves are left from an "
                 "earlier blob"), w


def test_decl_fabric_the_2026_11_incident():
    """formal-conjectures' doc-gen4 site gone and no previous entries to keep:
    0 own decls every night. Both warnings, and the run covers every log read."""
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        for day in range(9, 21):
            _moderate_log(cron, day, _builder_output(_fc(0, "JSONDecodeError")))
        fab = nh.decl_fabric(cron)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nh.print_decl_fabric(fab)
    zero, run = fab["warnings"]
    assert zero.startswith("formal-conjectures has 0 own decls"), zero
    assert ("fetch failed in each of the last 12 refreshes, since at least 2026-11-09 "
            "(JSONDecodeError)") in run, run
    assert "left from an earlier blob" not in run, run
    assert ("formal-conjectures        0 own decls  fetch failed (JSONDecodeError), kept 0 "
            "from the previous blob; 12 refreshes running") in out.getvalue(), out.getvalue()


def test_decl_fabric_run_stops_where_the_library_is_not_listed():
    """A newly enabled library's run cannot reach back before it was enabled."""
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 9, _builder_output(HEALTHY[:2]))
        for day in (10, 11, 12):
            _moderate_log(cron, day, _builder_output(_fc(0, CURL_404)))
        fab = nh.decl_fabric(cron)
    fc = next(lib for lib in fab["libraries"] if lib["key"] == "formal-conjectures")
    assert fc["run"] == 3 and fc["since"] == "2026-11-10", fc
    assert "since 2026-11-10 (curl: (22) The requested URL returned error: 404)" in \
        fab["warnings"][1], fab["warnings"]


def test_decl_fabric_run_passes_over_a_build_that_listed_nothing():
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 9, _builder_output(HEALTHY))
        for day in (10, 11):
            _moderate_log(cron, day, _builder_output(_fc(13823, "JSONDecodeError")))
        _moderate_log(cron, 12, ["(library-decls build failed — keeping the last KV copy)"])
        _moderate_log(cron, 13, _builder_output(_fc(13823, "JSONDecodeError")))
        fab = nh.decl_fabric(cron)
    fc = next(lib for lib in fab["libraries"] if lib["key"] == "formal-conjectures")
    assert fc["run"] == 3 and fc["since"] == "2026-11-10", fc
    assert len(fab["warnings"]) == 1 and "last 3 refreshes" in fab["warnings"][0], fab


def test_decl_fabric_skips_logs_without_the_step_and_reads_only_the_newest():
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        for day in (9, 10, 11):
            _moderate_log(cron, day, _builder_output(_fc(5, "CalledProcessError")))
        _moderate_log(cron, 12, None)
        fab = nh.decl_fabric(cron)
        assert fab["log"] == "moderate-20261111T032004.log", fab
        assert fab["newest_log"] == "moderate-20261112T032004.log", fab
        assert fab["libraries"][1]["run"] == 3, fab["libraries"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            nh.print_decl_fabric(fab)
        assert "the newest log, moderate-20261112T032004.log, has no finished refresh section" \
            in out.getvalue(), out.getvalue()
        assert nh.decl_fabric(cron, max_logs=2)["libraries"][1]["run"] == 1
        assert nh.decl_fabric(cron, max_logs=1)["log"] is None


def test_decl_fabric_flags_a_failed_build_and_kv_put():
    with tempfile.TemporaryDirectory() as td:
        cron = Path(td)
        _moderate_log(cron, 9, ["Traceback (most recent call last):",
                                "FileNotFoundError: catalog/data/libraries.json",
                                "(library-decls build failed — keeping the last KV copy)"])
        fab = nh.decl_fabric(cron)
        assert fab["libraries"] == [] and len(fab["warnings"]) == 1, fab
        assert fab["warnings"][0].startswith("the build failed"), fab["warnings"]
        _moderate_log(cron, 10, _builder_output(HEALTHY), after=["(libdecls kv put returned 1)"])
        fab = nh.decl_fabric(cron)
    assert fab["warnings"] == ["(libdecls kv put returned 1): KV libdecls:v1 is older than "
                               "this blob"], fab["warnings"]


def test_report_prints_the_decl_fabric():
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache)])
        assert ("decl fabric (KV libdecls:v1): no finished refresh section and no moderate logs "
                "under cron/") in out.getvalue(), out.getvalue()
        _moderate_log(cache / "cron", 9, _builder_output(HEALTHY))
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            rc = nh.main(["--cache", str(cache)])
        text = out.getvalue()
        assert rc == 0
        assert ("decl fabric (KV libdecls:v1), moderate-20261109T032004.log (the newest of "
                "1 refreshes read):") in text, text
        assert "  physlib              12,739 own decls\n" in text, text
        assert "verdict  no library is at 0 own decls" in text, text
        assert "WARNING" not in text, text
        for day in (10, 11, 12):
            _moderate_log(cache / "cron", day, _builder_output(_fc(0, CURL_404)))
        out = io.StringIO()
        with _at_now(), contextlib.redirect_stdout(out):
            nh.main(["--cache", str(cache)])
    text = out.getvalue()
    assert "  WARNING  formal-conjectures has 0 own decls" in text, text
    assert "  WARNING  formal-conjectures: fetch failed in each of the last 3 refreshes" in text
    assert "verdict" not in text.split("decl fabric")[1], text


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
