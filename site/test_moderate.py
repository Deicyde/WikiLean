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
        # F12 lockstep-contract shapes: non-dict anchors no longer crash and
        # anchors[0] is the fallback when `anchor` is unusable.
        {"anchor": "Statement"},
        {"anchor": ["Statement"]},
        {"anchors": [{"section": "S", "snippet": "x"}, {"section": "T"}]},
        {"anchor": 7, "anchors": [{"type": "theorem_box", "value": "T1"}]},
        {"anchors": []},
        {"anchors": ["not-a-dict"]},
    ]
    for s in samples:
        assert m._anchor_sig(s) == ba._anchor_sig(s), s


def test_anchor_sig_anchors_contract():
    # F12 LOCKSTEP CONTRACT: plain-dict anchor unchanged; non-dict anchor
    # falls through to anchors[0] (when a plain dict) else all-null — and
    # never raises.
    null_sig = m._anchor_sig({})
    assert m._anchor_sig({"anchor": "Statement"}) == null_sig
    assert m._anchor_sig({"anchor": ["Statement"]}) == null_sig
    assert m._anchor_sig({"anchor": 7}) == null_sig
    assert m._anchor_sig({"anchors": []}) == null_sig
    assert m._anchor_sig({"anchors": ["nope"]}) == null_sig
    a0 = m._anchor_sig({"anchors": [{"section": "S", "snippet": "x"}]})
    assert a0 == m._anchor_sig({"anchor": {"section": "S", "snippet": "x"}})
    # the singular dict anchor wins over anchors[]
    both = {"anchor": {"section": "A", "snippet": "a"},
            "anchors": [{"section": "B", "snippet": "b"}]}
    assert m._anchor_sig(both) == m._anchor_sig({"anchor": {"section": "A", "snippet": "a"}})


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
# finalize_for_post — veto adjacency (F6: near-miss tombstone resurrections)
# ---------------------------------------------------------------------------

def _tombstone(**kw) -> dict:
    base = ann(label="Veto", id="cccccccccccc", provenance="human",
               status="rejected",
               anchor={"section": "Examples", "snippet": "the beta construction"})
    base.update(kw)
    return base


def test_veto_adjacent_clear_overlap_dropped():
    # F6: a NEW annotation in the tombstone's section whose snippet contains
    # >50% of the tombstone snippet's tokens is dropped at the wire.
    tomb = _tombstone()
    near = ann(label="resurrection (shifted)",
               anchor={"section": "Examples",
                       "snippet": "the beta construction proceeds in stages"})
    out, stats = m.finalize_for_post([tomb], [near])
    assert stats["veto_adjacent_dropped"] == 1
    assert out == [tomb]  # only the re-inserted tombstone reaches the wire
    assert stats["human_reinserted_wire"] == 1


def test_veto_adjacent_near_miss_survives():
    # F6 near-miss: <=50% token containment of the tombstone snippet → kept.
    tomb = _tombstone()
    low = ann(label="genuinely new",
              anchor={"section": "Examples",
                      "snippet": "construction of gamma objects in stages"})
    out, stats = m.finalize_for_post([tomb], [low])
    assert stats["veto_adjacent_dropped"] == 0
    assert any(a.get("label") == "genuinely new" for a in out)


def test_veto_adjacent_other_section_survives():
    # F6: high token overlap in a DIFFERENT section is not adjacency.
    tomb = _tombstone()
    elsewhere = ann(label="same words elsewhere",
                    anchor={"section": "History",
                            "snippet": "the beta construction"})
    out, stats = m.finalize_for_post([tomb], [elsewhere])
    assert stats["veto_adjacent_dropped"] == 0
    assert any(a.get("label") == "same words elsewhere" for a in out)


def test_veto_adjacent_only_applies_to_new_annotations():
    # F6: an annotation MATCHED to existing (by id or sig) is never dropped,
    # even when its anchor overlaps a tombstone's.
    tomb = _tombstone()
    existing_ai = ann(label="old ai", id="dddddddddddd",
                      anchor={"section": "Examples",
                              "snippet": "the beta construction proceeds"})
    echoed = dict(existing_ai)
    out, stats = m.finalize_for_post([tomb, existing_ai], [echoed])
    assert stats["veto_adjacent_dropped"] == 0
    assert stats["ids_echoed"] == 1


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
    # (moderation_flags added by fix F14 — harvested agent dissent)
    meta2 = m.build_meta(ctx, {}, {})
    assert meta2["tokens"] == 0 and meta2["ladder"] == {
        "restored": 0, "reinserted": 0, "downgrades_blocked": 0,
        "moderation_flags": []}


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
# HTTP write path: 409/422/429 handling (no network, no agents).
#
# moderate.py imports `requests` lazily INSIDE each transport function, so
# sys.modules["requests"] is a clean injection seam; the asyncio module-global
# is swapped for a shim namespace so the 60s 429 backoff records instead of
# sleeping. SEAM SUGGESTION (not changed here): a ctx.transport callable
# (defaulting to requests.post/put) would make these tests need no module
# patching at all.
# ---------------------------------------------------------------------------

