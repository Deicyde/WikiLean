"""Closed in-memory execution of the legacy PlanetMath parser and source export."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import sys
import types
import unicodedata
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import planetmath_source_evidence as core

PARSER_SYMBOLS = {"ORG_REPOS_URL", "CONTENT_REPO_RE", "CANONICAL_RE", "_WS", "TEXT_CMDS", "LINKNAME", "ENV", "COMMENT", "LINK_ESCAPES",
    "ACCENTS", "ACCENT_RE", "LIGATURES", "_accent", "cmd_args", "detex", "extract_snippet", "main"}


@dataclass(frozen=True)
class CapturedFile:
    path: str
    raw: bytes

    def __lt__(self, other): return self.path < other.path

    def read_text(self, *, encoding, errors):
        core.require((encoding, errors) == ("utf-8", "replace"), "legacy text provider changed")
        return self.raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


class CapturedDirectory:
    def __init__(self, trees, parts=()): self.trees, self.parts = trees, parts
    def __truediv__(self, value):
        parts = (*self.parts, value)
        core.require((len(parts) == 1 and parts[0] == "planetmath") or
            (len(parts) == 2 and parts[0] == "planetmath" and parts[1] in self.trees), "legacy parser escaped retained Git trees")
        return CapturedDirectory(self.trees, parts)

    def rglob(self, pattern):
        core.require(len(self.parts) == 2 and pattern == "*.tex", "legacy path selector changed")
        files = self.trees[self.parts[1]]
        selected = []
        for path, (mode, raw) in files.items():
            core.require(not any(part.endswith(".tex") for part in path.split("/")[:-1]),
                "PlanetMath .tex directories are unreadable by the legacy parser")
            if not path.endswith(".tex"): continue
            core.require(mode in {"100644", "100755"}, "PlanetMath TeX symlinks require explicit source review")
            selected.append(CapturedFile(path, raw))
        return selected


def project(plan, trees, heads, qmap, programs):
    selected, names = [], []
    tree = ast.parse(programs["brain/ingest/planetmath.py"], filename="sealed:brain/ingest/planetmath.py")
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS: selected.append(node); names.append(name)
    core.require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)), "legacy PlanetMath parser selection differs")
    result = []
    def emit(db, pages, links, meta):
        core.require(db == "planetmath", "legacy parser emitted wrong source family")
        result.append(core.pair.normalize_pair(db, pages, links, meta, programs["brain/ingest/common.py"]))
    argument_parser = types.SimpleNamespace(add_argument=lambda *a, **k: None, parse_args=lambda: types.SimpleNamespace(max_age_hours=168))
    namespace = {"__doc__": "sealed PlanetMath parser", "re": re, "unicodedata": unicodedata, "hashlib": hashlib, "json": json,
        "argparse": types.SimpleNamespace(ArgumentParser=lambda **k: argument_parser),
        "list_content_repos": lambda: sorted(trees), "sync_repo": lambda name, _max_age: heads[name],
        "common": types.SimpleNamespace(CACHE_DIR=CapturedDirectory(trees), qid_map=lambda db: qmap if db == "planetmath" else None, emit=emit)}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "sealed:planetmath-parser", "exec"), namespace)
    core.require(namespace["main"]() is None and len(result) == 1, "PlanetMath parser did not emit one complete pair")
    meta, pages, links = result[0]
    core.require(plan["minimum_pages"] <= len(pages) <= core.POLICY["maximum_pages"] and
        plan["minimum_links"] <= len(links) <= core.POLICY["maximum_links"], "PlanetMath output differs from reviewed completeness bounds")
    return meta, pages, links


def build_export(capture, profile, programs, roots, when):
    core.origins()
    core.require(Path(core.__file__).resolve() == core.ROOT / "brain/planetmath_source_evidence.py", "PlanetMath core origin differs")
    plan, raw, records, tool = core.verify_capture_files(capture)
    core.verify_programs(profile, programs)
    sources, manifests, parent_objects, captured = core.capture_parents(plan, roots)
    repos, trees, heads, tree_ids, specs = core.replay(plan, raw, records)
    meta, pages, links = project(plan, trees, heads, core.qid_map(captured), programs)
    configuration = {"schema": core.NORMALIZATION_SCHEMA, "reviewed_parent_manifest_ids": plan["reviewed_parent_manifest_ids"],
        "observation": core.POLICY["observation"], "selection": "complete official public two-digit MSC repository listing; exact default heads and complete Git trees",
        "legacy_parser_symbols": sorted(PARSER_SYMBOLS), "inputs": "all regular lowercase-.tex files in sorted repo/native-path order; TeX symlinks fail closed",
        "projection": "exact retained main/cmd_args/detex/snippet parser and common.emit pure pair semantics; original case and first-entry fields",
        "join": "complete reviewed P7726 crossrefs; exact ID then legacy casefold fallback", "publication": "restricted private evidence; no new approval"}
    files = {"acquisition/" + name: value for name, value in capture.items()}
    files.update({"implementation/" + name: value for name, value in programs.items()})
    files["normalization/profile.json"], files["normalization/configuration.json"] = core.canonical(profile), core.canonical(configuration)
    files["normalization/facts.json"] = core.canonical({**core.parse(capture["facts.json"], "facts"), "pages": len(pages), "links": len(links),
        "tex_files": meta["n_tex"], "skipped_tex_files": meta["n_skipped"], "qid_joined": meta["n_qid_joined"]})
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + core.sha(data)
        files.setdefault(path, data)
        return {"name": name, "root": core.PHYSICAL_ROOT, "path": path, "sha256": core.sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    def evidence(path, **identity):
        return {"root": core.PHYSICAL_ROOT, "path": path, "sha256": core.sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    normalizer = {"name": "wikilean-planetmath-normalizer", "version": "1", "sha256": core.sha(core.canonical(profile))}
    support = [planned("normalization_profile", core.canonical(profile), ["receipt"]),
        planned("normalization_configuration", files["normalization/configuration.json"], ["receipt"]),
        planned("acquisition_plan", capture["plan.json"], ["receipt"]), planned("acquisition_profile", capture["profile.json"], ["receipt"]),
        planned("acquisition_tool", capture["tool.json"], ["receipt"])]
    for prefix, base in (("normalizer", "implementation/"), ("acquirer", "acquisition/implementation/")):
        support.extend(planned(prefix + "_program_" + str(i), files[base + name], ["receipt"], "text/x-python") for i, name in enumerate(core.TOOL_FILES))
    license = {"expression": "CC-BY-SA-2.5", "redistribution": "restricted",
        "notice": "PlanetMath contributors, https://planetmath.org/. Complete original Git files retained privately; source-registry attribution preserved. No publication approval."}
    added, added_manifests = [], {}
    all_objects = {}
    def source_document(source_name, pin, receipt_names, own_raw, parents, outputs, config, identity=False):
        receipts = {name: core.parse(capture["receipts/" + name + ".json"], "receipt") for name in receipt_names}
        by_receipt = {r["acquisition_receipt_id"]: r for r in receipts.values()}
        raw_objects, inputs = [], []
        for item, origin in own_raw:
            item = {**item, "roles": ["raw"]}
            raw_objects.append(item); inputs.append({**core.io.object_ref(item), "origin": origin})
        for item, parent_id in parents:
            core.require("normalized" in item["roles"], "PlanetMath lineage input is not normalized")
            item = {**item, "roles": ["raw"]}
            raw_objects.append(item); inputs.append({**core.io.object_ref(item), "origin": {"kind": "source_manifest", "id": parent_id}})
        parent_map = {parent_id: (added_manifests.get(parent_id) or next(m for m in manifests.values() if m["source_manifest_id"] == parent_id)) for _, parent_id in parents}
        lineage = {"schema": core.contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": source_name, "mode": "identity" if identity else "transform",
            "normalization_schema": core.NORMALIZATION_SCHEMA, "configuration_sha256": core.sha(core.canonical(config)), "tool": normalizer,
            "acquisition_receipt_ids": sorted(by_receipt), "parent_source_manifest_ids": sorted(parent_map),
            "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
            "outputs": sorted(map(core.io.object_ref, outputs), key=lambda item: item["object"]), "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = core.contracts.normalization_lineage_identity(lineage)
        path = "evidence/" + source_name + ".json"; files[path] = core.canonical(lineage)
        combined = {}
        for item in [*raw_objects, *outputs, *support, planned("source_configuration", core.canonical(config), ["receipt"])]:
            if item["name"] in combined:
                old = combined[item["name"]]
                core.require({k: v for k, v in old.items() if k != "roles"} == {k: v for k, v in item.items() if k != "roles"}, "PlanetMath object name collision")
                item = {**item, "roles": sorted(set(old["roles"]) | set(item["roles"]))}
            combined[item["name"]] = item
        requests = {request["parameters_sha256"] for receipt in receipts.values() for request in receipt["requests"]}
        preimages = [evidence("acquisition/requests/" + digest + ".json", parameters_sha256=digest) for digest in sorted(requests)]
        source = {"source": source_name, "source_kind": "acquired_dataset" if receipts else "sealed_snapshot",
            "pin": pin or {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]}, "objects": sorted(combined.values(), key=lambda o: o["name"]),
            "license": license, "acquisition": next(iter(receipts.values()))["tool"] if receipts else normalizer,
            "normalization": {"schema": core.NORMALIZATION_SCHEMA, "tool": normalizer,
                "inputs": sorted(i["name"] for i in raw_objects), "outputs": sorted(i["name"] for i in outputs)},
            "evidence": {"acquisition_receipts": sorted([evidence("acquisition/receipts/" + name + ".json", acquisition_receipt_id=r["acquisition_receipt_id"]) for name, r in receipts.items()], key=lambda r: r["acquisition_receipt_id"]),
                "normalization_lineage": evidence(path, normalization_lineage_id=lineage["normalization_lineage_id"]), "request_parameter_preimages": preimages}}
        manifest = core.io.source_plan_contracts._source_manifest_from_plan(source, "PlanetMath source")
        core.contracts.validate_source_manifest_evidence_documents(manifest, receipts=by_receipt, lineage=lineage,
            request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests=parent_map)
        files["source-manifests/" + source_name + ".json"] = core.canonical(manifest)
        added.append(source); added_manifests[manifest["source_manifest_id"]] = manifest
        all_objects[source_name] = combined
        return manifest["source_manifest_id"]
    listing = planned("repository_listing", capture["listing.json"], ["normalized"])
    listing_receipt = core.parse(capture["receipts/" + core.LISTING_SOURCE + ".json"], "listing receipt")
    listing_id = source_document(core.LISTING_SOURCE, listing_receipt["pin"], [core.LISTING_SOURCE],
        [(listing, {"kind": "acquisition_receipt", "id": listing_receipt["acquisition_receipt_id"]})], [], [listing], plan, identity=True)
    derived_inputs = [(listing, listing_id)]
    for repo in repos:
        name, prefix = repo["name"], "r" + repo["name"][:2] + "_"
        source = "planetmath-" + name[:2] + "-source"
        receipt = core.parse(capture["receipts/" + source + ".json"], "repository receipt")
        index = {"repository": "planetmath/" + name, "commit": heads[name], "tree": tree_ids[name],
            "entries": [{"path": p, "mode": mode, "git_blob": core.archive.git_hash("blob", data), "sha256": core.sha(data), "bytes": len(data)} for p, (mode, data) in sorted(trees[name].items())]}
        index_obj = planned(prefix + "git_tree", core.canonical(index), ["normalized"])
        outputs, selected = [index_obj], [index_obj]
        for path, (mode, data) in sorted(trees[name].items()):
            item = planned(prefix + "file_" + core.sha(path.encode()), data, ["normalized"], "application/octet-stream")
            outputs.append(item)
            if path.endswith(".tex"):
                core.require(mode in {"100644", "100755"}, "PlanetMath TeX symlink")
                selected.append(item)
        own_raw = [(planned(s[0], raw[s[0]], ["raw"], s[2]), {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]})
            for s in (core.head_spec(repo), core.archive_spec(repo, heads[name]))]
        config = {"schema": "wikilean.planetmath-git-tree/v1", "repository": "planetmath/" + name, "listing_source_manifest_id": listing_id,
            "listed_repository_id": repo["id"], "default_branch": repo["default_branch"], "commit": heads[name], "tree": tree_ids[name]}
        source_id = source_document(source, receipt["pin"], [source], own_raw, [(listing, listing_id)], outputs, config)
        derived_inputs.extend((obj, source_id) for obj in selected)
    for key, data in sorted(captured.items()):
        prior = parent_objects[key]
        obj = planned(prior["name"], data, ["normalized"], prior["media_type"])
        derived_inputs.append((obj, manifests[key[0]]["source_manifest_id"]))
    outputs = []
    for kind, rows in (("links", links), ("pages", pages)):
        data = b"".join(core.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
        files["normalized/planetmath_" + kind + ".jsonl"] = data
        outputs.append(planned("planetmath_" + kind, data, ["normalized"], "application/x-ndjson"))
    child_id = source_document(core.CHILD, None, [], [], derived_inputs, outputs, configuration)
    files["source-fragment.json"] = core.canonical({"schema": "wikilean.planetmath-source-fragment/v1", "scope": "source-plan-fragment", "physical_root": core.PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *added], key=lambda s: s["source"]),
        "input_bindings": [{"input_id": "external-" + kind, "state": "present", "sources": [core.CHILD],
            "members": [{"path": "planetmath_" + kind + ".jsonl", "source": core.CHILD, "object": "planetmath_" + kind}]} for kind in ("links", "pages")]})
    return core.archive.manifest_files(files, core.EXPORT_SCHEMA)


def verify_export(path, roots):
    core.origins()
    files, manifest = core.archive.read_bundle(path, core.EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = core.parse(files["normalization/profile.json"], "profile")
    programs = {name: files["implementation/" + name] for name in core.TOOL_FILES}
    when = core.parse(files["evidence/" + core.CHILD + ".json"], "lineage")["audit"]["normalized_at"]
    expected = build_export(capture, profile, programs, roots, when)
    core.require(expected == {**files, "manifest.json": core.canonical(manifest)}, "PlanetMath export differs from complete independent replay")
    identities = {}
    for name, data in files.items():
        if name.startswith("source-manifests/"):
            source = core.contracts.validate_source_manifest(core.parse(data, "source manifest"))
            core.contracts.verify_source_manifest_files(source, path)
            identities[source["source"]] = source["source_manifest_id"]
    return {"export_id": manifest["identity"], "source_manifest_ids": identities, "facts": core.parse(files["normalization/facts.json"], "facts")}
