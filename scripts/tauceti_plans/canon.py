"""Re-export of `scripts.brain_producers.common`, where the canonical JSON, hashing and git
blob reader now live. Kept so existing imports (`from .canon import ...`) keep working."""

from scripts.brain_producers.common import *  # noqa: F401,F403
from scripts.brain_producers.common import (  # noqa: F401
    ROADMAP_REPO,
    BuildError,
    GitTree,
    _git_env,
    dumps,
    git,
    jsonl_bytes,
    sha256_bytes,
    sha256_file,
)
