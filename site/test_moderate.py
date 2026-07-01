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


# The real requests.exceptions hierarchy — so the `except
# requests.exceptions.RequestException` in _write_article resolves against the
# fake `requests` module the harness installs (the fake module otherwise has no
# `.exceptions`). Imported once; if requests isn't installed the network-raise
# tests self-skip (see _require_requests_exceptions).
try:
    import requests as _real_requests
    _REAL_REQ_EXC = _real_requests.exceptions
except ImportError:  # pragma: no cover - requests is a hard dep in the venv
    _real_requests = None
    _REAL_REQ_EXC = None


def _require_requests_exceptions():
    if _REAL_REQ_EXC is None:
        raise unittest.SkipTest("requests not installed (network-raise tests)")


@contextlib.contextmanager
def _patched_transport(responses):
    """Install a recording fake `requests` module + a non-sleeping asyncio
    shim inside moderate. `responses` is a list of entries; the last repeats.
    An EMPTY list asserts no HTTP write happens at all.

    Each entry is normally an (status, payload) tuple. ADDITIVE extension for
    the checkpoint-and-retry-POST fix: an entry of the form
    ("raise", exc_instance) makes `_send` RAISE exc_instance instead of
    returning a response — so the `except requests.exceptions.RequestException`
    network-drop path in _write_article can be exercised. The fake module's
    `.exceptions` is wired to the real requests.exceptions so that except
    clause resolves. Existing (status, payload) callers are unchanged."""
    real_asyncio = m.asyncio
    rec = SimpleNamespace(calls=[], sleeps=[])
    queue = list(responses)

    def _send(url, json=None, headers=None, timeout=None):
        assert queue, f"unexpected HTTP write to {url}"
        rec.calls.append({"url": url, "body": json, "headers": headers})
        entry = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == "raise":
            raise entry[1]            # additive: simulate a dropped connection
        status, payload = entry
        return _Resp(status, payload)

    async def _sleep(seconds):
        rec.sleeps.append(seconds)

    fake_requests = types.ModuleType("requests")
    fake_requests.post = _send
    fake_requests.put = _send
    if _REAL_REQ_EXC is not None:
        fake_requests.exceptions = _REAL_REQ_EXC  # resolve the except clause
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
    # 'flags' key added by fix F14; step-2 'proposals' appears ONLY when a proposal
    # is harvested (none here), so the historical stats shape is preserved.
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


def test_preserve_human_harvests_moderation_proposal():
    if ba is None:
        raise unittest.SkipTest("claude-agent-sdk not installed")
    # Step 2: a search-verified moderation_proposal the agent attached to its copy
    # of a human annotation is harvested into stats['proposals'] as
    # {annotationId, fields, reason} (the mergeProposals wire contract), then the
    # restore strips it — the stored human annotation is unchanged.
    h = ann(label="h", id="aabbccddeeff", provenance="human", status="not_formalized")
    proposed = {**h, "moderation_proposal": {
        "fields": {"status": "formalized", "mathlib": {"decl": "Foo.bar"}},
        "reason": "found Foo.bar"}}
    out, stats = ba._preserve_human([h], [proposed])
    assert out == [h] and "moderation_proposal" not in out[0]
    assert stats["proposals"] == [{"annotationId": "aabbccddeeff",
                                   "fields": {"status": "formalized", "mathlib": {"decl": "Foo.bar"}},
                                   "reason": "found Foo.bar"}]
    # a stray proposal on an UNMATCHED annotation is neither harvested nor stored
    stray = {**ann(label="ai", id="ffeeddccbbaa"),
             "anchor": {"section": "Other", "snippet": "elsewhere"},
             "moderation_proposal": {"fields": {"status": "partial"}}}
    out2, stats2 = ba._preserve_human([h], [dict(h), stray])
    # no proposal harvested → the key is absent (historical shape preserved)
    assert stats2.get("proposals", []) == [] and all("moderation_proposal" not in a for a in out2)


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


# ===========================================================================
# Checkpoint-and-retry-POST durability fix
# (write_checkpoint / clear_checkpoint / list_checkpoints, the _write_article
# network-drop path, the process_* checkpoint lifecycle, and flush_pending —
# the headline recovery routine. All checkpoint I/O uses tmp pending dirs so
# nothing touches the real site/cache/.pending_posts.)
# ===========================================================================

