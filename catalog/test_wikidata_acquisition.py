#!/usr/bin/env python3
"""Hermetic tests for all-or-nothing Wikidata acquisition artifacts."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "catalog" / "mathlib_deps"))
sys.path.insert(0, str(ROOT / "brain" / "ingest"))

import fetch_wikidata_edges as edges  # noqa: E402
import fetch_wikidata_universe as universe  # noqa: E402
import wikidata_descriptions as descriptions  # noqa: E402
import wikidata_publish as publish  # noqa: E402


def wdqs_bindings(*bindings: dict) -> dict:
    return {"results": {"bindings": list(bindings)}}


def universe_binding(
    qid: str,
    label: str | None = None,
    slug: str | None = None,
) -> dict:
    binding = {
        "x": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "xLabel": {"value": label or qid},
    }
    if slug is not None:
        binding["article"] = {
            "value": f"https://en.wikipedia.org/wiki/{slug}",
        }
    return binding


def edge_binding(
    subject: str,
    predicate: str,
    obj: str,
    label: str = "",
) -> dict:
    return {
        "s": {"value": f"http://www.wikidata.org/entity/{subject}"},
        "p": {"value": f"http://www.wikidata.org/entity/{predicate}"},
        "pLabel": {"value": label},
        "o": {"value": f"http://www.wikidata.org/entity/{obj}"},
    }


def prior_descriptions(qids: int, values: dict[str, str]) -> bytes:
    return publish.canonical_json_bytes({
        "_meta": {
            "source": "test",
            "n_qids": qids,
            "n_descriptions": len(values),
        },
        "descriptions": values,
    })


def description_entity(qid: str, value: str | None = None) -> dict:
    entity = {"id": qid, "type": "item", "descriptions": {}}
    if value is not None:
        entity["descriptions"]["en"] = {"language": "en", "value": value}
    return entity


def redirected_description_entity(
    requested_qid: str,
    target_qid: str,
    value: str | None = None,
) -> dict:
    entity = description_entity(target_qid, value)
    entity["redirects"] = {"from": requested_qid, "to": target_qid}
    return entity


def missing_entity() -> dict:
    return {"missing": ""}


class ForceDisabledTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.force_patch = mock.patch.dict(
            os.environ,
            {publish.FORCE_ENV: "0"},
        )
        self.force_patch.start()
        self.addCleanup(self.force_patch.stop)


class AtomicPublicationTest(ForceDisabledTestCase):
    def test_replace_failure_preserves_prior_bytes_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "artifact.json"
            prior = b"prior bytes\n"
            output.write_bytes(prior)
            with mock.patch.object(
                publish.os,
                "replace",
                side_effect=OSError("injected replacement failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replacement failure"):
                    publish.atomic_write_bytes(output, b"replacement\n")
            self.assertEqual(output.read_bytes(), prior)
            self.assertEqual(list(root.glob(".artifact.json.*.tmp")), [])


class WikidataUniverseTest(ForceDisabledTestCase):
    def test_malformed_prior_fails_before_network_and_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            prior = b"{malformed prior\n"
            output.write_bytes(prior)
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(universe, "CLASSES", {"Q10": "first"}),
                mock.patch.object(universe, "query") as query,
            ):
                with self.assertRaisesRegex(RuntimeError, "malformed prior"):
                    universe.main()
            query.assert_not_called()
            self.assertEqual(output.read_bytes(), prior)

    def test_exhausted_request_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {
                    "qid": "Q1",
                    "label": "one",
                    "classes": ["Q10"],
                    "enwiki_slug": None,
                },
                {
                    "qid": "Q2",
                    "label": "two",
                    "classes": ["Q20"],
                    "enwiki_slug": None,
                },
            ])
            output.write_bytes(prior)
            first = wdqs_bindings(universe_binding("Q1", "one"))
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q10": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "RETRIES", 3),
                mock.patch.object(universe, "RETRY_DELAY", 0),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(
                    universe,
                    "query",
                    side_effect=[first, OSError("offline"), OSError("offline"),
                                 OSError("offline")],
                ) as query,
                mock.patch.object(universe.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    universe.main()
            self.assertEqual(query.call_count, 4)
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_but_collapsed_class_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {
                    "qid": "Q1",
                    "label": "one",
                    "classes": ["Q10"],
                    "enwiki_slug": None,
                },
                {
                    "qid": "Q2",
                    "label": "two",
                    "classes": ["Q10"],
                    "enwiki_slug": None,
                },
                {
                    "qid": "Q3",
                    "label": "three",
                    "classes": ["Q20"],
                    "enwiki_slug": None,
                },
            ])
            output.write_bytes(prior)
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q10": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(universe.time, "sleep"),
                mock.patch.object(
                    universe,
                    "query",
                    side_effect=[
                        wdqs_bindings(universe_binding("Q1", "one")),
                        wdqs_bindings(
                            universe_binding("Q3", "three"),
                            universe_binding("Q4", "four"),
                        ),
                    ],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "sanity floor 2"):
                    universe.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_but_unlabeled_response_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {
                    "qid": "Q1", "label": "one", "classes": ["Q10"],
                    "enwiki_slug": "One",
                },
                {
                    "qid": "Q2", "label": "two", "classes": ["Q20"],
                    "enwiki_slug": "Two",
                },
            ])
            output.write_bytes(prior)
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q10": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(universe.time, "sleep"),
                mock.patch.object(
                    universe,
                    "query",
                    side_effect=[
                        wdqs_bindings(universe_binding("Q1", slug="One")),
                        wdqs_bindings(universe_binding("Q2", slug="Two")),
                    ],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-QID labels"):
                    universe.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_but_slugless_response_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {
                    "qid": "Q1", "label": "one", "classes": ["Q10"],
                    "enwiki_slug": "One",
                },
                {
                    "qid": "Q2", "label": "two", "classes": ["Q20"],
                    "enwiki_slug": "Two",
                },
            ])
            output.write_bytes(prior)
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q10": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(universe.time, "sleep"),
                mock.patch.object(
                    universe,
                    "query",
                    side_effect=[
                        wdqs_bindings(universe_binding("Q1", "one")),
                        wdqs_bindings(universe_binding("Q2", "two")),
                    ],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "enwiki slugs"):
                    universe.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_success_is_canonical_and_response_order_independent(self) -> None:
        first_class_a = wdqs_bindings(
            universe_binding("Q10", "zeta", "Zeta"),
            universe_binding("Q2", "two", "Two"),
            universe_binding("Q10", "alpha", "Alpha"),
        )
        second_class_a = wdqs_bindings(
            universe_binding("Q10", "zeta", "Zeta"),
        )
        first_class_b = wdqs_bindings(
            universe_binding("Q10", "alpha", "Alpha"),
            universe_binding("Q10", "zeta", "Zeta"),
            universe_binding("Q2", "two", "Two"),
        )
        second_class_b = wdqs_bindings(
            universe_binding("Q10", "zeta", "Zeta"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "universe.jsonl"
            common_patches = (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q100": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(universe.time, "sleep"),
            )
            with common_patches[0], common_patches[1], common_patches[2], \
                    common_patches[3], mock.patch.object(
                        universe, "query", side_effect=[first_class_a, second_class_a]
                    ):
                self.assertEqual(universe.main(), 0)
            first_bytes = output.read_bytes()
            with (
                mock.patch.object(universe, "OUT", output),
                mock.patch.object(
                    universe,
                    "CLASSES",
                    {"Q100": "first", "Q20": "second"},
                ),
                mock.patch.object(universe, "PAUSE", 0),
                mock.patch.object(universe.time, "sleep"),
                mock.patch.object(
                    universe,
                    "query",
                    side_effect=[first_class_b, second_class_b],
                ),
            ):
                self.assertEqual(universe.main(), 0)
            self.assertEqual(output.read_bytes(), first_bytes)
            self.assertEqual(
                [json.loads(line) for line in first_bytes.splitlines()],
                [
                    {
                        "qid": "Q2",
                        "label": "two",
                        "classes": ["Q100"],
                        "enwiki_slug": "Two",
                    },
                    {
                        "qid": "Q10",
                        "label": "alpha",
                        "classes": ["Q20", "Q100"],
                        "enwiki_slug": "Alpha",
                    },
                ],
            )


class WikidataEdgesTest(ForceDisabledTestCase):
    def _inputs(self, root: Path, qids: tuple[str, ...]) -> tuple[Path, Path]:
        concept = root / "concept.jsonl"
        concept.write_text(
            "".join(json.dumps({"qid": qid}) + "\n" for qid in reversed(qids)),
            encoding="utf-8",
        )
        brain_nodes = root / "nodes.jsonl"
        brain_nodes.write_text("", encoding="utf-8")
        return concept, brain_nodes

    def test_malformed_prior_fails_before_network_and_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept, brain_nodes = self._inputs(root, ("Q1", "Q2"))
            output = root / "edges.jsonl"
            prior = b"{malformed prior\n"
            output.write_bytes(prior)
            with (
                mock.patch.object(edges, "CONCEPT", concept),
                mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                mock.patch.object(edges, "OUT", output),
                mock.patch.object(edges, "sparql_query") as query,
            ):
                with self.assertRaisesRegex(RuntimeError, "malformed prior"):
                    edges.main()
            query.assert_not_called()
            self.assertEqual(output.read_bytes(), prior)

    def test_exhausted_batch_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept, brain_nodes = self._inputs(root, ("Q1", "Q2"))
            output = root / "edges.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {"s": "Q1", "p": "P1", "p_label": "relates", "o": "Q2"},
            ])
            output.write_bytes(prior)
            first = wdqs_bindings(edge_binding("Q1", "P1", "Q2", "relates"))
            with (
                mock.patch.object(edges, "CONCEPT", concept),
                mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                mock.patch.object(edges, "OUT", output),
                mock.patch.object(edges, "BATCH", 1),
                mock.patch.object(edges, "PAUSE", 0),
                mock.patch.object(
                    edges,
                    "sparql_query",
                    side_effect=[first, OSError("offline"), OSError("offline"),
                                 OSError("offline")],
                ) as query,
                mock.patch.object(edges.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    edges.main()
            self.assertEqual(query.call_count, 4)
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_empty_response_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept, brain_nodes = self._inputs(root, ("Q1", "Q2"))
            output = root / "edges.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {"s": "Q1", "p": "P1", "p_label": "relates", "o": "Q2"},
            ])
            output.write_bytes(prior)
            with (
                mock.patch.object(edges, "CONCEPT", concept),
                mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                mock.patch.object(edges, "OUT", output),
                mock.patch.object(edges, "PAUSE", 0),
                mock.patch.object(edges, "sparql_query", return_value=wdqs_bindings()),
                mock.patch.object(edges.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sanity floor 1"):
                    edges.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_pid_fallback_labels_preserve_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept, brain_nodes = self._inputs(root, ("Q1", "Q2"))
            output = root / "edges.jsonl"
            prior = publish.canonical_jsonl_bytes([
                {"s": "Q1", "p": "P1", "p_label": "relates", "o": "Q2"},
            ])
            output.write_bytes(prior)
            with (
                mock.patch.object(edges, "CONCEPT", concept),
                mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                mock.patch.object(edges, "OUT", output),
                mock.patch.object(edges, "PAUSE", 0),
                mock.patch.object(
                    edges,
                    "sparql_query",
                    return_value=wdqs_bindings(
                        edge_binding("Q1", "P1", "Q2", "P1")
                    ),
                ),
                mock.patch.object(edges.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "predicate labels"):
                    edges.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_first_run_large_empty_response_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qids = tuple(f"Q{index}" for index in range(1, 51))
            concept, brain_nodes = self._inputs(root, qids)
            output = root / "edges.jsonl"
            with (
                mock.patch.object(edges, "CONCEPT", concept),
                mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                mock.patch.object(edges, "OUT", output),
                mock.patch.object(edges, "PAUSE", 0),
                mock.patch.object(edges, "sparql_query", return_value=wdqs_bindings()),
                mock.patch.object(edges.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sanity floor 25"):
                    edges.main()
            self.assertFalse(output.exists())

    def test_complete_success_is_canonical_and_response_order_independent(self) -> None:
        rows_a = wdqs_bindings(
            edge_binding("Q2", "P10", "Q1", "zeta"),
            edge_binding("Q1", "P2", "Q2", "forward"),
            edge_binding("Q2", "P10", "Q1", "alpha"),
            edge_binding("Q2", "P10", "Q1", "P10"),
            edge_binding("Q1", "P99", "Q999", "outside"),
        )
        rows_b = wdqs_bindings(*reversed(rows_a["results"]["bindings"]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concept, brain_nodes = self._inputs(root, ("Q1", "Q2"))
            output = root / "edges.jsonl"
            for response in (rows_a, rows_b):
                with (
                    mock.patch.object(edges, "CONCEPT", concept),
                    mock.patch.object(edges, "BRAIN_NODES", brain_nodes),
                    mock.patch.object(edges, "OUT", output),
                    mock.patch.object(edges, "PAUSE", 0),
                    mock.patch.object(edges, "sparql_query", return_value=response),
                    mock.patch.object(edges.time, "sleep"),
                ):
                    self.assertEqual(edges.main(), 0)
                if response is rows_a:
                    first_bytes = output.read_bytes()
            self.assertEqual(output.read_bytes(), first_bytes)
            self.assertEqual(
                [json.loads(line) for line in first_bytes.splitlines()],
                [
                    {"s": "Q1", "p": "P2", "p_label": "forward", "o": "Q2"},
                    {"s": "Q2", "p": "P10", "p_label": "alpha", "o": "Q1"},
                ],
            )


class WikidataDescriptionsTest(ForceDisabledTestCase):
    def _inputs(
        self,
        root: Path,
        *,
        grounding: tuple[str, ...] = (),
        universe: tuple[str, ...] = (),
        crossrefs: tuple[str, ...] = (),
    ) -> tuple[Path, Path, Path]:
        grounding_path = root / "grounding.json"
        grounding_path.write_text(
            json.dumps([{"qid": qid} for qid in grounding]),
            encoding="utf-8",
        )
        universe_path = root / "universe.jsonl"
        universe_path.write_text(
            "".join(
                json.dumps({"qid": qid, "description": "stale seed"}) + "\n"
                for qid in universe
            ),
            encoding="utf-8",
        )
        crossrefs_path = root / "crossrefs.json"
        crossrefs_path.write_text(
            json.dumps({"xrefs": {qid: {} for qid in crossrefs}}),
            encoding="utf-8",
        )
        return grounding_path, universe_path, crossrefs_path

    def _patch_inputs(
        self,
        grounding: Path,
        universe_path: Path,
        crossrefs: Path,
        output: Path,
    ):
        return (
            mock.patch.object(descriptions, "GROUNDING", grounding),
            mock.patch.object(descriptions, "UNIVERSE_EXT", universe_path),
            mock.patch.object(descriptions, "CROSSREFS", crossrefs),
            mock.patch.object(descriptions, "OUT", output),
            mock.patch.object(descriptions, "DELAY", 0),
            mock.patch.object(descriptions, "RETRY_DELAY", 0),
            mock.patch.object(descriptions.time, "sleep"),
        )

    def test_malformed_prior_fails_before_network_and_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1",)
            )
            output = root / "descriptions.json"
            prior = b"{malformed prior\n"
            output.write_bytes(prior)
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common, "curl_fetch"
                    ) as fetch:
                with self.assertRaisesRegex(RuntimeError, "malformed prior"):
                    descriptions.main()
            fetch.assert_not_called()
            self.assertEqual(output.read_bytes(), prior)

    def test_exhausted_batch_preserves_prior_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(2, {"Q1": "one", "Q2": "two"})
            output.write_bytes(prior)
            success = json.dumps({
                "entities": {
                    "Q1": description_entity("Q1", "one"),
                }
            }).encode()
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions, "BATCH", 1
                    ), mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        side_effect=[success, OSError("offline"), OSError("offline"),
                                     OSError("offline")],
                    ) as fetch:
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    descriptions.main()
            self.assertEqual(fetch.call_count, 4)
            self.assertEqual(output.read_bytes(), prior)

    def test_collapsed_input_qid_population_fails_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1",)
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(2, {"Q1": "one", "Q2": "two"})
            output.write_bytes(prior)
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common, "curl_fetch"
                    ) as fetch:
                with self.assertRaisesRegex(
                    RuntimeError, "input QID population.*sanity floor 2"
                ):
                    descriptions.main()
            fetch.assert_not_called()
            self.assertEqual(output.read_bytes(), prior)

    def test_complete_snapshot_ignores_prior_and_seed_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root,
                grounding=("Q2",),
                universe=("Q1",),
                crossrefs=("Q3",),
            )
            output = root / "descriptions.json"
            output.write_bytes(prior_descriptions(3, {"Q1": "stale previous"}))
            response_a = {
                "entities": {
                    "Q3": description_entity("Q3"),
                    "Q1": description_entity("Q1", "fresh one"),
                    "Q2": description_entity("Q2"),
                }
            }
            response_b = {
                "entities": dict(reversed(list(response_a["entities"].items())))
            }
            observed: list[bytes] = []
            for response in (response_a, response_b):
                patches = self._patch_inputs(
                    grounding, universe_path, crossrefs, output
                )
                with patches[0], patches[1], patches[2], patches[3], patches[4], \
                        patches[5], patches[6], mock.patch.object(
                            descriptions.common,
                            "curl_fetch",
                            return_value=json.dumps(response).encode(),
                        ):
                    self.assertEqual(descriptions.main(), 0)
                observed.append(output.read_bytes())
            self.assertEqual(observed[0], observed[1])
            payload = json.loads(observed[0])
            self.assertEqual(payload["descriptions"], {"Q1": "fresh one"})
            self.assertEqual(payload["_meta"]["n_qids"], 3)
            self.assertEqual(payload["_meta"]["n_descriptions"], 1)
            self.assertNotIn("fetched_at", payload["_meta"])
            self.assertNotIn("n_fetched_this_run", payload["_meta"])

    def test_valid_redirect_stores_target_description_under_requested_qid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1",)
            )
            output = root / "descriptions.json"
            response = {
                "entities": {
                    "Q1": redirected_description_entity(
                        "Q1", "Q2", "redirect target description"
                    ),
                }
            }
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ):
                self.assertEqual(descriptions.main(), 0)
            self.assertEqual(
                json.loads(output.read_bytes())["descriptions"],
                {"Q1": "redirect target description"},
            )

    def test_conflicting_redirect_identity_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1",)
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(1, {"Q1": "one"})
            output.write_bytes(prior)
            entity = redirected_description_entity("Q1", "Q2", "two")
            entity["redirects"]["to"] = "Q3"
            response = {"entities": {"Q1": entity}}
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    descriptions.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_explicit_override_allows_legitimate_all_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            output.write_bytes(prior_descriptions(2, {"Q1": "one", "Q2": "two"}))
            response = {
                "entities": {
                    "Q1": description_entity("Q1"),
                    "Q2": missing_entity(),
                }
            }
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ), mock.patch.dict(
                        os.environ,
                        {publish.FORCE_ENV: "1"},
                    ):
                self.assertEqual(descriptions.main(), 0)
            self.assertEqual(json.loads(output.read_bytes())["descriptions"], {})

    def test_small_first_run_all_missing_snapshot_is_legitimate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            response = {
                "entities": {
                    "Q1": description_entity("Q1"),
                    "Q2": missing_entity(),
                }
            }
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ):
                self.assertEqual(descriptions.main(), 0)
            self.assertEqual(json.loads(output.read_bytes())["descriptions"], {})

    def test_first_run_large_all_missing_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qids = tuple(f"Q{index}" for index in range(1, 51))
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=qids
            )
            output = root / "descriptions.json"
            response = {
                "entities": {
                    qid: description_entity(qid)
                    for qid in qids
                }
            }
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ):
                with self.assertRaisesRegex(RuntimeError, "sanity floor 50"):
                    descriptions.main()
            self.assertFalse(output.exists())

    def test_complete_all_missing_response_preserves_prior_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(2, {"Q1": "one", "Q2": "two"})
            output.write_bytes(prior)
            response = {
                "entities": {
                    "Q1": description_entity("Q1"),
                    "Q2": missing_entity(),
                }
            }
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=json.dumps(response).encode(),
                    ):
                with self.assertRaisesRegex(RuntimeError, "sanity floor 2"):
                    descriptions.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_incomplete_success_response_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(2, {"Q1": "one", "Q2": "two"})
            output.write_bytes(prior)
            incomplete = json.dumps({
                "entities": {"Q1": description_entity("Q1")}
            }).encode()
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=incomplete,
                    ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    descriptions.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_exact_keys_with_structurally_empty_entities_are_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1", "Q2")
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(2, {"Q1": "one", "Q2": "two"})
            output.write_bytes(prior)
            fake = json.dumps({"entities": {"Q1": {}, "Q2": {}}}).encode()
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=fake,
                    ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    descriptions.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_error_envelope_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(
                root, grounding=("Q1",)
            )
            output = root / "descriptions.json"
            prior = prior_descriptions(1, {"Q1": "one"})
            output.write_bytes(prior)
            error = json.dumps({"error": {"code": "badrequest"}}).encode()
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common,
                        "curl_fetch",
                        return_value=error,
                    ):
                with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                    descriptions.main()
            self.assertEqual(output.read_bytes(), prior)

    def test_empty_qid_set_preserves_prior_artifact_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            grounding, universe_path, crossrefs = self._inputs(root)
            output = root / "descriptions.json"
            prior = prior_descriptions(1, {"Q1": "one"})
            output.write_bytes(prior)
            patches = self._patch_inputs(
                grounding, universe_path, crossrefs, output
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], mock.patch.object(
                        descriptions.common, "curl_fetch"
                    ) as fetch:
                with self.assertRaisesRegex(RuntimeError, "empty QID set"):
                    descriptions.main()
            fetch.assert_not_called()
            self.assertEqual(output.read_bytes(), prior)


if __name__ == "__main__":
    unittest.main()
