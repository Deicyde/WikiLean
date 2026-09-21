"""Fresh OEIS names/anchored-entry evidence and pure legacy normalization.

The plan's parent identities are the explicit review boundary. Independently
verified source evidence supplies the complete P829 scope, never a disk cache.
The dump and per-entry requests are an observation, not an upstream snapshot.
"""
from __future__ import annotations

import ast
import base64
import copy
import gzip
import io as byteio
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import wikidata_crossref_sources as io
import mathlib_source_evidence as archive
import external_pair_normalization as pair

contracts = io.contracts
canonical, artifact, sha, parse = io.canonical, io.artifact, io.sha, io.parse
require, exact = io.require, io.exact
SOURCE = "oeis-observation"
CHILD = "external-oeis"
PLAN_SCHEMA = "wikilean.oeis-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.oeis-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.oeis-source-export/v1"
PROFILE_SCHEMA = "wikilean.oeis-source-profiles/v1"
TOOL_SCHEMA = "wikilean.oeis-source-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.oeis-anchored-normalization/v1"
REGISTRY = ROOT / "brain/oeis_source_profiles.json"
PHYSICAL_ROOT = "oeis_export"
PARENTS = {"wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
NAMES_URI = "https://oeis.org/names.gz"
ENTRY_URI = "https://oeis.org/{}?fmt=json"
ANUM = re.compile(r"A[0-9]{6}\Z")
POLICY = {"maximum_anchored_entries": 1000, "maximum_names_compressed_bytes": 64 * 1024 * 1024,
    "maximum_names_uncompressed_bytes": 128 * 1024 * 1024, "maximum_names": 1000000,
    "maximum_entry_bytes": 4 * 1024 * 1024, "maximum_total_response_bytes": 128 * 1024 * 1024,
    "minimum_request_interval_milliseconds": 1500, "timeout_seconds": 300,
    "observation": "independent-live-requests/no-snapshot", "names_uri": NAMES_URI, "entry_uri": ENTRY_URI}
TOOL_FILES = tuple(sorted({*io.TOOL_FILES, *pair.TOOL_FILES, "brain/mathlib_source_evidence.py",
    "brain/oeis_source_evidence.py", "brain/oeis_sources.py", "brain/ingest/oeis.py"}))


def origins():
    io.origins(); archive.validate_module_origins(); pair.origins()
    for module, path in ((io, "brain/wikidata_crossref_sources.py"), (archive, "brain/mathlib_source_evidence.py"),
                        (pair, "brain/external_pair_normalization.py")):
        require(Path(module.__file__).resolve(strict=True) == ROOT / path, "OEIS helper origin differs")
    require(io.contracts is archive.contracts, "OEIS helpers imported different contracts")


def profile_id(profile):
    return contracts.domain_hash("wikilean.oeis-source-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = io.read(REGISTRY)
    value = exact(parse(raw, "OEIS profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid OEIS profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete OEIS helper closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program"); contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unsupported OEIS policy or generation")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current OEIS profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(io.read(ROOT / p))} for p in TOOL_FILES], "unreviewed current OEIS generation")
    return profile


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "OEIS preimages differ from reviewed whole generation")


def validate_plan(plan):
    exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids", "anchored_qids", "minimum_names_inventory", "minimum_links"}, "OEIS plan")
    require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [s["source"] for s in plan["parents"]] == sorted(PARENTS), "exact sorted OEIS parent closure required")
    require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "every OEIS parent requires an explicitly reviewed source identity")
    for value in plan["reviewed_parent_manifest_ids"].values(): contracts._hash(value, "reviewed parent")
    qmap = plan["anchored_qids"]
    require(isinstance(qmap, dict) and 1 <= len(qmap) <= POLICY["maximum_anchored_entries"] and
        all(isinstance(a, str) and ANUM.fullmatch(a) and isinstance(q, str) and io.entities.QID_RE.fullmatch(q) for a, q in qmap.items()),
        "invalid exact OEIS anchor scope")
    require(type(plan["minimum_names_inventory"]) is int and 1 <= plan["minimum_names_inventory"] <= POLICY["maximum_names"], "invalid names inventory floor")
    require(type(plan["minimum_links"]) is int and 0 <= plan["minimum_links"] <= len(qmap) * (len(qmap) - 1), "invalid anchored links floor")
    return plan


