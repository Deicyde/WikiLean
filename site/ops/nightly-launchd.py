#!/usr/bin/env python3
"""Render, check, or install portable WikiLean LaunchAgent plists.

`install` only writes plist files.  It never invokes launchctl or starts a job.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape


OPS_DIR = Path(__file__).resolve().parent
REPO = OPS_DIR.parent.parent
TEMPLATE = OPS_DIR / "launchd-plist.template"
RUNTIME = OPS_DIR / "nightly-runtime.sh"
HOST_HOME_PREFIX = "/" + "Users/"


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    script: str
    hour: int
    minute: int
    stdout: str
    stderr: str
    mode: str


JOBS = {
    "moderate": Job(
        name="moderate",
        label="org.wikilean.moderate",
        script="nightly-moderate.sh",
        hour=3,
        minute=20,
        stdout="moderate.out.log",
        stderr="moderate.err.log",
        mode="moderation",
    ),
    "newtags": Job(
        name="newtags",
        label="org.wikilean.newtags",
        script="newtags-nightly.sh",
        hour=3,
        minute=10,
        stdout="newtags.out.log",
        stderr="newtags.err.log",
        mode="moderation",
    ),
    "brain": Job(
        name="brain",
        label="org.wikilean.brain",
        script="brain-nightly.sh",
        hour=2,
        minute=20,
        stdout="brain.out.log",
        stderr="brain.err.log",
        mode="brain",
    ),
}


def _repo_is_valid() -> bool:
    return all(
        path.is_file()
        for path in (
            REPO / "site" / "moderate.py",
            REPO / "wiki" / "package.json",
            TEMPLATE,
            RUNTIME,
        )
    )


def _selected_jobs(names: Optional[List[str]]) -> List[Job]:
    return [JOBS[name] for name in (names or list(JOBS))]


def _render(job: Job, home: Path, environment: dict[str, str]) -> bytes:
    script = (OPS_DIR / job.script).resolve()
    if not script.is_file():
        raise ValueError(f"missing launcher: {script}")
    log_dir = home / "Library" / "Logs" / "WikiLean"
    replacements = {
        "@LABEL@": job.label,
        "@SCRIPT@": str(script),
        "@STDOUT@": str(log_dir / job.stdout),
        "@STDERR@": str(log_dir / job.stderr),
        "@HOUR@": str(job.hour),
        "@MINUTE@": str(job.minute),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    if environment:
        entries = "\n".join(
            f"\t\t<key>{escape(key)}</key>\n\t\t<string>{escape(value)}</string>"
            for key, value in sorted(environment.items())
        )
        environment_xml = f"\t<key>EnvironmentVariables</key>\n\t<dict>\n{entries}\n\t</dict>"
    else:
        environment_xml = ""
    text = text.replace("@ENVIRONMENT@", environment_xml)
    for marker, value in replacements.items():
        text = text.replace(marker, escape(value))
    if re.search(r"@[A-Z]+@", text):
        raise ValueError("unexpanded marker remains in launchd plist template")
    payload = text.encode("utf-8")
    parsed = plistlib.loads(payload)
    expected_script = str(script)
    if parsed.get("ProgramArguments") != ["/bin/bash", expected_script]:
        raise ValueError(f"invalid ProgramArguments for {job.name}")
    if not Path(expected_script).is_absolute():
        raise ValueError(f"launcher path is not absolute for {job.name}")
    if parsed.get("EnvironmentVariables", {}) != environment:
        raise ValueError(f"invalid EnvironmentVariables for {job.name}")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _absolute_option(path: Optional[Path], label: str) -> Optional[Path]:
    if path is None:
        return None
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path: {path}")
    return path.resolve(strict=False)


def _launchd_environment(args: argparse.Namespace) -> dict[str, str]:
    # launchd does not inherit the operator's interactive WIKILEAN_*/BRAIN_*
    # exports.  Model that sparse environment exactly; explicit CLI paths are
    # the only overrides and will also be sealed into EnvironmentVariables.
    environment = {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "TMPDIR")
        if os.environ.get(key)
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    if args.python is not None:
        environment["WIKILEAN_PYTHON"] = str(args.python)
    if args.mathlib is not None:
        environment["WIKILEAN_MATHLIB"] = str(args.mathlib)
    if args.brain_mathlib is not None:
        environment["BRAIN_MATHLIB_CHECKOUT"] = str(args.brain_mathlib)
    return environment


def _runtime_check(mode: str, args: argparse.Namespace) -> dict[str, object]:
    result = subprocess.run(
        ["/bin/bash", str(RUNTIME), "json", mode],
        cwd="/",
        env=_launchd_environment(args),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip()
        raise ValueError(message or f"runtime preflight exited {result.returncode}")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("runtime preflight did not return valid JSON") from error
    if not isinstance(document, dict):
        raise ValueError("runtime preflight returned a non-object")
    return document


def _mode(jobs: list[Job]) -> str:
    modes = {job.mode for job in jobs}
    if modes == {"brain"}:
        return "brain"
    if modes == {"moderation"}:
        return "moderation"
    return "all"


def _rendered_environment(
    job: Job, args: argparse.Namespace, config: dict[str, object]
) -> dict[str, str]:
    # Always seal the exact interpreter that passed preflight. Otherwise an
    # auto-discovered Homebrew/system Python can differ between installation
    # and launchd execution.
    environment: dict[str, str] = {"WIKILEAN_PYTHON": str(config["python"])}
    if job.mode == "moderation" and args.mathlib is not None:
        environment["WIKILEAN_MATHLIB"] = str(config["mathlib"])
    if job.mode == "brain" and args.brain_mathlib is not None:
        environment["BRAIN_MATHLIB_CHECKOUT"] = str(config["brain_mathlib"])
    return environment


def _prepare(args: argparse.Namespace) -> tuple[list[Job], dict[str, object], Path]:
    if not _repo_is_valid():
        raise ValueError(f"derived repository root is invalid: {REPO}")
    _portable_sources_check()
    args.python = _absolute_option(args.python, "--python")
    args.mathlib = _absolute_option(args.mathlib, "--mathlib")
    args.brain_mathlib = _absolute_option(args.brain_mathlib, "--brain-mathlib")
    jobs = _selected_jobs(args.job)
    if args.mathlib is not None and args.brain_mathlib is None and any(
        job.mode == "brain" for job in jobs
    ):
        args.brain_mathlib = args.mathlib / "Mathlib"
    config = _runtime_check(_mode(jobs), args)
    return jobs, config, Path.home().resolve()


def _portable_sources_check() -> None:
    paths = [
        OPS_DIR / "nightly-launchd.py",
        OPS_DIR / "nightly-runtime.sh",
        OPS_DIR / "nightly-moderate.sh",
        OPS_DIR / "newtags-nightly.sh",
        OPS_DIR / "new-once.sh",
        OPS_DIR / "run-now.sh",
        OPS_DIR / "brain-nightly.sh",
        OPS_DIR / "nightly.env",
        OPS_DIR / "nightly.local.env.example",
        TEMPLATE,
    ]
    offenders = [str(path) for path in paths if HOST_HOME_PREFIX in path.read_text(encoding="utf-8")]
    if offenders:
        raise ValueError("host-specific /Users path remains in: " + ", ".join(offenders))


def cmd_check(args: argparse.Namespace) -> None:
    jobs, config, home = _prepare(args)
    for job in jobs:
        _render(job, home, _rendered_environment(job, args, config))
    print(f"repo={config['repo']}")
    print(f"python={config['python']}")
    if config.get("mathlib"):
        print(f"mathlib={config['mathlib']}")
    if config.get("brain_mathlib"):
        print(f"brain_mathlib={config['brain_mathlib']}")
    print("launchd templates ok")


def cmd_render(args: argparse.Namespace) -> None:
    jobs, config, home = _prepare(args)
    output_dir = args.output_dir.expanduser().resolve()
    for job in jobs:
        destination = output_dir / f"{job.label}.plist"
        _atomic_write(destination, _render(job, home, _rendered_environment(job, args, config)))
        print(destination)


def cmd_install(args: argparse.Namespace) -> None:
    # A broken interpreter, Mathlib checkout, or token should prevent a plist
    # from being installed and silently failing later under launchd.
    jobs, config, home = _prepare(args)
    log_dir = home / "Library" / "Logs" / "WikiLean"
    log_dir.mkdir(parents=True, exist_ok=True)
    output_dir = (args.output_dir or home / "Library" / "LaunchAgents").expanduser().resolve()
    for job in jobs:
        destination = output_dir / f"{job.label}.plist"
        _atomic_write(destination, _render(job, home, _rendered_environment(job, args, config)))
        print(f"installed {destination}")
    print("plists written; no jobs were loaded or started")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    def add_runtime_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--job", action="append", choices=sorted(JOBS))
        command.add_argument("--python", type=Path, help="absolute Python 3.12+ override")
        command.add_argument("--mathlib", type=Path, help="absolute mathlib4 checkout root")
        command.add_argument(
            "--brain-mathlib", type=Path, help="absolute mathlib4/Mathlib directory"
        )

    check = commands.add_parser("check", help="validate launchd runtime config and rendering")
    add_runtime_options(check)
    check.set_defaults(func=cmd_check)

    render = commands.add_parser("render", help="render plists to an explicit directory")
    render.add_argument("--output-dir", type=Path, required=True)
    add_runtime_options(render)
    render.set_defaults(func=cmd_render)

    install = commands.add_parser(
        "install", help="check config and write ~/Library/LaunchAgents plists without loading them"
    )
    install.add_argument("--output-dir", type=Path)
    add_runtime_options(install)
    install.set_defaults(func=cmd_install)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        print(f"nightly-launchd: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