import asyncio
import contextlib
import types


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


@contextlib.contextmanager
def _patched_transport(responses):
    """Install a recording fake `requests` module + a non-sleeping asyncio
    shim inside moderate. `responses` is a list of (status, payload); the
    last entry repeats. An EMPTY list asserts no HTTP write happens at all."""
    real_asyncio = m.asyncio
    rec = SimpleNamespace(calls=[], sleeps=[])
    queue = list(responses)

    def _send(url, json=None, headers=None, timeout=None):
        assert queue, f"unexpected HTTP write to {url}"
        rec.calls.append({"url": url, "body": json, "headers": headers})
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return _Resp(status, payload)

    async def _sleep(seconds):
        rec.sleeps.append(seconds)

    fake_requests = types.ModuleType("requests")
    fake_requests.post = _send
    fake_requests.put = _send
    shim = SimpleNamespace(to_thread=real_asyncio.to_thread, sleep=_sleep,
                           Semaphore=real_asyncio.Semaphore,
                           gather=real_asyncio.gather, run=real_asyncio.run)
    saved_requests = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests
    m.asyncio = shim
    try:
        yield rec
    finally:
        m.asyncio = real_asyncio
        if saved_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = saved_requests


def _write_ctx():
    return SimpleNamespace(api_base="http://api.test", token="tok-123")


def test_write_article_retries_429_then_succeeds():
    with _patched_transport([(429, {}), (429, {}), (200, {"version": 5})]) as t:
        code, payload, rebased = asyncio.run(
            m._write_article(_write_ctx(), "Lp space", {"base_version": 4}, "post"))
    assert code == 200 and payload == {"version": 5}
    assert rebased is False              # 429 retries are not rebases
    assert t.sleeps == [60, 60]          # backed off before each retry
    assert len(t.calls) == 3
    assert t.calls[0]["headers"]["Authorization"] == "Bearer tok-123"
    assert t.calls[0]["url"] == "http://api.test/api/article/Lp%20space"  # quoted
    assert t.calls[0]["body"] == {"base_version": 4}


def test_write_article_429_exhausts_after_three_retries():
    with _patched_transport([(429, {"error": "rate"})]) as t:
        code, payload, _ = asyncio.run(
            m._write_article(_write_ctx(), "S", {}, "post"))
    assert code == 429 and payload == {"error": "rate"}
    assert len(t.calls) == 4 and t.sleeps == [60, 60, 60]


def test_write_article_non_retryable_single_attempt():
    # 422 must NOT be retried — human-loss is not transient. (409 used to be
    # in this test; fix F9 gives POST 409s one zero-token rebase instead —
    # see the dedicated rebase tests. A 409 WITHOUT base_version, like this
    # bare body, still gets no rebase.)
    for status, payload in [(409, {"error": "stale", "version": 9}),
                            (422, {"error": "human annotations lost", "missing": ["X"]})]:
        with _patched_transport([(status, payload)]) as t:
            code, got, rebased = asyncio.run(
                m._write_article(_write_ctx(), "S", {}, "post"))
        assert code == status and got == payload
        assert rebased is False
        assert len(t.calls) == 1 and t.sleeps == []


def test_write_article_409_rebases_once_with_fresh_state():
    # F9: a stale 409 on a base_version POST triggers ONE rebase — re-GET,
    # re-run finalize_for_post against the fresh state, re-POST — for zero
    # agent tokens.
    h = ann(label="h", id="aaaaaaaaaaaa", provenance="human")
    fresh = {"annotations": [h], "version": 9, "wikipedia_title": "S"}
    body = {"annotations": [ann(label="a", id="bbbbbbbbbbbb")],
            "base_version": 4, "meta": {"ids": {"stale": True}}}
    with _patched_get(200, fresh) as gets:
        with _patched_transport([(409, {"error": "stale", "version": 9}),
                                 (200, {"version": 10})]) as t:
            code, payload, rebased = asyncio.run(
                m._write_article(_write_ctx(), "S", body, "post"))
    assert code == 200 and payload == {"version": 10}
    assert rebased is True               # P2c: surfaces as outcome '409-rebased'
    assert len(t.calls) == 2 and t.sleeps == []
    assert len(gets) == 1                       # exactly one re-GET
    rebased = t.calls[1]["body"]
    assert rebased["base_version"] == 9         # rebased onto the fresh version
    assert h in rebased["annotations"]          # fresh human re-appended
    assert rebased["meta"]["ids"]["human_reinserted_wire"] == 1  # stats refreshed


def test_write_article_409_second_conflict_returned():
    # F9: only ONE rebase — a second 409 goes back to the caller (skip+requeue).
    with _patched_get(200, {"annotations": [], "version": 9}) as gets:
        with _patched_transport([(409, {"error": "stale", "version": 9})]) as t:
            code, _, rebased = asyncio.run(m._write_article(
                _write_ctx(), "S", {"annotations": [], "base_version": 4}, "post"))
    assert code == 409
    assert rebased is True               # rebase attempted; terminal 409 → noop
    assert len(t.calls) == 2 and len(gets) == 1


