"""One round: find one unit of work, judge it, and send at most one write.

The shape, and the three promises it keeps:

* **Exactly one unit per round.** The units are tried in the order
  ``review`` → ``tag`` → ``locate``; the first that has a candidate is the
  round, and the round ends when its write is sent or refused. Nothing loops
  inside a round.
* **Nothing is sent that was not validated here.** The model's answer passes
  through judge.py's vocabulary checks and, for any declaration it proposed,
  the site's own existence oracle, before a request body exists at all. A
  refusal is journalled and the round ends with no write.
* **``--dry-run`` sends nothing.** It builds the same request, prints it, and
  stops. The one code path that can reach the network for a write is the
  ``if not options.dry_run`` branch below.

Back-off: a 429 or a 503 from the site sets a pause in state.py, which
``--loop`` honours and the next round refuses to start inside. One write per
round means a refused write is never retried behind the contributor's back.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from . import journal, state
from .config import MIN_INTERVAL, UNITS, Config
from .judge import (Action, Rejected, Unverifiable, check_exists, plan_review,
                    plan_tag, read_review, read_tag, review_prompt, tag_prompt)
from .model import ModelError, ask
from .site import Limited, Site, SiteError
from .store import (Candidate, Overlay, article_order, index_entries,
                    libraries_of, library_roots, review_candidates,
                    tag_candidates, vouched_declarations)

# How many articles one round will open looking for a candidate. Each is two
# small public reads; a higher number makes a round slower, not better.
MAX_ARTICLES_PER_ROUND = 8

# What the site answers when the same request would always be refused
# (articles.ts): a body it will not parse, an owner who has not accepted CC0,
# an unknown pageid or passage key, a /1 record set with no passage keys.
# Spending another model call on that candidate buys nothing, so it is written
# off. 409 is deliberately absent: it is the compare-and-set losing, or the
# campaign having moved, and the next round re-reads and gets it right.
PERMANENT_WRITE_STATUSES = frozenset({400, 403, 404, 422})
# Of those, the ones that are about the ACCOUNT and not about this candidate:
# every later round would buy a model call and be refused the same way, so
# `--loop` stops instead of backing off.
ACCOUNT_REFUSAL_STATUSES = frozenset({401, 403})
# ...except this one, which is about the row (articles.ts
# AGENT_HUMAN_ROW_REFUSED): a person wrote it between our read and our write.
HUMAN_ROW_REFUSAL = "agents may not change or remove an editor's tag"

# cli_main maps these; `work --loop` reads them to decide how to settle.
EX_OK = 0
EX_FAILED = 1
EX_USAGE = 2
EX_NOTHING = 75       # no eligible work anywhere we looked
EX_WAIT = 76          # the site asked us to wait (429, or the write brake)


@dataclass
class Options:
    unit: str | None = None
    dry_run: bool = False
    enable_locate: bool = False
    verbose: bool = True


@dataclass
class Outcome:
    code: int
    unit: str | None = None
    summary: str = ""
    wrote: bool = False
    candidate: Candidate | None = None
    wait: float = 0.0
    details: dict = field(default_factory=dict)


def units_wanted(options: Options, config: Config) -> list[str]:
    if options.unit:
        return [options.unit]
    wanted = ["review", "tag"]
    if config.locate_enabled and options.enable_locate:
        wanted.append("locate")
    return wanted


def _seen_key(candidate: Candidate) -> str:
    return f"{candidate.pageid}:{candidate.passage_key}:{candidate.reason}:{candidate.pair_key or '-'}"


@dataclass
class Found:
    candidate: Candidate
    libraries: dict[str, str]
    overlay: Overlay
    # The article's universe roots, and the (library, declaration) pairs its
    # own records already carry: between them they are how a proposed
    # declaration is checked against the library the model named.
    roots: dict[str, tuple[str, ...]] = field(default_factory=dict)
    vouched: dict[str, set[str]] = field(default_factory=dict)


def find_candidate(site: Site, config: Config, wanted: list[str], *,
                   log=lambda _m: None) -> Found | None:
    """Walk the article index from the cursor and return one candidate.

    The unit order is global, not per article: a review candidate in the
    eighth article still beats a tag candidate in the first. Review work short
    -circuits the walk as soon as it is found; a tag candidate is remembered
    and only used when the whole walk turned up no review work.
    """
    entries = state.cached_index()
    if entries is None:
        current = site.current()
        shards = [s for s in (current.get("index") or {}).get("shards", []) if isinstance(s, str)]
        entries = index_entries(site.index_shard(shard) for shard in shards)
        state.cache_index(entries, campaign=str(current.get("campaign") or ""))
    ordered = article_order(entries, skip=config.skip_pageids)
    if not ordered:
        return None
    seen = state.seen_keys()
    start = state.cursor() % len(ordered)
    fallback: tuple[int, Candidate, dict, Overlay] | None = None
    for step in range(min(MAX_ARTICLES_PER_ROUND, len(ordered))):
        position = (start + step) % len(ordered)
        entry = ordered[position]
        pageid = int(entry.get("pageid") or 0)
        if pageid <= 0:
            continue
        log(f"  looking at {entry.get('title')} ({pageid})")
        try:
            article = site.article(pageid)
            overlay = Overlay.of(site.overlay(pageid))
        except SiteError as exc:
            if isinstance(exc, Limited):
                raise
            log(f"  skipping {pageid}: {exc}")
            continue
        unseen = {
            unit: [c for c in finder(article, overlay) if _seen_key(c) not in seen]
            for unit, finder in (("review", review_candidates), ("tag", tag_candidates))
            if unit in wanted
        }
        if unseen.get("review"):
            state.set_cursor(position)
            return _found(site, unseen["review"][0], article, overlay)
        if unseen.get("tag") and fallback is None:
            fallback = (position, unseen["tag"][0], article, overlay)
        # Nothing to review here; move the cursor past it so the next round
        # starts further on.
        state.set_cursor(position + 1)
    if fallback is not None:
        position, candidate, article, overlay = fallback
        state.set_cursor(position)
        return _found(site, candidate, article, overlay)
    return None


def _found(site: Site, candidate: Candidate, article: dict, overlay: Overlay) -> Found:
    universe = site.universe(str((article.get("universe") or {}).get("sha256") or ""))
    return Found(candidate, libraries_of(universe), overlay,
                 roots=library_roots(universe), vouched=vouched_declarations(article))


def run_round(site: Site, config: Config, options: Options, *, log=print) -> Outcome:
    """One round, start to finish."""
    waiting = state.pause_until() - time.time()
    if waiting > 0 and not options.dry_run:
        return Outcome(EX_WAIT, summary=f"the site asked us to wait {waiting:.0f}s more", wait=waiting)

    wanted = units_wanted(options, config)
    if wanted == ["locate"] or (options.unit == "locate"):
        from .locate import run_locate

        return run_locate(config, options, log=log)

    run_id = "wikilean-cli-" + uuid.uuid4().hex[:12]
    try:
        found = find_candidate(site, config, wanted, log=lambda m: log(m) if options.verbose else None)
    except Limited as limited:
        journal.write("round", ok=False, stage="find", status=limited.status,
                      retry_after=limited.retry_after, error=str(limited), run_id=run_id)
        return _limited(limited, "finding work")
    except SiteError as exc:
        journal.write("round", ok=False, stage="find", status=exc.status, error=str(exc), run_id=run_id)
        return Outcome(EX_FAILED, summary=str(exc), details={"status": exc.status})

    if found is None:
        if "locate" in wanted:
            from .locate import run_locate

            return run_locate(config, options, log=log)
        journal.write("round", ok=True, unit=None, result="no eligible work")
        return Outcome(EX_NOTHING, summary="no eligible review or tag work in the articles looked at")

    candidate, libraries = found.candidate, found.libraries
    log(f"\n{candidate.unit}: {candidate.label()}")
    journal.write("round-start", unit=candidate.unit, reason=candidate.reason, pageid=candidate.pageid,
                  passage=candidate.passage.get("item"), pair_key=candidate.pair_key, run_id=run_id,
                  dry_run=options.dry_run)

    prompt = (review_prompt(candidate) if candidate.unit == "review"
              else tag_prompt(candidate, libraries))
    try:
        answer = ask(prompt, model=config.model, timeout=config.model_timeout)
    except ModelError as exc:
        journal.write("round", ok=False, unit=candidate.unit, stage="model", error=str(exc), run_id=run_id)
        log(f"  the model call failed: {exc}")
        return Outcome(EX_FAILED, unit=candidate.unit, summary=f"model call failed: {exc}", candidate=candidate)

    try:
        action = _decide(candidate, answer.value, libraries, site, found.overlay,
                         model=answer.model, run_id=run_id, roots=found.roots, vouched=found.vouched)
    except Limited as limited:
        # Checking the declaration is a read, and the site can rate-limit it or
        # pause. That ends the round like any other wait, with the pause
        # recorded, rather than escaping run_round and killing `--loop`.
        journal.write("round", ok=False, unit=candidate.unit, stage="validate", status=limited.status,
                      retry_after=limited.retry_after, error=str(limited), run_id=run_id)
        return _limited(limited, "checking the declaration")
    except SiteError as exc:
        journal.write("round", ok=False, unit=candidate.unit, stage="validate", status=exc.status,
                      error=str(exc), run_id=run_id)
        log(f"  the declaration could not be checked: {exc}")
        return Outcome(EX_FAILED, unit=candidate.unit, candidate=candidate,
                       summary=f"could not check the declaration: {exc}", details={"status": exc.status})
    except Rejected as exc:
        # Out of vocabulary, out of the universe, a name in another library, or
        # one nothing public can confirm: skipped and written down, never sent.
        unverifiable = isinstance(exc, Unverifiable)
        journal.write("round", ok=True, unit=candidate.unit, stage="validate", rejected=str(exc),
                      unverifiable=unverifiable, pageid=candidate.pageid, run_id=run_id, sent=False,
                      answer=answer.value)
        state.mark_seen(_seen_key(candidate))
        if unverifiable:
            log(f"  nothing is written: {exc}")
            return Outcome(EX_OK, unit=candidate.unit, summary=f"not checkable here: {exc}",
                           candidate=candidate)
        log(f"  the model's answer was refused: {exc}")
        return Outcome(EX_OK, unit=candidate.unit, summary=f"answer refused: {exc}", candidate=candidate)

    if not action.writes:
        journal.write("round", ok=True, unit=candidate.unit, pageid=candidate.pageid, run_id=run_id,
                      sent=False, result=action.summary)
        state.mark_seen(_seen_key(candidate))
        log(f"  {action.summary}")
        return Outcome(EX_OK, unit=candidate.unit, summary=action.summary, candidate=candidate)

    plan = site.plan(action.method, action.path, action.body)
    if options.dry_run:
        log("\n--- would send -------------------------------------------------")
        log(plan.render())
        log("--- and nothing else -------------------------------------------\n")
        journal.write("round", ok=True, unit=candidate.unit, pageid=candidate.pageid, run_id=run_id,
                      dry_run=True, sent=False, method=action.method, path=action.path, body=action.body,
                      result=action.summary)
        return Outcome(EX_OK, unit=candidate.unit, summary=f"dry run: {action.summary}", candidate=candidate)

    journal.write("request", unit=candidate.unit, method=action.method, path=action.path,
                  body=action.body, run_id=run_id)
    try:
        response = site.write(plan)
    except Limited as limited:
        journal.write("round", ok=False, unit=candidate.unit, stage="write", status=limited.status,
                      retry_after=limited.retry_after, error=str(limited), run_id=run_id)
        return _limited(limited, action.summary)
    except SiteError as exc:
        permanent = exc.status in PERMANENT_WRITE_STATUSES
        journal.write("round", ok=False, unit=candidate.unit, stage="write", status=exc.status,
                      error=str(exc), permanent=permanent, run_id=run_id)
        if permanent:
            # Nothing a later round changes, so this candidate is written off:
            # otherwise `--loop` re-judges the same passage every round and
            # buys another model call for every refusal.
            state.mark_seen(_seen_key(candidate))
        log(f"  the site refused the write: {exc}")
        return Outcome(EX_FAILED, unit=candidate.unit, summary=f"refused: {exc}", candidate=candidate,
                       details={"status": exc.status, "error": str(exc)})

    state.mark_seen(_seen_key(candidate))
    journal.write("round", ok=True, unit=candidate.unit, pageid=candidate.pageid, run_id=run_id,
                  sent=True, status=response.status, result=action.summary, response=response.data)
    log(f"  sent: {action.summary}")
    return Outcome(EX_OK, unit=candidate.unit, summary=action.summary, wrote=True, candidate=candidate)


def _decide(candidate: Candidate, answer: dict, libraries: dict[str, str], site: Site,
            overlay: Overlay, *, model: str, run_id: str,
            roots: dict[str, tuple[str, ...]] | None = None,
            vouched: dict[str, set[str]] | None = None) -> Action:
    if candidate.unit == "review":
        review = read_review(answer, candidate, libraries)
        if review.verdict == "missed":
            check_exists(str(review.declaration), str(review.library), libraries, site.declaration,
                         roots=roots, vouched=vouched)
        return plan_review(review, candidate, libraries, model=model, run_id=run_id, overlay=overlay)
    tag = read_tag(answer, libraries)
    if not tag.skip:
        check_exists(str(tag.declaration), str(tag.library), libraries, site.declaration,
                     roots=roots, vouched=vouched)
    return plan_tag(tag, candidate, libraries, model=model, run_id=run_id, overlay=overlay)


def _is_account_refusal(outcome: Outcome) -> bool:
    """Did the site refuse the write because of WHO is writing, not WHAT?"""
    if outcome.details.get("status") not in ACCOUNT_REFUSAL_STATUSES:
        return False
    return HUMAN_ROW_REFUSAL not in str(outcome.details.get("error") or "")


def _limited(limited: Limited, what: str) -> Outcome:
    state.pause_until(time.time() + limited.retry_after)
    reason = "editing is paused site-wide" if limited.paused else "rate limited"
    return Outcome(EX_WAIT, summary=f"{reason} while {what}: waiting {limited.retry_after:.0f}s",
                   wait=limited.retry_after)


# ---- the loop ----------------------------------------------------------------


def run_loop(site: Site, config: Config, options: Options, *, log=print,
             sleep=time.sleep, rounds: int | None = None) -> int:
    """Round after round, settling between them. Ctrl-C stops after the round."""
    completed = 0
    failures = 0
    while rounds is None or completed < rounds:
        try:
            outcome = run_round(site, config, options, log=log)
        except KeyboardInterrupt:
            log("\nstopped.")
            return 130
        completed += 1
        if outcome.summary:
            log(f"[{outcome.unit or 'round'}] {outcome.summary}")
        if outcome.code == EX_WAIT:
            delay = max(outcome.wait, 1.0)
        elif outcome.code == EX_NOTHING:
            # Nothing to do is not a failure; come back when the index cache
            # has expired rather than spinning through the same articles.
            delay = max(config.interval * 4, 300.0)
        elif outcome.code == EX_FAILED:
            if _is_account_refusal(outcome):
                # Backing off from this would be four paid model calls an hour
                # for ever, every one of them refused in the same way.
                log("  the site refuses this agent's writes, so there is no point going round again:")
                log(f"    {outcome.summary}")
                log("  the person who minted this agent may need to accept the CC0 terms at "
                    "/agents, or the token may have been revoked. `wikilean doctor` says more.")
                return EX_FAILED
            failures += 1
            delay = min(max(config.interval, MIN_INTERVAL) * (2 ** min(failures, 5)), 900.0)
        else:
            failures = 0
            # A floor, so a "interval": 0 in config.json cannot spin a round
            # (and a paid model call) straight into the account's write budget.
            delay = max(config.interval, MIN_INTERVAL)
        if rounds is not None and completed >= rounds:
            break
        log(f"  next round in {delay:.0f}s (Ctrl-C to stop)")
        try:
            sleep(delay)
        except KeyboardInterrupt:
            log("\nstopped.")
            return 130
    return EX_OK


def available_units(config: Config, options: Options) -> list[str]:
    return [unit for unit in UNITS if unit in units_wanted(options, config)]
