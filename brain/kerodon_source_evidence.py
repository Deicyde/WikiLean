"""Complete Kerodon structures and every derived tag's full content.

All explicit HTTP attempts are retained. Live responses are observations, never
an invented upstream snapshot; no cached or missing content certifies a source.
"""
from __future__ import annotations

import ast
import html
import types
import base64
import copy
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import mathlib_source_evidence as archive

contracts = archive.contracts
EvidenceError = archive.EvidenceError
canonical, sha, parse, exact = archive.canonical, archive.sha, archive.parse, archive.exact
read_regular = archive.read_regular
BASE = "https://kerodon.net/"
API = BASE
SOURCE = "kerodon-tag-walk"
PLAN_SCHEMA = "wikilean.kerodon-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.kerodon-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.kerodon-observation-export/v1"
TRANSCRIPT_SCHEMA = "wikilean.kerodon-observation-transcript/v1"
PROFILE_SCHEMA = "wikilean.kerodon-observation-profiles/v1"
TOOL_SCHEMA = "wikilean.kerodon-observation-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.kerodon-response-identity/v1"
REGISTRY = ROOT / "brain/kerodon_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/kerodon_source_evidence.py", "brain/kerodon_sources.py", "brain/ingest/kerodon.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
PARSER_SYMBOLS = {"HREF", "PAGE_URL", "flatten"}
ROOT_TAGS = ("0000", "02GZ")
TAG = re.compile(r"[0-9A-Z]{4}\Z")
RETRY_POLICY = {"maximum_attempts_per_request": 5, "http_statuses": [429, 502, 503, 504],
    "curl_transport_codes": [5, 6, 7, 28, 35, 52, 55, 56], "backoff_seconds": [5, 15, 30, 60],
    "maximum_retry_after_seconds": 120, "retry_after": "bounded integer seconds only; other present values abort"}
POLICY = {"uri": BASE, "observation": "independent-live-requests/no-snapshot", "roots": list(ROOT_TAGS),
    "selection": "both original structures followed by every unique derived tag in lexical order; full content once successfully",
    "maximum_requests": 10002, "maximum_tags": 10000, "maximum_attempts": 50010,
    "maximum_response_bytes": 8 * 1024 * 1024, "maximum_total_response_bytes": 256 * 1024 * 1024,
    "maximum_transcript_bytes": 432 * 1024 * 1024, "maximum_structure_depth": 16, "maximum_structure_nodes": 50000,
    "maximum_links": 2000000, "minimum_request_interval_milliseconds": 1200,
    "response_media_types": ["application/json", "application/xhtml+xml", "text/html"],
    "parser_symbols": sorted(PARSER_SYMBOLS), "retry": RETRY_POLICY}
USER_AGENT = "WikiLean-source-evidence/1.0 (https://github.com/Deicyde/WikiLean; identifiers and links only)"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "Kerodon helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.kerodon-observation-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid Kerodon profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete Kerodon program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed Kerodon policy or profile identity")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current Kerodon profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(read_regular(ROOT / p))} for p in TOOL_FILES], "unreviewed current Kerodon program generation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "Kerodon program preimages differ from reviewed whole generation")


def validate_plan(plan):
    exact(plan, {"schema", "source", "uri", "minimum_pages", "minimum_links"}, "Kerodon plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] == SOURCE and plan["uri"] == API, "unsupported Kerodon source or endpoint")
    for key, maximum in (("minimum_pages", POLICY["maximum_tags"]), ("minimum_links", POLICY["maximum_links"])):
        require(type(plan[key]) is int and 1 <= plan[key] <= maximum, "invalid reviewed Kerodon completeness floor")
    return plan


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed Kerodon tool generation")
    for key in ("python", "curl"):
        exact(tool[key], {"sha256", "version"}, "executable")
        contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "Kerodon acquisition requires isolated CPython3.12")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid curl identity")
    return profile


