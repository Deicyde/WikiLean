"""`wikilean` — the command line.

``main`` parses, ``_dispatch`` is a flat chain of branches, and every branch is
a few lines over a module that does the work. Bare ``wikilean`` is the
dashboard: on a terminal it offers the launcher and hands the terminal to the
command you pick; piped, it prints the same survey and exits, so a script can
read it.

Exit codes, because ``work --loop`` and any wrapper around it read them:

    0   the round did its one unit (or there was nothing to send)
    1   something failed
    2   the command was used wrongly
    75  no eligible work in the articles it looked at
    76  the site asked us to wait (rate limit, or the site-wide write brake)
    130 interrupted
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import VERSION, journal
from .config import UNITS, Config, config_problem
from .secrets import (ENV_TOKEN, TokenError, delete_token, fingerprint,
                      load_token, read_token_from, store_token, valid_token)
from .site import Site, SiteError
from .work import EX_FAILED, EX_OK, EX_USAGE, Options, run_loop, run_round


# Printed by every `work` that has the web tools (Jack, 2026-11-12: "Keep fetch
# open, warn"): fetch refuses localhost, names without a dot and plain HTTP, but
# not an HTTPS page an office network or VPN can reach.
WEB_NOTICE = ("web: your Claude may search the web and fetch pages. On an office network or a VPN, "
              "run with --no-web (worker/README.md, Safety).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wikilean",
        description="Contribute to WikiLean from your own machine, with your own Claude.",
        epilog="Run `wikilean` with no command for the dashboard.",
    )
    parser.add_argument("--version", action="version", version=f"wikilean {VERSION}")
    parser.add_argument("--base-url", default="", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    login = sub.add_parser("login", help="store your WikiLean agent token (minted at /agents)")
    login.add_argument("--stdin", action="store_true", help="read the token from standard input")
    login.add_argument("--store", choices=("auto", "keychain", "file"), default="auto",
                       help="where to keep it (default: the macOS Keychain, else a 0600 file)")
    sub.add_parser("logout", help="remove the stored token")

    doctor = sub.add_parser("doctor", help="check this machine; spends nothing")
    doctor.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="what is open, what your budget is, what you last did")
    status.add_argument("--json", action="store_true")

    work = sub.add_parser("work", help="do exactly one unit of work, or keep doing them")
    work.add_argument("--loop", action="store_true", help="round after round until you stop it")
    work.add_argument("--unit", choices=UNITS, help="only this unit")
    work.add_argument("--dry-run", action="store_true",
                      help="print exactly what would be sent, send nothing to WikiLean "
                           "(your Claude may still search and fetch, unless --no-web)")
    work.add_argument("--enable-locate", action="store_true",
                      help="allow the locate unit (also needs \"locate_enabled\" in the config)")
    work.add_argument("--no-web", action="store_true",
                      help="judge without web search or fetch: the model gets no tools at all")
    work.add_argument("--rounds", type=int, default=None, help=argparse.SUPPRESS)

    journal_command = sub.add_parser("journal", help="the local record of every round and request")
    journal_command.add_argument("-n", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load()
    if args.base_url:
        config.base_url = args.base_url.rstrip("/")
    try:
        return _dispatch(args, config, parser)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 130
    except SiteError as exc:
        print(f"wikilean: {exc}", file=sys.stderr)
        return EX_FAILED
    except TokenError as exc:
        print(f"wikilean: {exc}", file=sys.stderr)
        return EX_USAGE


def _dispatch(args, config: Config, parser: argparse.ArgumentParser) -> int:
    if args.command is None:
        return _dashboard(config)
    if args.command == "login":
        return _login(args)
    if args.command == "logout":
        return _logout()
    if args.command == "doctor":
        return _doctor(args, config)
    if args.command == "status":
        return _status(args, config)
    if args.command == "work":
        return _work(args, config)
    if args.command == "journal":
        for entry in journal.read(max(1, args.n)):
            print(json.dumps(entry, sort_keys=True))
        return EX_OK
    parser.print_help()
    return EX_USAGE


# ---- commands ----------------------------------------------------------------


def _login(args) -> int:
    stream = sys.stdin
    token = read_token_from(stream) if not args.stdin else stream.readline().strip()
    if not token:
        print("wikilean: no token given", file=sys.stderr)
        return EX_USAGE
    if not valid_token(token):
        # Never echo what was typed, even to say it is wrong.
        print("wikilean: that is not a WikiLean agent token. Mint one at "
              "https://wikilean.jackmccarthy.org/agents — it starts 'wla_'.", file=sys.stderr)
        return EX_USAGE
    where = store_token(token, prefer=args.store)
    journal.write("login", store=where, fingerprint=fingerprint(token))
    print(f"stored in the {where}. `wikilean doctor` will check it.")
    return EX_OK


def _logout() -> int:
    removed = delete_token()
    journal.write("logout", removed=removed)
    if not removed:
        print("no stored token to remove.")
        if os.environ.get(ENV_TOKEN):
            print(f"note: ${ENV_TOKEN} is set in this shell, and that is what would be used.")
        return EX_OK
    print("removed from the " + " and the ".join(removed) + ".")
    return EX_OK


def _doctor(args, config: Config) -> int:
    from .doctor import as_json, report, run_checks

    checks = run_checks(config)
    if args.json:
        print(as_json(checks))
        return 0 if all(check.ok or not check.fatal for check in checks) else 1
    return report(checks)


def _status(args, config: Config) -> int:
    from .dashboard import as_dict, render, survey

    found = survey(config)
    if args.json:
        print(json.dumps(as_dict(found), indent=2, sort_keys=True))
    else:
        render(found)
    return EX_OK if found.reachable else EX_FAILED


def _work(args, config: Config) -> int:
    options = Options(unit=args.unit, dry_run=args.dry_run, enable_locate=args.enable_locate,
                      web=not args.no_web)
    problem = config_problem()
    if problem:
        print(f"wikilean: {problem}", file=sys.stderr)
    if options.web and config.web:
        print(WEB_NOTICE, file=sys.stderr)
    stored = load_token()
    if stored is None and not options.dry_run:
        print("wikilean: no agent token. Run `wikilean login` (mint one at /agents).", file=sys.stderr)
        return EX_USAGE
    site = Site(config.base_url, stored.token if stored else None)
    if args.loop:
        return run_loop(site, config, options, rounds=args.rounds)
    return run_round(site, config, options).code


def _dashboard(config: Config) -> int:
    from .dashboard import MENU, menu, render, survey

    found = survey(config)
    render(found)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("  (not a terminal — snapshot only; `wikilean status --json` in scripts)\n")
        return EX_OK if found.reachable else EX_FAILED
    menu(found)
    try:
        choice = input("  > ").strip().lower()
    except EOFError:
        return EX_OK
    for key, _label, argv in MENU:
        if choice == key:
            print(f"\n  running: wikilean {' '.join(argv)}\n")
            # Hand the terminal to the command, exactly as it would have been
            # typed: what runs is what the menu printed.
            return main(argv)
    return EX_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
