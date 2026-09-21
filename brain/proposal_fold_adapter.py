"""Run the reviewed proposal fold over an explicit in-memory file generation.

The original function bodies remain the algorithm. Only filesystem, acquisition,
argument parsing and source-search boundaries are supplied here. No disk reader,
installed observation pointer, process launcher or network client is available to
the executed functions. Version 1 deliberately covers the existing empty-entity,
non-xref/non-repository proposal generation; widening that scope needs review.
"""
from __future__ import annotations

import __future__
import ast
import builtins
import io
import json
import re
from pathlib import PurePosixPath
from types import SimpleNamespace


class FoldError(ValueError):
    pass


FUNCTIONS = frozenset({
    "is_qid", "oracle_names", "oracle_module", "resolve_module", "hierarchy_paths",
    "crossref_dbs", "row_key", "frontier_repo_keys", "frontier_decl_names",
    "fc_decl_names", "_hashable_key", "_completed_retract_key", "request_plan_bytes", "main",
})
REQUIRED = frozenset({
    "catalog/data/rebuild_grounding.json", "catalog/data/hierarchy.json",
    "catalog/data/source_registry.json", "catalog/data/wikidata_universe.jsonl",
    "catalog/data/universe_extension.jsonl", "catalog/data/grounding_overrides.jsonl",
    "catalog/data/formal_conjectures.jsonl", "oracle.json",
})
OUTPUTS = frozenset({
    "brain/data/container_links.jsonl", "brain/data/discovery_proposals.jsonl",
    "brain/data/discovery_rejected.jsonl", "brain/data/grading_disputes.jsonl",
    "brain/data/fc_links.jsonl", "catalog/data/grounding_overrides.jsonl",
    "catalog/data/universe_extension.jsonl", "request-plan.json",
})
EMPTY_PLAN = b'{"qids":[],"schema":"wikilean.wikidata-entity-request-plan/v1"}'


def require(value, message):
    if not value:
        raise FoldError(message)


def rows(raw):
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


class CapturedFiles:
    def __init__(self, files):
        self.files = dict(files)
        self.written = set()
        self.allowed = set(OUTPUTS) | {path + ".tmp" for path in OUTPUTS}
        self.reads = set()

    def path(self, value):
        if isinstance(value, CapturedPath):
            require(value.owner is self, "foreign captured file generation")
            return value
        path = PurePosixPath(value)
        require(not path.is_absolute() and ".." not in path.parts, "captured path escapes its generation")
        return CapturedPath(self, str(path))


class CapturedPath:
    def __init__(self, owner, path):
        self.owner, self.value = owner, PurePosixPath(path)

    def __str__(self):
        return str(self.value)

    def __truediv__(self, other):
        return self.owner.path(self.value / other)

    @property
    def name(self):
        return self.value.name

    @property
    def suffix(self):
        return self.value.suffix

    def with_suffix(self, suffix):
        return self.owner.path(self.value.with_suffix(suffix))

    def exists(self):
        name = str(self)
        return name in self.owner.files or name == "Mathlib" or any(
            path.startswith(name + "/") for path in self.owner.files)

    def glob(self, pattern):
        require(pattern == "*.jsonl", "unsupported captured glob")
        return [self.owner.path(path) for path in sorted(self.owner.files)
                if PurePosixPath(path).parent == self.value and PurePosixPath(path).match(pattern)]

    def __lt__(self, other):
        return str(self) < str(other)

    def read_text(self):
        self.owner.reads.add(str(self))
        return self.owner.files[str(self)].decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def write_text(self, text):
        require(str(self) in self.owner.allowed, "fold writes an undeclared output")
        self.owner.files[str(self)] = text.encode("utf-8")
        self.owner.written.add(str(self))
        return len(text)

    def replace(self, destination):
        destination = self.owner.path(destination)
        require(str(self) in self.owner.allowed and str(destination) in OUTPUTS,
                "fold replaces an undeclared output")
        self.owner.files[str(destination)] = self.owner.files.pop(str(self))
        self.owner.written.discard(str(self))
        self.owner.written.add(str(destination))

    def open(self, mode="r"):
        require(mode in {"r", "a"}, "unsupported captured open mode")
        if mode == "r":
            return io.StringIO(self.read_text())
        require(str(self) in OUTPUTS, "fold appends an undeclared output")
        initial = self.owner.files.get(str(self), b"")
        owner, path = self.owner, self

        class Appender(io.StringIO):
            def close(handle):
                if not handle.closed:
                    owner.files[str(path)] = initial + handle.getvalue().encode("utf-8")
                    owner.written.add(str(path))
                super().close()

        return Appender()