def checked(raw, ref):
    require(len(raw) == ref["bytes"] and sha(raw) == ref["sha256"], "parent bytes differ from reviewed source object")
    return raw


def physical(ref, roots):
    require(ref["root"] in roots, "missing parent physical root")
    contracts.validate_literal_relative_path(ref["path"], "parent member")
    path = roots[ref["root"]] / ref["path"]
    io.real_path(path)
    return path


def capture_parents(plan, roots):
    validate_plan(plan)
    sources = {s["source"]: copy.deepcopy(s) for s in plan["parents"]}
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "OEIS parent") for name, source in sources.items()}
    require({n: m["source_manifest_id"] for n, m in manifests.items()} == plan["reviewed_parent_manifest_ids"], "OEIS parent differs from explicitly reviewed identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    selected = {("wikidata-crossrefs", "wikidata_crossrefs"), ("wikidata-crossrefs", "requested_qid_scope"), (io.CURATED_SOURCE, "source_registry")}
    objects, captured = {}, {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and source["objects"][0]["path"] == io.REGISTRY_PATH,
                "unexpected curated OEIS parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            objects[key] = item
            if key in selected:
                require("normalized" in item["roles"], "OEIS scope input must be normalized")
                captured[key] = raw
        if source["source_kind"] == "curated_git_tree": continue
        receipts, preimages = {}, {}
        for ref in source["evidence"]["acquisition_receipts"]:
            receipts[ref["acquisition_receipt_id"]] = parse(checked(io.read(physical(ref, roots)), ref), "parent receipt")
        ref = source["evidence"]["normalization_lineage"]
        lineage = parse(checked(io.read(physical(ref, roots)), ref), "parent lineage")
        for ref in source["evidence"]["request_parameter_preimages"]:
            checked(io.read(physical(ref, roots)), ref)
            preimages[ref["parameters_sha256"]] = {k: ref[k] for k in ("parameters_sha256", "bytes", "media_type")}
        require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing OEIS parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    require(set(captured) == selected, "OEIS parent scope closure is incomplete")
    require(anchor_map(captured) == plan["anchored_qids"], "OEIS plan differs from exact reviewed P829 anchor scope")
    return sources, manifests, objects, captured


def anchor_map(captured):
    refs = parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = exact(parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope"), {"schema", "qids"}, "scope")
    require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and scope["qids"] == sorted(set(scope["qids"]))
        and set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    require("oeis" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P829", []) and
        "oeis" in refs["properties"].get("P829", []), "OEIS anchors require the curated P829 mapping")
    qmap = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        require(bool(io.entities.QID_RE.fullmatch(qid)), "invalid crossref QID")
        values = refs["xrefs"][qid].get("oeis", [])
        require(isinstance(values, list) and all(isinstance(v, str) and v for v in values), "OEIS identifiers must be concrete strings")
        for aid in values:
            if ANUM.fullmatch(aid): qmap.setdefault(aid, qid)
    require(1 <= len(qmap) <= POLICY["maximum_anchored_entries"], "OEIS anchor scope is empty or exceeds bound")
    return dict(sorted(qmap.items()))


def request_specs(plan):
    validate_plan(plan)
    return [("names_gz", NAMES_URI, "application/gzip", POLICY["maximum_names_compressed_bytes"]),
        *(("entry-" + aid.lower(), ENTRY_URI.format(aid), "application/json", POLICY["maximum_entry_bytes"]) for aid in sorted(plan["anchored_qids"]))]


def parameters(spec):
    uri, _separator, query = spec[1].partition("?")
    return {"method": "GET", "uri": uri, "query_urlencoded": query, "headers": {"Accept": spec[2], "Accept-Encoding": "identity",
        "User-Agent": "WikiLean-source-evidence/1.0 (https://github.com/Deicyde/WikiLean; names and anchored links)"},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "retries": 0, "cache": False, "connect_timeout_seconds": 30,
            "timeout_seconds": POLICY["timeout_seconds"], "maximum_response_bytes": spec[3]}}


def request(spec):
    return {"kind": "http_get", "uri": parameters(spec)["uri"], "parameters_sha256": sha(canonical(parameters(spec)))}


def validate_response(spec, raw, response):
    exact(response, {"curl_exit_code", "http_status", "content_type", "sha256", "bytes"}, "OEIS response")
    media = {"application/gzip", "application/x-gzip", "application/octet-stream"} if spec[0] == "names_gz" else {"application/json"}
    require(type(response["curl_exit_code"]) is int and response["curl_exit_code"] == 0 and type(response["http_status"]) is int
        and response["http_status"] == 200 and response["content_type"] in media, "OEIS request did not succeed")
    require(type(response["bytes"]) is int and 0 < len(raw) <= spec[3] and len(raw) == response["bytes"] and sha(raw) == response["sha256"],
        "OEIS response bytes differ or exceed bound")


def names_inventory(raw, minimum):
    names = {}
    with gzip.GzipFile(fileobj=byteio.BytesIO(raw)) as stream:
        data = stream.read(POLICY["maximum_names_uncompressed_bytes"] + 1)
        require(len(data) <= POLICY["maximum_names_uncompressed_bytes"], "OEIS expanded names exceed bound")
    # Same replacement decoding and universal-newline behavior as gzip.open
    # in the legacy harvester; malformed non-A-number lines remain ignored.
    text = data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        if line.startswith("#"): continue
        aid, _, name = line.rstrip("\n").partition(" ")
        if ANUM.fullmatch(aid) and name: names[aid] = name.strip()
        require(len(names) <= POLICY["maximum_names"], "OEIS names inventory exceeds count bound")
    require(len(names) >= minimum, "OEIS names inventory falls below reviewed completeness floor")
    return names


def entry_document(aid, raw):
    value = parse(raw, "OEIS entry", data=True)
    require(isinstance(value, dict) and type(value.get("number")) is int and value["number"] == int(aid[1:]), "OEIS response is not the requested sequence")
    require(isinstance(value.get("name", ""), str), "OEIS entry name must be text")
    xrefs = value.get("xref")
    require(xrefs is None or (isinstance(xrefs, list) and all(isinstance(v, str) for v in xrefs)), "OEIS entry references must be a text list")
    return value


def normalize(plan, raw, programs):
    names = names_inventory(raw["names_gz"], plan["minimum_names_inventory"])
    entries = {aid: entry_document(aid, raw["entry-" + aid.lower()]) for aid in plan["anchored_qids"]}
    # Execute the exact old main with immutable providers. Cache, acquisition,
    # and filesystem helpers are neither selected nor exposed to this parser.
    selected, symbols = [], {"main", "NAMES_URL", "ENTRY_URL", "PAGE_URL", "ANUM"}
    tree = ast.parse(programs["brain/ingest/oeis.py"], filename="sealed:brain/ingest/oeis.py")
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in symbols: selected.append(node)
    require(len(selected) == len(symbols), "legacy OEIS parser selector differs")
    result = []
    def emit(db, pages, links, extra_meta):
        require(db == "oeis", "unexpected parser output family")
        result.append(pair.normalize_pair(db, pages, links, extra_meta, programs["brain/ingest/common.py"]))
    namespace = {"re": re, "sys": types.SimpleNamespace(stderr=byteio.StringIO()), "load_names": lambda: names,
        "entry_json": lambda aid, _fetched: entries[aid],
        "common": types.SimpleNamespace(qid_map=lambda db: plan["anchored_qids"] if db == "oeis" else None, emit=emit)}
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), "sealed:oeis", "exec"), namespace)
    require(namespace["main"]() == 0 and len(result) == 1, "OEIS parser did not produce one complete pair")
    meta, pages, links = result[0]
    require({row["id"] for row in pages} == set(plan["anchored_qids"]), "OEIS normalization lost a reviewed anchored page")
    require(len(links) >= plan["minimum_links"], "OEIS anchored links fall below reviewed completeness floor")
    return meta, pages, links


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "OEIS tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed OEIS tool generation")
    for key in ("python", "curl"):
        exact(tool[key], {"sha256", "version"}, "executable")
        contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "OEIS acquisition requires isolated CPython3.12")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid OEIS curl identity")
    return profile