import tempfile as _tempfile


@contextlib.contextmanager
def _tmp_pending():
    """A tmp directory for checkpoint files (never the real PENDING_DIR)."""
    with _tempfile.TemporaryDirectory() as td:
        yield Path(td)


@contextlib.contextmanager
def _patched_pending_dir():
    """Point moderate.PENDING_DIR at a tmp dir so process_review/process_new —
    which call write/clear_checkpoint WITHOUT a pending_dir arg (they default
    to the module global) — write into the sandbox. Yields the tmp Path."""
    saved = m.PENDING_DIR
    with _tempfile.TemporaryDirectory() as td:
        m.PENDING_DIR = Path(td)
        try:
            yield Path(td)
        finally:
            m.PENDING_DIR = saved


@contextlib.contextmanager
def _patched_get_article(sequence):
    """Scriptable async get_article for flush_pending (which calls m.get_article
    directly, not _http_get). `sequence` is a list of (dict_or_None, status);
    the last repeats. Records the slugs requested, in order."""
    saved = m.get_article
    queue = list(sequence)
    calls: list[str] = []

    async def fake_get_article(api_base, slug, token=None):
        calls.append(slug)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    m.get_article = fake_get_article
    try:
        yield calls
    finally:
        m.get_article = saved


def _flush_ctx():
    return SimpleNamespace(api_base="http://api.test", token="tok-flush")


# ---------------------------------------------------------------------------
# 1. write_checkpoint / clear_checkpoint / list_checkpoints
# ---------------------------------------------------------------------------

def test_checkpoint_roundtrip_and_atomic_file():
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 7}
        m.write_checkpoint("Lp_space", "post", body, "run-abc", "review",
                           pending_dir=pend)
        files = sorted(p.name for p in pend.iterdir())
        assert files == ["Lp_space.json"]            # final name only, no .tmp
        cps = m.list_checkpoints(pend)
        assert len(cps) == 1
        cp = cps[0]
        assert cp["slug"] == "Lp_space" and cp["method"] == "post"
        assert cp["kind"] == "review" and cp["run_id"] == "run-abc"
        assert cp["body"] == body
        assert isinstance(cp["saved_at"], int) and cp["saved_at"] > 0


def test_clear_checkpoint_removes_then_noop_on_missing():
    with _tmp_pending() as pend:
        m.write_checkpoint("S", "post", {}, "r", "review", pending_dir=pend)
        assert len(m.list_checkpoints(pend)) == 1
        m.clear_checkpoint("S", pending_dir=pend)
        assert m.list_checkpoints(pend) == []
        # clearing an absent slug is a silent no-op (FileNotFoundError swallowed)
        m.clear_checkpoint("S", pending_dir=pend)
        m.clear_checkpoint("never-existed", pending_dir=pend)


def test_list_checkpoints_skips_corrupt_file_not_fatal():
    with _tmp_pending() as pend:
        m.write_checkpoint("Good", "post", {"x": 1}, "r", "review",
                           pending_dir=pend)
        # a corrupt checkpoint (truncated JSON) must be dropped, not crash
        (pend / "Corrupt.json").write_text("{not json", encoding="utf-8")
        cps = m.list_checkpoints(pend)
        assert [c["slug"] for c in cps] == ["Good"]   # corrupt one skipped


def test_list_checkpoints_missing_dir_is_empty():
    with _tmp_pending() as pend:
        sub = pend / "does-not-exist"
        assert m.list_checkpoints(sub) == []          # no crash on absent dir


def test_list_checkpoints_ignores_half_written_tmp():
    # Crash-safety: a .{slug}.tmp partial write (write_checkpoint's pre-replace
    # artifact) is NOT *.json, so list_checkpoints never reads a torn file.
    with _tmp_pending() as pend:
        m.write_checkpoint("Done", "post", {"x": 1}, "r", "review",
                           pending_dir=pend)
        import urllib.parse
        tmp = pend / f".{urllib.parse.quote('Half', safe='')}.tmp"
        tmp.write_text("{partial", encoding="utf-8")   # simulate mid-replace
        cps = m.list_checkpoints(pend)
        assert [c["slug"] for c in cps] == ["Done"]    # tmp invisible


