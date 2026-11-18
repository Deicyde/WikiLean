"""Acquisition: the only step that uses the network. Writes DIR/PINS.json.

Layout under DIR, the same one run.sh produces:
  TauCetiRoadmap/        git clone, HEAD is the pin
  trough/                sparse clone of Deicyde/trough with data/tauceti/
  atlas-data.json        the JSON embedded in the atlas page
  decls/mathlib.bmp      doc-gen4 declaration-data.bmp (JSON) of the Mathlib docs
  decls/tauceti.bmp      the same for the Tau Ceti docs
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import urllib.request

from .build import URLS
from .canon import git, sha256_file

USER_AGENT = "wikilean-tauceti-plans/1"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def _clone(url: str, dest: str, sparse: str | None = None) -> None:
    if os.path.isdir(os.path.join(dest, ".git")):
        git(dest, "fetch", "--quiet", "origin")
        git(dest, "checkout", "--quiet", "--detach", "FETCH_HEAD")
        return
    args = ["git", "clone", "--quiet"]
    if sparse:
        args += ["--filter=blob:none", "--sparse"]
    subprocess.run(args + [url, dest], check=True)
    if sparse:
        git(dest, "sparse-checkout", "set", sparse)


def fetch(inputs: str) -> dict:
    os.makedirs(os.path.join(inputs, "decls"), exist_ok=True)
    pins: dict = {}
    for name, sparse in (("TauCetiRoadmap", None), ("trough", "data/tauceti")):
        dest = os.path.join(inputs, name)
        _clone(URLS[name], dest, sparse)
        pins[name] = {"commit": git(dest, "rev-parse", "HEAD").decode().strip(), "fetched_at": _now()}

    html = _get(URLS["atlas"]).decode("utf-8")
    m = re.search(r'<script id="atlas-data" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("atlas page: no atlas-data script element")
    atlas_path = os.path.join(inputs, "atlas-data.json")
    with open(atlas_path, "w", encoding="utf-8") as f:
        f.write(m.group(1))
    pins["atlas"] = {"sha256": sha256_file(atlas_path), "fetched_at": _now()}

    for name, rel in (("mathlib_decls", "decls/mathlib.bmp"), ("tauceti_decls", "decls/tauceti.bmp")):
        path = os.path.join(inputs, rel)
        data = _get(URLS[name])
        json.loads(data)  # doc-gen4's .bmp is JSON; refuse anything else
        with open(path, "wb") as f:
            f.write(data)
        pins[name] = {"sha256": sha256_file(path), "fetched_at": _now()}

    with open(os.path.join(inputs, "PINS.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "wikilean.brain.plans.pins/v1", "inputs": pins}, f, indent=1, sort_keys=True)
        f.write("\n")
    return pins
