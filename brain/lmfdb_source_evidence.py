"""Evidence for one read-only repeatable-read observation of public LMFDB knowls.

The publisher documents devmirror.lmfdb.xyz as its public read-only bulk source:
https://github.com/LMFDB/lmfdb/blob/main/GettingStarted.md . Its self-signed
certificate is explicitly pinned from a retained observation. This does not
claim public-CA authentication or grant permission to publish the derived data.
"""
from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import mathlib_source_evidence as archive

contracts = archive.contracts
EvidenceError = archive.EvidenceError
canonical, sha, parse, exact = archive.canonical, archive.sha, archive.parse, archive.exact
read_regular = archive.read_regular
URI = "postgresql://devmirror.lmfdb.xyz:5432/lmfdb"
SOURCE = "lmfdb-knowl-observation"
PLAN_SCHEMA = "wikilean.lmfdb-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.lmfdb-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.lmfdb-observation-export/v1"
PROFILE_SCHEMA = "wikilean.lmfdb-observation-profiles/v1"
TOOL_SCHEMA = "wikilean.lmfdb-observation-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.lmfdb-response-identity/v1"
REGISTRY = ROOT / "brain/lmfdb_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/lmfdb_source_evidence.py", "brain/lmfdb_sources.py", "brain/lmfdb_source_dependencies.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
POLICY = {"uri": URI, "database_role": "publisher-public-read-only", "network_timeout_seconds": 30,
    "transaction": "repeatable-read/read-only/rollback", "statement_timeout_milliseconds": 120000,
    "network_receive_limit_bytes": 64 * 1024 * 1024, "maximum_rows": 20000,
    "maximum_row_json_bytes": 32 * 1024 * 1024, "tls_minimum": "TLSv1.2",
    "server_identity": "explicit-observed-self-signed-certificate-pin; no public-CA authentication claimed"}
CLIENT_STARTUP = {"client_encoding": "UTF8", "TimeZone": "UTC", "options":
    "-c default_transaction_read_only=on -c statement_timeout=120000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=180000"}
APPLICATION_NAME = "wikilean-private-source-observation-v1"
BEGIN = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
END = "ROLLBACK"
SQL = """WITH latest AS MATERIALIZED (
  SELECT DISTINCT ON (id) id,title,content,links,timestamp,status,type
  FROM public.kwl_knowls ORDER BY id,timestamp DESC
), eligible AS MATERIALIZED (
  SELECT id,title,content,links,timestamp FROM latest WHERE status IN (0,1) AND type=0
), stats AS (
  SELECT count(*) AS rows,coalesce(sum(octet_length(row_to_json(e)::text)),0)::bigint AS bytes FROM eligible e
)
SELECT json_build_object(
 'schema','wikilean.lmfdb-query-response/v1',
 'read_only',current_setting('transaction_read_only'),
 'isolation',current_setting('transaction_isolation'),
 'server_version',current_setting('server_version'),
 'snapshot',pg_current_snapshot()::text,
 'columns',(SELECT json_agg(json_build_object('name',column_name,'type',data_type) ORDER BY ordinal_position)
            FROM information_schema.columns WHERE table_schema='public' AND table_name='kwl_knowls'),
 'row_count',stats.rows,'row_json_bytes',stats.bytes,
 'ambiguous_latest_ids',(SELECT count(*) FROM (
   SELECT k.id FROM public.kwl_knowls k JOIN latest t ON k.id=t.id AND k.timestamp IS NOT DISTINCT FROM t.timestamp
   GROUP BY k.id HAVING count(*)>1
 ) duplicates),
 'rows',CASE WHEN stats.rows<=20000 AND stats.bytes<=33554432 THEN
   (SELECT coalesce(json_agg(json_build_object('id',id,'title',title,'content',content,'links',links,'timestamp',timestamp) ORDER BY id),'[]'::json) FROM eligible)
   ELSE '[]'::json END
)::text FROM stats"""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "LMFDB archive helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.lmfdb-observation-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid LMFDB profile registry")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy", "runtime"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete LMFDB program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program hash")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed LMFDB profile or policy")
        validate_runtime(profile["runtime"])
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current LMFDB profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(read_regular(ROOT / p))} for p in TOOL_FILES], "unreviewed current LMFDB implementation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "LMFDB program preimages differ from reviewed generation")


