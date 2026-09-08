"""Complete DLMF index/TOC/section observations with exact derived request closure.

Canonical /idx/ avoids its upstream redirect. The 36 chapter TOCs and index
define the entire section list through the existing reviewed href parser.
Every section is acquired once; no cache or truncated graph is authoritative.
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
BASE = "https://dlmf.nist.gov/"
API = BASE
SOURCE = "dlmf-page-walk"
PLAN_SCHEMA = "wikilean.dlmf-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.dlmf-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.dlmf-observation-export/v1"
TRANSCRIPT_SCHEMA = "wikilean.dlmf-observation-transcript/v1"
PROFILE_SCHEMA = "wikilean.dlmf-observation-profiles/v1"
TOOL_SCHEMA = "wikilean.dlmf-observation-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.dlmf-response-identity/v1"
REGISTRY = ROOT / "brain/dlmf_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/dlmf_source_evidence.py", "brain/dlmf_sources.py", "brain/ingest/dlmf.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
PARSER_SYMBOLS = {"HREF", "SECTION_HREF", "TITLE", "SECTION_OF", "hrefs_to_sections", "clean_title"}
POLICY = {"uri": BASE, "observation": "independent-live-requests/no-snapshot", "chapters": list(range(1, 37)),
    "index_uri": BASE + "idx/", "maximum_requests": 1100, "maximum_response_bytes": 8 * 1024 * 1024,
    "maximum_total_response_bytes": 256 * 1024 * 1024, "maximum_sections": 1063,
    "maximum_links": 2000000, "minimum_request_interval_milliseconds": 1000,
    "response_media_types": ["application/xhtml+xml", "text/html"], "parser_symbols": sorted(PARSER_SYMBOLS)}
USER_AGENT = "WikiLean-source-evidence/1.0 (https://github.com/Deicyde/WikiLean; identifiers and links only)"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "DLMF helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.dlmf-observation-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid DLMF profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete DLMF program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed DLMF policy or profile identity")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current DLMF profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(read_regular(ROOT / p))} for p in TOOL_FILES], "unreviewed current DLMF program generation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "DLMF program preimages differ from reviewed whole generation")


def validate_plan(plan):
    exact(plan, {"schema", "source", "uri", "minimum_pages", "minimum_links"}, "DLMF plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] == SOURCE and plan["uri"] == API, "unsupported DLMF source or endpoint")
    for key, maximum in (("minimum_pages", POLICY["maximum_sections"]), ("minimum_links", POLICY["maximum_links"])):
        require(type(plan[key]) is int and 1 <= plan[key] <= maximum, "invalid reviewed DLMF completeness floor")
    return plan


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed DLMF tool generation")
    for key in ("python", "curl"):
        exact(tool[key], {"sha256", "version"}, "executable")
        contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "DLMF acquisition requires isolated CPython3.12")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid curl identity")
    return profile


def parser(program):
    tree = ast.parse(program, filename="sealed:brain/ingest/dlmf.py")
    selected, names = [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(node); names.append(name)
    require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)), "legacy DLMF parser selector differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace = {"re": re, "html": html}
    exec(compile(ast.fix_missing_locations(module), "sealed:dlmf-parser", "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def parameters(name):
    require(isinstance(name, str) and (name == "idx" or re.fullmatch(r"toc_(?:[1-9]|[12][0-9]|3[0-6])", name) or
        re.fullmatch(r"section_(?:[1-9]|[12][0-9]|3[0-6])\.[0-9]+", name)), "unreviewed DLMF request target")
    suffix = "idx/" if name == "idx" else name.removeprefix("toc_").removeprefix("section_")
    return {"method": "GET", "uri": BASE + suffix,
        "headers": {"Accept": "text/html, application/xhtml+xml", "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "retries": 0, "cache": False, "connect_timeout_seconds": 30,
            "timeout_seconds": 120, "maximum_response_bytes": POLICY["maximum_response_bytes"]}}


def request(params):
    return {"kind": "http_get", "uri": params["uri"], "parameters_sha256": sha(canonical(params))}


def response_document(name, raw, metadata, syntax):
    exact(metadata, {"curl_exit_code", "http_status", "content_type", "sha256", "bytes"}, "response")
    require(type(metadata["curl_exit_code"]) is int and metadata["curl_exit_code"] == 0 and
        type(metadata["http_status"]) is int and metadata["http_status"] == 200 and
        metadata["content_type"] in POLICY["response_media_types"], "DLMF HTTP request did not succeed")
    require(type(metadata["bytes"]) is int and metadata["bytes"] == len(raw) and sha(raw) == metadata["sha256"] and
        0 < len(raw) <= POLICY["maximum_response_bytes"], "DLMF response bytes differ or exceed bound")
    page = raw.decode("utf-8", "replace")
    title = syntax.clean_title(page)
    if name == "idx": expected = r"Index(?:\s|$)"
    elif name.startswith("toc_"): expected = r"Chapter\s+" + name[4:] + r"(?:\s|$)"
    else: expected = "§" + re.escape(name[8:]) + r"(?:\s|$)"
    require(isinstance(title, str) and re.match(expected, title), "DLMF response title does not identify its requested page")
    require("</html>" in page.lower(), "DLMF HTML response lacks its closing document boundary")
    return page


class WalkState:
    """Retained index/TOC pages determine the exact remaining request list."""
    def __init__(self, plan, parser_program):
        self.plan = validate_plan(plan)
        self.syntax = parser(parser_program)
        self.names = ["idx", *("toc_" + str(chapter) for chapter in POLICY["chapters"])]
        self.sections, self.pages, self.links = set(), {}, set()
        self.total, self.count = 0, 0

    @property
    def next_name(self):
        return self.names[self.count] if self.count < len(self.names) else None

    def accept(self, record):
        require(self.count < POLICY["maximum_requests"], "DLMF request budget exceeded")
        exact(record, {"request", "response", "body_base64"}, "transcript record")
        name = self.next_name
        require(name is not None and record["request"] == parameters(name), "DLMF request does not follow the complete derived page list")
        try:
            raw = base64.b64decode(record["body_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise EvidenceError("invalid DLMF retained response encoding") from exc
        require(base64.b64encode(raw).decode("ascii") == record["body_base64"], "noncanonical DLMF response encoding")
        self.total += len(raw)
        require(self.total <= POLICY["maximum_total_response_bytes"], "DLMF walk exceeds response budget")
        page = response_document(name, raw, record["response"], self.syntax)
        hrefs = self.syntax.hrefs_to_sections(page)
        if name == "idx":
            self.sections.update(section for section in hrefs if int(section.split(".")[0]) in POLICY["chapters"])
        elif name.startswith("toc_"):
            chapter = name[4:]
            own = {section for section in hrefs if section.split(".")[0] == chapter}
            require(own, "DLMF chapter TOC contains no own-chapter sections")
            self.sections.update(own)
        else:
            section = name[8:]
            self.pages[section] = self.syntax.clean_title(page)
            self.links.update((section, destination) for destination in hrefs if destination != section and destination in self.sections)
        require(len(self.sections) <= POLICY["maximum_sections"] and len(self.links) <= POLICY["maximum_links"], "DLMF graph exceeds reviewed bound")
        self.count += 1
        if self.count == 1 + len(POLICY["chapters"]):
            require(len(self.sections) >= self.plan["minimum_pages"], "DLMF enumerated sections fall below reviewed floor")
            self.names.extend("section_" + section for section in sorted(self.sections, key=lambda s: tuple(map(int, s.split(".")))))
            require(len(self.names) <= POLICY["maximum_requests"], "DLMF derived request list exceeds bound")

    def result(self, *, complete=True):
        if complete:
            require(self.next_name is None and self.count > 1 + len(POLICY["chapters"]) and set(self.pages) == self.sections,
                "DLMF transcript ends before every derived section was acquired")
            require(len(self.pages) >= self.plan["minimum_pages"] and len(self.links) >= self.plan["minimum_links"],
                "DLMF observation falls below reviewed completeness floors")
        return {"requests": self.count, "pages": len(self.pages), "links": len(self.links), "response_bytes": self.total,
            "chapter_tocs": len(POLICY["chapters"]), "sections_enumerated": len(self.sections)}, self.next_name


def replay(plan, records, programs, *, complete=True):
    require(isinstance(records, list) and len(records) <= POLICY["maximum_requests"], "invalid DLMF request count")
    state = WalkState(plan, programs["brain/ingest/dlmf.py"])
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
    requests = sorted([request(row["request"]) for row in records], key=canonical)
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": SOURCE,
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": API,
        "tool": {"name": "wikilean-dlmf-acquirer", "version": "1", "sha256": sha(canonical(tool))},
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
    require(document["schema"] == TRANSCRIPT_SCHEMA, "unsupported DLMF transcript")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, document["records"], tool, programs, when)
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"}, "DLMF capture differs from independently replayed closure")
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
    physical_root = "dlmf_observation_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("page_transcript", raw, ["raw", "normalized"])
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-dlmf-response-identity", "version": "1", "sha256": sha(canonical(profile))}
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
        "license": {"expression": "LicenseRef-DLMF-Review", "redistribution": "restricted",
            "notice": "Private retained HTML observations support identifiers, titles and hrefs only. Article prose is not emitted as normalized graph data; public redistribution requires separate review."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["page_transcript"], "outputs": ["page_transcript"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda p: p["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "DLMF source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.dlmf-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source], "input_bindings": []})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    require(build_export(capture, profile, programs, when) == {**files, "manifest.json": canonical(manifest)}, "DLMF source export differs from independent replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"], "facts": parse(capture["facts.json"], "facts")}
