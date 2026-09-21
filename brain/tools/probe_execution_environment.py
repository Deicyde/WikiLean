#!/usr/bin/env python3
"""Emit canonical live facts from inside the selected replay sandbox."""
from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import execution_environment as environment  # noqa: E402
import oci_runtime  # noqa: E402


def collect_probe_document(
    *,
    numpy_scheme_paths: Mapping[str, str] | None = None,
    python_probe: Callable[[], dict[str, Any]] = environment.probe_python_runtime,
    numpy_probe: Callable[..., dict[str, Any]] = environment.probe_numpy_runtime,
    sqlite_probe: Callable[[], dict[str, Any]] = environment.probe_sqlite_runtime,
    locale_probe: Callable[[], dict[str, Any]] = environment.probe_locale_runtime,
) -> dict[str, Any]:
    """Collect and validate exactly the facts observable inside the process."""
    numpy = (
        numpy_probe()
        if numpy_scheme_paths is None
        else numpy_probe(scheme_paths=numpy_scheme_paths)
    )
    sqlite = sqlite_probe()
    document = {
        "schema": environment.LIVE_PROBE_SCHEMA_V2 if "linkage" in sqlite else environment.LIVE_PROBE_SCHEMA,
        "python": python_probe(),
        "numpy": numpy,
        "sqlite": sqlite,
        "locale": locale_probe(),
    }
    return environment.validate_live_probe_document(document)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if (
        len(argv) not in {4, 6}
        or argv[0] != "--purelib"
        or argv[2] != "--platlib"
    ):
        raise environment.ExecutionEnvironmentError(
            "execution-environment probe requires --purelib PATH --platlib PATH"
        )
    if len(argv) == 6:
        if argv[4] != "--oci-policy-json":
            raise environment.ExecutionEnvironmentError("unexpected execution-environment probe arguments")
        policy = oci_runtime._json(argv[5].encode("utf-8"), "OCI numerical policy")
        oci_runtime.verify_numerical_runtime(policy)
    data = environment.canonical_json_bytes(
        collect_probe_document(
            numpy_scheme_paths={"purelib": argv[1], "platlib": argv[3]}
        )
    )
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