def validate_plan(plan):
    exact(plan, {"schema", "source", "uri", "minimum_rows", "peer_certificate_sha256"}, "LMFDB plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] == SOURCE and plan["uri"] == URI, "unsupported LMFDB source")
    require(type(plan["minimum_rows"]) is int and 1 <= plan["minimum_rows"] <= POLICY["maximum_rows"], "invalid LMFDB completeness floor")
    contracts._digest(plan["peer_certificate_sha256"], "certificate pin")
    return plan


def parameters(plan):
    validate_plan(plan)
    return {"uri": URI, "transport": {**POLICY, "peer_certificate_sha256": plan["peer_certificate_sha256"]},
        "transaction_commands": [BEGIN, SQL, END], "startup_parameters": CLIENT_STARTUP, "application_name": APPLICATION_NAME}


def request(plan):
    return {"kind": "database_query", "uri": URI, "parameters_sha256": sha(canonical(parameters(plan)))}


def validate_runtime(runtime):
    exact(runtime, {"python", "postgres_driver"}, "driver runtime")
    driver = exact(runtime["postgres_driver"], {"packages", "module_loading", "files"}, "driver")
    expected = {"pg8000": {"distribution": "pg8000", "version": "1.31.5"}, "scramp": {"distribution": "scramp", "version": "1.4.17"},
        "asn1crypto": {"distribution": "asn1crypto", "version": "1.5.1"}, "dateutil": {"distribution": "python_dateutil", "version": "2.9.0.post0"},
        "six": {"distribution": "six", "version": "1.17.0"}}
    require(driver["packages"] == expected and driver["module_loading"] == "captured-source-only; no bytecode cache", "unreviewed LMFDB driver generation")
    require(isinstance(driver["files"], list) and driver["files"], "missing complete driver package files")
    names = []
    for entry in driver["files"]:
        exact(entry, {"path", "sha256", "bytes"}, "driver file")
        contracts.validate_literal_relative_path(entry["path"], "driver file")
        contracts._digest(entry["sha256"], "driver file hash")
        require(type(entry["bytes"]) is int and 0 <= entry["bytes"] <= 16 * 1024 * 1024, "invalid driver file size")
        names.append(entry["path"])
    require(names == sorted(set(names)), "driver closure paths are not uniquely sorted")
    contracts.execution_environment_contract._validate_python(runtime["python"])
    require(runtime["python"]["version"].startswith("3.12."), "LMFDB driver requires CPython3.12")


def verify_dependencies(runtime, dependencies):
    validate_runtime(runtime)
    driver = runtime["postgres_driver"]
    require(isinstance(dependencies, dict) and driver["files"] == [{"path": n, "sha256": sha(raw), "bytes": len(raw)} for n, raw in sorted(dependencies.items())], "retained driver package files differ")
    for name in dependencies:
        contracts.validate_literal_relative_path(name, "driver file")


def validate_tool(tool, dependencies):
    exact(tool, {"schema", "profile_id", "files", "python_startup", "runtime"}, "tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed LMFDB acquisition tool")
    require(isinstance(tool["python_startup"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python_startup"]), "LMFDB acquisition requires isolated CPython3.12")
    verify_dependencies(tool["runtime"], dependencies)
    require(tool["runtime"] == profile["runtime"] and tool["python_startup"] ==
        "CPython " + tool["runtime"]["python"]["version"] + " -I -S", "driver runtime differs from reviewed whole generation")
    return profile


def normalize(plan, raw, transport, certificate):
    validate_plan(plan)
    exact(transport, {"peer_certificate_sha256", "tls_version", "cipher", "transaction_rolled_back", "response_bytes", "response_sha256"}, "transport")
    require(sha(certificate) == transport["peer_certificate_sha256"] == plan["peer_certificate_sha256"], "LMFDB observed certificate differs from explicit pin")
    require(transport["tls_version"] in {"TLSv1.2", "TLSv1.3"} and isinstance(transport["cipher"], str) and transport["cipher"] and
        transport["transaction_rolled_back"] is True, "LMFDB TLS or read-only transaction did not complete")
    require(type(transport["response_bytes"]) is int and transport["response_bytes"] == len(raw) and transport["response_sha256"] == sha(raw) and
        0 < len(raw) <= POLICY["network_receive_limit_bytes"], "LMFDB captured response differs or exceeds bound")
    data = exact(parse(raw, "LMFDB response", artifact=True), {"schema", "read_only", "isolation", "server_version", "snapshot", "columns",
        "row_count", "row_json_bytes", "ambiguous_latest_ids", "rows"}, "response")
    require(data["schema"] == "wikilean.lmfdb-query-response/v1" and data["read_only"] == "on" and data["isolation"] == "repeatable read", "LMFDB read-only snapshot settings differ")
    require(isinstance(data["server_version"], str) and data["server_version"] and isinstance(data["snapshot"], str) and
        re.fullmatch(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", data["snapshot"]), "missing LMFDB server/snapshot identity")
    require(isinstance(data["columns"], list), "missing LMFDB schema")
    columns = {}
    for column in data["columns"]:
        exact(column, {"name", "type"}, "column")
        require(isinstance(column["name"], str) and column["name"] not in columns and isinstance(column["type"], str), "duplicate or invalid LMFDB column")
        columns[column["name"]] = column["type"]
    required = {"id": "text", "title": "text", "content": "text", "links": "ARRAY", "timestamp": "timestamp without time zone", "status": "smallint", "type": "smallint"}
    require(all(columns.get(k) == v for k, v in required.items()), "LMFDB query schema drifted")
    require(type(data["row_count"]) is int and plan["minimum_rows"] <= data["row_count"] <= POLICY["maximum_rows"] and
        type(data["row_json_bytes"]) is int and 0 < data["row_json_bytes"] <= POLICY["maximum_row_json_bytes"] and
        type(data["ambiguous_latest_ids"]) is int and data["ambiguous_latest_ids"] == 0, "LMFDB row count/budget/latest-revision checks failed")
    require(isinstance(data["rows"], list) and len(data["rows"]) == data["row_count"], "LMFDB row set is incomplete")
    ids = []
    for row in data["rows"]:
        exact(row, {"id", "title", "content", "links", "timestamp"}, "knowl")
        require(isinstance(row["id"], str) and row["id"] and all(row[k] is None or isinstance(row[k], str) for k in ("title", "content", "timestamp")), "invalid LMFDB knowl")
        require(row["links"] is None or (isinstance(row["links"], list) and all(x is None or isinstance(x, str) for x in row["links"])), "invalid LMFDB knowl links")
        ids.append(row["id"])
    require(len(ids) == len(set(ids)), "duplicate LMFDB knowl identity")
    return data


def body_ref(raw):
    return {"object": "knowl_query_response", "sha256": sha(raw), "bytes": len(raw), "media_type": "application/json"}


def capture_files(plan, raw, transport, certificate, tool, programs, dependencies, when):
    profile = validate_tool(tool, dependencies)
    verify_programs(profile, programs)
    normalize(plan, raw, transport, certificate)
    req = request(plan)
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": SOURCE,
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": URI,
        "tool": {"name": "wikilean-lmfdb-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": [req], "batch": {"status": "complete", "requests_total": 1, "requests_succeeded": 1, "requests_failed": 0,
            "request_set_root": contracts.acquisition_request_set_root([req])}, "outputs": [body_ref(raw)], "audit": {"acquired_at": when}}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    return archive.manifest_files({"plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile),
        "requests/" + req["parameters_sha256"] + ".json": canonical(parameters(plan)), "transport.json": canonical(transport), "peer-certificate.der": certificate,
        "raw/query-response.json": raw, "receipt.json": canonical(receipt),
        **{"implementation/" + name: data for name, data in programs.items()},
        **{"dependencies/" + name: data for name, data in dependencies.items()}}, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan, tool, transport = [parse(files[k + ".json"], k) for k in ("plan", "tool", "transport")]
    programs = {n: files["implementation/" + n] for n in TOOL_FILES}
    dependencies = {n.removeprefix("dependencies/"): raw for n, raw in files.items() if n.startswith("dependencies/")}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, files["raw/query-response.json"], transport, files["peer-certificate.der"], tool, programs, dependencies, when)
    require(files == {n: raw for n, raw in expected.items() if n != "manifest.json"}, "LMFDB capture closure differs from independent replay")
    return plan, files["raw/query-response.json"], tool


def verify_capture(path):
    origins()
    files, manifest = archive.read_bundle(path, CAPTURE_SCHEMA)
    verify_capture_files(files)
    return files, manifest


def build_export(capture, profile, programs, when):
    plan, raw, tool = verify_capture_files(capture)
    verify_programs(profile, programs)
    files = {"acquisition/" + name: data for name, data in capture.items()}
    files.update({"implementation/" + name: data for name, data in programs.items()})
    files["normalization/profile.json"] = canonical(profile)
    physical_root = "lmfdb_observation_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("knowl_query_response", raw, ["raw", "normalized"])
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-lmfdb-response-identity", "version": "1", "sha256": sha(canonical(profile))}
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": SOURCE, "mode": "identity",
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "inputs": [{**body_ref(raw), "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        "outputs": [body_ref(raw)], "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    support = [planned("normalization_profile", canonical(profile), ["receipt"]), planned("normalization_plan", canonical(plan), ["receipt"]),
        planned("acquisition_profile", capture["profile.json"], ["receipt"]), planned("acquisition_tool", canonical(tool), ["receipt"]),
        planned("acquisition_transport", capture["transport.json"], ["receipt"]),
        planned("acquisition_peer_certificate", capture["peer-certificate.der"], ["receipt"], "application/pkix-cert")]
    for prefix, retained in (("normalizer", programs), ("acquirer", {n: capture["implementation/" + n] for n in TOOL_FILES})):
        support.extend(planned(prefix + "_program_" + str(i), retained[n], ["receipt"], "text/x-python") for i, n in enumerate(TOOL_FILES))
    dependency_paths = sorted(n for n in capture if n.startswith("dependencies/"))
    support.extend(planned("acquirer_dependency_" + str(i), capture[n], ["receipt"], "application/octet-stream") for i, n in enumerate(dependency_paths))
    def evidence_ref(path, **identity):
        return {"root": physical_root, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    preimages = [evidence_ref("acquisition/requests/" + row["parameters_sha256"] + ".json", parameters_sha256=row["parameters_sha256"])
        for row in receipt["requests"]]
    source = {"source": SOURCE, "source_kind": "acquired_dataset", "pin": receipt["pin"], "objects": sorted([body, *support], key=lambda o: o["name"]),
        "license": {"expression": "CC-BY-SA-4.0", "redistribution": "restricted",
            "notice": "Private read-only LMFDB knowl observation. Observed self-signed certificate is explicitly pinned; no public-CA authentication claimed. Publication requires attribution and separate source-policy review."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["knowl_query_response"], "outputs": ["knowl_query_response"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda p: p["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "LMFDB source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.lmfdb-observation-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source], "input_bindings": []})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    require(build_export(capture, profile, programs, when) == {**files, "manifest.json": canonical(manifest)}, "LMFDB source export differs from independent replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    response = parse(capture["raw/query-response.json"], "response", artifact=True)
    return {"source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"],
        "facts": {"knowls": response["row_count"], "response_bytes": len(capture["raw/query-response.json"]), "read_only": response["read_only"],
            "isolation": response["isolation"], "snapshot": response["snapshot"], "server_version": response["server_version"]}}
