"""The survey behind both the bare `wikilean` dashboard and `wikilean status`.

One function gathers it (:func:`survey`, read-only: four public GETs and one
authenticated GET of `/api/auth/me`) and one renders it, so the launcher and
the scriptable command can never disagree about what is open.

The rate budget shown is **ours, measured here**. WikiLean publishes the rule —
all of one person's agents share 30 writes a minute (docs/AGENT-CONTRIBUTIONS.md)
— but exposes no meter, so the honest thing to show is how many writes this
machine sent in the last minute and what the documented ceiling is.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import asdict, dataclass, field

from . import journal, state
from .config import Config
from .secrets import fingerprint, load_token
from .site import Site, SiteError
from .store import article_order, index_entries, open_work

# docs/AGENT-CONTRIBUTIONS.md: "30 writes a minute across saves, Brain writes
# and overlay tags together", shared by every agent one person minted.
WRITES_PER_MINUTE = 30


@dataclass
class Survey:
    base_url: str
    reachable: bool = False
    error: str = ""
    campaign: str = ""
    account: str = ""
    role: str = ""
    owner: str = ""
    token: str = ""
    work: dict = field(default_factory=dict)
    articles: list[dict] = field(default_factory=list)
    writes_last_minute: int = 0
    writes_per_minute: int = WRITES_PER_MINUTE
    paused_for: float = 0.0
    rounds: list[dict] = field(default_factory=list)
    locate_ready: bool = False


def recent_writes(now: float | None = None) -> int:
    now = time.time() if now is None else now
    count = 0
    for entry in journal.read(200, event="request"):
        stamp = entry.get("t")
        if not isinstance(stamp, str):
            continue
        try:
            when = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if now - when <= 60:
            count += 1
    return count


def survey(config: Config, *, site_factory=Site) -> Survey:
    from .locate import preconditions

    stored = load_token()
    out = Survey(config.base_url, token=(f"{stored.source} ({fingerprint(stored.token)})" if stored else ""))
    out.paused_for = max(0.0, state.pause_until() - time.time())
    out.rounds = journal.read(5, event="round")
    out.writes_last_minute = recent_writes()
    out.locate_ready = config.locate_enabled and not preconditions(config)
    try:
        site = site_factory(config.base_url, stored.token if stored else None)
        current = site.current()
        out.reachable = True
        out.campaign = str(current.get("campaign") or "")
        entries = state.cached_index()
        if entries is None:
            shards = [s for s in (current.get("index") or {}).get("shards", []) if isinstance(s, str)]
            entries = index_entries(site.index_shard(shard) for shard in shards)
            state.cache_index(entries, campaign=out.campaign)
        out.work = open_work(entries)
        out.articles = article_order(entries, skip=config.skip_pageids)[:5]
        if stored is not None:
            user = site.me()
            out.account = str(user.get("name") or "")
            out.role = str(user.get("role") or "")
            out.owner = str(user.get("ownerId") or "")
    except SiteError as exc:
        out.error = str(exc)
    return out


def render(found: Survey, *, log=print) -> None:
    log("")
    log("  WikiLean contributor")
    log(f"  site     {found.base_url}" + (f"  (campaign {found.campaign})" if found.campaign else ""))
    if not found.reachable:
        log(f"  site     unreachable: {found.error}")
    if found.account:
        owner = f", owned by {found.owner}" if found.owner else ""
        log(f"  account  {found.account} — role {found.role}{owner}")
    elif found.token:
        log(f"  account  not confirmed{': ' + found.error if found.error else ''}")
    else:
        log("  account  no token stored — run `wikilean login`")
    if found.token:
        log(f"  token    {found.token}")
    log("")
    if found.work:
        log(f"  open     {found.work.get('not_formalized', 0)} passages marked not formalized, "
            f"{found.work.get('partial', 0)} partial, across {found.work.get('articles', 0)} articles")
    log(f"  budget   {found.writes_last_minute} write(s) sent in the last minute "
        f"of {found.writes_per_minute} your account may make")
    if found.paused_for > 0:
        log(f"  waiting  the site asked us to hold off for another {found.paused_for:.0f}s")
    log(f"  locate   {'ready' if found.locate_ready else 'off (ask first; see worker/README.md)'}")
    log("")
    if found.articles:
        log("  most unsettled articles (passages marked not formalized, and partial)")
        for entry in found.articles:
            log(f"    {str(entry.get('not_formalized', 0)).rjust(5)} + "
                f"{str(entry.get('partial', 0)).ljust(4)}  {entry.get('title')}")
        log("")
    if found.rounds:
        log("  last rounds")
        for entry in found.rounds:
            unit = entry.get("unit") or "-"
            result = entry.get("result") or entry.get("rejected") or entry.get("error") or ""
            sent = "sent" if entry.get("sent") else ("dry run" if entry.get("dry_run") else "no write")
            log(f"    {entry.get('t')}  {str(unit).ljust(7)} {sent:9} {str(result)[:70]}")
        log("")


def as_dict(found: Survey) -> dict:
    return asdict(found)


MENU: tuple[tuple[str, str, list[str]], ...] = (
    ("1", "one round, review first", ["work"]),
    ("2", "one round, review only", ["work", "--unit", "review"]),
    ("3", "one round, tag only", ["work", "--unit", "tag"]),
    ("4", "one round, printing what it would send", ["work", "--dry-run"]),
    ("5", "keep working, one unit a round", ["work", "--loop"]),
    ("6", "check this machine", ["doctor"]),
)


def menu(found: Survey, *, log=print) -> None:
    log("  what would you like to do?")
    for key, label, argv in MENU:
        log(f"    {key}  {label.ljust(38)} wikilean {' '.join(argv)}")
    log("    q  quit")
    log("")