def fold(program, files, mathlib, *, comparison_fc_seed=None):
    """Return exact legacy output bytes and a deterministic boundary audit.

    comparison_fc_seed is only for the separately reported legacy comparison;
    the authoritative proposal stream always starts without an opaque FC seed.
    Mathlib keys are full native paths below Mathlib/, with exact captured bytes.
    """
    require(REQUIRED <= set(files), "fold input generation is incomplete")
    proposal_paths = sorted(path for path in files if path.startswith("brain/proposals/"))
    require(proposal_paths and all(PurePosixPath(path).parent == PurePosixPath("brain/proposals")
            and path.endswith(".jsonl") for path in proposal_paths), "invalid proposal shard scope")
    require(set(files) == REQUIRED | set(proposal_paths), "undeclared fold input or prior folded seed")
    for path in proposal_paths:
        if path.endswith(".verified.jsonl"):
            require(path.removesuffix(".verified.jsonl") in proposal_paths,
                    "orphan skeptic shard would be ignored by legacy fold")
        for row in rows(files[path]):
            require(isinstance(row, dict) and row.get("action") not in {"xref", "repo_link"},
                    "proposal fold v1 does not cover external/repository joins")
    require(mathlib and all(path.startswith("Mathlib/") and ".." not in PurePosixPath(path).parts
            and isinstance(raw, bytes) for path, raw in mathlib.items()), "exact Mathlib source tree is required")
    generation = CapturedFiles(files)
    if comparison_fc_seed is not None:
        generation.files["brain/data/fc_links.jsonl"] = comparison_fc_seed
    source_matches = {}

    def matches(decl):
        if decl not in source_matches:
            segment = decl.split(".")[-1]
            pattern = re.compile(r"(theorem|lemma|def|abbrev|structure|class|instance|inductive) +"
                r"([A-Za-z0-9_'.«»]+\.)?" + re.escape(segment) + r"($|[^A-Za-z0-9_'])", re.MULTILINE)
            source_matches[decl] = [path for path, raw in sorted(mathlib.items())
                if b"\0" not in raw and pattern.search(raw.decode("utf-8"))]
        return source_matches[decl]

    def checkout_module(decl):
        paths = [path for path in matches(decl) if path.endswith(".lean")]
        return paths[0].removeprefix("Mathlib/").removesuffix(".lean").replace("/", ".") if len(paths) == 1 else None

    def known_qids():
        result = {}
        for path in ("catalog/data/wikidata_universe.jsonl", "catalog/data/universe_extension.jsonl"):
            for row in rows(generation.files[path]):
                if row.get("qid"):
                    result[row["qid"]] = row
        return result

    def unavailable(*args, **kwargs):
        raise FoldError("fold reached an unsupported acquisition/external boundary")

    log = io.StringIO()
    arguments = SimpleNamespace(write_wikidata_request_plan=generation.path("request-plan.json"), wikidata_entity_bundle=None)
    namespace = {
        "__builtins__": {k: v for k, v in vars(builtins).items()
                         if k not in {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}},
        "json": json, "re": re, "Path": generation.path,
        "sys": SimpleNamespace(stderr=log, exit=lambda message: unavailable(message)),
        "print": lambda *args, **kwargs: builtins.print(*args, **{**kwargs, "file": log}),
        "glob": SimpleNamespace(glob=lambda pattern: [str(path) for path in generation.path("brain/proposals").glob("*.jsonl")]
                                if pattern == "brain/proposals/*.jsonl" else unavailable()),
        "DATA": generation.path("brain/data"), "CATALOG": generation.path("catalog/data"),
        "PROPOSALS": generation.path("brain/proposals"), "ORACLE": generation.path("oracle.json"),
        "CHECKOUT": generation.path("Mathlib"),
        "REQUEST_PLAN_SCHEMA": "wikilean.wikidata-entity-request-plan/v1",
        "_oracle_modules": None, "_frontier_names": {}, "_parse_args": lambda argv: arguments,
        "checkout_has": lambda decl: bool(matches(decl)), "checkout_module": checkout_module,
        "known_qids": known_qids, "external_page_ids": unavailable,
        "verify_wikidata_entity_bundle": unavailable, "WikidataEntityBundleError": FoldError,
        "write_request_plan": lambda path, qids: path.write_text(namespace["request_plan_bytes"](qids).decode("utf-8")),
    }
    module = ast.parse(program.decode("utf-8"), filename="brain/fold_proposals.py")
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    require({node.name for node in functions} == FUNCTIONS and len(functions) == len(FUNCTIONS),
            "reviewed fold function boundary differs")
    constant_names = {"QID_RE", "CONF_ORDER", "REPO_KEY_RE"}
    constants = [node for node in module.body if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constant_names]
    require({node.targets[0].id for node in constants} == constant_names and len(constants) == len(constant_names),
            "reviewed fold constant boundary differs")
    selected = ast.Module(body=[*constants, *functions], type_ignores=[])
    require(not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(selected)),
            "selected fold algorithm introduced an ambient import")
    exec(compile(selected, "brain/fold_proposals.py", "exec", flags=__future__.annotations.compiler_flag), namespace)
    require(namespace["main"]([]) == 0, "proposal planning failed")
    require(generation.files["request-plan.json"] == EMPTY_PLAN,
            "proposal fold v1 requires independently proved empty entity request plan")
    arguments.write_wikidata_request_plan = None
    require(namespace["main"]([]) == 0, "proposal fold failed")
    outputs = {path: generation.files[path] for path in sorted(generation.written)}
    require(set(outputs) == OUTPUTS, "fold output closure differs from reviewed v1 scope")
    audit = {"schema": "wikilean.proposal-fold-boundary/v1", "entity_request_plan": json.loads(EMPTY_PLAN),
        "proposal_files": proposal_paths, "source_rescue_matches": source_matches,
        "file_reads": sorted(generation.reads), "log": log.getvalue(),
        "fc_seed": "absent" if comparison_fc_seed is None else "comparison-only"}
    return outputs, audit