def test_checkpoint_url_unsafe_slugs_roundtrip():
    # Slugs with chars unsafe in filenames must encode and decode losslessly.
    with _tmp_pending() as pend:
        slugs = ["C*-algebra", "0.999...", "L^p_space", "a/b", "x?y", "Σ-algebra"]
        for s in slugs:
            m.write_checkpoint(s, "post", {"slug_echo": s}, "r", "review",
                               pending_dir=pend)
        got = {c["slug"]: c["body"]["slug_echo"] for c in m.list_checkpoints(pend)}
        assert set(got) == set(slugs)
        for s in slugs:
            assert got[s] == s
            # round-trip through clear, too (uses the same quoting)
            m.clear_checkpoint(s, pending_dir=pend)
        assert m.list_checkpoints(pend) == []


def test_checkpoint_overwrite_same_slug_replaces():
    # Re-checkpointing the same slug (e.g. a retried run) replaces, not appends.
    with _tmp_pending() as pend:
        m.write_checkpoint("S", "post", {"v": 1}, "r1", "review", pending_dir=pend)
        m.write_checkpoint("S", "post", {"v": 2}, "r2", "review", pending_dir=pend)
        cps = m.list_checkpoints(pend)
        assert len(cps) == 1 and cps[0]["body"] == {"v": 2}
        assert cps[0]["run_id"] == "r2"


# ---------------------------------------------------------------------------
# 2. _write_article: dropped-connection retry+sentinel (no crash)
# ---------------------------------------------------------------------------

def test_write_article_network_drop_retries_then_sentinel():
    # A raised requests.exceptions.ConnectionError retries on the 5s/15s 5xx
    # backoff schedule, then returns (0, {}, False) — NOT a crash. The (0)
    # sentinel is what tells the caller to KEEP the checkpoint.
    _require_requests_exceptions()
    ce = _REAL_REQ_EXC.ConnectionError("name resolution failed")
    with _patched_transport([("raise", ce)]) as t:
        code, payload, rebased = asyncio.run(
            m._write_article(_write_ctx(), "Lp space",
                             {"base_version": 4}, "post"))
    assert code == 0 and payload == {} and rebased is False
    assert t.sleeps == [5, 15]            # retried on the backoff schedule
    assert len(t.calls) == 3             # initial + two retries, then give up


def test_write_article_network_drop_then_recovers():
    # The connection drops once, then the retry succeeds — no sentinel, a real
    # 200 comes back (proves the retry actually re-sends, not just swallows).
    _require_requests_exceptions()
    ce = _REAL_REQ_EXC.ConnectTimeout("timed out")
    with _patched_transport([("raise", ce), (200, {"version": 6})]) as t:
        code, payload, rebased = asyncio.run(
            m._write_article(_write_ctx(), "S", {"base_version": 4}, "post"))
    assert code == 200 and payload == {"version": 6}
    assert t.sleeps == [5]              # one backoff, then success
    assert len(t.calls) == 2


def test_write_article_503_then_200_still_works():
    # Existing-style 5xx recovery still works alongside the new except clause.
    with _patched_transport([(503, {}), (200, {"version": 5})]) as t:
        code, payload, _ = asyncio.run(
            m._write_article(_write_ctx(), "S", {}, "post"))
    assert code == 200 and payload == {"version": 5}
    assert t.sleeps == [5] and len(t.calls) == 2


def test_write_article_network_drop_on_put_sentinel():
    # The sentinel path is method-agnostic — a PUT create that loses the network
    # also returns (0, {}, False) so process_new keeps its create checkpoint.
    _require_requests_exceptions()
    ce = _REAL_REQ_EXC.ConnectionError("down")
    with _patched_transport([("raise", ce)]) as t:
        code, payload, rebased = asyncio.run(
            m._write_article(_write_ctx(), "S", {}, "put"))
    assert code == 0 and payload == {} and rebased is False
    assert t.sleeps == [5, 15] and len(t.calls) == 3


# ---------------------------------------------------------------------------
# 3. process_review checkpoint lifecycle (real handler, fake agents, sandboxed
#    PENDING_DIR). Asserts the checkpoint is cleared on durable outcomes and
#    KEPT (+ rec.checkpointed) on transient ones.
# ---------------------------------------------------------------------------

