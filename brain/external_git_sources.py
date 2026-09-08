"""Pure nLab/Stacks reductions over explicitly reviewed, complete v3 Git parents.

Only sealed objects are parsed. Selected legacy syntax routines run against an
in-memory tree; acquisition, cache, and filesystem publication paths are absent.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import types
import urllib.parse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/ingest", ROOT / "catalog"):
    sys.path.append(str(directory))
import wikidata_crossref_sources as io
import common
import build_context
import external_pair_normalization as pair_normalizer

contracts = io.contracts
PLAN_SCHEMA = "wikilean.external-git-plan/v1"
EXPORT_SCHEMA = "wikilean.external-git-export/v1"
PROFILE_SCHEMA = "wikilean.external-git-profiles/v1"
REGISTRY = ROOT / "brain/external_git_profiles.json"
PHYSICAL_ROOT = "external_git"
PARENTS = {"nlab-source", "stacks-source", "wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
CHILDREN = ("external-nlab", "external-stacks")
GIT_SOURCES = {"nlab": "ncatlab/nlab-content", "stacks": "stacks/stacks-project"}
TOOL_FILES = tuple(sorted((set(io.TOOL_FILES) - {"brain/export_wikidata_crossrefs.py"}) |
    {"brain/external_git_sources.py", "brain/export_external_git.py", "brain/ingest/nlab.py",
     "brain/ingest/stacks.py", "brain/ingest/common.py", "brain/build_context.py", "brain/external_pair_normalization.py"}))


def origins():
    io.origins()
    pair_normalizer.origins()
    for module, path in ((common, "brain/ingest/common.py"), (build_context, "brain/build_context.py"),
        (pair_normalizer, "brain/external_pair_normalization.py")):
        io.require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected external Git helper origin: " + path)
    io.require(common.seal_external_pair_meta is build_context.seal_external_pair_meta,
        "external Git helpers use different imported dependencies")


origins()
LOADED = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES if path != "brain/export_external_git.py"}


def profile_id(profile):
    return contracts.domain_hash("wikilean.external-git-profile.v1", {"files": profile["files"]})


def profiles():
    raw = io.read(REGISTRY)
    value = io.exact(io.parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    io.require(value["schema"] == PROFILE_SCHEMA and raw == io.canonical(value) and isinstance(value["profiles"], list), "invalid profile registry")
    ids = []
    for profile in value["profiles"]:
        io.exact(profile, {"profile_id", "files"}, "profile")
        io.require(isinstance(profile["files"], list), "invalid helper closure")
        paths = []
        for item in profile["files"]:
            io.exact(item, {"path", "sha256"}, "helper")
            contracts.validate_literal_relative_path(item["path"], "helper path")
            contracts._digest(item["sha256"], "helper hash")
            paths.append(item["path"])
        io.require(paths == sorted(set(paths)) and "brain/external_git_sources.py" in paths and
            profile_id(profile) == profile["profile_id"], "invalid whole helper generation")
        ids.append(profile["profile_id"])
    io.require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current profile")
    return value


def current_profile():
    origins()
    value = profiles()
    profile = next(p for p in value["profiles"] if p["profile_id"] == value["current_profile"])
    actual = {path: io.sha(io.read(ROOT / path)) for path in TOOL_FILES}
    io.require(profile["files"] == [{"path": path, "sha256": actual[path]} for path in TOOL_FILES], "unreviewed current external Git implementation")
    io.require(all(actual[path] == digest for path, digest in LOADED.items()), "loaded external Git helper changed")
    return profile


def validate_plan(plan):
    io.exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids"}, "external Git plan")
    io.require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [source["source"] for source in plan["parents"]] == sorted(PARENTS), "exact sorted external Git parent closure required")
    io.require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every external Git parent requires an explicitly reviewed manifest identity")
    for value in plan["reviewed_parent_manifest_ids"].values():
        contracts._hash(value, "reviewed parent")
    return plan


def physical(ref, roots):
    io.require(ref["root"] in roots, "missing parent physical root: " + ref["root"])
    contracts.validate_literal_relative_path(ref["path"], "parent member")
    path = roots[ref["root"]] / ref["path"]
    io.real_path(path)
    return path


def checked(raw, ref):
    io.require(len(raw) == ref["bytes"] and io.sha(raw) == ref["sha256"], "parent bytes differ from reviewed source object")
    return raw


def capture_parents(plan, roots):
    validate_plan(plan)
    sources = {s["source"]: copy.deepcopy(s) for s in plan["parents"]}
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "external Git parent") for name, source in sources.items()}
    io.require({name: m["source_manifest_id"] for name, m in manifests.items()} == plan["reviewed_parent_manifest_ids"],
        "parent differs from explicitly reviewed source manifest identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    objects = {(name, item["name"]): item for name, source in sources.items() for item in source["objects"]}
    selected = {(name, obj) for name, obj in objects if
        (name in {"nlab-source", "stacks-source"} and (obj == "git_tree" or obj.startswith("file-"))) or
        (name == "wikidata-crossrefs" and obj in {"wikidata_crossrefs", "requested_qid_scope"}) or
        (name == io.CURATED_SOURCE and obj == "source_registry")}
    captured = {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            io.require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and
                source["objects"][0]["path"] == io.REGISTRY_PATH, "unexpected curated external Git parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            io.require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            if key in selected:
                io.require("normalized" in item["roles"], "external Git input must be normalized")
                captured[key] = raw
    lineages = {}
    for name, source in sources.items():
        if source["source_kind"] == "curated_git_tree":
            continue
        evidence, receipts, preimages = source["evidence"], {}, {}
        for ref in evidence["acquisition_receipts"]:
            receipts[ref["acquisition_receipt_id"]] = io.parse(checked(io.read(physical(ref, roots)), ref), "parent receipt")
        ref = evidence["normalization_lineage"]
        lineage = io.parse(checked(io.read(physical(ref, roots)), ref), "parent lineage")
        lineages[name] = lineage
        for ref in evidence["request_parameter_preimages"]:
            checked(io.read(physical(ref, roots)), ref)
            preimages[ref["parameters_sha256"]] = {key: ref[key] for key in ("parameters_sha256", "bytes", "media_type")}
        io.require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing external Git parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    return sources, manifests, objects, captured, lineages


# These explicit selectors exclude module-level cache creation and every live
# acquisition/publication entry point. The exact selected source bytes are part
# of the approved normalization profile, including historical replay.
PARSER_SYMBOLS = {
    "nlab": {"REDIRECT_RE", "WIKILINK_RE", "HEADING_RE", "IDEA_RE", "ANY_LINK_RE", "_WS",
             "strip_sidebars", "unlink", "_directive_line", "extract_snippet", "_first_para", "build"},
    "stacks": {"TAG_URL", "STATEMENT_ENVS", "STRIP_ENVS", "KIND_TOKENS", "TOKEN", "COMMENT", "CITE", "LABEL_CMD",
               "split_chapter", "kind_hint", "clean_body", "parse_chapter"},
}
CONFIGURATION = {"schema": "wikilean.external-git-normalization/v1", "text_decoding": "utf-8-replace; tags/tags strict-utf-8",
    "input_selection": {"nlab": "pages/*/*/*/*/*/name and existing sibling content.md; complete Git tree proves absence",
                        "stacks": "tags/tags and every root-level *.tex"},
    "symlinks": "retain Git blob evidence; reject any selected non-regular input",
    "parsers": {db: sorted(symbols) for db, symbols in PARSER_SYMBOLS.items()},
    "pair_normalization": "reviewed common.emit pure prefix through validate_external_pair",
    "nlab_join": "reviewed P4215 and requested-QID scope; lowest numeric QID; canonical-name then redirect/casefold",
    "stacks_join": "no Wikidata property; no QID inference", "metadata": "source commit and clock-free counts",
    "publication": "private evidence only; existing snippet attribution is not a new licensing permission"}


def selected_parser(db, raw, qmap):
    tree = ast.parse(raw, filename="sealed:brain/ingest/" + db + ".py")
    selected, names = [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS[db]:
            selected.append(node)
            names.append(name)
    io.require(set(names) == PARSER_SYMBOLS[db] and len(names) == len(set(names)), "legacy pure parser selector differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace = {"re": re, "urllib": urllib, "common": types.SimpleNamespace(qid_map=lambda requested: qmap if requested == db else None)}
    exec(compile(ast.fix_missing_locations(module), "sealed:" + db, "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def normalize_pair(db, pages, links, meta, program):
    try:
        return pair_normalizer.normalize_pair(db, pages, links, meta, program)
    except ValueError as exc:
        raise io.ExportError(str(exc)) from exc


class MemoryPath:
    """The legacy nLab builder sees only this finite, already verified file map."""
    def __init__(self, files, modes, path="", *, directories=None):
        self.files, self.modes, self.path = files, modes, path
        self.directories = directories if directories is not None else {parent for name in files for parent in (str(path) for path in Path(name).parents) if parent != "."}

    def __truediv__(self, name):
        return MemoryPath(self.files, self.modes, (self.path + "/" + name).lstrip("/"), directories=self.directories)

    def __lt__(self, other):
        return self.path < other.path

    @property
    def parent(self):
        return MemoryPath(self.files, self.modes, self.path.rpartition("/")[0], directories=self.directories)

    def glob(self, pattern):
        prefix = self.path + "/" if self.path else ""
        patterns = pattern.split("/")
        return [MemoryPath(self.files, self.modes, path, directories=self.directories) for path in sorted(set(self.files) | self.directories)
                if path.startswith(prefix) and len(parts := path[len(prefix):].split("/")) == len(patterns)
                and all(fnmatch.fnmatchcase(part, match) for part, match in zip(parts, patterns))]

    def exists(self):
        return self.path in self.files or self.path in self.directories

    def read_text(self, encoding="utf-8", errors="strict"):
        io.require(self.path in self.files and self.modes[self.path] in {"100644", "100755"}, "selected Git input is not a regular file: " + self.path)
        # Path.read_text uses universal newline conversion.
        return self.files[self.path].decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")


def complete_tree(db, manifests, captured):
    name = db + "-source"
    source = manifests[name]
    tree = io.parse(captured[(name, "git_tree")], "public Git tree")
    io.exact(tree, {"repository", "commit", "tree", "entries"}, "public Git tree")
    io.require(source["source_kind"] == "acquired_dataset" and source["pin"]["type"] == "git_commit" and
        tree["commit"] == source["pin"]["value"] and tree["repository"] == GIT_SOURCES[db], "wrong acquired public Git source")
    files, modes, blobs, used = {}, {}, {}, set()
    io.require(isinstance(tree["entries"], list), "Git tree entries must be complete")
    for row in tree["entries"]:
        io.exact(row, {"path", "mode", "git_blob", "sha256", "bytes"}, "Git member")
        path = contracts.validate_literal_relative_path(row["path"], "Git member path")
        io.require(path not in files and row["mode"] in {"100644", "100755", "120000"}, "duplicate or unsupported Git member")
        key = (name, "file-" + io.sha(path.encode()))
        io.require(key in captured, "normalized Git tree is incomplete")
        raw = checked(captured[key], row)
        io.require(io.git_oid("blob", raw) == row["git_blob"], "normalized member differs from Git blob")
        files[path], modes[path], blobs[path] = raw, row["mode"], row["git_blob"]
        used.add(key)
    io.require(list(files) == sorted(files) and used == {key for key in captured if key[0] == name and key[1].startswith("file-")},
        "unordered or undeclared normalized Git members")
    # Reconstruct native Git tree IDs, retaining symlink blobs without following
    # them. The tree index is a complete membership/absence proof, not a glob hint.
    directory = {}
    for path in files:
        current = directory
        parts = path.split("/")
        for part in parts[:-1]:
            io.require(not isinstance(current.get(part), tuple), "Git file/directory collision")
            current = current.setdefault(part, {})
        io.require(parts[-1] not in current, "Git file/directory collision")
        current[parts[-1]] = (modes[path], blobs[path])
    def tree_id(entries):
        records = []
        for name, value in entries.items():
            mode, oid = ("40000", tree_id(value)) if isinstance(value, dict) else value
            records.append(((name + ("/" if mode == "40000" else "")).encode(), mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        return io.git_oid("tree", b"".join(raw for _sort, raw in sorted(records)))
    io.require(tree_id(directory) == tree["tree"], "complete Git root differs")
    return tree, MemoryPath(files, modes)


def nlab_qids(captured):
    refs = io.parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = io.parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope")
    io.exact(scope, {"schema", "qids"}, "scope")
    io.require(scope["schema"] == io.SCOPE_SCHEMA and scope["qids"] == sorted(set(scope["qids"])) and
        set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    io.require("nlab" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P4215", []) and
        "nlab" in refs["properties"].get("P4215", []), "nLab identifiers require the curated P4215 mapping")
    qmap = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        io.require(bool(io.entities.QID_RE.fullmatch(qid)), "invalid crossref QID")
        values = refs["xrefs"][qid].get("nlab", [])
        io.require(isinstance(values, list) and all(isinstance(value, str) and value for value in values), "nLab external IDs must be concrete strings")
        for value in values:
            qmap.setdefault(value, qid)
    return qmap


def stacks_rows(memory, parser):
    tags = {}
    for line in (memory / "tags" / "tags").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag, _, label = line.partition(",")
        tags[label] = tag
    chapters = memory.glob("*.tex")
    stems = {Path(path.path).stem for path in chapters}
    snippets, refs = {}, []
    for path in chapters:
        parser.parse_chapter(Path(path.path).stem, path.read_text(errors="replace"), stems, snippets, refs)
    pages = []
    for label, tag in sorted(tags.items(), key=lambda item: item[1]):
        row = {"db": "stacks", "id": tag, "title": label, "url": parser.TAG_URL.format(tag)}
        kind, snippet = parser.kind_hint(label, stems), snippets.get(label)
        if kind:
            row["kind_hint"] = kind
        if snippet and snippet.strip():
            row["snippet"] = snippet
        pages.append(row)
    links, unresolved = set(), 0
    for src, dst, context in refs:
        src, dst = tags.get(src), tags.get(dst)
        if src is None or dst is None:
            unresolved += 1
        else:
            links.add((src, dst, context))
    return pages, [{"db": "stacks", "src": src, "dst": dst, "context": context} for src, dst, context in sorted(links)], {
        "n_snippets": sum("snippet" in page for page in pages), "n_refs_raw": len(refs), "n_refs_unresolved": unresolved, "n_chapters": len(stems)}


def reduce_git(db, manifests, objects, captured, programs):
    tree, memory = complete_tree(db, manifests, captured)
    parser = selected_parser(db, programs["brain/ingest/" + db + ".py"], nlab_qids(captured) if db == "nlab" else {})
    if db == "nlab":
        pages, links, stats = parser.build(memory)
        selected = {path.path for path in (memory / "pages").glob("*/*/*/*/*/name")}
        selected |= {path.rpartition("/")[0] + "/content.md" for path in selected if path.rpartition("/")[0] + "/content.md" in memory.files}
    else:
        io.require("tags/tags" in memory.files, "complete Stacks tree is missing tags/tags")
        pages, links, stats = stacks_rows(memory, parser)
        selected = {"tags/tags", *(path.path for path in memory.glob("*.tex"))}
    meta, pages, links = normalize_pair(db, pages, links, {"source_pin": tree["commit"], **stats}, programs["brain/ingest/common.py"])
    output = {db + "_" + family: b"".join(io.artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
              for family, rows in (("pages", pages), ("links", links))}
    output[db + "_derivation"] = io.canonical({"schema": "wikilean.external-git-derivation/v1", "db": db,
        "source_manifest_id": manifests[db + "-source"]["source_manifest_id"], "commit": tree["commit"], "tree": tree["tree"],
        "selected_files": sorted(selected), "n_tree_files": len(memory.files), "crossrefs": {
            "state": "reviewed-P4215" if db == "nlab" else "not-used-no-Stacks-property",
            "source_manifest_id": manifests["wikidata-crossrefs"]["source_manifest_id"],
            "registry_source_manifest_id": manifests[io.CURATED_SOURCE]["source_manifest_id"]},
        "snippet_permission": "existing legacy attribution retained; private evidence only; no new permission asserted"})
    inputs = {(db + "-source", "git_tree"), *((db + "-source", "file-" + io.sha(path.encode())) for path in selected),
              (io.CURATED_SOURCE, "source_registry"), ("wikidata-crossrefs", "wikidata_crossrefs"),
              ("wikidata-crossrefs", "requested_qid_scope")}
    return output, inputs


def build_documents(plan, sources, manifests, objects, captured, lineages, profile, programs, when):
    io.require(profile in profiles()["profiles"] and profile["files"] ==
        [{"path": path, "sha256": io.sha(raw)} for path, raw in sorted(programs.items())], "unreviewed external Git program preimages")
    reduced = {"external-" + db: reduce_git(db, manifests, objects, captured, programs) for db in GIT_SOURCES}
    files = {"plan.json": io.canonical(plan), "normalization/profile.json": io.canonical(profile),
        "normalization/configuration.json": io.canonical(CONFIGURATION)}
    files.update({"implementation/" + path: raw for path, raw in programs.items()})
    def planned(name, path, roles, media="application/json"):
        raw = files[path]
        item = {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(raw), "bytes": len(raw),
                "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
        files.setdefault("objects/sha256/" + item["sha256"], raw)
        return item
    tool = {"name": "wikilean-external-gits", "version": "1", "sha256": io.sha(io.canonical(profile))}
    support = [planned("normalizer_profile", "normalization/profile.json", ["receipt"]),
        planned("normalizer_configuration", "normalization/configuration.json", ["receipt"])]
    support += [planned("normalizer_program_" + str(i), "implementation/" + path, ["receipt"], "text/x-python")
                for i, path in enumerate(sorted(programs))]
    children, child_manifests = [], []
    for source, (output, selected) in reduced.items():
        raw_objects, inputs, normalized = [], [], []
        for key in sorted(selected):
            original = objects[key]
            path = "inputs/sha256/" + original["sha256"]
            files[path] = captured[key]
            item = planned(key[1], path, ["raw"], original["media_type"])
            raw_objects.append(item)
            inputs.append({**io.object_ref(item), "origin": {"kind": "source_manifest", "id": manifests[key[0]]["source_manifest_id"]}})
        for name, raw in sorted(output.items()):
            path = "normalized/" + name + (".jsonl" if name.endswith(("_pages", "_links")) else ".json")
            files[path] = raw
            normalized.append(planned(name, path, ["normalized"], "application/x-ndjson" if path.endswith(".jsonl") else "application/json"))
        parents = {manifests[key[0]]["source_manifest_id"]: manifests[key[0]] for key in selected}
        schema = "wikilean." + source + "/v1"
        lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "normalization_lineage_id": "sha256:" + "0" * 64,
            "source": source, "mode": "transform", "acquisition_receipt_ids": [], "parent_source_manifest_ids": sorted(parents),
            "normalization_schema": schema, "configuration_sha256": io.sha(files["normalization/configuration.json"]), "tool": tool,
            "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
            "outputs": [io.object_ref(item) for item in sorted(normalized, key=lambda item: item["name"])],
            "result": "complete", "audit": {"normalized_at": when}}
        lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
        path = "evidence/" + source + ".json"
        files[path] = io.canonical(lineage)
        child = {"source": source, "source_kind": "sealed_snapshot", "pin": {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]},
            "objects": sorted([*raw_objects, *normalized, *support], key=lambda item: item["name"]),
            "license": {"expression": "LicenseRef-Private-Upstream-Git",
                "redistribution": "restricted", "notice": "Derived from explicitly reviewed private source evidence; publication is not approved."},
            "acquisition": tool, "normalization": {"schema": schema, "tool": tool,
                "inputs": sorted(item["name"] for item in raw_objects), "outputs": sorted(output)},
            "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [], "normalization_lineage": {
                "root": PHYSICAL_ROOT, "path": path, "sha256": io.sha(files[path]), "bytes": len(files[path]),
                "media_type": "application/json", "normalization_lineage_id": lineage["normalization_lineage_id"]}}}
        manifest = io.source_plan_contracts._source_manifest_from_plan(child, "derived external Git source")
        contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
            request_parameter_preimages={}, parent_source_manifests=parents)
        children.append(child)
        child_manifests.append(manifest)
        files["source-manifests/" + source + ".json"] = io.canonical(manifest)
    fragment = {"schema": "wikilean.external-git-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), *children], key=lambda source: source["source"]),
        "input_bindings": [{"input_id": "external-" + family, "state": "present", "sources": list(CHILDREN),
            "members": [{"path": db + "_" + family + ".jsonl", "source": "external-" + db, "object": db + "_" + family}
                        for db in GIT_SOURCES]} for family in ("links", "pages")]}
    files["source-fragment.json"] = io.canonical(fragment)
    document = {"schema": EXPORT_SCHEMA, "normalization_profile_id": profile["profile_id"], "normalized_at": when,
        "source_manifest_ids": sorted(m["source_manifest_id"] for m in child_manifests),
        "files": {path: {"sha256": io.sha(raw), "bytes": len(raw)} for path, raw in sorted(files.items())}}
    document["export_id"] = contracts.domain_hash(EXPORT_SCHEMA, document)
    files["export.json"] = io.canonical(document)
    return files