def test_write_article_put_409_not_rebased():
    # F9: a PUT 409 means 'exists' — no rebase applies to creates.
    with _patched_transport([(409, {"error": "exists"})]) as t:
        code, _, rebased = asyncio.run(m._write_article(
            _write_ctx(), "S", {"base_version": 1}, "put"))
    assert code == 409 and len(t.calls) == 1
    assert rebased is False


def test_write_article_5xx_retries_with_backoff():
    # F9: 5xx → retry twice with 5s/15s backoff (CAS makes committed-then-500
    # retries safe — they 409 into the rebase path).
    with _patched_transport([(500, {}), (502, {}), (200, {"version": 5})]) as t:
        code, payload, _ = asyncio.run(m._write_article(_write_ctx(), "S", {}, "post"))
    assert code == 200 and payload == {"version": 5}
    assert t.sleeps == [5, 15] and len(t.calls) == 3


def test_write_article_non_json_body_yields_empty_payload():
    # (5xx now retries twice with 5s/15s backoff before giving up — fix F9)
    with _patched_transport([(500, None)]) as t:
        code, payload, _ = asyncio.run(m._write_article(_write_ctx(), "S", {}, "put"))
    assert code == 500 and payload == {}
    assert t.sleeps == [5, 15] and len(t.calls) == 3


# ---------------------------------------------------------------------------
# process_review handler behavior around the write statuses (fake agents via
# ctx.ba — annotate_one is an injection point by design; _http_get patched
# for the read path).
# ---------------------------------------------------------------------------

_REVIEW_EXISTING = [
    ann(label="h", id="aaaaaaaaaaaa", provenance="human"),
    ann(label="a", id="bbbbbbbbbbbb"),
]


@contextlib.contextmanager
def _patched_get(status: int, payload):
    saved = m._http_get
    calls = []

    def fake_get(url, token=None):
        calls.append(url)
        return _Resp(status, payload)

    m._http_get = fake_get
    try:
        yield calls
    finally:
        m._http_get = saved


def _review_ctx(annot_dir: Path, dry_run: bool = False) -> SimpleNamespace:
    state = {"agent_calls": 0, "target_revid": "unset"}

    async def fake_annotate_one(article, sem, seed_decls, moderate=False,
                                existing_override=None, target_revid=None):
        # target_revid param added by fix F1 (review at the pinned revision)
        state["agent_calls"] += 1
        state["target_revid"] = target_revid
        slug = article["title"].replace(" ", "_")
        (annot_dir / f"{slug}.json").write_text(json.dumps(
            {"annotations": existing_override or []}), encoding="utf-8")
        return {"slug": slug, "mode": "moderate", "tokens": 10,
                "cost_usd_equiv": 0.01, "elapsed_s": 0.1,
                "ladder": {"restored": 0, "reinserted": 0,
                           "downgrades_blocked": 0, "moderation_flags": []}}

    ctx = SimpleNamespace(
        mode="review", api_base="http://api.test", token="tok",
        auth="subscription", dry_run=dry_run, concurrency=1,
        budget_tokens=None, run_id="testrun1", seed_decls={},
        model="test-model", prompt_sha="abc123abc123", mathlib_sha="1234567",
        ba=SimpleNamespace(annotate_one=fake_annotate_one, ANNOT=annot_dir))
    ctx._state = state
    return ctx


def _run_review(post_responses, dry_run=False, get_status=200, get_payload=None):
    import tempfile
    if get_payload is None:
        get_payload = {"annotations": _REVIEW_EXISTING, "version": 7,
                       "wikipedia_title": "T Article", "revid": 123456789}
    with tempfile.TemporaryDirectory() as td:
        ctx = _review_ctx(Path(td), dry_run=dry_run)
        with _patched_get(get_status, get_payload):
            with _patched_transport(post_responses) as t:
                rec = asyncio.run(m.process_review(
                    {"slug": "T_Article", "reason": "test"}, ctx,
                    asyncio.Semaphore(1)))
    return rec, t, ctx


def test_process_review_200_records_version():
    rec, t, _ = _run_review([(200, {"version": 8, "matched": "5/5"})])
    assert rec["post_status"] == 200
    assert rec["posted_version"] == 8 and rec["server_matched"] == "5/5"
    body = t.calls[0]["body"]
    assert body["base_version"] == 7
    assert body["comment"] == "ai-moderate:review:testrun1"
    assert body["meta"]["run_id"] == "testrun1"  # contract ID3 rides every POST
    # the stored human went over the wire byte-identical (422 made impossible)
    assert _REVIEW_EXISTING[0] in body["annotations"]


def test_process_review_threads_pinned_revid_to_agents():
    # F1: the pinned revid from the GET rides into annotate_one so the agents
    # review exactly the revision D1 pins; the POST body carries NO revid
    # (the pin is unchanged — that's the point).
    rec, t, ctx = _run_review([(200, {"version": 8, "matched": "5/5"})])
    assert ctx._state["target_revid"] == 123456789
    assert "revid" not in t.calls[0]["body"]