def _run_review_cp(post_responses, dry_run=False, get_status=200,
                   get_payload=None):
    """_run_review, but with PENDING_DIR sandboxed to a tmp dir; returns
    (rec, transport_rec, ctx, pending_dir) so the test can inspect what
    survived on disk."""
    if get_payload is None:
        get_payload = {"annotations": _REVIEW_EXISTING, "version": 7,
                       "wikipedia_title": "T Article", "revid": 123456789}
    with _patched_pending_dir() as pend:
        with _tempfile.TemporaryDirectory() as td:
            ctx = _review_ctx(Path(td), dry_run=dry_run)
            with _patched_get(get_status, get_payload):
                with _patched_transport(post_responses) as t:
                    rec = asyncio.run(m.process_review(
                        {"slug": "T_Article", "reason": "test"}, ctx,
                        asyncio.Semaphore(1)))
            survivors = m.list_checkpoints(pend)
    return rec, t, ctx, survivors


def test_process_review_200_clears_checkpoint():
    rec, t, _, survivors = _run_review_cp([(200, {"version": 8, "matched": "5/5"})])
    assert rec["post_status"] == 200
    assert "checkpointed" not in rec
    assert survivors == []                 # cleared — durable in D1


def test_process_review_network_failure_keeps_checkpoint():
    # code 0 (dropped network, retries exhausted) → KEEP checkpoint + flag rec.
    _require_requests_exceptions()
    ce = _REAL_REQ_EXC.ConnectionError("down")
    rec, t, _, survivors = _run_review_cp([("raise", ce)])
    assert rec["checkpointed"] is True
    assert rec["error"] == "network_post_failed"
    assert rec["post_status"] == 0
    assert m.decision_outcome(rec) == "checkpointed"
    assert len(survivors) == 1 and survivors[0]["slug"] == "T_Article"
    assert survivors[0]["method"] == "post"
    # the survived checkpoint carries the finalized body, ready to flush
    assert survivors[0]["body"]["base_version"] == 7
    assert _REVIEW_EXISTING[0] in survivors[0]["body"]["annotations"]  # human kept


def test_process_review_5xx_exhausted_keeps_checkpoint():
    # 500 that never clears (last entry repeats) → 5xx-exhausted → KEEP.
    rec, t, _, survivors = _run_review_cp([(500, {"error": "boom"})])
    assert rec["checkpointed"] is True
    assert rec["post_status"] == 500
    assert m.decision_outcome(rec) == "checkpointed"
    assert len(survivors) == 1
    assert t.sleeps == [5, 15]             # exhausted the 5xx schedule first


def test_process_review_429_exhausted_keeps_checkpoint():
    rec, t, _, survivors = _run_review_cp([(429, {})])
    assert rec["checkpointed"] is True
    assert rec["error"] == "rate_limited_429"
    assert m.decision_outcome(rec) == "checkpointed"
    assert len(survivors) == 1


def test_process_review_409_clears_checkpoint():
    # A true edit conflict (twice — past the in-line rebase) is re-queued, not
    # flushed: the stale rebase isn't worth keeping. Checkpoint cleared.
    rec, t, _, survivors = _run_review_cp([(409, {"error": "stale", "version": 9})])
    assert rec["skipped"] == "stale_409"
    assert "checkpointed" not in rec
    assert survivors == []


def test_process_review_422_clears_checkpoint():
    # Unretryable finalize bug — re-POST won't help, so don't keep a checkpoint.
    rec, t, _, survivors = _run_review_cp(
        [(422, {"error": "human annotations lost", "missing": ["h"]})])
    assert rec["error"] == "human_lost_422"
    assert "checkpointed" not in rec
    assert survivors == []


def test_process_review_get_failure_writes_no_checkpoint():
    # The checkpoint is written only just before the POST — a GET failure
    # short-circuits before any agent run or checkpoint write.
    rec, t, ctx, survivors = _run_review_cp([], get_status=500, get_payload={})
    assert rec["error"] == "get_failed_500"
    assert survivors == []                 # nothing was ever checkpointed


