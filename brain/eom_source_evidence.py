"""Complete, fresh EOM MediaWiki pagination evidence; no article text is requested.

The response chain follows the documented MediaWiki continuation protocol:
https://www.mediawiki.org/wiki/API:Continue . Each response determines the next
request; independent replay proves the final response terminated that exact walk.
This is an observation over independent requests, not an upstream snapshot.
"""
from __future__ import annotations

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
API = "https://encyclopediaofmath.org/api.php"
SOURCE = "eom-api-walk"
PLAN_SCHEMA = "wikilean.eom-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.eom-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.eom-observation-export/v1"
TRANSCRIPT_SCHEMA = "wikilean.eom-observation-transcript/v1"
PROFILE_SCHEMA = "wikilean.eom-observation-profiles/v1"
TOOL_SCHEMA = "wikilean.eom-observation-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.eom-response-identity/v1"
REGISTRY = ROOT / "brain/eom_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/eom_source_evidence.py", "brain/eom_sources.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
QUERY = {"action": "query", "format": "json", "generator": "allpages", "gaplimit": "500",
    "gapnamespace": "0", "prop": "links", "plnamespace": "0", "pllimit": "max"}
POLICY = {"uri": API, "query": QUERY, "observation": "independent-live-requests/no-snapshot",
    "maximum_requests": 1000, "maximum_response_bytes": 8 * 1024 * 1024,
    "maximum_total_response_bytes": 256 * 1024 * 1024, "maximum_pages": 100000,
    "maximum_links": 2000000, "minimum_request_interval_milliseconds": 500,
    "continuation_keys": ["continue", "gapcontinue", "plcontinue"],
    "response_media_types": ["application/json", "text/javascript"]}
USER_AGENT = "WikiLean-source-evidence/1.0 (https://github.com/Deicyde/WikiLean; ids and links only)"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "EOM helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.eom-observation-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid EOM profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete EOM program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed EOM policy or profile identity")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current EOM profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(read_regular(ROOT / p))} for p in TOOL_FILES], "unreviewed current EOM program generation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "EOM program preimages differ from reviewed whole generation")


def validate_plan(plan):
    exact(plan, {"schema", "source", "uri", "minimum_pages", "minimum_links"}, "EOM plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] == SOURCE and plan["uri"] == API, "unsupported EOM source or endpoint")
    for key, maximum in (("minimum_pages", POLICY["maximum_pages"]), ("minimum_links", POLICY["maximum_links"])):
        require(type(plan[key]) is int and 1 <= plan[key] <= maximum, "invalid reviewed EOM completeness floor")
    return plan


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed EOM tool generation")
    for key in ("python", "curl"):
        exact(tool[key], {"sha256", "version"}, "executable")
        contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "EOM acquisition requires isolated CPython3.12")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid curl identity")
    return profile


def continuation(value):
    require(isinstance(value, dict) and "continue" in value and 2 <= len(value) <= 3 and
        set(value) <= set(POLICY["continuation_keys"]), "invalid EOM continuation fields")
    require(all(isinstance(v, str) and v and len(v) <= 8192 for v in value.values()), "invalid EOM continuation values")
    return value


def parameters(cursor):
    if cursor:
        continuation(cursor)
    return {"method": "GET", "uri": API,
        "query_urlencoded": urllib.parse.urlencode(sorted({**QUERY, **cursor}.items())),
        "headers": {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "retries": 0, "cache": False, "connect_timeout_seconds": 30,
            "timeout_seconds": 120, "maximum_response_bytes": POLICY["maximum_response_bytes"]}}


def request(params):
    return {"kind": "http_get", "uri": API, "parameters_sha256": sha(canonical(params))}


def response_document(raw, metadata):
    exact(metadata, {"curl_exit_code", "http_status", "content_type", "sha256", "bytes"}, "response")
    require(type(metadata["curl_exit_code"]) is int and metadata["curl_exit_code"] == 0 and
        type(metadata["http_status"]) is int and metadata["http_status"] == 200 and
        metadata["content_type"] in POLICY["response_media_types"], "EOM HTTP request did not succeed")
    require(type(metadata["bytes"]) is int and metadata["bytes"] == len(raw) and sha(raw) == metadata["sha256"] and
        0 < len(raw) <= POLICY["maximum_response_bytes"], "EOM response bytes differ or exceed bound")
    data = parse(raw, "EOM API response", artifact=True)
    require(isinstance(data, dict) and not ({"error", "errors", "warnings", "query-continue"} & set(data)) and
        isinstance(data.get("query"), dict) and isinstance(data["query"].get("pages"), dict), "EOM API response is erroneous or incomplete")
    for key, page in data["query"]["pages"].items():
        require(isinstance(page, dict) and type(page.get("pageid")) is int and page["pageid"] > 0 and
            key == str(page["pageid"]) and type(page.get("ns")) is int and page["ns"] == 0 and
            isinstance(page.get("title"), str) and page["title"] and isinstance(page.get("links", []), list), "invalid EOM page")
        for link in page.get("links", []):
            require(isinstance(link, dict) and type(link.get("ns")) is int and link["ns"] == 0 and
                isinstance(link.get("title"), str) and link["title"], "invalid EOM page link")
    if "continue" in data:
        continuation(data["continue"])
    return data