def test_process_review_409_skips_for_requeue():
    # Mid-run human edit: F9 gives it ONE zero-token rebase (re-GET +
    # re-finalize + re-POST); a second 409 is still not an error —
    # /api/work re-queues it next run.
    rec, t, _ = _run_review([(409, {"error": "stale", "version": 9})])
    assert rec["skipped"] == "stale_409"
    assert "error" not in rec
    assert len(t.calls) == 2  # initial POST + the single rebase re-POST (F9)


def test_process_review_422_loud_and_no_retry():
    # 422 = finalize_for_post bug; must be loud and must NOT retry.
    rec, t, _ = _run_review([(422, {"error": "human annotations lost",
                                    "missing": ["h"]})])
    assert rec["error"] == "human_lost_422"
    assert len(t.calls) == 1


def test_process_review_429_after_transport_retries():
    rec, t, _ = _run_review([(429, {})])
    assert rec["error"] == "rate_limited_429"
    assert len(t.calls) == 4 and t.sleeps == [60, 60, 60]


def test_process_review_get_failure_skips_agents():
    rec, t, ctx = _run_review([], get_status=500, get_payload={})
    assert rec["error"] == "get_failed_500"
    assert ctx._state["agent_calls"] == 0  # never burned tokens
    assert t.calls == []                   # never wrote


def test_process_review_dry_run_no_agents_no_writes():
    rec, t, ctx = _run_review([], dry_run=True)
    assert rec["dry_run"] is True
    assert rec["base_version"] == 7
    assert ctx._state["agent_calls"] == 0
    assert t.calls == []
    # the dry-run wire stats come from finalize_for_post(existing, existing)
    assert rec["ids"]["ids_echoed"] == 1          # the ai annotation echoes its id
    assert rec["ids"]["human_restored_wire"] == 0


def test_process_review_agent_error_short_circuits():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        ctx = _review_ctx(Path(td))

        async def failing_annotate_one(article, sem, seed_decls, **kw):
            return {"slug": "T_Article", "error": "agent1_no_json"}

        ctx.ba = SimpleNamespace(annotate_one=failing_annotate_one,
                                 ANNOT=Path(td))
        with _patched_get(200, {"annotations": [], "version": 1,
                                "wikipedia_title": "T"}):
            with _patched_transport([]) as t:
                rec = asyncio.run(m.process_review(
                    {"slug": "T_Article"}, ctx, asyncio.Semaphore(1)))
    assert rec["error"] == "agent1_no_json"
    assert t.calls == []


def test_process_new_refuses_put_without_sidecar_revid():
    import tempfile
    # F16: a create whose fetch sidecar lacks a valid revid is skipped
    # ('skipped (no revid)') instead of seeding an unpinnable article.
    with tempfile.TemporaryDirectory() as td:
        annot = Path(td) / "annotations"
        cache = Path(td) / "cache"
        annot.mkdir()
        cache.mkdir()  # deliberately NO <slug>.meta.json sidecar

        async def fake_annotate_one(article, sem, seed_decls, moderate=False,
                                    existing_override=None, target_revid=None):
            slug = article["title"].replace(" ", "_")
            (annot / f"{slug}.json").write_text(json.dumps(
                {"slug": slug, "wikipedia_title": article["title"],
                 "annotations": [ann(label="a")]}), encoding="utf-8")
            return {"slug": slug, "tokens": 5, "cost_usd_equiv": 0.01,
                    "elapsed_s": 0.1}

        ctx = SimpleNamespace(
            mode="new", api_base="http://api.test", token="tok",
            auth="subscription", dry_run=False, concurrency=1,
            budget_tokens=None, run_id="testrun3", seed_decls={},
            model="test-model", prompt_sha="abc123abc123", mathlib_sha="1234567",
            qid_map={},
            ba=SimpleNamespace(annotate_one=fake_annotate_one, ANNOT=annot,
                               CACHE=cache))
        with _patched_transport([]) as t:  # empty list asserts NO HTTP write
            rec = asyncio.run(m.process_new(
                {"slug": "New_Article", "title": "New Article"}, ctx,
                asyncio.Semaphore(1)))
    assert rec["skipped"] == "no_revid"
    assert t.calls == []


# ---------------------------------------------------------------------------
# run_jobs: token budget + consecutive-window-exhaustion abort logic
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _patched_log():
    """Patch BOTH run-loop sidecars (run log + P2c decisions.jsonl) into a tmp
    dir. Yields a namespace: .run and .decisions paths."""
    import tempfile
    saved = m.MODERATE_LOG
    saved_dec = m.DECISIONS_LOG
    with tempfile.TemporaryDirectory() as td:
        m.MODERATE_LOG = Path(td) / "run.log"
        m.DECISIONS_LOG = Path(td) / "decisions.jsonl"
        try:
            yield SimpleNamespace(run=m.MODERATE_LOG, decisions=m.DECISIONS_LOG)
        finally:
            m.MODERATE_LOG = saved
            m.DECISIONS_LOG = saved_dec