def test_process_new_network_failure_keeps_put_checkpoint():
    # process_new mirrors the pattern with method 'put'.
    _require_requests_exceptions()
    ce = _REAL_REQ_EXC.ConnectionError("down")
    with _patched_pending_dir() as pend:
        with _tempfile.TemporaryDirectory() as td:
            annot = Path(td) / "annotations"
            cache = Path(td) / "cache"
            annot.mkdir()
            cache.mkdir()
            (cache / "New_Article.meta.json").write_text(
                json.dumps({"slug": "New_Article", "revid": 1299891234}),
                encoding="utf-8")

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
                budget_tokens=None, run_id="testrunN", seed_decls={},
                model="test-model", prompt_sha="abc123abc123",
                mathlib_sha="1234567", qid_map={},
                ba=SimpleNamespace(annotate_one=fake_annotate_one, ANNOT=annot,
                                   CACHE=cache))
            with _patched_transport([("raise", ce)]) as t:
                rec = asyncio.run(m.process_new(
                    {"slug": "New_Article", "title": "New Article"}, ctx,
                    asyncio.Semaphore(1)))
            survivors = m.list_checkpoints(pend)
    assert rec["checkpointed"] is True
    assert rec["error"] == "network_put_failed"
    assert rec["put_status"] == 0
    assert m.decision_outcome(rec) == "checkpointed"
    assert len(survivors) == 1 and survivors[0]["method"] == "put"


# ---------------------------------------------------------------------------
# 4. flush_pending — the headline recovery routine (ZERO agent tokens).
# ---------------------------------------------------------------------------

def _human(label="h", **kw):
    base = ann(label=label, id="aaaaaaaaaaaa", provenance="human")
    base.update(kw)
    return base


def test_flush_post_preserves_human_and_clears():
    # Seed a 'post' checkpoint whose body does NOT contain a human annotation;
    # current D1 state (re-GET) DOES (a human edit landed after the failed
    # write). flush must re-finalize against current state, PRESERVE the human
    # annotation verbatim, set base_version to current, re-POST, and clear.
    with _tmp_pending() as pend:
        ai = ann(label="a", id="bbbbbbbbbbbb")
        body = {"annotations": [ai], "base_version": 3,
                "comment": "ai-moderate:review:r0",
                "meta": {"run_id": "r0", "ids": {"stale": True}}}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        human = _human()
        current = {"annotations": [human], "version": 9,
                   "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]) as gets:
            with _patched_transport([(200, {"version": 10})]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 1, "skipped": 0, "failed": 0}
        assert gets == ["Foo"]                       # exactly one re-GET
        posted = t.calls[0]["body"]
        assert posted["base_version"] == 9           # rebased onto current
        assert human in posted["annotations"]        # human edit PRESERVED
        assert posted["meta"]["flushed"] is True     # flush marker on meta
        assert posted["meta"]["ids"]["human_reinserted_wire"] == 1
        assert m.list_checkpoints(pend) == []        # cleared on 200


def test_flush_post_422_clears_and_counts_failed():
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3,
                "meta": {"run_id": "r0"}}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [], "version": 9, "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]):
            with _patched_transport([(422, {"missing": ["h"]})]):
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 0, "failed": 1}
        assert m.list_checkpoints(pend) == []        # 422 → drop (won't help)


def test_flush_post_reget_none_keeps_checkpoint():
    # re-GET returns None (the article 404'd / read failed) → KEEP + failed.
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        with _patched_get_article([(None, 404)]):
            with _patched_transport([]) as t:        # empty → asserts NO POST
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 0, "failed": 1}
        assert t.calls == []                         # never re-POSTed
        assert len(m.list_checkpoints(pend)) == 1    # KEPT for next flush


def test_flush_post_409_keeps_checkpoint_deferred():
    # 409 on flush = actively edited again. KEEP + skipped (deferred, retry).
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [], "version": 9, "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]):
            with _patched_transport([(409, {"error": "stale", "version": 11})]):
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 1, "failed": 0}
        assert len(m.list_checkpoints(pend)) == 1    # KEPT (deferred)


def test_flush_post_transient_keeps_checkpoint():
    # A 500 on flush (still transient) → KEEP + failed, ready for next flush.
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [], "version": 9, "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]):
            with _patched_transport([(500, {})]):
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 0, "failed": 1}
        assert len(m.list_checkpoints(pend)) == 1


