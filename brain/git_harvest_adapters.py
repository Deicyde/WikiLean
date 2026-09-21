"""Pure projections of verified Git snapshots through existing harvest semantics."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT / "brain/ingest"))
import common
import erdosproblems
import formal_conjectures as fc
import git_snapshot
import lean_repo


def snapshot(commit, tree, files, scope, suffix=None):
    selected = []
    for path, (mode, data) in sorted(files.items()):
        if path != scope and not path.startswith(scope + "/"):
            continue
        if suffix is not None and not path.endswith(suffix):
            continue
        if mode not in {"100644", "100755"}:
            raise ValueError("harvester selection contains a non-regular Git member: " + path)
        selected.append(git_snapshot.GitTextFile(path, data.decode("utf-8", "strict")))
    if not selected:
        raise ValueError("harvester selection is empty: " + scope)
    return git_snapshot.GitTextSnapshot(commit, tuple(selected), tree)


def formal_conjectures(source):
    rows, count = fc.harvest_snapshot(source)
    if not rows:
        raise ValueError("formal-conjectures harvest contains no declarations")
    return {"source": "google-deepmind/formal-conjectures",
            "license": "Apache-2.0 (The Formal Conjectures Authors) — docstrings/code stored with attribution",
            "commit": source.commit, "n_files": count, "n_decls": len(rows),
            "n_research": sum((row.get("category") or "").startswith("research") for row in rows)}, rows


def tauceti(source):
    rows, count = lean_repo.harvest_rows(source, "TauCeti")
    if not rows:
        raise ValueError("TauCeti harvest contains no declarations")
    return {"source": "TauCetiProject/TauCeti", "repo": "TauCetiProject/TauCeti", "lib": "TauCeti",
            "license": lean_repo.SPECS["tauceti"]["license"], "commit": source.commit,
            "n_files": count, "n_decls": len(rows)}, rows


def erdos(source):
    if len(source.files) != 1 or source.files[0].path != "data/problems.yaml":
        raise ValueError("Erdos requires its exact problems.yaml Git member")
    # safe_load intentionally uses the pure Python SafeLoader, matching the
    # existing ingest. Optional libyaml availability never selects CSafeLoader.
    problems = erdosproblems.yaml.safe_load(source.files[0].text)
    if not isinstance(problems, list) or not problems:
        raise ValueError("unexpected captured problems.yaml shape")
    pages, joins = [], []
    for problem in problems:
        number = str(problem.get("number") or "").strip()
        if not number:
            continue
        status = ((problem.get("status") or {}).get("state") or "").strip() or None
        pages.append({key: value for key, value in {"db": "erdos", "id": number,
            "title": "Erdős Problem " + number, "url": erdosproblems.PAGE_URL.format(number),
            "kind_hint": status}.items() if value is not None})
        prize = (problem.get("prize") or "").strip()
        joins.append({key: value for key, value in {"erdos": number, "status": status,
            "prize": prize if prize and prize != "no" else None,
            "oeis": problem.get("oeis") or None, "tags": problem.get("tags") or None,
            "formalized": (problem.get("formalized") or {}).get("state") or None}.items() if value is not None})
    if not pages or not joins:
        raise ValueError("Erdos harvest contains no problems")
    # Erdos has no links. Reproduce common.emit's page-id normalization and
    # pair metadata without reading old outputs, locks, journals, or the clock.
    normalized, seen, changed, dropped = [], set(), set(), 0
    for page in pages:
        raw_id = str(page["id"])
        identifier = common.strip_controls(raw_id)
        if not identifier.strip():
            dropped += 1
            continue
        if identifier in seen:
            if identifier != raw_id or identifier in changed:
                dropped += 1
                continue
            raise ValueError("duplicate Erdos page id " + repr(identifier))
        if identifier != raw_id:
            changed.add(identifier)
        seen.add(identifier)
        page["id"] = identifier
        normalized.append(page)
    if not normalized:
        raise ValueError("Erdos harvest has no valid page IDs")
    pair_meta = {"db": "erdos", "n_pages": len(normalized), "n_links": 0, "n_links_resolved": 0,
        "n_pages_dropped_bad_id": dropped, "n_links_dropped_bad_id": 0,
        "source_pin": "teorth/erdosproblems data/problems.yaml @ " + source.commit,
        "source_license": "Apache-2.0 (github.com/teorth/erdosproblems); erdosproblems.com prose is Thomas Bloom's and is not redistributed — link facts and constructed titles only"}
    pair_meta = common.seal_external_pair_meta(pair_meta, normalized, [])
    common.validate_external_pair("erdos", pair_meta, normalized, pair_meta, [])
    join_meta = {"source": "teorth/erdosproblems data/problems.yaml", "license": "Apache-2.0",
                 "commit": source.commit, "n_problems": len(joins)}
    return {"erdos-joins": (join_meta, joins), "erdos-pages": (pair_meta, normalized), "erdos-links": (pair_meta, [])}