def _jobs_ctx(budget=None, concurrency=1):
    return SimpleNamespace(mode="review", run_id="testrun2",
                           concurrency=concurrency, budget_tokens=budget)


def _scripted_process(script):
    """Async process() that pops one rec per job, never yielding — so jobs
    complete in submission order and abort behavior is deterministic."""
    queue = list(script)

    async def process(job, ctx, sem):
        return {"slug": job["slug"], **queue.pop(0)}

    return process


def test_run_jobs_token_budget_aborts_remaining():
    jobs = [{"slug": f"s{i}"} for i in range(4)]
    script = [{"tokens": 60}] * 4
    with _patched_log() as log:
        rc, stats = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(budget=100), _scripted_process(script)))
        lines = log.run.read_text().splitlines()
    assert rc == 3              # aborted, matching batch_annotate's convention
    assert stats["tokens"] == 120  # job 0 (60) + job 1 (60, trips the budget)
    assert stats["processed"] == 2 and stats["errors"] == 0
    assert len(lines) == 2      # jobs 2-3 skipped without being processed
    assert all(json.loads(l)["run_id"] == "testrun2" for l in lines)


def test_run_jobs_window_exhaustion_aborts_after_threshold():
    n = m.ABORT_AFTER  # 5
    jobs = [{"slug": f"s{i}"} for i in range(n + 2)]
    script = [{"error": "rate limit hit", "tokens": 1}] * (n + 2)
    with _patched_log() as log:
        rc, _ = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(), _scripted_process(script)))
        lines = log.run.read_text().splitlines()
    assert rc == 3
    assert len(lines) == n      # aborted exactly at the threshold


def test_run_jobs_success_resets_consecutive_counter():
    n = m.ABORT_AFTER
    jobs = [{"slug": f"s{i}"} for i in range(2 * n - 1)]
    script = ([{"error": "overloaded", "tokens": 1}] * (n - 1)
              + [{"tokens": 1}]
              + [{"error": "overloaded", "tokens": 1}] * (n - 1))
    with _patched_log() as log:
        rc, _ = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(), _scripted_process(script)))
        lines = log.run.read_text().splitlines()
    assert rc == 0                       # never reached ABORT_AFTER in a row
    assert len(lines) == len(jobs)


def test_run_jobs_process_exception_counted_not_fatal():
    # F12: one crashing job must not kill the asyncio.gather — it is logged
    # and counted as a job error; the rest of the batch still runs.
    jobs = [{"slug": "boom"}, {"slug": "fine"}]

    async def process(job, ctx, sem):
        if job["slug"] == "boom":
            raise ValueError("bad annotation")
        return {"slug": job["slug"], "tokens": 1}

    with _patched_log() as log:
        rc, _ = asyncio.run(m.run_jobs(jobs, _jobs_ctx(), process))
        lines = [json.loads(line) for line in log.run.read_text().splitlines()]
    assert rc == 0
    assert len(lines) == 2
    errs = [rec for rec in lines if rec.get("error")]
    assert len(errs) == 1
    assert errs[0]["slug"] == "boom"
    assert errs[0]["error"].startswith("job_crashed: ValueError")


def test_run_jobs_non_window_errors_never_abort():
    jobs = [{"slug": f"s{i}"} for i in range(m.ABORT_AFTER + 2)]
    script = [{"error": "fetch_failed", "tokens": 1}] * len(jobs)
    with _patched_log() as log:
        rc, _ = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(), _scripted_process(script)))
        lines = log.run.read_text().splitlines()
    assert rc == 0
    assert len(lines) == len(jobs)       # all processed despite the errors


# ---------------------------------------------------------------------------
# --from-file candidate parsing: hostile/odd lines
# ---------------------------------------------------------------------------

def test_load_candidate_file_hostile_lines():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "hostile.jsonl"
        p.write_text(
            '"just a string"\n'                       # JSON scalar → skipped
            "42\n"                                    # JSON number → skipped
            "[1, 2]\n"                                # JSON array → skipped
            "null\n"                                  # JSON null → skipped
            '{"title": 42, "slug": "Numeric_title"}\n'   # non-str title → skipped
            '{"title": "T", "slug": 42}\n'               # non-str slug → skipped
            "   \n"                                   # whitespace-only → ignored
            '{"title": "Émigré theörem", "slug": "Emigre_theorem"}\n'
            '{"title": "Null source", "slug": "Null_source", "source": null}\n',
            encoding="utf-8")
        cands = m.load_candidate_file(p)
    assert [c["slug"] for c in cands] == ["Emigre_theorem", "Null_source"]
    assert cands[0]["title"] == "Émigré theörem"      # unicode survives
    assert cands[1]["source"] == "from-file"          # null source → default


def test_load_candidate_file_empty_file():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        assert m.load_candidate_file(p) == []


# ---------------------------------------------------------------------------
# eval_moderation.py wiring (the deterministic eval is its own CI gate; here
# we only pin its CLI contract: the token gate refuses, offline exits 0)
# ---------------------------------------------------------------------------

