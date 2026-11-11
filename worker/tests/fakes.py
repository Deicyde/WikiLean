"""A fake WikiLean and a fake `claude`, for the tests in this directory.

Every fixture here is invented. No Wikipedia text is in this repository: the
"article" below is a made-up page about a made-up object, which is all the
tests need, since what they check is request shapes and refusals rather than
mathematics.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def pair_key(passage_key: str, repository: str, declaration: str) -> str:
    return hashlib.sha256(f"{passage_key}\n{repository}\n{declaration}".encode()).hexdigest()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


UNIVERSE_SHA = sha("universe")
REPO = "https://example.invalid/fixture/mathlib"
CORE_REPO = "https://example.invalid/fixture/lean4"

# The two libraries' Lake module roots, as a sealed universe always carries
# them (build_store.load_universe requires a non-empty list). They are what
# says which library a declaration's module belongs to.
ROOTS = {"mathlib": ["Fixture"], "lean-core": ["FixtureCore.*"]}
# What the fake /decl knows: declaration -> the module it lives in. The real
# route answers "library": "mathlib" for a Lean core name too, because the
# Mathlib index carries core; the fake does the same, so a test that trusted
# that field would fail here as it does live.
KNOWN_DECLS = {
    "Fixture.mulComm": "Fixture.Basic",
    "Fixture.settled": "Fixture.Basic",
    "Fixture.known": "Fixture.Basic",
    # Core names, indexed beside Mathlib's and belonging to lean-core.
    "FixtureCore.natSucc": "FixtureCore.Prelude",
    "FixtureCore.zeroAdd": "FixtureCore.Basic",
    # A name in no library of the fixture universe at all.
    "Elsewhere.lemma": "Elsewhere.Basic",
}

PASSAGE_DOWNGRADED = sha("passage-downgraded")
PASSAGE_ABSENCE = sha("passage-absence")
PASSAGE_UNTAGGED = sha("passage-untagged")

DOWNGRADED_DECL = "Fixture.mulComm"
DOWNGRADED_PAIR = pair_key(PASSAGE_DOWNGRADED, REPO, DOWNGRADED_DECL)

CAMPAIGN = "run:loc-fixture-a+chal-fixture-a"
PAGEID = 4242
# A second article with tag work and no review work, which the index orders
# FIRST: a round must still prefer the review work in the article behind it.
SECOND_PAGEID = 4243
SECOND_PASSAGE = sha("second-untagged")


def universe() -> dict:
    return {
        "schema": "wikilean.articles.universe/1",
        "id": "u-fixture",
        "sha256": UNIVERSE_SHA,
        "frozen": "2026-01-01",
        "libraries": [
            {"id": "mathlib", "repo": REPO, "commit": "0" * 40, "display_name": "Fixture Mathlib",
             "roots": list(ROOTS["mathlib"])},
            {"id": "lean-core", "repo": CORE_REPO, "commit": "1" * 40, "display_name": "Fixture core",
             "roots": list(ROOTS["lean-core"])},
        ],
        "excluded": [],
    }


def _passage(identifier: str, key: str, *, kind: str, prominence: str, outcome: str,
             status: str, locations: list, absence: dict | None) -> dict:
    return {
        "id": f"fixture-{PAGEID}-1::{identifier}",
        "item": identifier,
        "passage_key": key,
        "kind": kind,
        "section": ["Fixture section"],
        "prominence": prominence,
        "quote": {"exact": f"A fixture sentence about the {identifier} of a fixture object."},
        "subject_uri": "https://example.invalid/fixture",
        "outcome": outcome,
        "status": status,
        "basis": None,
        "coverage": None,
        "locations": locations,
        "absence": absence,
        "unresolved": None,
    }


def article() -> dict:
    """One synthetic article carrying one candidate of each kind."""
    downgraded = {
        "library": "mathlib", "repo": REPO, "commit": "0" * 40,
        "declaration": DOWNGRADED_DECL, "module": "Fixture.Basic", "path": "Fixture/Basic.lean",
        "line": 1, "relation": "broadMatch", "proof_status": "proved", "rank": 1,
        "confidence": "medium", "challenge": "downgraded",
        "location_key": sha("loc-downgraded"), "pair_key": DOWNGRADED_PAIR, "note": None,
    }
    survived = {
        "library": "mathlib", "repo": REPO, "commit": "0" * 40,
        "declaration": "Fixture.settled", "module": "Fixture.Basic", "path": "Fixture/Basic.lean",
        "line": 9, "relation": "exactMatch", "proof_status": "proved", "rank": 1,
        "confidence": "high", "challenge": "survived",
        "location_key": sha("loc-survived"), "pair_key": pair_key(sha("other"), REPO, "Fixture.settled"),
        "note": None,
    }
    # A second library on the same passage, so the article's own records vouch
    # for a declaration in lean-core as well as in mathlib.
    core = {
        "library": "lean-core", "repo": CORE_REPO, "commit": "1" * 40,
        "declaration": "FixtureCore.natSucc", "module": "FixtureCore.Prelude",
        "path": "src/Init/Prelude.lean", "line": 3, "relation": "broadMatch",
        "proof_status": "proved", "rank": 2, "confidence": "medium", "challenge": "survived",
        "location_key": sha("loc-core"),
        "pair_key": pair_key(sha("passage-settled"), CORE_REPO, "FixtureCore.natSucc"), "note": None,
    }
    receipt = {"sha256": sha("receipt"), "queries": 4, "candidates": 3}
    return {
        "schema": "wikilean.articles.article/1",
        "pageid": PAGEID,
        "title": "Fixture monoid",
        "revision": 101,
        "source_id": "fixture",
        "wikidata": None,
        "assessment": {"class": "C", "importance": "high"},
        "campaign": CAMPAIGN,
        "universe": {"id": "u-fixture", "sha256": UNIVERSE_SHA, "frozen": "2026-01-01", "libraries": 2},
        "record_set": sha("record-set"),
        "records_sha256": sha("records"),
        "seal": {"set_id": "fixture-a", "schema": "cf-locate-recordset/2", "status_rule": 3,
                 "created": "2026-01-01T00:00:00Z", "skill_commit": "2" * 40},
        "summary": {"passages": 4, "locations": 2, "related": 0,
                    "by_status": {"formalized": 1, "statement_only": 0, "partial": 0,
                                  "not_formalized": 2, "unresolved": 1},
                    "by_library": {}, "by_proof_status": {}, "by_relation": {}},
        "passages": [
            _passage("settled", sha("passage-settled"), kind="theorem", prominence="central",
                     outcome="located", status="formalized", locations=[survived, core], absence=None),
            _passage("downgraded", PASSAGE_DOWNGRADED, kind="theorem", prominence="central",
                     outcome="located", status="partial", locations=[downgraded], absence=None),
            _passage("absence", PASSAGE_ABSENCE, kind="theorem", prominence="subject",
                     outcome="absent", status="not_formalized", locations=[],
                     absence={"universe": "u-fixture", "universe_sha256": UNIVERSE_SHA, "libraries": 2,
                              "receipt": receipt, "challenge": "survived"}),
            _passage("untagged", PASSAGE_UNTAGGED, kind="definition", prominence="supporting",
                     outcome="absent", status="not_formalized", locations=[],
                     absence={"universe": "u-fixture", "universe_sha256": UNIVERSE_SHA, "libraries": 2,
                              "receipt": receipt, "challenge": "sampled_out"}),
        ],
    }


def second_article() -> dict:
    """Tag work only: one formal passage with no record and no judged absence."""
    out = article()
    out["pageid"] = SECOND_PAGEID
    out["title"] = "Fixture semigroup"
    out["passages"] = [
        _passage("untagged-two", SECOND_PASSAGE, kind="theorem", prominence="subject",
                 outcome="absent", status="not_formalized", locations=[],
                 absence={"universe": "u-fixture", "universe_sha256": UNIVERSE_SHA, "libraries": 2,
                          "receipt": {"sha256": sha("receipt2"), "queries": 2, "candidates": 1},
                          "challenge": "not_required"}),
    ]
    return out


def current() -> dict:
    return {
        "schema": "wikilean.articles.current/1",
        "campaign": "fixture-b1",
        "built_at": "2026-01-01T00:00:00Z",
        "universe": {"id": "u-fixture", "sha256": UNIVERSE_SHA, "frozen": "2026-01-01", "libraries": 2},
        "record_set": {"set_id": "fixture-a", "schema": "cf-locate-recordset/2", "status_rule": 3,
                       "seal_sha256": sha("seal"), "created": "2026-01-01T00:00:00Z", "byte_budget": 0},
        "counts": {"articles": 1, "passages": 4, "locations": 2, "related": 0,
                   "by_status": {"formalized": 1, "statement_only": 0, "partial": 1,
                                 "not_formalized": 2, "unresolved": 0},
                   "by_library": {}, "by_importance": {}},
        "index": {"hex": 1, "shards": ["0"], "entries": 1},
    }


def index_shard(*, second: bool = False) -> dict:
    rows = [["Fixture monoid", PAGEID, 101, 4, 1, 0, 1, 2, 0, "high", ["mathlib"]]]
    if second:
        # More unsettled passages, so article_order puts it first.
        rows.insert(0, ["Fixture semigroup", SECOND_PAGEID, 101, 9, 0, 0, 0, 9, 0, "top", []])
    return {
        "schema": "wikilean.articles.index/1",
        "shard": "0",
        "fields": ["title", "pageid", "revision", "passages", "formalized", "statement_only",
                   "partial", "not_formalized", "unresolved", "importance", "libraries"],
        "articles": rows,
    }


class FakeWikiLean:
    """A loopback stand-in for the site's public and agent routes.

    It records every request it receives (``calls``) and can be told to answer
    the next write with a status and a Retry-After.
    """

    def __init__(self, *, declarations: dict[str, str] | None = None, overlay: list[dict] | None = None,
                 second: bool = False):
        self.calls: list[dict] = []
        # declaration -> module, as GET /decl/<name> answers it.
        self.declarations = dict(KNOWN_DECLS) if declarations is None else dict(declarations)
        self.overlay_rows = overlay or []
        self.second = second
        # The next write only; `write_answer` is what every write after that
        # gets, which is how a permanent refusal is modelled.
        self.next_write: tuple[int, dict, dict] | None = None
        self.write_answer: tuple[int, dict, dict] | None = None
        # When set, every GET /decl/<name> answers with it: the oracle being
        # down, rate limited or paused.
        self.decl_answer: tuple[int, dict, dict] | None = None
        self.role = "agent"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, payload: dict, headers: dict | None = None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body=None):
                outer.calls.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                })

            def do_GET(self):
                self._record()
                path = self.path
                if path == "/api/articles/current":
                    return self._send(200, current())
                if path.startswith("/api/articles/index/"):
                    return self._send(200, index_shard(second=outer.second))
                if path == f"/api/articles/article/{PAGEID}":
                    return self._send(200, article())
                if path == f"/api/articles/article/{SECOND_PAGEID}":
                    return self._send(200, second_article())
                if path.startswith("/api/articles/overlay/"):
                    pageid = int(path.rsplit("/", 1)[1])
                    rows = [row for row in outer.overlay_rows if row.get("pageid", pageid) == pageid]
                    return self._send(200, {"ok": True, "pageid": pageid, "tags": rows})
                if path == f"/api/articles/universe/{UNIVERSE_SHA}":
                    return self._send(200, universe())
                if path == "/api/auth/me":
                    if "authorization" not in {k.lower() for k in self.headers}:
                        return self._send(401, {"user": None})
                    return self._send(200, {"user": {"id": "agent-1", "name": "Fixture (agent)",
                                                     "role": outer.role, "ownerId": "person-1"}})
                if path.startswith("/decl/"):
                    import urllib.parse

                    if outer.decl_answer is not None:
                        return self._send(*outer.decl_answer)
                    name = urllib.parse.unquote(path[len("/decl/"):])
                    if name in outer.declarations:
                        # "library" is the INDEX the name was found in, which
                        # is "mathlib" for a core name too; only "module" says
                        # which library it belongs to.
                        return self._send(200, {"ok": True, "decl": name, "library": "mathlib",
                                                "module": outer.declarations[name]})
                    return self._send(404, {"ok": False, "error": "unknown declaration", "decl": name})
                return self._send(404, {"ok": False, "error": "no such route"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode()) if raw else None
                except ValueError:
                    body = {"_unparsed": raw.decode("utf-8", "replace")}
                self._record(body)
                answer = outer.next_write if outer.next_write is not None else outer.write_answer
                outer.next_write = None
                if answer is not None:
                    return self._send(*answer)
                if self.path.startswith("/api/articles/tag"):
                    return self._send(200, {"ok": True, "pair_key": DOWNGRADED_PAIR, "version": 1,
                                            "state": "live", "status": "statement_only"})
                return self._send(404, {"ok": False, "error": "no such route"})

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def writes(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "POST"]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""A stand-in for Claude Code that calls nothing.

It asserts on its own environment, so a regression that hands the model the
contributor's WikiLean token fails inside the fake rather than going unnoticed.

What it should answer is read from the JSON file named by --control, NOT from
an environment variable: the CLI gives the model an allowlisted environment
that no test variable could survive, which is the property under test.
"""
import json, os, sys

