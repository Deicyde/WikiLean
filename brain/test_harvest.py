#!/usr/bin/env python3
"""Unit tests for harvest_community_edges.validate_edge / harvest (phase 4).

Exercises the graduation gate WITHOUT D1 or the oracle: endpoint validation,
kind + xref whitelist, provenance stamping, and the human-vs-AI trust split
(AI edges route through the oracle, which is monkeypatched here). Run:

    python3 brain/test_harvest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest_community_edges as H  # noqa: E402

NODES = {"Q181296", "decl:Mathlib:CommGroup", "path:Mathlib/Algebra"}
PIN = "2026-07-05"


def row(**kw):
    base = {"id": "aaaaaaaaaaaa", "src": "Q181296", "dst": "decl:Mathlib:CommGroup",
            "kind": "formalizes", "evidence": '{"note":"n"}', "added_by": "jack",
            "actor_type": "human", "created_at": 1}
    base.update(kw)
    return base


def one(**kw):
    return H.validate_edge(row(**kw), NODES, PIN)


def test_human_edge_graduates():
    edge, reason = one()
    assert edge is not None and reason == "", reason
    assert edge["provenance"]["source"] == "community"
    assert edge["provenance"]["method"] == "community-human (brain_edges)"
    assert edge["confidence"] == "high"
    assert edge["evidence"]["added_by"] == "jack"
    assert edge["evidence"]["edge_id"] == "aaaaaaaaaaaa"


def test_bad_kind_dropped():
    assert one(kind="depends")[0] is None
    assert one(kind="contains")[0] is None


def test_unknown_endpoints_dropped():
    assert one(src="Q999999")[0] is None
    assert one(dst="Q999999")[0] is None  # non-xref dst must be a node


def test_xref_graduates_and_validates_db():
    edge, _ = one(kind="xref", dst="xref:lmfdb_knowl:group.abelian",
                  evidence='{"note":"same","db":"lmfdb_knowl","value":"group.abelian"}')
    assert edge is not None
    assert edge["evidence"]["db"] == "lmfdb_knowl"
    assert one(kind="xref", dst="xref:notadb:x", evidence='{"note":"n"}')[0] is None
    assert one(kind="xref", dst="Q181296", evidence='{"note":"n"}')[0] is None  # malformed xref dst


def test_ai_edge_routes_through_oracle(monkeypatch=None):
    orig = H._ai_endpoint_ok
    try:
        H._ai_endpoint_ok = lambda nid: True  # oracle passes
        edge, _ = one(actor_type="ai")
        assert edge is not None and edge["confidence"] == "medium"
        assert edge["provenance"]["method"] == "community-ai (brain_edges)"
        H._ai_endpoint_ok = lambda nid: False  # oracle fails → dropped
        assert one(actor_type="ai")[0] is None
    finally:
        H._ai_endpoint_ok = orig


def test_harvest_summary():
    rows = [row(), row(kind="depends"), row(src="Q0"),
            row(kind="xref", dst="xref:lmfdb_knowl:g", evidence='{"note":"n"}')]
    kept, dropped = H.harvest(rows, NODES, PIN)
    assert len(kept) == 2, len(kept)                 # the human formalizes + the xref
    assert dropped.get("bad kind: depends") == 1
    assert dropped.get("src not a known node") == 1


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