def test_eval_moderation_live_gate_refuses_without_confirmation():
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "eval_moderation.py"),
                        "--live"], capture_output=True, text=True)
    assert r.returncode == 2, r.returncode
    assert "costs tokens" in (r.stdout + r.stderr)


def test_eval_moderation_requires_a_mode():
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "eval_moderation.py")],
                       capture_output=True, text=True)
    assert r.returncode == 2  # argparse: one of --offline/--live is required


def test_eval_moderation_offline_green():
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "eval_moderation.py"),
                        "--offline"], capture_output=True, text=True)
    assert r.returncode == 0, f"offline eval failed:\n{r.stdout}\n{r.stderr}"
    assert "0 failed" in r.stdout


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
    # 'flags' key added by fix F14 (harvested moderation_flag dissent)
    assert stats == {"restored": 1, "reinserted": 1, "flags": []}
    assert out[1] == {**h2, "provenance": "human"}
    assert out[-1]["moderation_note"].startswith("re-inserted")
    # no humans → zero stats, produced unchanged
    out2, stats2 = ba._preserve_human([ann(label="x")], [ann(label="x")])
    assert stats2 == {"restored": 0, "reinserted": 0, "flags": []}
    assert out2 == [ann(label="x")]


def test_preserve_human_id_match_replaces_in_place():
    if ba is None:
        raise unittest.SkipTest("claude-agent-sdk not installed")
    # F7: the agent re-anchors a human annotation (sig no longer matches).
    # The id match must REPLACE the altered copy in place — the final output
    # contains exactly ONE copy (the stored original), no duplicate.
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    reanchored = {**h, "anchor": {"section": "Statement", "snippet": "MOVED"}}
    other = ann(label="other", id="bbccddeeff00")
    out, stats = ba._preserve_human([h, other], [reanchored, dict(other)])
    assert out == [h, other]
    assert stats["restored"] == 1 and stats["reinserted"] == 0


def test_preserve_human_harvests_moderation_flag():
    if ba is None:
        raise unittest.SkipTest("claude-agent-sdk not installed")
    # F14: dissent the agent attached to its copy of a human annotation is
    # harvested into stats['flags'] before the restore strips it.
    h = ann(label="h", id="aabbccddeeff", provenance="human")
    flagged = {**h, "moderation_flag": "decl looks wrong"}
    out, stats = ba._preserve_human([h], [flagged])
    assert out == [h]  # the flag itself never reaches the output copy
    assert stats == {"restored": 1, "reinserted": 0,
                     "flags": [["aabbccddeeff", "decl looks wrong"]]}


# ---------------------------------------------------------------------------
# P2c — decisions.jsonl sidecar (pure helpers + run_jobs wiring)
# ---------------------------------------------------------------------------

def test_parse_anchor_stats():
    # review/new shape: the 'N/M matched' string batch_annotate records
    assert m.parse_anchor_stats("5/7 matched") == {"matched": 5, "total": 7}
    assert m.parse_anchor_stats("102/102 matched") == {"matched": 102, "total": 102}
    # wp-update shape: int pair from update_from_upstream.process_slug
    assert m.parse_anchor_stats(3, 4) == {"matched": 3, "total": 4}
    assert m.parse_anchor_stats(0, 0) == {"matched": 0, "total": 0}
    # unavailable shapes → None, never a crash
    for bad in (None, "", "garbage", 3, (True, 4)):
        args = bad if isinstance(bad, tuple) else (bad,)
        assert m.parse_anchor_stats(*args) is None, bad
    assert m.parse_anchor_stats(3, None) is None
    assert m.parse_anchor_stats(True, 4) is None  # bool is not an anchor count


def test_decision_outcome_review_new_recs():
    assert m.decision_outcome({"dry_run": True}) == "dry-run"
    assert m.decision_outcome({"post_status": 200}) == "posted"
    assert m.decision_outcome({"put_status": 201}) == "posted"
    assert m.decision_outcome({"post_status": 200, "rebased": True}) == "409-rebased"
    assert m.decision_outcome({"skipped": "stale_409", "rebased": True,
                               "post_status": 409}) == "noop"
    assert m.decision_outcome({"skipped": "exists_409", "put_status": 409}) == "noop"
    assert m.decision_outcome({"skipped": "no_revid"}) == "noop"
    assert m.decision_outcome({"error": "human_lost_422",
                               "post_status": 422}) == "422"
    assert m.decision_outcome({"error": "create_422_impossible",
                               "put_status": 422}) == "422"
    assert m.decision_outcome({"error": "agent1_no_json"}) == "error"
    assert m.decision_outcome({"error": "rate_limited_429",
                               "post_status": 429}) == "error"
    assert m.decision_outcome({"error": "get_failed_500"}) == "error"
    assert m.decision_outcome({}) == "error"  # rec shape we don't recognize