control = {}
argv = sys.argv[1:]
if "--control" in argv:
    with open(argv[argv.index("--control") + 1], encoding="utf-8") as handle:
        control = json.load(handle)
prompt = sys.stdin.read()
if control.get("record"):
    with open(control["record"], "w", encoding="utf-8") as handle:
        json.dump({"argv": argv, "env": dict(os.environ), "prompt": prompt}, handle)

leaked = sorted(k for k in os.environ if k.startswith(("WIKILEAN_", "ANTHROPIC_")))
if leaked:
    print("the model was given " + ",".join(leaked), file=sys.stderr)
    sys.exit(3)
if "wla_" in prompt:
    print("the prompt contains a token", file=sys.stderr)
    sys.exit(4)
if control.get("fail"):
    print("simulated failure", file=sys.stderr)
    sys.exit(1)
reply = control.get("reply", '{"verdict": "unsure"}')
if control.get("raw"):
    sys.stdout.write(reply)
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}))
'''


def write_fake_claude(directory: Path, **control) -> str:
    """Write the fake and return the value for $WIKILEAN_CLAUDE_CMD.

    ``control`` is the fake's whole configuration (``reply``, ``fail``,
    ``raw``, ``record``); :func:`set_control` changes it afterwards.
    """
    path = directory / "fake-claude.py"
    path.write_text(FAKE_CLAUDE, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    set_control(directory, **control)
    return f"{sys.executable} {path} --control {control_path(directory)}"


def control_path(directory: Path) -> Path:
    return directory / "fake-claude-control.json"


def set_control(directory: Path, **control) -> None:
    payload = {"reply": '{"verdict": "unsure"}', "fail": False, "raw": False, "record": None}
    payload.update({key: (str(value) if key == "record" and value else value)
                    for key, value in control.items()})
    control_path(directory).write_text(json.dumps(payload), encoding="utf-8")


def private_home(directory: Path) -> dict[str, str]:
    """Environment overrides that keep a test out of the real home directory."""
    home = directory / "home"
    for child in ("config", "state", "cache"):
        (home / child).mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)
