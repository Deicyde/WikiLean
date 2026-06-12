#!/usr/bin/env python3
"""Unit tests for moderate.py's deterministic helpers (the ID-discipline
post-pass, wire-level human preservation, run meta) plus the ladder-stats
return of batch_annotate._preserve_human.

No pytest dependency required:
    python3 site/test_moderate.py                      # plain asserts
    python3 -m pytest site/test_moderate.py            # also works
The batch_annotate tests need claude-agent-sdk (it imports at module level);
they self-skip when it isn't installed — run under catalog/.venv for full
coverage:
    catalog/.venv/bin/python site/test_moderate.py
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import moderate as m  # noqa: E402  (stdlib-only at import time)

try:
    import batch_annotate as ba
except ImportError:
    ba = None

HEX12 = set("0123456789abcdef")


def _is_fresh(aid) -> bool:
    return (isinstance(aid, str) and len(aid) == 12
            and all(c in HEX12 for c in aid))


def ann(**kw) -> dict:
    """Annotation factory with a unique-ish anchor per label."""
    label = kw.get("label", "x")
    base = {
        "kind": "theorem", "label": label,
        "anchor": {"section": "Statement", "snippet": f"snippet {label}"},
        "status": "formalized",
        "mathlib": {"decl": "Foo.bar", "module": "Mathlib.Foo", "match_kind": "exact"},
        "note": "n", "provenance": "ai",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# fresh_id / _anchor_sig
# ---------------------------------------------------------------------------

def test_fresh_id_format():
    used: set[str] = set()
    for _ in range(50):
        nid = m.fresh_id(used)
        assert _is_fresh(nid), nid
        used.add(nid)
    assert len(used) == 50


def test_anchor_sig_lockstep_with_batch_annotate():
    if ba is None:
        raise unittest.SkipTest("claude-agent-sdk not installed")
    samples = [
        ann(label="a"),
        ann(label="b", anchor={"type": "theorem_box", "value": "Theorem 1"}),
        {"anchor": {"section": "Lead", "snippet": "s", "from": "x"}},
        {},  # no anchor at all
    ]
    for s in samples:
        assert m._anchor_sig(s) == ba._anchor_sig(s), s


# ---------------------------------------------------------------------------
# finalize_for_post — ID discipline (contract ID1)
# ---------------------------------------------------------------------------

def test_echoed_id_survives():
    existing = [ann(label="a", id="aabbccddeeff")]
    produced = [ann(label="a", id="aabbccddeeff", note="improved")]
    out, stats = m.finalize_for_post(existing, produced)
    assert out[0]["id"] == "aabbccddeeff"
    assert out[0]["note"] == "improved"  # agent's work stands for non-human
    assert stats["ids_echoed"] == 1 and stats["ids_fresh"] == 0


def test_unknown_id_treated_as_new():
    existing = [ann(label="a", id="aabbccddeeff")]
    produced = [ann(label="b", id="ffffffffffff")]  # id the store never issued
    out, stats = m.finalize_for_post(existing, produced)
    assert out[0]["id"] != "ffffffffffff" and _is_fresh(out[0]["id"])
    assert stats["ids_fresh"] == 1


def test_missing_id_inherits_via_anchor_sig():
    existing = [ann(label="a", id="aabbccddeeff")]
    produced = [ann(label="a", note="agent dropped the id field")]
    out, stats = m.finalize_for_post(existing, produced)
    assert out[0]["id"] == "aabbccddeeff"
    assert stats["ids_inherited"] == 1


def test_missing_id_no_match_gets_fresh():
    existing = [ann(label="a", id="aabbccddeeff")]
    produced = [ann(label="a", id="aabbccddeeff"), ann(label="brand-new")]
    out, stats = m.finalize_for_post(existing, produced)
    assert _is_fresh(out[1]["id"]) and out[1]["id"] != "aabbccddeeff"
    assert stats["ids_echoed"] == 1 and stats["ids_fresh"] == 1


def test_duplicate_echoed_id_second_is_new():
    existing = [ann(label="a", id="aabbccddeeff")]
    produced = [ann(label="a", id="aabbccddeeff"),
                ann(label="b", id="aabbccddeeff")]
    out, _ = m.finalize_for_post(existing, produced)
    assert out[0]["id"] == "aabbccddeeff"
    assert out[1]["id"] != "aabbccddeeff" and _is_fresh(out[1]["id"])


def test_prebackfill_ai_annotation_gets_fresh_id():
    # Pre-ID-backfill store: existing ai annotation has no id → a pass-through
    # mints one (only HUMAN annotations must stay byte-identical).
    existing = [ann(label="a")]
    out, stats = m.finalize_for_post(existing, list(existing))
    assert _is_fresh(out[0]["id"])
    assert stats["ids_fresh"] == 1


# ---------------------------------------------------------------------------
# finalize_for_post — wire-level human preservation (server 422 twin)
# ---------------------------------------------------------------------------

def test_human_posted_byte_identical_despite_agent_flag():
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    produced = [{**h, "moderation_flag": "looks dubious"}]
    out, stats = m.finalize_for_post([h], produced)
    assert out[0] == h  # moderation_flag stripped — deepEqual-safe server-side
    assert stats["human_restored_wire"] == 1


def test_human_without_id_never_gains_one():
    # Contract ID2: id backfill for human annotations is SQL-only — a bot POST
    # adding `id` to a stored human would 422 (deepEqual checks key sets).
    h = ann(label="h", provenance="human")  # no id (pre-backfill)
    out, _ = m.finalize_for_post([h], [dict(h)])
    assert out[0] == h and "id" not in out[0]


def test_dropped_human_reinserted_verbatim():
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    a = ann(label="a", id="bbccddeeff00")
    out, stats = m.finalize_for_post([h, a], [dict(a)])
    assert out[-1] == h
    assert stats["human_reinserted_wire"] == 1


def test_preserve_human_moderation_note_stripped_at_wire():
    # _preserve_human re-inserts dropped humans WITH a moderation_note field —
    # the wire pass must restore the stored original or the server 422s.
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    produced = [{**h, "moderation_note": "re-inserted by moderator (agent omitted it)"}]
    out, stats = m.finalize_for_post([h], produced)
    assert out[0] == h
    assert stats["human_restored_wire"] == 1


def test_tombstone_passthrough_verbatim():
    # status='rejected' tombstones are human vetoes; they ride the same human
    # path (provenance 'human') and must survive byte-identical.
    t = ann(label="veto", id="aabbccddeeff", provenance="human",
            status="rejected")
    out, _ = m.finalize_for_post([t], [])  # agent dropped it entirely
    assert out == [t]


def test_human_matched_by_id_when_anchor_edited():
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    edited = {**h, "anchor": {"section": "Statement", "snippet": "REWRITTEN"}}
    out, stats = m.finalize_for_post([h], [edited])
    assert out[0] == h  # id match wins; anchor restored
    assert stats["human_restored_wire"] == 1


def test_anti_laundering_downgrade():
    # An output annotation claiming provenance 'human' with no stored human
    # twin must not pass through — bot writes can't mint human provenance.
    fake = ann(label="fake", provenance="human")
    out, stats = m.finalize_for_post([], [fake])
    assert out[0]["provenance"] == "ai-moderated"
    assert stats["provenance_downgraded"] == 1


# ---------------------------------------------------------------------------
# build_meta (contract ID3) + token resolution
# ---------------------------------------------------------------------------

def test_build_meta_shape():
    ctx = SimpleNamespace(run_id="deadbeef", mode="review", model="claude-opus-4-7",
                          prompt_sha="abc123abc123", mathlib_sha="1234567",
                          auth="subscription")
    rec = {"tokens": 1000, "cost_usd_equiv": 1.23, "elapsed_s": 9.0,
           "agent1_meta": {"duration_ms": 4000}, "agent2_meta": {"duration_ms": 5000},
           "ladder": {"restored": 1, "reinserted": 2, "downgrades_blocked": 3}}
    meta = m.build_meta(ctx, rec, {"ids_fresh": 1})
    for k in ("run_id", "mode", "model", "prompt_sha", "tokens", "cost_usd_equiv",
              "duration_ms", "mathlib_sha", "auth_mode", "ladder"):
        assert k in meta, k
    assert meta["duration_ms"] == 9000
    assert meta["ladder"]["downgrades_blocked"] == 3
    assert meta["auth_mode"] == "subscription"
    # dry-run path: empty rec still yields the full ID3 shape
    meta2 = m.build_meta(ctx, {}, {})
    assert meta2["tokens"] == 0 and meta2["ladder"] == {
        "restored": 0, "reinserted": 0, "downgrades_blocked": 0}


def test_resolve_token_env_wins():
    old = os.environ.get("WIKILEAN_API_TOKEN")
    os.environ["WIKILEAN_API_TOKEN"] = "tok-from-env"
    try:
        assert m.resolve_token() == "tok-from-env"
    finally:
        if old is None:
            os.environ.pop("WIKILEAN_API_TOKEN", None)
        else:
            os.environ["WIKILEAN_API_TOKEN"] = old


# ---------------------------------------------------------------------------
# new mode (contract D-C1): create payload builder + candidate parsing
# (no network, no agents — pure helpers over dicts and temp files)
# ---------------------------------------------------------------------------

def _envelope(**kw) -> dict:
    base = {"slug": "Test_article", "wikipedia_title": "Test article",
            "display_title": "Test article", "schema_version": 3,
            "annotations": [ann(label="a"), ann(label="b")]}
    base.update(kw)
    return base


def test_build_create_body_shape():
    body, wire = m.build_create_body(
        _envelope(), revid=12345, wikidata_qid="Q42", run_id="deadbeef")
    assert body["wikipedia_title"] == "Test article"
    assert body["display_title"] == "Test article"
    assert body["wikidata_qid"] == "Q42"
    assert body["revid"] == 12345
    assert body["comment"] == "ai-create:deadbeef"
    assert "meta" not in body  # caller attaches build_meta(ctx, rec, wire)
    # every annotation gets a fresh 12-hex id (the server heals anyway, but
    # minting client-side keeps disk artifacts and D1 in agreement)
    assert len(body["annotations"]) == 2
    assert all(_is_fresh(a["id"]) for a in body["annotations"])
    assert wire["ids_fresh"] == 2 and wire["ids_echoed"] == 0


def test_build_create_body_optional_fields_omitted():
    env = _envelope()
    del env["display_title"]
    body, _ = m.build_create_body(env, revid=None, wikidata_qid=None,
                                  run_id="deadbeef")
    for k in ("display_title", "wikidata_qid", "revid"):
        assert k not in body, k
    # non-positive / non-int revids are dropped, not sent (D-C1: positive
    # int; True is an int subclass and is rejected too)
    for bad in (0, -5, "12345", 1.5, True):
        b, _ = m.build_create_body(env, revid=bad, run_id="deadbeef")
        assert "revid" not in b, bad


def test_build_create_body_empty_annotations_ok():
    body, wire = m.build_create_body(_envelope(annotations=[]), run_id="x")
    assert body["annotations"] == []
    assert wire["ids_fresh"] == 0


def test_build_create_body_downgrades_human_provenance():
    # A create has no stored humans — provenance 'human' from the agent is
    # laundering and must be downgraded (server-side twin: bot writes can't
    # mint human provenance).
    env = _envelope(annotations=[ann(label="fake", provenance="human")])
    body, wire = m.build_create_body(env, run_id="x")
    assert body["annotations"][0]["provenance"] == "ai-moderated"
    assert wire["provenance_downgraded"] == 1


def test_load_candidate_file(tmp_path=None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "new-titles.jsonl"
        p.write_text(
            '{"title": "Pentagon tiling", "slug": "Pentagon_tiling", "source": "wpmath-embeddedin"}\n'
            "\n"                                      # blank line ignored
            "{not json}\n"                            # malformed → skipped
            '{"title": "", "slug": "Empty_title"}\n'  # empty title → skipped
            '{"title": "No slug here"}\n'             # missing slug → skipped
            '{"title": "Dup", "slug": "Pentagon_tiling"}\n'  # dup slug → skipped
            '{"title": "Minimal", "slug": "Minimal"}\n',     # source defaults
            encoding="utf-8")
        cands = m.load_candidate_file(p)
    assert [c["slug"] for c in cands] == ["Pentagon_tiling", "Minimal"]
    assert cands[0]["source"] == "wpmath-embeddedin"
    assert cands[1]["source"] == "from-file"


def test_sidecar_revid():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        (cache / "Good.meta.json").write_text(
            json.dumps({"slug": "Good", "revid": 1299891234}), encoding="utf-8")
        (cache / "BadRevid.meta.json").write_text(
            json.dumps({"slug": "BadRevid", "revid": "not-an-int"}), encoding="utf-8")
        (cache / "Garbage.meta.json").write_text("{nope", encoding="utf-8")
        assert m.sidecar_revid("Good", cache) == 1299891234
        assert m.sidecar_revid("BadRevid", cache) is None
        assert m.sidecar_revid("Garbage", cache) is None
        assert m.sidecar_revid("Missing", cache) is None


def test_load_qid_map():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        data = Path(td)
        (data / "pilot_tagged.jsonl").write_text(
            '{"title": "Cardinal number", "wikidata_qid": "Q163875"}\n'
            '{"title": "No QID article"}\n',
            encoding="utf-8")
        (data / "tier2_tagged.jsonl").write_text(
            '{"title": "Cardinal number", "wikidata_qid": "Q_LOSER"}\n'  # first wins
            '{"title": "Monoid", "wikidata_qid": "Q208237"}\n',
            encoding="utf-8")
        qm = m.load_qid_map(data)
    assert qm == {"Cardinal number": "Q163875", "Monoid": "Q208237"}


# ---------------------------------------------------------------------------
# batch_annotate._preserve_human ladder stats (skips without the SDK)
# ---------------------------------------------------------------------------

def test_preserve_human_stats():
    if ba is None:
        raise unittest.SkipTest("claude-agent-sdk not installed")
    h1 = ann(label="kept", provenance="human")
    h2 = ann(label="altered", provenance="human")
    h3 = ann(label="dropped", provenance="human")
    existing = [h1, h2, h3, ann(label="ai-one")]
    produced = [dict(h1),                       # echoed unchanged → not counted
                {**h2, "note": "agent rewrote"},  # altered → restored
                ann(label="ai-one")]              # h3 dropped → reinserted
    out, stats = ba._preserve_human(existing, produced)
    assert stats == {"restored": 1, "reinserted": 1}
    assert out[1] == {**h2, "provenance": "human"}
    assert out[-1]["moderation_note"].startswith("re-inserted")
    # no humans → zero stats, produced unchanged
    out2, stats2 = ba._preserve_human([ann(label="x")], [ann(label="x")])
    assert stats2 == {"restored": 0, "reinserted": 0} and out2 == [ann(label="x")]


# ---------------------------------------------------------------------------
# plain runner (no pytest needed)
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = skipped = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS {name}")
        except unittest.SkipTest as e:
            skipped += 1
            print(f"  SKIP {name} ({e})")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