def parser(program):
    tree = ast.parse(program, filename="sealed:brain/ingest/kerodon.py")
    selected, names = [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(node); names.append(name)
    require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)), "legacy Kerodon parser selector differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace = {"re": re}
    exec(compile(ast.fix_missing_locations(module), "sealed:kerodon-parser", "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def parameters(name):
    require(isinstance(name, str) and re.fullmatch(r"(?:structure|content)_[0-9A-Z]{4}", name), "unreviewed Kerodon request target")
    kind, tag = name.split("_")
    require(kind != "structure" or tag in ROOT_TAGS, "unreviewed Kerodon root")
    return {"method": "GET", "uri": BASE + "data/tag/" + tag + ("/structure" if kind == "structure" else "/content/full"),
        "headers": {"Accept": "application/json, text/html, application/xhtml+xml", "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "implicit_retries": 0, "recorded_retry_policy": RETRY_POLICY, "cache": False,
            "connect_timeout_seconds": 30, "timeout_seconds": 120, "maximum_response_bytes": POLICY["maximum_response_bytes"]}}


def request(params):
    return {"kind": "http_get", "uri": params["uri"], "parameters_sha256": sha(canonical(params))}


def response_bytes(record):
    metadata = exact(record["response"], {"curl_exit_code", "http_status", "content_type", "sha256", "bytes", "retry_after"}, "response")
    require(type(metadata["curl_exit_code"]) is int and type(metadata["http_status"]) is int and
        0 <= metadata["http_status"] <= 599 and isinstance(metadata["content_type"], str) and
        len(metadata["content_type"]) <= 128, "invalid Kerodon response status")
    retry = metadata["retry_after"]
    require(retry is None or retry == {"kind": "unsupported-or-excessive"} or
        (isinstance(retry, dict) and set(retry) == {"kind", "seconds"} and retry["kind"] == "delay-seconds" and
         type(retry["seconds"]) is int and 0 <= retry["seconds"] < 10**15), "invalid retained Retry-After")
    try:
        raw = base64.b64decode(record["body_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise EvidenceError("invalid Kerodon retained response encoding") from exc
    require(base64.b64encode(raw).decode("ascii") == record["body_base64"] and
        type(metadata["bytes"]) is int and metadata["bytes"] == len(raw) and sha(raw) == metadata["sha256"] and
        len(raw) <= POLICY["maximum_response_bytes"], "Kerodon response bytes differ or exceed bound")
    return raw


def retry_delay(metadata, ordinal):
    if ordinal >= RETRY_POLICY["maximum_attempts_per_request"]:
        return None
    status, code = metadata["http_status"], metadata["curl_exit_code"]
    eligible = (status in RETRY_POLICY["http_statuses"] and code in (0, 22)) or (
        status == 0 and code in RETRY_POLICY["curl_transport_codes"])
    if not eligible:
        return None
    delay = RETRY_POLICY["backoff_seconds"][ordinal - 1]
    after = metadata["retry_after"]
    if after is not None:
        if after.get("kind") != "delay-seconds": return None
        delay = max(delay, after["seconds"])
    return delay if delay <= RETRY_POLICY["maximum_retry_after_seconds"] else None


def structure_document(raw, root):
    try:
        value = parse(raw, "Kerodon structure", artifact=True)
    except contracts.VerificationError as exc:
        raise EvidenceError("invalid Kerodon structure JSON") from exc
    require(isinstance(value, dict) and value.get("tag") == root and value.get("type") == "part", "Kerodon structure does not identify requested root")
    pending, nodes, visits = [(value, 0)], {}, 0
    while pending:
        node, depth = pending.pop()
        visits += 1
        require(visits <= POLICY["maximum_structure_nodes"], "Kerodon structure exceeds node bound")
        require(isinstance(node, dict) and {"tag", "type", "reference"} <= set(node) <= {"tag", "type", "reference", "name", "children"}, "invalid Kerodon structure node")
        require(depth <= POLICY["maximum_structure_depth"] and isinstance(node["tag"], str) and TAG.fullmatch(node["tag"]), "invalid Kerodon tag or structure depth")
        require(all(isinstance(node[k], str) and len(node[k]) <= 16384 for k in ("type", "reference")) and
            node["type"] and ("name" not in node or isinstance(node["name"], str) and len(node["name"]) <= 16384), "invalid Kerodon node attributes")
        identity = {k: v for k, v in node.items() if k != "children"}
        # Gerby repeats tags in nested structure projections. Preserve legacy
        # first-seen semantics, while refusing inconsistent identities.
        require(node["tag"] not in nodes or nodes[node["tag"]] == identity, "conflicting duplicate tag inside Kerodon structure")
        nodes.setdefault(node["tag"], identity)
        require(len(nodes) <= POLICY["maximum_tags"], "Kerodon structure exceeds tag bound")
        require(node.get("children") is None or isinstance(node["children"], list), "invalid Kerodon children")
        children = node.get("children") or []
        require(isinstance(children, list) and len(children) <= POLICY["maximum_tags"], "invalid Kerodon children")
        pending.extend((child, depth + 1) for child in reversed(children))
    return value, nodes


class WalkState:
    """Both roots define the exact required tag set; every HTTP attempt is retained."""
    def __init__(self, plan, parser_program):
        self.plan = validate_plan(plan)
        self.syntax = parser(parser_program)
        self.names = ["structure_" + root for root in ROOT_TAGS]
        self.nodes, self.pages, self.links, self.contents = {}, {}, set(), set()
        self.total, self.count, self.attempt_count, self.ordinal, self.transcript_bytes = 0, 0, 0, 1, 128
        self.failures = 0

    @property
    def next_name(self):
        return self.names[self.count] if self.count < len(self.names) else None

    def accept(self, record):
        require(self.attempt_count < POLICY["maximum_attempts"], "Kerodon attempt budget exceeded")
        exact(record, {"request", "response", "body_base64", "attempt", "outcome", "retry_delay_seconds"}, "transcript record")
        name = self.next_name
        require(name is not None and record["request"] == parameters(name) and
            type(record["attempt"]) is int and record["attempt"] == self.ordinal, "Kerodon attempt does not follow complete derived tag list")
        require(type(record["retry_delay_seconds"]) is int and record["retry_delay_seconds"] >= 0, "invalid Kerodon retry delay")
        raw = response_bytes(record)
        self.total += len(raw)
        self.transcript_bytes += len(canonical(record)) + 1
        require(self.total <= POLICY["maximum_total_response_bytes"] and self.transcript_bytes <= POLICY["maximum_transcript_bytes"], "Kerodon walk exceeds response or transcript budget")
        if record["outcome"] == "failed":
            delay = retry_delay(record["response"], self.ordinal)
            require(delay is not None and record["retry_delay_seconds"] == delay, "nonretryable, exhausted or wrong-delay failure in Kerodon complete transcript")
            self.ordinal += 1; self.attempt_count += 1; self.failures += 1
            return
        require(record["outcome"] == "succeeded" and record["retry_delay_seconds"] == 0, "invalid Kerodon outcome")
        metadata = record["response"]
        require(metadata["curl_exit_code"] == 0 and metadata["http_status"] == 200 and
            metadata["content_type"] in POLICY["response_media_types"] and raw, "Kerodon HTTP request did not succeed with content")
        kind, tag = name.split("_")
        if kind == "structure":
            value, nodes = structure_document(raw, tag)
            require(all(key not in self.nodes or self.nodes[key] == node for key, node in nodes.items()), "conflicting duplicate Kerodon tag across roots")
            self.nodes.update(nodes)
            require(len(self.nodes) <= POLICY["maximum_tags"], "Kerodon union exceeds tag bound")
            self.syntax.flatten(value, self.pages)
        else:
            text = raw.decode("utf-8", "replace")
            require(re.search(r'(?:data-tag|id)="' + tag + r'"', text) and re.search(r'</[A-Za-z][A-Za-z0-9]*>\s*$', text),
                "Kerodon content lacks requested tag identity or closing fragment boundary")
            self.contents.add(tag)
            self.links.update((tag, destination) for destination in self.syntax.HREF.findall(text) if destination != tag)
            require(len(self.links) <= POLICY["maximum_links"], "Kerodon links exceed bound")
        self.count += 1; self.attempt_count += 1; self.ordinal = 1
        if self.count == len(ROOT_TAGS):
            require(len(self.pages) >= self.plan["minimum_pages"], "Kerodon enumerated tags fall below reviewed floor")
            self.names.extend("content_" + tag for tag in sorted(self.pages))
            require(len(self.names) <= POLICY["maximum_requests"] <= contracts.MAX_ACQUISITION_REQUESTS, "Kerodon derived request list exceeds bound")

    def result(self, *, complete=True):
        if complete:
            require(self.next_name is None and self.count > len(ROOT_TAGS) and self.contents == set(self.pages), "Kerodon transcript ends before every derived tag was acquired")
            require(len(self.pages) >= self.plan["minimum_pages"] and len(self.links) >= self.plan["minimum_links"], "Kerodon observation falls below reviewed completeness floors")
        return {"requests": self.count, "attempts": self.attempt_count, "failed_attempts": self.failures,
            "pages": len(self.pages), "links": len(self.links), "response_bytes": self.total,
            "structure_roots": list(ROOT_TAGS), "tags_enumerated": len(self.pages), "contents_acquired": len(self.contents)}, self.next_name


def replay(plan, records, programs, *, complete=True):
    require(isinstance(records, list) and len(records) <= POLICY["maximum_attempts"], "invalid Kerodon attempt count")
    state = WalkState(plan, programs["brain/ingest/kerodon.py"])
    for record in records:
        state.accept(record)
    return state.result(complete=complete)


def body_ref(raw):
    return {"object": "page_transcript", "sha256": sha(raw), "bytes": len(raw), "media_type": "application/json"}


def capture_files(plan, records, tool, programs, when):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    facts, _ = replay(plan, records, programs)
    raw = canonical({"schema": TRANSCRIPT_SCHEMA, "records": records})
    require(len(raw) <= POLICY["maximum_transcript_bytes"] < archive.MAX_FILE, "Kerodon transcript exceeds source object bound")
    descriptors = {canonical(request(row["request"])): request(row["request"]) for row in records}
    requests = [descriptors[key] for key in sorted(descriptors)]
    indices = {canonical(item): index for index, item in enumerate(requests)}
    attempts = [{"request_index": indices[canonical(request(row["request"]))], "outcome": row["outcome"],
        "response_sha256": row["response"]["sha256"], "response_bytes": row["response"]["bytes"]} for row in records]
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V2, "source": SOURCE,
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": API,
        "tool": {"name": "wikilean-kerodon-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "attempts": attempts, "batch": {"status": "complete", "requests_total": len(records),
            "requests_succeeded": len(requests), "requests_failed": len(records) - len(requests),
            "request_set_root": contracts.acquisition_request_set_root(requests)},
        "outputs": [body_ref(raw)], "audit": {"acquired_at": when}}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    return archive.manifest_files({"plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile),
        "facts.json": canonical(facts), "receipt.json": canonical(receipt), "raw/transcript.json": raw,
        **{"requests/" + request(row["request"])["parameters_sha256"] + ".json": canonical(row["request"]) for row in records},
        **{"implementation/" + name: data for name, data in programs.items()}}, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan, tool = [parse(files[key + ".json"], key) for key in ("plan", "tool")]
    document = exact(parse(files["raw/transcript.json"], "transcript"), {"schema", "records"}, "transcript")
    require(document["schema"] == TRANSCRIPT_SCHEMA, "unsupported Kerodon transcript")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, document["records"], tool, programs, when)
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"}, "Kerodon capture differs from independently replayed closure")
    return plan, files["raw/transcript.json"], tool


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
    physical_root = "kerodon_observation_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("page_transcript", raw, ["raw", "normalized"])
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-kerodon-response-identity", "version": "1", "sha256": sha(canonical(profile))}
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": SOURCE, "mode": "identity",
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "inputs": [{**body_ref(raw), "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        "outputs": [body_ref(raw)], "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    support = [planned("normalization_profile", canonical(profile), ["receipt"]), planned("normalization_plan", canonical(plan), ["receipt"]),
        planned("acquisition_profile", capture["profile.json"], ["receipt"]), planned("acquisition_tool", canonical(tool), ["receipt"])]
    for prefix, retained in (("normalizer", programs), ("acquirer", {n: capture["implementation/" + n] for n in TOOL_FILES})):
        support.extend(planned(prefix + "_program_" + str(i), retained[n], ["receipt"], "text/x-python") for i, n in enumerate(TOOL_FILES))
    def evidence_ref(path, **identity):
        return {"root": physical_root, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    preimages = [evidence_ref("acquisition/requests/" + row["parameters_sha256"] + ".json", parameters_sha256=row["parameters_sha256"])
        for row in receipt["requests"]]
    source = {"source": SOURCE, "source_kind": "acquired_dataset", "pin": receipt["pin"], "objects": sorted([body, *support], key=lambda o: o["name"]),
        "license": {"expression": "LicenseRef-Kerodon-Review", "redistribution": "restricted",
            "notice": "Private retained HTML observations support identifiers, titles and hrefs only. Article prose is not emitted as normalized graph data; public redistribution requires separate review."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["page_transcript"], "outputs": ["page_transcript"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda p: p["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "Kerodon source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.kerodon-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source], "input_bindings": []})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    require(build_export(capture, profile, programs, when) == {**files, "manifest.json": canonical(manifest)}, "Kerodon source export differs from independent replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"], "facts": parse(capture["facts.json"], "facts")}
