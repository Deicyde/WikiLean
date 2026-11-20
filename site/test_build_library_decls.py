#!/usr/bin/env python3
"""The multi-library decl fabric builder: doc-gen4 and Verso indexes, HTTP failures, per-library fail-soft."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_library_decls as b

FC_BASE = "https://google-deepmind.github.io/formal-conjectures/src/"
FC = {
    "key": "formal-conjectures", "label": "Formal Conjectures", "docs_base": FC_BASE,
    "declaration_data": FC_BASE + "xref.json", "index_format": "verso-xref",
    "module_roots": ["FormalConjectures", "FormalConjecturesForMathlib"], "aliases": [], "enabled": True,
}
CSLIB = {
    "key": "cslib", "label": "CSLib", "docs_base": "https://api.cslib.io/docs/",
    "declaration_data": "https://api.cslib.io/docs/declarations/declaration-data.bmp",
    "module_roots": ["Cslib"], "aliases": [], "enabled": True,
}


def constant(address: str, module: str, anchor: str, private: bool = False) -> list[dict]:
    return [{"address": address, "data": {"module": module, "private": private, "userName": "x"}, "id": anchor}]


# Entries copied from formal-conjectures' published src/xref.json (2026-11-20), plus two crafted ones.
XREF = {
    "VersoHtml.constant": {"title": "Lean constant", "description": "", "contents": {
        "CollatzConjecture.collatz_conjecture": constant(
            "/FormalConjectures/Wikipedia/CollatzConjecture/", "FormalConjectures.Wikipedia.CollatzConjecture",
            "CollatzConjecture___collatz_conjecture"),
        "Green100.IsSofic": constant(
            "/FormalConjectures/GreensOpenProblems/«100»/", "FormalConjectures.GreensOpenProblems.«100»",
            "Green100___IsSofic"),
        "AdditiveCombinatorics.sumConv": constant(
            "/FormalConjecturesForMathlib/Combinatorics/Additive/Convolution/",
            "FormalConjecturesForMathlib.Combinatorics.Additive.Convolution", "AdditiveCombinatorics___sumConv"),
        # Not one of the registry's roots: "FormalConjectures" must match on a dot boundary.
        "AMS": constant("/FormalConjecturesUtil/Attributes/AMS/", "FormalConjecturesUtil.Attributes.AMS", "AMS"),
        "_private.0.Alphabet": constant(
            "/FormalConjecturesForMathlib/Computability/TuringMachine/Notation/",
            "FormalConjecturesForMathlib.Computability.TuringMachine.Notation", "_private___0___Alphabet", private=True),
        "Crafted.climbs": constant("/FormalConjectures/../../x/", "FormalConjectures.X", "Crafted___climbs"),
        "Crafted.host": constant("//evil.example/FormalConjectures/Y/", "FormalConjectures.Y", "Crafted___host"),
    }},
    "VersoHtml.module": {"title": "Lean module", "description": "", "contents": {}},
}
DOCGEN = {"declarations": {
    "Cslib.Automata.DA": {"docLink": "./Cslib/Automata/DA/Basic.html#Cslib.Automata.DA"},
    "Nat.add": {"docLink": "./Init/Prelude.html#Nat.add"},
}}
NOT_FOUND = "curl: (22) The requested URL returned error: 404\n"


def quietly(fn, *args):
    """fn(*args) with its stdout and stderr captured; returns (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args)
    return result, out.getvalue(), err.getvalue()


def serving(documents: dict[str, object]):
    """A fetch that serves documents by URL and answers 404 for anything else."""
    def fetch(url: str) -> object:
        if url not in documents:
            raise b.FetchError(NOT_FOUND.strip())
        return documents[url]
    return fetch


class VersoIndex(unittest.TestCase):
    def setUp(self):
        self.own, self.hrefs = b.verso_decls(XREF, FC["module_roots"])

    def test_keeps_the_librarys_own_public_constants(self):
        self.assertEqual(self.own, {
            "CollatzConjecture.collatz_conjecture": "FormalConjectures.Wikipedia.CollatzConjecture",
            "Green100.IsSofic": "FormalConjectures.GreensOpenProblems.«100»",
            "AdditiveCombinatorics.sumConv": "FormalConjecturesForMathlib.Combinatorics.Additive.Convolution",
            "Crafted.host": "FormalConjectures.Y",
        })
        self.assertEqual(set(self.hrefs), set(self.own))

    def test_href_is_the_page_and_anchor_percent_encoded(self):
        self.assertEqual(self.hrefs["CollatzConjecture.collatz_conjecture"],
                         "FormalConjectures/Wikipedia/CollatzConjecture/#CollatzConjecture___collatz_conjecture")
        self.assertEqual(self.hrefs["Green100.IsSofic"],
                         "FormalConjectures/GreensOpenProblems/%C2%AB100%C2%BB/#Green100___IsSofic")

    def test_a_link_never_leaves_docs_base(self):
        for name, href in self.hrefs.items():
            url = FC_BASE + href
            self.assertEqual(urlsplit(url).netloc, "google-deepmind.github.io", name)
            self.assertTrue(urlsplit(url).path.startswith("/formal-conjectures/src/"), name)
        self.assertNotIn("Crafted.climbs", self.hrefs)


class DocGenIndex(unittest.TestCase):
    def test_filters_to_module_roots_as_before(self):
        self.assertEqual(b.docgen_decls(DOCGEN, ["Cslib"]), {"Cslib.Automata.DA": "Cslib.Automata.DA.Basic"})


