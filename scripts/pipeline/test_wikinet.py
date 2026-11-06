#!/usr/bin/env python3
"""Stage 9a: the Wikipedia client and the fetch command, with no network.

docs/PIPELINE.md sections 3 and 8. Every request in this file goes through a recorded
:class:`wikinet.FixtureTransport` or a fake ``curl`` runner; nothing here opens a
socket, and :class:`TestNothingElseFetches` asserts that no other module of the
pipeline could.
"""
from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fetch_sources  # noqa: E402
import render_web  # noqa: E402
import sourceset  # noqa: E402
import vocab  # noqa: E402
import wikinet  # noqa: E402

SITE = "https://en.wikipedia.org"
PAGE = ('<div class="mw-parser-output"><p>A <b>widget space</b> is a set with a widget '
        'structure and more words.</p><h2 id="S">Section</h2><p>More text here.</p></div>')


def recording(directory: Path, params: dict, value) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = wikinet.request_key(params)[:16] + ".json"
    (directory / name).write_text(
        json.dumps({"query": params, "json": value}, sort_keys=True), encoding="utf-8")


class FakeProc:
    def __init__(self, returncode=0, stdout="200", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def runner_for(answers, *, body: bytes = b'{"ok":1}', headers: bytes = b""):
    """A ``subprocess.run`` stand-in that writes ``body`` and returns ``answers`` in turn."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        out = argv[argv.index("-o") + 1]
        Path(out).write_bytes(body() if callable(body) else body)
        if "-D" in argv:
            Path(argv[argv.index("-D") + 1]).write_bytes(headers)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    run.calls = calls
    return run


class TestTheFixedParameterSet(unittest.TestCase):
    def test_the_parse_parameters_are_pinned(self):
        self.assertEqual(wikinet.PARSE_PARAMS, {
            "action": "parse", "prop": "text", "formatversion": "2",
            "disablelimitreport": "1", "disableeditsection": "1", "format": "json"})

    def test_params_sha256_is_what_a_fetched_entry_records(self):
        # a change to the set is a change to this sha, and an entry fetched with
        # another set can never pass for one fetched with this one
        self.assertEqual(wikinet.params_sha256(),
                         "08f24dd2506244dcf596e1f4932946c970a5c2e656c84133fd11b15295b2e890")
        self.assertEqual(wikinet.fetch_block(),
                         {"api": "action=parse", "params_sha256": wikinet.params_sha256()})

    def test_a_revision_is_a_positive_integer(self):
        self.assertEqual(wikinet.parse_params(7)["oldid"], "7")
        for bad in (0, -1, True, "7", None):
            with self.assertRaises(wikinet.WikiError):
                wikinet.parse_params(bad)


class TestRequestUrl(unittest.TestCase):
    def test_https_only_and_the_api_only(self):
        with self.assertRaises(wikinet.WikiError):
            wikinet.request_url("http://en.wikipedia.org", {"a": "1"})
        with self.assertRaises(wikinet.WikiError):
            wikinet.request_url(SITE, {"a": "1"}, "/wiki/Main_Page")

    def test_the_parameters_are_sorted_and_encoded(self):
        url = wikinet.request_url(SITE, {"b": "2", "a": "x y"})
        self.assertEqual(url, SITE + "/w/api.php?a=x+y&b=2")

    def test_the_recording_key_leaves_out_maxlag(self):
        self.assertEqual(wikinet.request_key({"a": "1"}),
                         wikinet.request_key({"a": "1", "maxlag": "5"}))


class TestCurlTransport(unittest.TestCase):
    def transport(self, answers, **over):
        run = runner_for(answers, **{k: v for k, v in over.items()
                                     if k in ("body", "headers")})
        slept: list[float] = []
        t = wikinet.CurlTransport(runner=run, sleep=slept.append, clock=lambda: 0.0,
                                  exe="/usr/bin/curl")
        return t, run, slept

    def test_the_command_line_is_bounded_and_polite(self):
        t, _run, _slept = self.transport([FakeProc()])
        argv = t.argv("https://x/y", "/tmp/out", "/tmp/hdr")
        for flag in ("--fail", "--proto", "=https", "--proto-redir", "--max-redirs",
                     "--max-filesize", "--max-time"):
            self.assertIn(flag, argv)
        self.assertIn(wikinet.USER_AGENT, argv)
        self.assertNotIn("-k", argv)
        self.assertNotIn("--insecure", argv)
        # the agent names WikiLean and nothing about the operator
        self.assertNotIn("@", wikinet.USER_AGENT)

    def test_maxlag_rides_every_request(self):
        t, run, _slept = self.transport([FakeProc()])
        t.get(SITE, {"a": "1"})
        self.assertIn("maxlag=5", run.calls[0][-1])

    def test_a_lagged_replica_is_retried_then_given_up_on(self):
        lag = json.dumps({"error": {"code": "maxlag"}}).encode()
        t, run, slept = self.transport([FakeProc()], body=lag)
        with self.assertRaises(wikinet.WikiUnavailable):
            t.get(SITE, {"a": "1"})
        self.assertEqual(len(run.calls), wikinet.MAX_ATTEMPTS)
        backoffs = [s for s in slept if s >= wikinet.MAXLAG_WAIT]
        self.assertEqual(len(backoffs), wikinet.MAX_ATTEMPTS - 1)

    def test_a_429_is_retried_and_honours_retry_after(self):
        t, run, slept = self.transport([FakeProc(stdout="429"), FakeProc()],
                                       headers=b"HTTP/2 429\r\nRetry-After: 30\r\n\r\n")
        t.get(SITE, {"a": "1"})
        self.assertEqual(len(run.calls), 2)
        self.assertIn(30.0, slept)

    def test_a_403_stops_at_once(self):
        t, run, _slept = self.transport([FakeProc(stdout="403")])
        with self.assertRaises(wikinet.WikiUnavailable):
            t.get(SITE, {"a": "1"})
        self.assertEqual(len(run.calls), 1)

    def test_another_failure_is_not_retried(self):
        t, run, _slept = self.transport([FakeProc(returncode=1, stdout="404")])
        with self.assertRaises(wikinet.WikiError):
            t.get(SITE, {"a": "1"})
        self.assertEqual(len(run.calls), 1)

    def test_retry_after_reads_the_last_response_only(self):
        headers = b"HTTP/2 301\r\nRetry-After: 9\r\n\r\nHTTP/2 429\r\nRetry-After: 42\r\n\r\n"
        self.assertEqual(wikinet.retry_after(headers), 42.0)
        self.assertIsNone(wikinet.retry_after(b"HTTP/2 200\r\nRetry-After: Fri\r\n\r\n"))


class TestFixtureTransport(unittest.TestCase):
    def test_a_request_with_no_recording_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            recording(Path(tmp), {"a": "1"}, {"ok": True})
            t = wikinet.FixtureTransport(tmp)
            self.assertEqual(json.loads(t.get(SITE, {"a": "1"})), {"ok": True})
            with self.assertRaises(wikinet.WikiError) as caught:
                t.get(SITE, {"a": "2"})
            self.assertIn("a=2", str(caught.exception))


class TestTheThreeRequests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_fetch_parse_checks_the_revision_it_answered_for(self):
        recording(self.dir, wikinet.parse_params(5), {"parse": {"revid": 5, "text": PAGE}})
        recording(self.dir, wikinet.parse_params(6), {"parse": {"revid": 7, "text": PAGE}})
        recording(self.dir, wikinet.parse_params(8), {"parse": {"revid": 8}})
        t = wikinet.FixtureTransport(self.dir)
        self.assertIn(b"widget space", wikinet.fetch_parse(t, SITE, 5))
        for bad in (6, 8):
            with self.assertRaises(wikinet.WikiError):
                wikinet.fetch_parse(t, SITE, bad)

    def test_an_api_error_is_the_refusal(self):
        recording(self.dir, wikinet.parse_params(5),
                  {"error": {"code": "nosuchrevid", "info": "no"}})
        with self.assertRaises(wikinet.WikiError) as caught:
            wikinet.fetch_parse(wikinet.FixtureTransport(self.dir), SITE, 5)
        self.assertIn("nosuchrevid", str(caught.exception))

    def test_project_pages_follows_the_continuation_and_takes_articles_only(self):
        first = {**wikinet.PROJECT_PARAMS, "wppprojects": "Mathematics"}
        recording(self.dir, first, {
            "continue": {"wppcontinue": "x"},
            "query": {"projectpages": [{"pages": [
                {"ns": 0, "pageid": 1, "title": "A",
                 "assessment": {"class": "B", "importance": "high"}},
                {"ns": 1, "pageid": 2, "title": "Talk:A"}]}]}})
        recording(self.dir, {**first, "wppcontinue": "x"}, {
            "query": {"projectpages": [{"pages": [
                {"ns": 0, "pageid": 3, "title": "C", "assessment": {}}]}]}})
        pages = wikinet.project_pages(wikinet.FixtureTransport(self.dir), SITE,
                                      "Mathematics")
        self.assertEqual(sorted(pages), [1, 3])
        self.assertEqual(pages[1], {"title": "A", "class": "B", "importance": "high"})
        self.assertEqual(pages[3]["class"], None)

    def test_a_continuation_that_repeats_itself_is_refused(self):
        first = {**wikinet.PROJECT_PARAMS, "wppprojects": "M"}
        recording(self.dir, first, {"continue": {"c": "x"}, "query": {}})
        recording(self.dir, {**first, "c": "x"}, {"continue": {"c": "x"}, "query": {}})
        with self.assertRaises(wikinet.WikiError) as caught:
            wikinet.project_pages(wikinet.FixtureTransport(self.dir), SITE, "M")
        self.assertIn("repeats itself", str(caught.exception))

    def test_page_infos_batches_and_reads_the_four_facts(self):
        ids = list(range(1, wikinet.INFO_BATCH + 2))
        for at in (0, wikinet.INFO_BATCH):
            batch = ids[at:at + wikinet.INFO_BATCH]
            recording(self.dir, {**wikinet.PAGE_INFO_PARAMS,
                                 "pageids": "|".join(str(p) for p in batch)},
                      {"query": {"pages": [
                          {"pageid": p, "lastrevid": 100 + p,
                           "pageprops": ({"wikibase_item": f"Q{p}"} if p == 1 else
                                         {"disambiguation": ""}),
                           **({"redirect": True} if p == 2 else {})}
                          for p in batch]}})
        t = wikinet.FixtureTransport(self.dir)
        infos = wikinet.page_infos(t, SITE, reversed(ids))
        self.assertEqual(len(infos), len(ids))
        self.assertEqual(len(t.requests), 2)
        self.assertEqual(infos[1], {"revision": 101, "wikidata": "Q1", "redirect": False,
                                    "disambiguation": False})
        self.assertTrue(infos[2]["redirect"])
        self.assertTrue(infos[3]["disambiguation"])


class TestFetchCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.ss_dir = self.root / "wpm-test"
        ss = sourceset.Sourceset.create(self.ss_dir, "wpm-test", "enwiki")
        self.entry = {"assessment": {"class": "C", "importance": "mid"}, "family": "enwiki",
                      "fetch": None, "flags": {"biography": None, "disambiguation": False,
                                               "list": False, "redirect": False},
                      "html_sha256": None, "layer_sha256": None,
                      "licence_class": "open_share_alike", "origin": "enumerated",
                      "pageid": 11, "raw_sha256": None, "renderer": None, "revision": 22,
                      "source_id": "enwiki-11-22", "title": "Widget space",
                      "wikidata": "Q11"}
        ss.write_version({self.entry["source_id"]: self.entry})
        self.fixtures = self.root / "recordings"
        recording(self.fixtures, wikinet.parse_params(22),
                  {"parse": {"revid": 22, "text": PAGE}})

    def run_command(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = fetch_sources.main(["--dir", str(self.ss_dir), *argv])
        return code, out.getvalue()

    def test_the_default_is_a_dry_run_that_asks_for_nothing(self):
        code, text = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("dry run", text)
        self.assertIn("oldid=22", text)
        self.assertEqual(self.ss_dir.joinpath("manifest-3.jsonl").exists(), False)

    def test_execute_and_fixtures_together_are_refused(self):
        code = fetch_sources.main(["--dir", str(self.ss_dir), "--execute",
                                   "--fixtures", str(self.fixtures)])
        self.assertEqual(code, 2)

    def test_a_replayed_run_fetches_renders_and_writes_the_next_version(self):
        code, text = self.run_command("--fixtures", str(self.fixtures))
        self.assertEqual(code, 0)
        ss = sourceset.Sourceset.open(self.ss_dir)
        manifest = ss.latest()
        self.assertEqual(manifest.version, 3)
        filled = manifest.get("enwiki-11-22")
        self.assertEqual(filled["renderer"], vocab.RENDERER_WEB_HTML)
        self.assertEqual(filled["fetch"], wikinet.fetch_block())
        self.assertIn(manifest.sha256, text)
        directory = ss.source_dir("enwiki-11-22")
        for name in render_web.RENDER_FILES:
            self.assertTrue((directory / name).is_file(), name)
        self.assertEqual(ss.source_problems(filled), [])
        rendering = render_web.render(render_web.read_body(directory / "body.html"))
        self.assertEqual(rendering.layer_sha256, filled["layer_sha256"])

    def test_a_revision_is_fetched_once(self):
        self.run_command("--fixtures", str(self.fixtures))
        empty = self.root / "no-recordings"
        empty.mkdir()
        code, text = self.run_command("--fixtures", str(empty))
        self.assertEqual(code, 0)
        self.assertIn("already records", text)

    def test_render_only_fetches_nothing(self):
        code = fetch_sources.main(["--dir", str(self.ss_dir), "--render-only",
                                   "--fixtures", str(self.fixtures)])
        self.assertEqual(code, 2)       # no source.raw.html is held yet


class TestNothingElseFetches(unittest.TestCase):
    """Only this module may reach the network, and only under ``--execute``."""

    NETWORK = re.compile(r"\b(?:urlopen|urllib\.request|requests\.|httpx|socket\.|"
                         r"curl|wget)\b")

    def test_no_other_pipeline_module_names_a_fetch(self):
        allowed = {"wikinet.py", "test_wikinet.py"}
        offenders = []
        for path in sorted(HERE.rglob("*.py")):
            if path.name in allowed or "fixtures" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.split("\n"), 1):
                code = line.split("#", 1)[0]
                if self.NETWORK.search(code):
                    offenders.append(f"{path.relative_to(HERE)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])

    def test_the_fetch_command_builds_a_live_transport_only_under_execute(self):
        text = (HERE / "fetch_sources.py").read_text(encoding="utf-8")
        self.assertIn("elif execute:\n        transport = wikinet.CurlTransport()", text)
        self.assertEqual(text.count("CurlTransport("), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
