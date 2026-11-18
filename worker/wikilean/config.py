"""Where the CLI keeps its files, and the handful of settings a contributor sets.

Three directories, all XDG with the usual defaults, all created mode 0700:

    config   ~/.config/wikilean/        config.json, and the token file when the
                                        Keychain is not used
    state    ~/.local/state/wikilean/   journal.jsonl and the round cursor
    cache    ~/.cache/wikilean/         the index shards, re-read at most hourly

Nothing in config.json is a secret: the token lives in the Keychain or in its
own 0600 file (secrets.py), never here.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

BASE_URL = "https://wikilean.jackmccarthy.org"

# The three units, in the order `work` tries them.
UNITS: tuple[str, ...] = ("review", "tag", "locate")

# The shortest gap `work --loop` will leave between two rounds. A round is a
# paid model call and (usually) a write against an account that may make 30 a
# minute, so there is no setting of `interval` for which no gap is right.
MIN_INTERVAL = 5.0


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable, "").strip()
    root = Path(value) if value.startswith("/") else Path.home() / default
    return root / "wikilean"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def ensure_dir(path: Path) -> Path:
    """Create a private directory (0700) and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is ignored when the directory already exists.
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read, without a readable window."""
    ensure_dir(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    """The contributor's settings. Every field has a working default."""

    base_url: str = BASE_URL
    # Passed to `claude --model` when set; empty means the contributor's default.
    model: str = ""
    # Seconds between rounds in `work --loop`, never below MIN_INTERVAL.
    interval: float = 90.0
    # Seconds a single `claude -p` call may take before it is killed.
    model_timeout: float = 300.0
    # Whether the model may search the web and fetch pages while it judges
    # (model.WEB_TOOLS, and nothing else). False, or `work --no-web`, keeps the
    # call tool-less.
    web: bool = True
    # docs/START-CONTRIBUTING.md path 3 is "ask first", so the locate unit needs
    # BOTH this flag and --enable-locate on the command line before it runs.
    locate_enabled: bool = False
    # An existing WikiLean checkout the locate unit runs the contributor
    # pipeline from, and the work repo clone and work directory it uses.
    locate_checkout: str = ""
    locate_repo: str = ""
    locate_workdir: str = ""
    # The pipeline's model stages run on the contributor's Claude subscription,
    # and CLAUDE.md's "Max-auth gotcha" says an ANTHROPIC_API_KEY in the
    # environment makes such a call fail silently. The locate unit therefore
    # unsets ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL unless this says not to,
    # which is how somebody genuinely paying by API key opts in.
    locate_keep_anthropic_env: bool = False
    # Articles the contributor does not want to be given.
    skip_pageids: list[int] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        target = path or config_path()
        out = cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            # A config file this cannot read leaves every setting at its default
            # except the web tools, which it may have been written to turn off
            # (config_problem says why, and `work` prints it).
            out.web = False
            raw = {}
        if "web" in raw and not isinstance(raw["web"], bool):
            out.web = False
        for name, value in ((f, raw[f]) for f in cls.__dataclass_fields__ if f in raw):
            current = getattr(out, name)
            if isinstance(current, bool) and isinstance(value, bool):
                setattr(out, name, value)
            elif isinstance(current, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(out, name, float(value))
            elif isinstance(current, str) and isinstance(value, str):
                setattr(out, name, value)
            elif isinstance(current, list) and isinstance(value, list):
                setattr(out, name, [v for v in value if isinstance(v, int)])
        out.base_url = (os.environ.get("WIKILEAN_BASE_URL") or out.base_url).rstrip("/")
        out.interval = max(out.interval, MIN_INTERVAL)
        return out

    def save(self, path: Path | None = None) -> None:
        write_private(path or config_path(), json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")


def config_problem(path: Path | None = None) -> str | None:
    """Why config.json cannot be read as settings, or None (it reads, or is absent)."""
    target = path or config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return f"{target} is not readable JSON ({type(exc).__name__}): every setting is its default, and web is off"
    if not isinstance(raw, dict):
        return f"{target} is not a JSON object: every setting is its default, and web is off"
    if "web" in raw and not isinstance(raw["web"], bool):
        return f'{target}: "web" must be true or false, not {raw["web"]!r}, so web is off'
    return None