class Fetch(unittest.TestCase):
    def run_curl(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        done = subprocess.CompletedProcess(["curl"], returncode, stdout, stderr)
        with mock.patch.object(b.subprocess, "run", return_value=done) as run:
            try:
                return b.fetch_json("https://example.test/x.json"), run
            except b.FetchError as e:
                return e, run

    def test_an_http_error_is_reported_as_one(self):
        error, run = self.run_curl(22, stderr=NOT_FOUND.encode())
        self.assertIsInstance(error, b.FetchError)
        self.assertIn("404", str(error))
        self.assertIn("-fsS", run.call_args.args[0])

    def test_an_html_page_is_not_json(self):
        error, _ = self.run_curl(0, stdout=b"<!DOCTYPE html><title>Page not found</title>")
        self.assertIsInstance(error, b.FetchError)
        self.assertIn("not JSON", str(error))

    def test_utf8_json_is_read_from_bytes(self):
        value, _ = self.run_curl(0, stdout=json.dumps({"n": "«100»"}, ensure_ascii=False).encode())
        self.assertEqual(value, {"n": "«100»"})

    def test_a_failed_fetch_names_its_reason(self):
        got, _, err = quietly(b.fetch_decls, FC, serving({}))
        self.assertIsNone(got)
        self.assertIn("formal-conjectures: FETCH FAILED (curl: (22) The requested URL returned error: 404)", err)

    def test_an_unknown_format_fails_soft(self):
        got, _, err = quietly(b.fetch_decls, {**FC, "index_format": "sphinx"}, serving({FC["declaration_data"]: XREF}))
        self.assertIsNone(got)
        self.assertIn("unknown index_format 'sphinx'", err)


class Blob(unittest.TestCase):
    def test_carries_hrefs_only_for_a_verso_library(self):
        fetch = serving({FC["declaration_data"]: XREF, CSLIB["declaration_data"]: DOCGEN})
        blob, out, _ = quietly(b.build_blob, [CSLIB, FC], {}, fetch)
        self.assertEqual(blob["decls"]["cslib"], {"Cslib.Automata.DA": "Cslib.Automata.DA.Basic"})
        self.assertEqual(len(blob["decls"]["formal-conjectures"]), 4)
        self.assertEqual(set(blob["hrefs"]), {"formal-conjectures"})
        self.assertEqual(blob["libraries"]["formal-conjectures"]["docs_base"], FC_BASE)
        self.assertEqual(blob["libraries"]["formal-conjectures"]["index_format"], "verso-xref")
        self.assertIn("formal-conjectures: 4 own decls", out)

    def test_a_failure_keeps_last_good_from_the_same_format(self):
        fetch = serving({FC["declaration_data"]: XREF, CSLIB["declaration_data"]: DOCGEN})
        good, _, _ = quietly(b.build_blob, [CSLIB, FC], {}, fetch)
        kept, out, _ = quietly(b.build_blob, [CSLIB, FC], json.loads(json.dumps(good)), serving({}))
        self.assertEqual(kept["decls"], good["decls"])
        self.assertEqual(kept["hrefs"], good["hrefs"])
        self.assertIn("formal-conjectures: keeping 4 decls from the previous blob", out)

    def test_a_failure_drops_entries_read_from_another_format(self):
        # The blob in KV since 2026-07: no index_format, FC read from doc-gen4, no hrefs.
        prev = {
            "libraries": {"cslib": {"label": "CSLib", "docs_base": CSLIB["docs_base"], "aliases": []},
                          "formal-conjectures": {"label": "Formal Conjectures", "aliases": [],
                                                 "docs_base": "https://google-deepmind.github.io/formal-conjectures/doc/"}},
            "decls": {"cslib": {"Cslib.Automata.DA": "Cslib.Automata.DA.Basic"},
                      "formal-conjectures": {"Erdos1.erdos_1": "FormalConjectures.ErdosProblems.«1»"}},
        }
        blob, out, _ = quietly(b.build_blob, [CSLIB, FC], prev, serving({}))
        self.assertEqual(blob["decls"]["cslib"], prev["decls"]["cslib"])
        self.assertEqual(blob["decls"]["formal-conjectures"], {})
        self.assertEqual(blob["hrefs"], {})
        self.assertIn("formal-conjectures: keeping 0 decls from the previous blob", out)


class Registry(unittest.TestCase):
    def test_every_enabled_library_names_a_known_index_under_its_docs(self):
        libraries = json.loads(b.REGISTRY.read_text(encoding="utf-8"))["libraries"]
        enabled = [lib for lib in libraries if lib.get("enabled")]
        self.assertTrue(enabled)
        for lib in enabled:
            self.assertIn(lib.get("index_format", b.DOCGEN), b.FORMATS, lib["key"])
            self.assertTrue(lib["docs_base"].endswith("/"), lib["key"])
            self.assertTrue(lib["declaration_data"].startswith(lib["docs_base"]), lib["key"])
            self.assertTrue(lib["module_roots"], lib["key"])

    def test_formal_conjectures_reads_its_verso_site(self):
        fc = next(lib for lib in json.loads(b.REGISTRY.read_text(encoding="utf-8"))["libraries"]
                  if lib["key"] == "formal-conjectures")
        self.assertEqual(fc["index_format"], "verso-xref")
        self.assertEqual(fc["declaration_data"], fc["docs_base"] + "xref.json")


if __name__ == "__main__":
    unittest.main()
