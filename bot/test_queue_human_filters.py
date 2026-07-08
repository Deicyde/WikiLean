#!/usr/bin/env python3
"""Regression tests for human-only Brain queue defaults."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "bot"
if str(BOT) not in sys.path:
    sys.path.insert(0, str(BOT))

import brain_queue  # noqa: E402
import lmfdb_queue  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


class HumanQueueFilterTests(unittest.TestCase):
    def test_lmfdb_queue_defaults_to_mathlib_source_tags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes = root / "nodes.jsonl"
            edges = root / "edges.jsonl"
            tag_xrefs = root / "mathlib_tag_xrefs.jsonl"
            write_jsonl(
                nodes,
                [
                    {"id": "Q1", "label": "Human concept"},
                    {"id": "Q2", "label": "AI concept"},
                    {"id": "decl:Mathlib:HumanDecl", "module": "Mathlib.Test.Human"},
                    {"id": "decl:Mathlib:AiDecl", "module": "Mathlib.Test.Ai"},
                ],
            )
            write_jsonl(
                edges,
                [
                    {"src": "Q1", "dst": "xref:lmfdb_knowl:human.knowl", "kind": "xref", "confidence": "high"},
                    {"src": "Q2", "dst": "xref:lmfdb_knowl:ai.knowl", "kind": "xref", "confidence": "high"},
                    {
                        "src": "Q1",
                        "dst": "decl:Mathlib:HumanDecl",
                        "kind": "formalizes",
                        "confidence": "high",
                        "provenance": {"method": lmfdb_queue.HUMAN_WIKIDATA_METHOD},
                        "evidence": {"module": "Mathlib.Test.Human", "source_tagged": True},
                    },
                    {
                        "src": "Q2",
                        "dst": "decl:Mathlib:AiDecl",
                        "kind": "formalizes",
                        "confidence": "high",
                        "provenance": {"method": "agent+oracle"},
                        "evidence": {"module": "Mathlib.Test.Ai"},
                    },
                ],
            )
            tag_xrefs.write_text("")
            old_nodes, old_tags = lmfdb_queue.NODES, lmfdb_queue.TAG_XREFS
            old_centrality = lmfdb_queue.CENTRALITY
            try:
                lmfdb_queue.NODES = nodes
                lmfdb_queue.TAG_XREFS = tag_xrefs
                lmfdb_queue.CENTRALITY = root / "missing-centrality.json"

                default = lmfdb_queue.build(source=edges, include_seen=True)
                with_ai = lmfdb_queue.build(source=edges, include_seen=True, include_ai_formalizes=True)
            finally:
                lmfdb_queue.NODES, lmfdb_queue.TAG_XREFS = old_nodes, old_tags
                lmfdb_queue.CENTRALITY = old_centrality

        self.assertEqual([r["id"] for r in default], ["human.knowl"])
        self.assertEqual([r["id"] for r in with_ai], ["human.knowl", "ai.knowl"])
        self.assertEqual(default[0]["provenance_tier"], "wikidata-p12987+mathlib-source-wikidata")

    def test_brain_queue_defaults_to_human_community_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes = root / "nodes.jsonl"
            edges = root / "community_edges.jsonl"
            write_jsonl(
                nodes,
                [
                    {"id": "Q1", "label": "Human concept"},
                    {"id": "Q2", "label": "AI concept"},
                    {"id": "decl:Mathlib:HumanDecl", "module": "Mathlib.Test.Human"},
                    {"id": "decl:Mathlib:AiDecl", "module": "Mathlib.Test.Ai"},
                ],
            )
            write_jsonl(
                edges,
                [
                    {
                        "src": "Q1",
                        "dst": "decl:Mathlib:HumanDecl",
                        "kind": "formalizes",
                        "confidence": "high",
                        "evidence": {"actor_type": "human", "added_by": "jack", "edge_id": "h"},
                    },
                    {
                        "src": "Q2",
                        "dst": "decl:Mathlib:AiDecl",
                        "kind": "formalizes",
                        "confidence": "medium",
                        "evidence": {"actor_type": "ai", "added_by": "pipeline", "edge_id": "a"},
                    },
                ],
            )
            old_nodes, old_centrality = brain_queue.NODES, brain_queue.CENTRALITY
            try:
                brain_queue.NODES = nodes
                brain_queue.CENTRALITY = root / "missing-centrality.json"

                default = brain_queue.build(source=edges, include_seen=True)
                with_ai = brain_queue.build(source=edges, include_seen=True, include_ai=True)
            finally:
                brain_queue.NODES = old_nodes
                brain_queue.CENTRALITY = old_centrality

        self.assertEqual([r["qid"] for r in default], ["Q1"])
        self.assertEqual([r["qid"] for r in with_ai], ["Q1", "Q2"])
        self.assertEqual(default[0]["provenance_tier"], "community-human")


if __name__ == "__main__":
    unittest.main()