class WalkState:
    """The producer and verifier apply identical incremental completeness checks."""
    def __init__(self, plan):
        self.plan = validate_plan(plan)
        self.cursor, self.seen, self.pages, self.names, self.links = {}, set(), {}, {}, set()
        self.total, self.count = 0, 0

    def accept(self, record):
        require(self.count < POLICY["maximum_requests"], "EOM request budget exceeded")
        exact(record, {"request", "response", "body_base64"}, "transcript record")
        require(self.cursor is not None and record["request"] == parameters(self.cursor), "EOM request does not follow the previous response")
        key = sha(canonical(record["request"]))
        require(key not in self.seen, "EOM continuation repeats an earlier request")
        self.seen.add(key)
        try:
            raw = base64.b64decode(record["body_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise EvidenceError("invalid EOM retained response encoding") from exc
        require(base64.b64encode(raw).decode("ascii") == record["body_base64"], "noncanonical EOM response encoding")
        self.total += len(raw)
        require(self.total <= POLICY["maximum_total_response_bytes"], "EOM walk exceeds response budget")
        data = response_document(raw, record["response"])
        for page in data["query"]["pages"].values():
            pid, title = page["pageid"], page["title"]
            logical_id = title.replace(" ", "_")
            require(pid not in self.pages or self.pages[pid] == title, "EOM page changed title within observation")
            require(logical_id not in self.names or self.names[logical_id] == pid, "EOM logical page identity belongs to multiple upstream IDs")
            self.pages[pid], self.names[logical_id] = title, pid
            self.links.update((logical_id, link["title"].replace(" ", "_")) for link in page.get("links", [])
                if title.replace(" ", "_") != link["title"].replace(" ", "_"))
        require(len(self.pages) <= POLICY["maximum_pages"] and len(self.links) <= POLICY["maximum_links"], "EOM graph exceeds reviewed bound")
        self.cursor = data.get("continue")
        self.count += 1

    def result(self, *, complete=True):
        if complete:
            require(self.count > 0 and self.cursor is None, "EOM transcript ends before pagination completed")
            require(len(self.names) >= self.plan["minimum_pages"] and len(self.links) >= self.plan["minimum_links"], "EOM observation falls below reviewed completeness floors")
        return {"requests": self.count, "pages": len(self.names), "links": len(self.links), "response_bytes": self.total}, self.cursor


def replay(plan, records, *, complete=True):
    require(isinstance(records, list) and len(records) <= POLICY["maximum_requests"], "invalid EOM request count")
    state = WalkState(plan)
    for record in records:
        state.accept(record)
    return state.result(complete=complete)


def body_ref(raw):
    return {"object": "api_transcript", "sha256": sha(raw), "bytes": len(raw), "media_type": "application/json"}


def capture_files(plan, records, tool, programs, when):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    facts, _ = replay(plan, records)
    raw = canonical({"schema": TRANSCRIPT_SCHEMA, "records": records})
    requests = sorted([request(row["request"]) for row in records], key=canonical)
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": SOURCE,
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": API,
        "tool": {"name": "wikilean-eom-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "batch": {"status": "complete", "requests_total": len(records),
            "requests_succeeded": len(records), "requests_failed": 0,
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
    require(document["schema"] == TRANSCRIPT_SCHEMA, "unsupported EOM transcript")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, document["records"], tool, programs, when)
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"}, "EOM capture differs from independently replayed closure")
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
    physical_root = "eom_observation_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("api_transcript", raw, ["raw", "normalized"])
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-eom-response-identity", "version": "1", "sha256": sha(canonical(profile))}
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
        "license": {"expression": "LicenseRef-EOM-Review", "redistribution": "restricted",
            "notice": "Private API observation of identifiers, titles and links; no article text requested. Public redistribution requires separate review."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["api_transcript"], "outputs": ["api_transcript"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda p: p["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "EOM source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.eom-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source], "input_bindings": []})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    require(build_export(capture, profile, programs, when) == {**files, "manifest.json": canonical(manifest)}, "EOM source export differs from independent replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"], "facts": parse(capture["facts.json"], "facts")}
