"""Acquisition: the only step that uses the network. Writes DIR/PINS.json.

Layout under DIR, the same one run.sh produces:
  TauCetiRoadmap/        git clone, HEAD is the pin
  trough/                sparse clone of Deicyde/trough with data/tauceti/
  atlas-data.json        the JSON embedded in the atlas page
  decls/mathlib.bmp      doc-gen4 declaration-data.bmp (JSON) of the Mathlib docs
  decls/tauceti.bmp      the same for the Tau Ceti docs

Without pins it fetches the newest state. With pins (`--pins`, a PINS.json or run 1's
PINS.txt) it checks out each git input at its pinned commit and checks every URL input's
sha256 against its pin; any mismatch raises FetchError naming the input, and no PINS.json is
left behind. Every network call goes through one opener (`Opener`), so tests inject a fake.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

from .build import URLS, read_pins_file
from .canon import git, sha256_bytes

USER_AGENT = "wikilean-tauceti-plans/1"
# Whose source links name the commit a doc-gen4 site was built from.
DOCS_REPOS = {"mathlib_decls": "leanprover-community/mathlib4", "tauceti_decls": "TauCetiProject/TauCeti"}


class FetchError(Exception):
    pass


class Opener:
    """The network: HTTP GETs and git clones. Replace it to run offline."""

    def get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read()

    def clone(self, url: str, dest: str, sparse: str | None = None, commit: str | None = None) -> None:
        """Clone (or update) `url` into `dest` and detach HEAD at `commit`, else at the newest."""
        if not os.path.isdir(os.path.join(dest, ".git")):
            args = ["git", "clone", "--quiet", "--no-checkout"]
            if sparse:
                args += ["--filter=blob:none", "--sparse"]
            subprocess.run(args + [url, dest], check=True)
            if sparse:
                git(dest, "sparse-checkout", "set", sparse)
        if commit:
            git(dest, "fetch", "--quiet", "origin")
            if subprocess.run(["git", "-C", dest, "cat-file", "-e", f"{commit}^{{commit}}"],
                              capture_output=True).returncode != 0:
                git(dest, "fetch", "--quiet", "origin", commit)
            target = commit
        else:
            # The remote's default branch, named explicitly. After a bare `git fetch origin`
            # with HEAD detached (any re-fetch), FETCH_HEAD's first line is whichever branch
            # sorts first, so a refresh could silently build from a PR branch.
            git(dest, "fetch", "--quiet", "origin", "HEAD")
            target = "FETCH_HEAD"
        git(dest, "checkout", "--quiet", "--detach", target)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def docs_commit(opener: Opener, name: str, bmp_url: str, bmp: dict) -> str | None:
    """The commit a doc-gen4 site was built from: the 40-hex commit in the
    `github.com/<owner>/<repo>/blob/<sha>/` source links of the module page that the
    `.bmp`'s first entry's `docLink` names. None unless the page names exactly one."""
    first = next(iter((bmp.get("declarations") or {}).values()), None)
    link = first.get("docLink") if isinstance(first, dict) else None
    if not isinstance(link, str):
        return None
    root = urllib.parse.urljoin(bmp_url, "../")  # the docs root holding declarations/
    page = urllib.parse.urljoin(root, link.split("#", 1)[0])
    html = opener.get(page).decode("utf-8", "replace")
    repo = re.escape(DOCS_REPOS[name])
    shas = set(re.findall(rf"github\.com/{repo}/blob/([0-9a-f]{{40}})/", html))
    return shas.pop() if len(shas) == 1 else None


def fetch(inputs: str, pins_file: str | None = None, opener: Opener | None = None) -> dict:
    opener = opener or Opener()
    want = read_pins_file(pins_file) if pins_file else None
    out_path = os.path.join(inputs, "PINS.json")
    if os.path.exists(out_path):
        os.remove(out_path)  # a failed fetch must leave none
    os.makedirs(os.path.join(inputs, "decls"), exist_ok=True)
    pins: dict = {}

    def pinned(name, key):
        return (want or {}).get(name, {}).get(key) if want is not None else None

    for name, sparse in (("TauCetiRoadmap", None), ("trough", "data/tauceti")):
        dest = os.path.join(inputs, name)
        commit = pinned(name, "commit")
        if want is not None and not commit:
            raise FetchError(f"{name}: the pins name no commit")
        try:
            opener.clone(URLS[name], dest, sparse, commit)
        except (subprocess.CalledProcessError, OSError) as e:
            raise FetchError(f"{name}: could not check out {commit or 'the newest commit'} ({e})") from None
        head = git(dest, "rev-parse", "HEAD").decode().strip()
        if commit and head != commit:
            raise FetchError(f"{name}: checked out {head}, pinned {commit}")
        pins[name] = {"commit": head, "fetched_at": _now()}

    def store(name, rel, data):
        sha = sha256_bytes(data)
        expect = pinned(name, "sha256")
        if want is not None and expect != sha:
            raise FetchError(f"{name}: sha256 {sha} does not match pinned {expect}")
        path = os.path.join(inputs, rel)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        return sha

    html = opener.get(URLS["atlas"]).decode("utf-8")
    m = re.search(r'<script id="atlas-data" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise FetchError("atlas: the page has no atlas-data script element")
    pins["atlas"] = {"sha256": store("atlas", "atlas-data.json", m.group(1).encode("utf-8")),
                     "fetched_at": _now()}

    for name, rel in (("mathlib_decls", "decls/mathlib.bmp"), ("tauceti_decls", "decls/tauceti.bmp")):
        data = opener.get(URLS[name])
        try:
            bmp = json.loads(data)  # doc-gen4's .bmp is JSON; refuse anything else
        except ValueError as e:
            raise FetchError(f"{name}: not JSON ({e})") from None
        sha = store(name, rel, data)
        commit = docs_commit(opener, name, URLS[name], bmp)
        expect = pinned(name, "docs_commit")
        if expect and commit != expect:
            raise FetchError(f"{name}: docs commit {commit} does not match pinned {expect}")
        pins[name] = {"sha256": sha, "docs_commit": commit, "fetched_at": _now()}

    with open(out_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"schema": "wikilean.brain.plans.pins/v1", "inputs": pins}, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(out_path + ".tmp", out_path)
    return pins