def test_decision_outcome_wp_update_recs():
    # update_from_upstream.process_slug rec shape (string 'outcome')
    assert m.decision_outcome({"outcome": "repinned"}) == "posted"
    assert m.decision_outcome({"outcome": "would-repin"}) == "dry-run"
    assert m.decision_outcome({"outcome": "up-to-date"}) == "noop"
    assert m.decision_outcome({"outcome": "needs-work"}) == "noop"
    assert m.decision_outcome({"outcome": "unknown-slug"}) == "noop"
    assert m.decision_outcome({"outcome": "no-latest-revid"}) == "noop"
    assert m.decision_outcome(
        {"outcome": "stale (409) — re-run to rebase"}) == "noop"
    assert m.decision_outcome(
        {"outcome": "422 human-preservation: ['x']"}) == "422"
    assert m.decision_outcome({"outcome": "fetch-error (boom)"}) == "error"
    assert m.decision_outcome({"outcome": "http-500: 'oops'"}) == "error"
    assert m.decision_outcome({"outcome": "error (ValueError: x)"}) == "error"


def test_decision_line_shape():
    ctx = SimpleNamespace(run_id="deadbeef", mode="review",
                          model="test-model", prompt_sha="abc123abc123")
    ladder = {"restored": 1, "reinserted": 0, "downgrades_blocked": 0,
              "moderation_flags": [["aabbccddeeff", "decl looks wrong"]]}
    ids = {"ids_echoed": 5, "ids_fresh": 1}
    rec = {"slug": "Lp_space", "tokens": 1000, "cost_usd_equiv": 1.23,
           "ladder": ladder, "ids": ids, "matched": "6/6 matched",
           "base_version": 7, "post_status": 200}
    line = m.decision_line(ctx, rec)
    assert set(line) == {"ts", "run_id", "mode", "slug", "model", "prompt_sha",
                         "tokens", "cost_usd_equiv", "ladder", "ids",
                         "anchors", "base_version", "outcome"}
    assert isinstance(line["ts"], int) and line["ts"] > 1_700_000_000_000  # ms
    assert line["run_id"] == "deadbeef" and line["mode"] == "review"
    assert line["ladder"]["moderation_flags"] == [["aabbccddeeff",
                                                   "decl looks wrong"]]
    assert line["anchors"] == {"matched": 6, "total": 6}
    assert line["base_version"] == 7 and line["outcome"] == "posted"
    # a ctx without model/prompt_sha (wp-update, bare test ctx) → None, no crash
    line2 = m.decision_line(SimpleNamespace(run_id="r", mode="wp-update"),
                            {"slug": "X", "outcome": "up-to-date",
                             "matched": 3, "total": 4})
    assert line2["model"] is None and line2["prompt_sha"] is None
    assert line2["tokens"] == 0 and line2["cost_usd_equiv"] is None
    assert line2["anchors"] == {"matched": 3, "total": 4}
    assert line2["outcome"] == "noop"


def test_append_decision_tmp_dir():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nested" / "decisions.jsonl"  # parent dir auto-created
        m.append_decision({"slug": "A", "outcome": "posted"}, p)
        m.append_decision({"slug": "B", "outcome": "noop"}, p)
        lines = [json.loads(l) for l in p.read_text().splitlines()]
    assert [l["slug"] for l in lines] == ["A", "B"]


def test_run_jobs_appends_one_decision_line_per_job():
    jobs = [{"slug": "posted_one"}, {"slug": "dry_one"},
            {"slug": "skipped_one"}, {"slug": "error_one"}]
    script = [
        {"tokens": 100, "cost_usd_equiv": 0.5, "base_version": 3,
         "matched": "5/7 matched", "post_status": 200,
         "ladder": {"restored": 0, "reinserted": 0, "downgrades_blocked": 0,
                    "moderation_flags": []},
         "ids": {"ids_echoed": 5}},
        {"dry_run": True, "base_version": 9, "ids": {"ids_echoed": 2}},
        {"skipped": "stale_409", "post_status": 409, "rebased": True},
        {"error": "agent2_no_json", "tokens": 7},
    ]
    with _patched_log() as log:
        rc, stats = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(), _scripted_process(script)))
        dec = [json.loads(l) for l in log.decisions.read_text().splitlines()]
    assert rc == 0 and stats["processed"] == 4 and stats["errors"] == 1
    by_slug = {d["slug"]: d for d in dec}
    assert len(dec) == 4 and len(by_slug) == 4  # one line per article
    assert by_slug["posted_one"]["outcome"] == "posted"
    assert by_slug["posted_one"]["anchors"] == {"matched": 5, "total": 7}
    assert by_slug["posted_one"]["base_version"] == 3
    assert by_slug["dry_one"]["outcome"] == "dry-run"
    assert by_slug["skipped_one"]["outcome"] == "noop"
    assert by_slug["error_one"]["outcome"] == "error"
    assert all(d["run_id"] == "testrun2" and d["mode"] == "review"
               for d in dec)