def test_flush_put_already_exists_dropped():
    # A 'put' create checkpoint whose slug now EXISTS (GET 200) is dropped
    # (skipped) with no PUT — review mode owns existing articles.
    with _tmp_pending() as pend:
        body = {"wikipedia_title": "Foo", "annotations": [], "revid": 1}
        m.write_checkpoint("Foo", "put", body, "r0", "new", pending_dir=pend)
        current = {"annotations": [], "version": 1, "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]):
            with _patched_transport([]) as t:        # empty → asserts NO PUT
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 1, "failed": 0}
        assert t.calls == []
        assert m.list_checkpoints(pend) == []        # dropped


def test_flush_put_absent_creates_and_clears():
    # A 'put' checkpoint whose slug is absent (GET 404) → PUT 201 → flushed.
    with _tmp_pending() as pend:
        body = {"wikipedia_title": "Foo", "annotations": [ann(label="a")],
                "revid": 1, "comment": "ai-create:r0"}
        m.write_checkpoint("Foo", "put", body, "r0", "new", pending_dir=pend)
        with _patched_get_article([(None, 404)]) as gets:
            with _patched_transport([(201, {"version": 1})]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 1, "skipped": 0, "failed": 0}
        assert gets == ["Foo"]
        assert len(t.calls) == 1                      # the PUT
        assert m.list_checkpoints(pend) == []         # cleared on 201


def test_flush_put_409_race_dropped():
    # A 'put' whose slug is absent on GET but 409s on PUT (a racing writer
    # created it) → cleared + skipped (not a transient failure).
    with _tmp_pending() as pend:
        body = {"wikipedia_title": "Foo", "annotations": [], "revid": 1}
        m.write_checkpoint("Foo", "put", body, "r0", "new", pending_dir=pend)
        with _patched_get_article([(None, 404)]):
            with _patched_transport([(409, {"error": "exists"})]):
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 1, "failed": 0}
        assert m.list_checkpoints(pend) == []


def test_flush_put_transient_keeps_checkpoint():
    with _tmp_pending() as pend:
        body = {"wikipedia_title": "Foo", "annotations": [], "revid": 1}
        m.write_checkpoint("Foo", "put", body, "r0", "new", pending_dir=pend)
        with _patched_get_article([(None, 404)]):
            with _patched_transport([(500, {})]):
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        assert stats == {"flushed": 0, "skipped": 0, "failed": 1}
        assert len(m.list_checkpoints(pend)) == 1     # KEPT


def test_flush_empty_dir_no_network():
    # Empty pending dir → {0,0,0} and ZERO HTTP. The empty transport list and a
    # get_article that would explode both prove no network is touched.
    with _tmp_pending() as pend:
        async def exploding_get(api_base, slug, token=None):
            raise AssertionError("flush of an empty dir must not GET")
        saved = m.get_article
        m.get_article = exploding_get
        try:
            with _patched_transport([]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        finally:
            m.get_article = saved
        assert stats == {"flushed": 0, "skipped": 0, "failed": 0}
        assert t.calls == []


def test_flush_malformed_checkpoint_dropped():
    # A checkpoint with no slug (or a non-dict body) is dropped + failed,
    # without touching the network.
    with _tmp_pending() as pend:
        (pend / "bad1.json").write_text(
            json.dumps({"method": "post", "body": {}}), encoding="utf-8")  # no slug
        (pend / "bad2.json").write_text(
            json.dumps({"slug": "X", "method": "post", "body": "not-a-dict"}),
            encoding="utf-8")
        async def exploding_get(api_base, slug, token=None):
            raise AssertionError("malformed checkpoints must not GET")
        saved = m.get_article
        m.get_article = exploding_get
        try:
            with _patched_transport([]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        finally:
            m.get_article = saved
        assert stats["failed"] == 2 and stats["flushed"] == 0
        assert t.calls == []


def test_flush_idempotent_when_still_failing():
    # Idempotence: flushing twice with a still-failing transport leaves the
    # checkpoint on disk BOTH times (recovery is retried, never lost).
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3}
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [], "version": 9, "wikipedia_title": "Foo"}
        for _ in range(2):
            with _patched_get_article([(current, 200)]):
                with _patched_transport([(500, {})]):
                    stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                        pending_dir=pend))
            assert stats == {"flushed": 0, "skipped": 0, "failed": 1}
            assert len(m.list_checkpoints(pend)) == 1   # survives every attempt


