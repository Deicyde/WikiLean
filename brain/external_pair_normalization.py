"""Pure, explicitly selected prefix of the reviewed external pair writer.

Callers must bind common_program bytes and these three files (this helper,
brain/ingest/common.py, brain/build_context.py) in their approved tool profile.
The returned rows retain the writer's exact cleanup and pair-envelope semantics;
no cache, output directory, lock, journal, clock, or network operation is called.
"""
from __future__ import annotations

import ast
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for directory in (ROOT / "brain", ROOT / "brain/ingest"):
    sys.path.append(str(directory))
import common
import build_context

TOOL_FILES = ("brain/build_context.py", "brain/external_pair_normalization.py", "brain/ingest/common.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def origins():
    for module, path in ((common, "brain/ingest/common.py"), (build_context, "brain/build_context.py")):
        require(Path(getattr(module, "__file__", "")).resolve() == ROOT / path, "unexpected pair normalization helper origin: " + path)
    require(common.seal_external_pair_meta is build_context.seal_external_pair_meta and
        common.validate_external_pair is build_context.validate_external_pair, "pair normalizer has different helper imports")


origins()


def normalize_pair(db, pages, links, meta, program):
    origins()
    # Preserve the exact reviewed writer's row/metadata semantics. Its first
    # top-level `with` starts the filesystem transaction; it is never included.
    tree = ast.parse(program, filename="sealed:brain/ingest/common.py")
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "emit"]
    require(len(functions) == 1, "legacy pair normalizer missing")
    function = copy.deepcopy(functions[0])
    boundary = next((i for i, node in enumerate(function.body) if isinstance(node, ast.With)), None)
    require(boundary is not None and isinstance(function.body[boundary - 1], ast.Expr) and
        isinstance(function.body[boundary - 1].value, ast.Call) and
        isinstance(function.body[boundary - 1].value.func, ast.Name) and
        function.body[boundary - 1].value.func.id == "validate_external_pair", "legacy pure pair boundary differs")
    function.body = function.body[:boundary] + [ast.Return(value=ast.Tuple(elts=[ast.Name(id=name, ctx=ast.Load())
        for name in ("meta", "kept_pages", "kept_links")], ctx=ast.Load()))]
    namespace = dict(common.__dict__)
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "sealed:pair-normalizer", "exec"), namespace)
    return namespace["emit"](db, pages, links, meta)