def capture_files(plan, raw, responses, tool, programs, parents, when):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    require(anchor_map(parents[3]) == plan["anchored_qids"], "captured OEIS scope differs from parent inputs")
    specs = request_specs(plan)
    require(set(raw) == set(responses) == {s[0] for s in specs}, "OEIS response closure is incomplete or has extras")
    require(sum(len(value) for value in raw.values()) <= POLICY["maximum_total_response_bytes"], "OEIS response budget exceeded")
    for spec in specs: validate_response(spec, raw[spec[0]], responses[spec[0]])
    meta, pages, links = normalize(plan, raw, programs)
    transcript = canonical({"schema": "wikilean.oeis-http-transcript/v1", "responses": [
        {"object": spec[0], "request": request(spec), "response": responses[spec[0]],
            "body_base64": base64.b64encode(raw[spec[0]]).decode("ascii")} for spec in specs]})
    outputs = [{"object": "observation", "sha256": sha(transcript), "bytes": len(transcript), "media_type": "application/json"}]
    requests = sorted([request(spec) for spec in specs], key=canonical)
    pin = {"type": "content_sha256", "value": sha(transcript)}
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": SOURCE, "pin": pin, "upstream_uri": "https://oeis.org/",
        "tool": {"name": "wikilean-oeis-acquirer", "version": "1", "sha256": sha(canonical(tool))}, "requests": requests,
        "batch": {"status": "complete", "requests_total": len(specs), "requests_succeeded": len(specs), "requests_failed": 0,
            "request_set_root": contracts.acquisition_request_set_root(requests)}, "outputs": outputs, "audit": {"acquired_at": when}}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    files = {"observation.json": transcript, "plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile), "receipt.json": canonical(receipt),
        "facts.json": canonical({"names_inventory": meta["n_names_inventory"], "anchored_entries": len(plan["anchored_qids"]),
            "pages": len(pages), "links": len(links), "requests": len(specs), "response_bytes": sum(len(value) for value in raw.values())}),
        "request-results.json": canonical([{"object": spec[0], "request": request(spec), "response": responses[spec[0]]} for spec in specs]),
        **{"raw/" + name: value for name, value in raw.items()},
        **{"requests/" + request(spec)["parameters_sha256"] + ".json": canonical(parameters(spec)) for spec in specs},
        **{"implementation/" + name: value for name, value in programs.items()},
        **{"parent-inputs/" + name + "/" + obj: value for (name, obj), value in parents[3].items()}}
    return archive.manifest_files(files, CAPTURE_SCHEMA)