def test_flush_mixed_batch_independent_outcomes():
    # Several checkpoints in one flush: a successful post, a deferred 409 post,
    # and a put that already exists — each handled independently, stats summed.
    # (Slugs are processed in sorted filename order: A_post, B_409, C_put.)
    # NB: B_409's flush re-POST 409s, which fires the single in-line F9 rebase
    # inside _write_article — one extra re-GET + re-POST — before the terminal
    # 409 is returned as 'deferred'. So GETs = A(1)+B(2)+C(1)=4, POSTs=A(1)+B(2).
    with _tmp_pending() as pend:
        m.write_checkpoint("A_post", "post", {"annotations": [], "base_version": 1},
                           "r", "review", pending_dir=pend)
        m.write_checkpoint("B_409", "post", {"annotations": [], "base_version": 1},
                           "r", "review", pending_dir=pend)
        m.write_checkpoint("C_put", "put", {"wikipedia_title": "C", "annotations": [],
                           "revid": 1}, "r", "new", pending_dir=pend)
        cur = lambda v: {"annotations": [], "version": v, "wikipedia_title": "x"}
        # GET order: A_post flush-GET, B_409 flush-GET, B_409 inline-rebase GET,
        # C_put existence GET.
        with _patched_get_article([(cur(5), 200), (cur(5), 200),
                                   (cur(5), 200), (cur(1), 200)]) as gets:
            # POST order: A_post→200, B_409→409, B_409 rebase re-POST→409.
            with _patched_transport([(200, {"version": 6}),
                                     (409, {"error": "stale"})]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        # A_post → flushed; B_409 → deferred (skipped, kept); C_put → already
        # exists (skipped, cleared). Both deferrals/drops count as 'skipped'.
        assert stats == {"flushed": 1, "skipped": 2, "failed": 0}
        assert gets == ["A_post", "B_409", "B_409", "C_put"]
        assert len(t.calls) == 3                     # A(1) + B(2 incl. rebase)
        survivors = {c["slug"] for c in m.list_checkpoints(pend)}
        assert survivors == {"B_409"}                # only the deferred one stays


# ---------------------------------------------------------------------------
# 5. decision_outcome: 'checkpointed' is distinct from error/posted.
# ---------------------------------------------------------------------------

def test_decision_outcome_checkpointed():
    # The transient-write-failure rec maps to 'checkpointed', NOT error/posted —
    # even though it also carries an `error` string and a 0/429/5xx post_status.
    assert m.decision_outcome(
        {"checkpointed": True, "error": "network_post_failed",
         "post_status": 0}) == "checkpointed"
    assert m.decision_outcome(
        {"checkpointed": True, "error": "rate_limited_429",
         "post_status": 429}) == "checkpointed"
    assert m.decision_outcome(
        {"checkpointed": True, "error": "post_500: {}",
         "post_status": 500}) == "checkpointed"
    assert m.decision_outcome(
        {"checkpointed": True, "error": "network_put_failed",
         "put_status": 0}) == "checkpointed"
    # checkpointed takes precedence over the error mapping (no false 'error')
    for rec in ({"checkpointed": True, "error": "network_post_failed"},):
        assert m.decision_outcome(rec) != "error"
        assert m.decision_outcome(rec) != "posted"
    # a real (non-checkpointed) error still maps to 'error', not 'checkpointed'
    assert m.decision_outcome({"error": "agent1_no_json"}) == "error"
    # a clean post is 'posted', never 'checkpointed'
    assert m.decision_outcome({"post_status": 200}) == "posted"


# ---------------------------------------------------------------------------
# Adversarial-review demonstrations (checkpoint lifecycle bug hunt).
# These DOCUMENT what the lead claimed and pin the behavior the trace relied
# on. None is a FAIL of the current code; if one ever flips, it surfaces the
# regression the prose warned about. (See the agent report for the written-up
# findings with severity + file:line.)
# ---------------------------------------------------------------------------

def test_adversarial_a_no_leak_every_post_path_clears_or_flags():
    # (a) Leak hunt: across the four process_review POST outcomes, the
    # checkpoint is EITHER cleared OR intentionally kept with rec.checkpointed.
    # There is no path that writes a checkpoint and silently strips it.
    cleared = [
        ([(200, {"version": 8, "matched": "5/5"})], False),   # 200 → clear
        ([(409, {"error": "stale", "version": 9})], False),   # 409 → clear
        ([(422, {"missing": ["h"]})], False),                 # 422 → clear
    ]
    for resp, _ in cleared:
        rec, _t, _c, survivors = _run_review_cp(resp)
        assert survivors == [], resp
        assert not rec.get("checkpointed"), resp
    # kept paths are flagged (tested above) — assert the invariant directly:
    rec, _t, _c, survivors = _run_review_cp([(500, {})])
    assert bool(survivors) == bool(rec.get("checkpointed")) is True


def test_adversarial_b_flush_rebases_onto_current_not_stale():
    # (b) Clobber hunt: flush sets base_version to the CURRENT D1 version from
    # the fresh re-GET, never the stale checkpoint base_version — so a CAS
    # write can't overwrite an intervening human edit blindly.
    with _tmp_pending() as pend:
        body = {"annotations": [ann(label="a")], "base_version": 3}  # stale base
        m.write_checkpoint("Foo", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [_human()], "version": 99,
                   "wikipedia_title": "Foo"}
        with _patched_get_article([(current, 200)]):
            with _patched_transport([(200, {"version": 100})]) as t:
                asyncio.run(m.flush_pending(_flush_ctx(), pending_dir=pend))
        assert t.calls[0]["body"]["base_version"] == 99   # current, not 3
        assert _human() in t.calls[0]["body"]["annotations"]


def test_adversarial_d_flush_409_loop_is_bounded_per_run():
    # (d) Infinite-loop hunt: a 409 on flush KEEPS the checkpoint, but flush
    # processes each checkpoint a BOUNDED number of times per invocation — it
    # iterates a snapshot list, not a re-polled queue. The flush's own re-GET
    # builds a base_version body, so the post_article call ALSO runs the single
    # in-line F9 rebase (one extra re-GET + re-POST) before the terminal 409
    # propagates back as 'deferred'. That's exactly 2 GETs and 2 POSTs for one
    # hot article, then it defers to the NEXT run — no spin within one flush.
    with _tmp_pending() as pend:
        body = {"annotations": [], "base_version": 1}
        m.write_checkpoint("Hot", "post", body, "r0", "review", pending_dir=pend)
        current = {"annotations": [], "version": 5, "wikipedia_title": "Hot"}
        get_calls = {"n": 0}
        async def counting_get(api_base, slug, token=None):
            get_calls["n"] += 1
            return current, 200
        saved = m.get_article
        m.get_article = counting_get
        try:
            with _patched_transport([(409, {"error": "stale"})]) as t:
                stats = asyncio.run(m.flush_pending(_flush_ctx(),
                                                    pending_dir=pend))
        finally:
            m.get_article = saved
        # BOUNDED: flush re-GET (1) + F9 in-line rebase re-GET (1) = 2; two
        # POSTs; then the second 409 terminates. Not unbounded.
        assert get_calls["n"] == 2
        assert len(t.calls) == 2
        assert stats["skipped"] == 1
        assert len(m.list_checkpoints(pend)) == 1   # deferred to next run


def test_adversarial_f_checkpointed_sentinel_is_a_job_error_not_window():
    # (f) The (0) network sentinel rec has rec.error set, so run_jobs counts it
    # as a job error (n_err) — but 'network_post_failed' is NOT a window-type
    # error string, so it does NOT trip the consec-error abort. A whole batch
    # of network drops must not be mistaken for window exhaustion.
    jobs = [{"slug": f"s{i}"} for i in range(m.ABORT_AFTER + 2)]
    script = [{"checkpointed": True, "error": "network_post_failed",
               "post_status": 0}] * len(jobs)
    with _patched_log() as log:
        rc, stats = asyncio.run(m.run_jobs(
            jobs, _jobs_ctx(), _scripted_process(script)))
        lines = [json.loads(l) for l in log.run.read_text().splitlines()]
    assert rc == 0                               # NOT aborted (network != window)
    assert stats["processed"] == len(jobs)       # every job ran
    assert stats["errors"] == len(jobs)          # each counted as an error
    assert all(m.decision_outcome(rec) == "checkpointed" for rec in lines)


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