def test_process_review_rebased_success_marks_409_rebased():
    # Mid-run human edit, but the F9 rebase lands: outcome '409-rebased'.
    rec, t, _ = _run_review([(409, {"error": "stale", "version": 9}),
                             (200, {"version": 10})])
    assert rec["post_status"] == 200 and rec["rebased"] is True
    assert rec["posted_version"] == 10
    assert m.decision_outcome(rec) == "409-rebased"
    assert len(t.calls) == 2


# ---------------------------------------------------------------------------
# P2c — pipeline_runs registration (RUNS-API contract; injected transport)
# ---------------------------------------------------------------------------

def test_build_runs_payload_shape():
    p = m.build_runs_payload(
        run_id="cafebabe", kind="review", started_at=1_000, finished_at=2_000,
        articles_processed=3, errors=1, tokens=12345, cost_usd_equiv=2.96,
        model="claude-opus-4-7", prompt_sha="abc123abc123",
        notes="modes=review")
    assert p == {"run_id": "cafebabe", "kind": "review",
                 "started_at": 1_000, "finished_at": 2_000,
                 "articles_processed": 3, "errors": 1, "tokens": 12345,
                 "cost_usd_equiv": 2.96, "model": "claude-opus-4-7",
                 "prompt_sha": "abc123abc123", "notes": "modes=review"}


def test_build_runs_payload_optional_fields_and_sentinels():
    p = m.build_runs_payload(
        run_id="cafebabe", kind="wp-update", started_at=1, finished_at=2,
        articles_processed=0, errors=0, tokens=0, cost_usd_equiv=None,
        model=None, prompt_sha="unavailable", notes=None)
    # optional fields omitted; the 'unavailable' sentinel never goes on the wire
    for k in ("model", "prompt_sha", "notes"):
        assert k not in p, k
    assert p["cost_usd_equiv"] is None  # contract: number|null


def _runs_transport(status, payload):
    calls = []

    def transport(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return _Resp(status, payload)

    return transport, calls


def test_register_run_200_posts_bearer():
    transport, calls = _runs_transport(200, {"ok": True})
    ok = m.register_run("http://api.test/", "tok-123",
                        {"run_id": "cafebabe"}, transport=transport)
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["url"] == "http://api.test/api/runs"  # trailing / stripped
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-123"
    assert calls[0]["body"] == {"run_id": "cafebabe"}


def test_register_run_duplicate_still_ok():
    transport, calls = _runs_transport(200, {"ok": True, "duplicate": True})
    assert m.register_run("http://api.test", "tok", {"run_id": "x"},
                          transport=transport) is True


def test_register_run_404_tolerated():
    # Endpoint not deployed yet: one warning, False return, NO exception —
    # the runner must never fail on telemetry.
    transport, calls = _runs_transport(404, {"error": "not found"})
    ok = m.register_run("http://api.test", "tok", {"run_id": "x"},
                        transport=transport)
    assert ok is False and len(calls) == 1  # exactly one attempt, no retries


def test_register_run_other_status_tolerated():
    for status in (400, 403, 500):
        transport, calls = _runs_transport(status, {})
        assert m.register_run("http://api.test", "tok", {"run_id": "x"},
                              transport=transport) is False
        assert len(calls) == 1


def test_register_run_transport_exception_tolerated():
    def exploding_transport(url, json=None, headers=None, timeout=None):
        raise OSError("connection refused")

    assert m.register_run("http://api.test", "tok", {"run_id": "x"},
                          transport=exploding_transport) is False


def _runs_args(dry_run: bool):
    return SimpleNamespace(dry_run=dry_run, run_id="cafebabe",
                           command="review", api_base="http://api.test")


def test_maybe_register_run_real_run_posts_aggregates():
    transport, calls = _runs_transport(200, {"ok": True})
    totals = {"processed": 3, "errors": 1, "tokens": 12345, "cost": 2.9614}
    ok = m.maybe_register_run(_runs_args(dry_run=False), "tok", 1_000, totals,
                              model="test-model", prompt_sha="abc123abc123",
                              notes="modes=review", transport=transport)
    assert ok is True and len(calls) == 1
    body = calls[0]["body"]
    assert body["run_id"] == "cafebabe" and body["kind"] == "review"
    assert body["articles_processed"] == 3 and body["errors"] == 1
    assert body["tokens"] == 12345 and body["cost_usd_equiv"] == 2.9614
    assert body["started_at"] == 1_000
    assert isinstance(body["finished_at"], int)
    assert body["finished_at"] >= body["started_at"]


def test_maybe_register_run_dry_run_never_posts():
    def forbidden_transport(url, json=None, headers=None, timeout=None):
        raise AssertionError("dry-run must not POST /api/runs")

    ok = m.maybe_register_run(_runs_args(dry_run=True), "tok", 1_000,
                              m.zero_stats(), transport=forbidden_transport)
    assert ok is False


def test_maybe_register_run_tokenless_never_posts():
    def forbidden_transport(url, json=None, headers=None, timeout=None):
        raise AssertionError("tokenless run must not POST /api/runs")

    ok = m.maybe_register_run(_runs_args(dry_run=False), None, 1_000,
                              m.zero_stats(), transport=forbidden_transport)
    assert ok is False


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