def verify_capture_files(files, roots):
    plan = validate_plan(parse(files["plan.json"], "OEIS plan"))
    parents = capture_parents(plan, roots)
    tool = parse(files["tool.json"], "OEIS tool")
    raw = {name.removeprefix("raw/"): data for name, data in files.items() if name.startswith("raw/")}
    records = parse(files["request-results.json"], "request results")
    require(isinstance(records, list) and len(records) == len(request_specs(plan)), "OEIS request result count differs")
    responses = {}
    for row in records:
        exact(row, {"object", "request", "response"}, "request result")
        require(row["object"] not in responses, "duplicate OEIS request result")
        responses[row["object"]] = row["response"]
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, raw, responses, tool, programs, parents, when)
    require(files == {name: value for name, value in expected.items() if name != "manifest.json"}, "OEIS capture differs from independent request and source replay")
    return plan, raw, tool, parents


def verify_capture(path, roots):
    origins()
    files, manifest = archive.read_bundle(path, CAPTURE_SCHEMA)
    verify_capture_files(files, roots)
    return files, manifest


def build_export(capture, profile, programs, roots, when):
    plan, raw, tool, (sources, manifests, objects, captured) = verify_capture_files(capture, roots)
    verify_programs(profile, programs)
    meta, pages, links = normalize(plan, raw, programs)
    configuration = {"schema": NORMALIZATION_SCHEMA, "reviewed_parent_manifest_ids": plan["reviewed_parent_manifest_ids"],
        "anchored_qids": plan["anchored_qids"], "names": "legacy UTF8-replace/newline and last-name-wins parsing; bounded complete gzip",
        "entries": "exact successful requested A-number JSON objects; no cache or missing-entry fallback",
        "legacy_parser": "main,NAMES_URL,ENTRY_URL,PAGE_URL,ANUM with immutable providers",
        "pair": "reviewed common.emit pure prefix; complete clock-free external pair", "licensing": "restricted retained evidence; existing OEIS attribution"}
    files = {"acquisition/" + name: value for name, value in capture.items()}
    files.update({"implementation/" + name: value for name, value in programs.items()})
    files["normalization/profile.json"], files["normalization/configuration.json"] = canonical(profile), canonical(configuration)
    for kind, rows in (("pages", pages), ("links", links)):
        files["normalized/oeis_" + kind + ".jsonl"] = b"".join(artifact(row) + b"\n" for row in [{"_meta": meta}, *rows])
    def planned(name, path, roles, media="application/json"):
        value = files[path]
        files.setdefault("objects/sha256/" + sha(value), value)
        return {"name": name, "root": PHYSICAL_ROOT, "path": path, "sha256": sha(value), "bytes": len(value),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    receipt = parse(capture["receipt.json"], "receipt")
    receipt_id = receipt["acquisition_receipt_id"]
    def evidence(path, **identity):
        return {"root": PHYSICAL_ROOT, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    license = {"expression": "CC-BY-SA-4.0", "redistribution": "restricted",
        "notice": "The Online Encyclopedia of Integer Sequences, https://oeis.org/; names and anchored cross-reference facts. Private evidence; no publication approval."}
    support = [planned("normalization_profile", "normalization/profile.json", ["receipt"]),
        planned("normalization_configuration", "normalization/configuration.json", ["receipt"]),
        planned("acquisition_profile", "acquisition/profile.json", ["receipt"]), planned("acquisition_tool", "acquisition/tool.json", ["receipt"]),
        planned("acquisition_plan", "acquisition/plan.json", ["receipt"])]
    for prefix, base in (("normalizer", "implementation/"), ("acquirer", "acquisition/implementation/")):
        support.extend(planned(prefix + "_program_" + str(i), base + name, ["receipt"], "text/x-python") for i, name in enumerate(TOOL_FILES))
    body = planned("observation", "acquisition/observation.json", ["raw", "normalized"])
    identity_tool = {"name": "wikilean-oeis-observation-identity", "version": "1", "sha256": sha(canonical(profile))}
    observation_schema = "wikilean.oeis-http-transcript/v1"
    observation_lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": SOURCE, "mode": "identity",
        "normalization_schema": observation_schema, "configuration_sha256": sha(capture["plan.json"]), "tool": identity_tool,
        "acquisition_receipt_ids": [receipt_id], "parent_source_manifest_ids": [],
        "inputs": [{**io.object_ref(body), "origin": {"kind": "acquisition_receipt", "id": receipt_id}}],
        "outputs": [io.object_ref(body)], "result": "complete", "audit": {"normalized_at": when}}
    observation_lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(observation_lineage)
    files["evidence/observation-lineage.json"] = canonical(observation_lineage)
    preimages = [evidence("acquisition/requests/" + row["parameters_sha256"] + ".json", parameters_sha256=row["parameters_sha256"]) for row in receipt["requests"]]
    observation = {"source": SOURCE, "source_kind": "acquired_dataset", "pin": receipt["pin"],
        "objects": sorted([body, *support], key=lambda item: item["name"]), "license": license,
        "acquisition": receipt["tool"], "normalization": {"schema": observation_schema, "tool": identity_tool, "inputs": ["observation"], "outputs": ["observation"]},
        "evidence": {"acquisition_receipts": [evidence("acquisition/receipt.json", acquisition_receipt_id=receipt_id)],
            "normalization_lineage": evidence("evidence/observation-lineage.json", normalization_lineage_id=observation_lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda item: item["parameters_sha256"])}}
    observation_manifest = io.source_plan_contracts._source_manifest_from_plan(observation, "OEIS observation source")
    contracts.validate_source_manifest_evidence_documents(observation_manifest, receipts={receipt_id: receipt}, lineage=observation_lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["observation-source-manifest.json"] = canonical(observation_manifest)
    observation_id = observation_manifest["source_manifest_id"]
    raw_objects = [planned("observation", "acquisition/observation.json", ["raw"])]
    inputs = [{**io.object_ref(raw_objects[0]), "origin": {"kind": "source_manifest", "id": observation_id}}]
    parent_manifests = {observation_id: observation_manifest}
    for key, value in sorted(captured.items()):
        prior = objects[key]
        path = "inputs/sha256/" + prior["sha256"]
        files[path] = value
        item = planned(key[1], path, ["raw"], prior["media_type"])
        raw_objects.append(item)
        parent_id = manifests[key[0]]["source_manifest_id"]
        parent_manifests[parent_id] = manifests[key[0]]
        inputs.append({**io.object_ref(item), "origin": {"kind": "source_manifest", "id": parent_id}})
    outputs = [planned("oeis_" + kind, "normalized/oeis_" + kind + ".jsonl", ["normalized"], "application/x-ndjson") for kind in ("links", "pages")]
    normalizer = {"name": "wikilean-oeis-normalizer", "version": "1", "sha256": sha(canonical(profile))}
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": CHILD, "mode": "transform", "normalization_schema": NORMALIZATION_SCHEMA,
        "configuration_sha256": sha(files["normalization/configuration.json"]), "tool": normalizer, "acquisition_receipt_ids": [],
        "parent_source_manifest_ids": sorted(parent_manifests), "inputs": sorted(inputs, key=lambda item: (item["origin"]["kind"], item["origin"]["id"], item["object"])),
        "outputs": list(map(io.object_ref, outputs)), "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    source = {"source": CHILD, "source_kind": "sealed_snapshot", "pin": {"type": "dataset_revision", "value": lineage["normalization_lineage_id"]},
        "objects": sorted([*raw_objects, *outputs, *support], key=lambda item: item["name"]), "license": license,
        "acquisition": normalizer, "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": sorted(item["name"] for item in raw_objects), "outputs": [item["name"] for item in outputs]},
        "evidence": {"acquisition_receipts": [], "request_parameter_preimages": [],
            "normalization_lineage": evidence("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"])}}
    manifest = io.source_plan_contracts._source_manifest_from_plan(source, "OEIS normalized source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={}, lineage=lineage,
        request_parameter_preimages={}, parent_source_manifests=parent_manifests)
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.oeis-source-fragment/v1", "scope": "source-plan-fragment", "physical_root": PHYSICAL_ROOT,
        "source_publishable": False, "redistribution": "restricted", "sources": sorted([*sources.values(), observation, source], key=lambda item: item["source"]),
        "input_bindings": [{"input_id": "external-" + kind, "state": "present", "sources": [CHILD],
            "members": [{"path": "oeis_" + kind + ".jsonl", "source": CHILD, "object": "oeis_" + kind}]} for kind in ("links", "pages")]})
    return archive.manifest_files(files, EXPORT_SCHEMA)

def verify_export(path, roots):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {name.removeprefix("acquisition/"): raw for name, raw in files.items() if name.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    expected = build_export(capture, profile, programs, roots, when)
    require(expected == {**files, "manifest.json": canonical(manifest)}, "OEIS export differs from independent normalization and evidence replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"export_id": manifest["identity"], "source_manifest_id": source["source_manifest_id"], "facts": parse(capture["facts.json"], "facts")}
