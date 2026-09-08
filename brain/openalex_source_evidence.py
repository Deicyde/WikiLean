"""Scoped OpenAlex/arXiv citation observations with complete phase replay.

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
BASE = "https://api.openalex.org/works"
API = BASE
SOURCE = "openalex-arxiv-observation"
SCOPE_SOURCE = "wikilean-derived-theoremgraph-links"
SCOPE_OBJECT = "theoremgraph-links"
PLAN_SCHEMA = "wikilean.openalex-observation-plan/v1"
CAPTURE_SCHEMA = "wikilean.openalex-observation-capture/v1"
EXPORT_SCHEMA = "wikilean.openalex-observation-export/v1"
TRANSCRIPT_SCHEMA = "wikilean.openalex-observation-transcript/v1"
PROFILE_SCHEMA = "wikilean.openalex-observation-profiles/v1"
TOOL_SCHEMA = "wikilean.openalex-observation-tool/v1"
NORMALIZATION_SCHEMA = "wikilean.openalex-response-identity/v1"
REGISTRY = ROOT / "brain/openalex_source_profiles.json"
TOOL_FILES = tuple(sorted({"brain/openalex_source_evidence.py", "brain/openalex_sources.py", "brain/ingest/openalex_citations.py",
    "brain/mathlib_source_evidence.py", "brain/stage_io.py", "brain/tools/authority_contracts.py",
    "brain/tools/execution_environment.py", "brain/tools/source_plan_contracts.py"}))
PARSER_SYMBOLS = {"ARXIV_ID", "ARXIV_RE", "ABS_URL_RE", "doi_of", "arxiv_of_work"}
WID = re.compile(r"https://openalex\.org/W[0-9]{1,20}\Z")
RETRY_POLICY = {"maximum_attempts_per_request": 5, "http_statuses": [502, 503, 504],
    "curl_transport_codes": [5, 6, 7, 28, 35, 52, 55, 56], "backoff_seconds": [5, 15, 30, 60],
    "maximum_retry_after_seconds": 120, "retry_after": "bounded integer seconds only; other present values abort"}
DOCS = {
    "access": "https://help.openalex.org/api/authentication/",
    "costs": "https://help.openalex.org/access/example-costs/",
    "openalex_license": "https://help.openalex.org/data/how-its-built/",
    "arxiv_license": "https://info.arxiv.org/help/api/tou.html"}
POLICY = {"uri": BASE, "observation": "independent-live-requests/no-snapshot", "mailto": None,
    "selection": "reviewed theoremgraph arXiv scope; legacy A/A2/Jwork/B/C/final-B; one twin expansion round",
    "maximum_requests": 18000, "maximum_scope_ids": 10000, "maximum_attempts": 25000,
    "maximum_filtered_attempts": 900, "maximum_references": 1000000, "maximum_redirects": 3,
    "maximum_response_bytes": 8 * 1024 * 1024, "maximum_total_response_bytes": 256 * 1024 * 1024,
    "maximum_transcript_bytes": 432 * 1024 * 1024, "maximum_selector_bytes": 16 * 1024 * 1024,
    "minimum_request_interval_milliseconds": 250, "arxiv_interval_milliseconds": 3100,
    "openalex_batch_size": 50, "arxiv_batch_size": 100, "per_page": 100,
    "auth": "keyless public API only; no account, credentials, payment or quota escalation; 429 aborts",
    "redirects": "recorded 301 only for direct singleton; same API work-ID endpoint; at most three",
    "response_media_types": ["application/json", "application/atom+xml", "application/xml", "text/xml", "text/html"],
    "parser_symbols": sorted(PARSER_SYMBOLS), "retry": RETRY_POLICY, "documentation": DOCS}
USER_AGENT = "WikiLean-brain/2.0 (https://wikilean.jackmccarthy.org; contact via GitHub Deicyde/WikiLean)"


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def origins():
    archive.validate_module_origins()
    require(Path(archive.__file__).resolve() == ROOT / "brain/mathlib_source_evidence.py", "OpenAlex helper origin differs")


def profile_id(profile):
    return contracts.domain_hash("wikilean.openalex-observation-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = read_regular(REGISTRY, 1024 * 1024)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid OpenAlex profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete OpenAlex program closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program")
            contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed OpenAlex policy or profile identity")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current OpenAlex profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(read_regular(ROOT / p))} for p in TOOL_FILES], "unreviewed current OpenAlex program generation")
    return copy.deepcopy(profile)


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "OpenAlex program preimages differ from reviewed whole generation")


def validate_plan(plan):
    exact(plan, {"schema", "source", "uri", "mailto", "scope", "arxiv_ids", "excluded_non_arxiv_ids", "minimum_links"}, "OpenAlex plan")
    require(plan["schema"] == PLAN_SCHEMA and plan["source"] == SOURCE and plan["uri"] == API and plan["mailto"] is None, "unsupported OpenAlex source/auth")
    scope = exact(plan["scope"], {"source", "source_manifest_id", "object", "sha256", "bytes"}, "scope")
    require(scope["source"] == SCOPE_SOURCE and scope["object"] == SCOPE_OBJECT, "unreviewed scope selector")
    contracts._hash(scope["source_manifest_id"], "reviewed scope source")
    contracts._digest(scope["sha256"], "scope digest")
    require(type(scope["bytes"]) is int and 0 < scope["bytes"] <= POLICY["maximum_selector_bytes"], "selector exceeds bound")
    aid_pattern = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?\Z")
    for key in ("arxiv_ids", "excluded_non_arxiv_ids"):
        values = plan[key]
        require(isinstance(values, list) and all(isinstance(v, str) and v and len(v) <= 256 for v in values) and
            values == sorted(set(values)) and len(values) <= POLICY["maximum_scope_ids"], "invalid exact scope values")
        require(all(bool(aid_pattern.fullmatch(v)) == (key == "arxiv_ids") for v in values), "scope classification differs from legacy selector")
    require(all(re.search(r"v[0-9]+$", aid) is None for aid in plan["arxiv_ids"]), "versioned theoremgraph IDs need explicit scope review before acquisition")
    require(plan["arxiv_ids"] and type(plan["minimum_links"]) is int and 0 <= plan["minimum_links"] <= POLICY["maximum_references"], "invalid citation floor")
    return plan


def validate_selector(plan, raw, program):
    require(len(raw) == plan["scope"]["bytes"] and sha(raw) == plan["scope"]["sha256"], "selector bytes differ from reviewed source")
    doc = parse(raw, "theoremgraph selector", artifact=True)
    require(isinstance(doc, dict) and isinstance(doc.get("links"), dict), "invalid theoremgraph selector")
    identifiers = set()
    for rows in doc["links"].values():
        require(isinstance(rows, list), "invalid theoremgraph rows")
        for row in rows:
            require(isinstance(row, dict), "invalid theoremgraph row")
            value = row.get("arxiv_id")
            require(value is None or isinstance(value, str), "invalid theoremgraph identifier")
            if value: identifiers.add(value)
    syntax = parser(program)
    good = sorted(v for v in identifiers if syntax.ARXIV_RE.match(v))
    bad = sorted(identifiers - set(good))
    require(good == plan["arxiv_ids"] and bad == plan["excluded_non_arxiv_ids"], "plan omits or adds a selector identifier")
    return raw


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "curl"}, "tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed OpenAlex tool generation")
    for key in ("python", "curl"):
        exact(tool[key], {"sha256", "version"}, "executable")
        contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "OpenAlex acquisition requires isolated CPython3.12")
    require(isinstance(tool["curl"]["version"], str) and tool["curl"]["version"].startswith("curl "), "invalid curl identity")
    return profile


def parser(program):
    tree = ast.parse(program, filename="sealed:brain/ingest/openalex.py")
    selected, names = [], []
    for node in tree.body:
        name = node.name if isinstance(node, ast.FunctionDef) else (
            node.targets[0].id if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None)
        if name in PARSER_SYMBOLS:
            selected.append(node); names.append(name)
    require(set(names) == PARSER_SYMBOLS and len(names) == len(set(names)), "legacy OpenAlex parser selector differs")
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[])
    namespace = {"re": re}
    exec(compile(ast.fix_missing_locations(module), "sealed:openalex-parser", "exec"), namespace)
    return types.SimpleNamespace(**namespace)


def parameters(target):
    exact(target, {"phase", "values", "uri"}, "request target")
    return {"method": "GET", "uri": target["uri"], "selection": target,
        "headers": {"Accept": "application/json, application/atom+xml, text/html", "Accept-Encoding": "identity", "User-Agent": USER_AGENT},
        "transport": {"default_config": False, "credentials": False, "proxy": False, "tls_verification": True,
            "redirects": False, "implicit_retries": 0, "recorded_retry_policy": RETRY_POLICY, "cache": False,
            "connect_timeout_seconds": 30, "timeout_seconds": 120, "maximum_response_bytes": POLICY["maximum_response_bytes"]}}


def request(params):
    return {"kind": "http_get", "uri": params["uri"].split("?", 1)[0], "parameters_sha256": sha(canonical(params))}


def response_bytes(record):
    metadata = exact(record["response"], {"curl_exit_code", "http_status", "content_type", "sha256", "bytes", "retry_after", "location", "quota"}, "response")
    require(type(metadata["curl_exit_code"]) is int and type(metadata["http_status"]) is int and
        0 <= metadata["http_status"] <= 599 and isinstance(metadata["content_type"], str) and
        len(metadata["content_type"]) <= 128, "invalid OpenAlex response status")
    require(metadata["location"] is None or isinstance(metadata["location"], str) and len(metadata["location"]) <= 256, "invalid retained redirect")
    quota = exact(metadata["quota"], {"limit_usd", "prepaid_remaining_usd", "cost_usd", "remaining_usd"}, "quota")
    require(all(isinstance(v, str) and len(v) <= 32 for v in quota.values()), "invalid retained quota")
    retry = metadata["retry_after"]
    require(retry is None or retry == {"kind": "unsupported-or-excessive"} or
        (isinstance(retry, dict) and set(retry) == {"kind", "seconds"} and retry["kind"] == "delay-seconds" and
         type(retry["seconds"]) is int and 0 <= retry["seconds"] < 10**15), "invalid retained Retry-After")
    try:
        raw = base64.b64decode(record["body_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise EvidenceError("invalid OpenAlex retained response encoding") from exc
    require(base64.b64encode(raw).decode("ascii") == record["body_base64"] and
        type(metadata["bytes"]) is int and metadata["bytes"] == len(raw) and sha(raw) == metadata["sha256"] and
        len(raw) <= POLICY["maximum_response_bytes"], "OpenAlex response bytes differ or exceed bound")
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


def target(phase, values, uri):
    return {"phase": phase, "values": values, "uri": uri}


def batch_target(phase, values, field, select):
    return target(phase, values, API + "?" + urllib.parse.urlencode({
        "filter": field + ":" + "|".join(values), "select": select, "per-page": "100"}))


def work(value, fields):
    require(isinstance(value, dict) and isinstance(value.get("id"), str) and WID.fullmatch(value["id"]), "invalid OpenAlex work identity")
    require(set(value) == set(fields.split(",")), "OpenAlex selected fields differ")
    if "doi" in value:
        require(value["doi"] is None or isinstance(value["doi"], str) and len(value["doi"]) <= 4096, "invalid OpenAlex DOI")
    if "referenced_works" in value:
        refs = value["referenced_works"]
        require(isinstance(refs, list) and len(refs) <= 100000 and all(isinstance(v, str) and WID.fullmatch(v) for v in refs), "invalid OpenAlex references")
    if "locations" in value:
        require(value["locations"] is None or isinstance(value["locations"], list) and len(value["locations"]) <= 10000, "invalid OpenAlex locations")
        for loc in value["locations"] or []:
            require(isinstance(loc, dict) and all(loc.get(k) is None or isinstance(loc[k], str) and len(loc[k]) <= 16384
                for k in ("landing_page_url", "pdf_url")), "invalid OpenAlex landing URL")
    return value


def batch_document(raw, fields):
    data = parse(raw, "OpenAlex batch", artifact=True)
    require(isinstance(data, dict) and "error" not in data and isinstance(data.get("results"), list) and isinstance(data.get("meta"), dict), "malformed OpenAlex batch")
    count = data["meta"].get("count")
    require(type(count) is int and count == len(data["results"]) <= POLICY["per_page"], "OpenAlex batch overflow or incomplete page")
    return [work(v, fields) for v in data["results"]]


def arxiv_document(raw, aids):
    import xml.etree.ElementTree as ET
    require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper(), "arXiv XML document declarations are forbidden")
    try: root = ET.fromstring(raw)
    except ET.ParseError as exc: raise EvidenceError("invalid complete arXiv Atom document") from exc
    ns = "{http://www.w3.org/2005/Atom}"
    require(root.tag == ns + "feed", "arXiv response is not an Atom feed")
    found = {}
    for ent in root.findall(ns + "entry"):
        identity = ent.findtext(ns + "id", "")
        match = re.fullmatch(r"https?://arxiv\.org/abs/(.+?)(?:v\d+)?", identity)
        require(match is not None, "arXiv error or unidentified entry")
        aid = match.group(1)
        require(aid in aids and aid not in found, "arXiv returned duplicated or out-of-scope entry")
        doi = ent.findtext("{http://arxiv.org/schemas/atom}doi", "").strip().rstrip(".,;")
        found[aid] = doi if doi and re.fullmatch(r"10\.\d{4,9}/[^\s,|]+", doi) else None
    require(set(found) == set(aids), "arXiv response omits a queried identifier")
    return found


def documentation(raw, key):
    text = html.unescape(re.sub(r"<[^>]*>", " ", raw.decode("utf-8", "strict")))
    text = re.sub(r"\s+", " ", text).lower()
    expected = {
        "access": ("no key at all", "keyless", "429"),
        "costs": ("$0.10", "without a key"),
        "openalex_license": ("cc0",),
        "arxiv_license": ("descriptive", "cc0 1.0", "one request every three seconds")}
    require(all(phrase in text for phrase in expected[key]), "upstream access/license terms changed; review required")


def query_walk(plan, syntax):
    """Deterministic legacy phase graph; every yield requires one complete response."""
    serial = 0
    def named(phase):
        nonlocal serial
        result = phase + ":" + str(serial); serial += 1
        return result
    for key, uri in DOCS.items():
        response = yield target(named("docs"), [key], uri)
        documentation(response_bytes(response), key)
    aids = plan["arxiv_ids"]
    ours = {a: {"openalex_id": None, "referenced_works": []} for a in aids}
    batch = POLICY["openalex_batch_size"]
    for start in range(0, len(aids), batch):
        chunk = aids[start:start + batch]
        values = [syntax.doi_of(a) for a in chunk]
        row = yield batch_target(named("a"), values, "doi", "id,doi,referenced_works")
        by_doi = {("https://doi.org/" + syntax.doi_of(a)).lower(): a for a in chunk}
        for item in batch_document(response_bytes(row), "id,doi,referenced_works"):
            aid = by_doi.get((item.get("doi") or "").lower())
            require(aid is not None, "OpenAlex DOI filter returned an out-of-scope work")
            ours[aid] = {"openalex_id": item["id"], "referenced_works": item["referenced_works"]}
    for aid in aids:
        if ours[aid]["openalex_id"]: continue
        uri = API + "/https://doi.org/" + urllib.parse.quote(syntax.doi_of(aid), safe="/:.") + "?select=id,doi,referenced_works"
        seen = set()
        for hop in range(POLICY["maximum_redirects"] + 1):
            require(uri not in seen, "OpenAlex singleton redirect cycle")
            seen.add(uri)
            row = yield target(named("direct"), [aid], uri)
            status = row["response"]["http_status"]
            if status == 404: break
            if status == 301:
                require(hop < POLICY["maximum_redirects"], "OpenAlex redirect limit exceeded")
                destination = row["response"]["location"]
                require(isinstance(destination, str) and re.fullmatch(r"https://api\.openalex\.org/works/W[0-9]{1,20}(?:\?select=id,doi,referenced_works)?", destination), "unreviewed OpenAlex redirect authority")
                uri = destination.split("?", 1)[0] + "?select=id,doi,referenced_works"
                continue
            item = work(parse(response_bytes(row), "OpenAlex singleton", artifact=True), "id,doi,referenced_works")
            # The DOI endpoint itself is the exact lookup evidence; merged works
            # may retain a different primary journal DOI after a recorded redirect.
            ours[aid] = {"openalex_id": item["id"], "referenced_works": item["referenced_works"]}
            break
    jdois = {}
    for start in range(0, len(aids), POLICY["arxiv_batch_size"]):
        chunk = aids[start:start + POLICY["arxiv_batch_size"]]
        uri = "https://export.arxiv.org/api/query?id_list=" + ",".join(chunk) + "&max_results=" + str(len(chunk))
        row = yield target(named("arxiv"), chunk, uri)
        jdois.update({a: d for a, d in arxiv_document(response_bytes(row), chunk).items() if d})
    jwork = {a: None for a in jdois}
    for start in range(0, len(jdois), batch):
        chunk = sorted(jdois)[start:start + batch]
        row = yield batch_target(named("journal"), [jdois[a] for a in chunk], "doi", "id,doi")
        by_doi = {("https://doi.org/" + jdois[a]).lower(): a for a in chunk}
        for item in batch_document(response_bytes(row), "id,doi"):
            aid = by_doi.get((item.get("doi") or "").lower())
            require(aid is not None, "journal DOI filter returned an out-of-scope work")
            jwork[aid] = item["id"]
    w2aid, refs_by_aid = {}, {a: set() for a in aids}
    for aid, rec in ours.items():
        if rec["openalex_id"]:
            w2aid[rec["openalex_id"]] = aid
            refs_by_aid[aid].update(rec["referenced_works"])
    resolved = len(w2aid)
    jtwins = {w: a for a, w in sorted(jwork.items()) if w and w not in w2aid}
    w2aid.update(jtwins)
    def identify(wids, phase):
        require(len(wids) <= POLICY["maximum_references"], "reference scope exceeds bound")
        result = {w: None for w in sorted(wids)}
        values = sorted(wids)
        for start in range(0, len(values), batch):
            chunk = values[start:start + batch]
            row = yield batch_target(named(phase), [w.rsplit("/", 1)[1] for w in chunk], "openalex_id", "id,doi,locations")
            for item in batch_document(response_bytes(row), "id,doi,locations"):
                require(item["id"] in result and item["id"] in chunk, "reference filter returned an out-of-scope work")
                result[item["id"]] = syntax.arxiv_of_work(item)
        return result
    all_refs = set().union(*refs_by_aid.values())
    ident = yield from identify(all_refs - set(w2aid), "identify")
    btwins = {w for w, aid in ident.items() if aid in refs_by_aid and w not in w2aid}
    for wid in btwins: w2aid[wid] = ident[wid]
    twins = set(jtwins) | btwins
    twin_refs = {w: [] for w in sorted(twins)}
    values = sorted(twins)
    for start in range(0, len(values), batch):
        chunk = values[start:start + batch]
        row = yield batch_target(named("twins"), [w.rsplit("/", 1)[1] for w in chunk], "openalex_id", "id,referenced_works")
        for item in batch_document(response_bytes(row), "id,referenced_works"):
            require(item["id"] in chunk, "twin filter returned an out-of-scope work")
            twin_refs[item["id"]] = item["referenced_works"]
    new_refs = set()
    for wid, refs in twin_refs.items():
        refs_by_aid[w2aid[wid]].update(refs); new_refs.update(refs)
    ident2 = yield from identify(new_refs - set(w2aid) - set(ident), "identify-final")
    for wid, aid in ident2.items():
        if aid in refs_by_aid: w2aid[wid] = aid
    pairs = {(a, w2aid[w]) for a, refs in refs_by_aid.items() for w in refs if w in w2aid and w2aid[w] != a}
    rows = [{"src": a, "dst": b} for a, b in sorted(pairs)]
    require(len(rows) >= plan["minimum_links"], "citations below reviewed completeness floor")
    return {"rows": rows, "scope_ids": len(aids), "excluded_ids": len(plan["excluded_non_arxiv_ids"]), "links": len(rows),
        "phase_a_resolved": resolved, "journal_dois": len(jdois), "journal_twins": len(jtwins), "identified_twins": len(btwins),
        "reference_identifications": len(ident), "final_reference_identifications": len(ident2)}


class WalkState:
    def __init__(self, plan, parser_program):
        self.plan = validate_plan(plan)
        self.generator = query_walk(plan, parser(parser_program))
        self.target = next(self.generator)
        self.total = self.count = self.attempt_count = self.failures = self.filtered_attempts = 0
        self.ordinal, self.transcript_bytes, self.result = 1, 128, None

    @property
    def next_name(self): return self.target

    def accept(self, record):
        require(self.attempt_count < POLICY["maximum_attempts"] and self.count < POLICY["maximum_requests"], "OpenAlex request budget exceeded")
        exact(record, {"request", "response", "body_base64", "attempt", "outcome", "retry_delay_seconds"}, "transcript record")
        require(self.target is not None and record["request"] == parameters(self.target) and type(record["attempt"]) is int and
            record["attempt"] == self.ordinal, "OpenAlex attempt diverges from exact derived request scope")
        phase = self.target["phase"].split(":", 1)[0]
        if phase not in {"docs", "direct", "arxiv"}:
            self.filtered_attempts += 1
            require(self.filtered_attempts <= POLICY["maximum_filtered_attempts"], "OpenAlex keyless filtered-call budget exceeded")
        raw = response_bytes(record)
        self.total += len(raw); self.transcript_bytes += len(canonical(record)) + 1
        require(self.total <= POLICY["maximum_total_response_bytes"] and self.transcript_bytes <= POLICY["maximum_transcript_bytes"], "OpenAlex response/transcript budget exceeded")
        require(type(record["retry_delay_seconds"]) is int and record["retry_delay_seconds"] >= 0, "invalid retry delay")
        if record["outcome"] == "failed":
            delay = retry_delay(record["response"], self.ordinal)
            require(delay is not None and record["retry_delay_seconds"] == delay, "unreviewed failure, quota, or wrong retry delay")
            self.ordinal += 1; self.attempt_count += 1; self.failures += 1
            return
        meta = record["response"]
        require(record["outcome"] == "succeeded" and record["retry_delay_seconds"] == 0, "invalid OpenAlex outcome")
        require((meta["http_status"] == 200 and meta["curl_exit_code"] == 0) or
            (phase == "direct" and ((meta["http_status"] == 404 and meta["curl_exit_code"] in (0, 22)) or
                (meta["http_status"] == 301 and meta["curl_exit_code"] == 0))), "OpenAlex request did not complete successfully")
        require(raw and meta["content_type"] in POLICY["response_media_types"], "OpenAlex response has no reviewed content")
        if phase not in {"docs", "arxiv"} and meta["http_status"] == 200:
            quota = meta["quota"]
            require(quota["limit_usd"] == "0.1" and quota["prepaid_remaining_usd"] == "0" and quota["cost_usd"] in {"0", "0.0", "0.0000", "0.0001"}, "OpenAlex keyless quota/authority changed")
            require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", quota["remaining_usd"]) and float(quota["remaining_usd"]) > 0, "OpenAlex daily free quota exhausted")
        try: self.target = self.generator.send(record)
        except StopIteration as complete: self.target, self.result = None, complete.value
        self.ordinal = 1; self.count += 1; self.attempt_count += 1

    def facts(self):
        require(self.target is None and self.result is not None, "OpenAlex citation walk is incomplete")
        return {**{k: v for k, v in self.result.items() if k != "rows"}, "requests": self.count, "attempts": self.attempt_count,
            "failed_attempts": self.failures, "filtered_attempts": self.filtered_attempts, "response_bytes": self.total}


def replay(plan, records, programs):
    require(isinstance(records, list), "invalid OpenAlex transcript")
    state = WalkState(plan, programs["brain/ingest/openalex_citations.py"])
    for row in records: state.accept(row)
    return state.facts(), state.next_name


def body_ref(raw):
    return {"object": "citation_transcript", "sha256": sha(raw), "bytes": len(raw), "media_type": "application/json"}


def capture_files(plan, records, tool, programs, when, selector):
    profile = validate_tool(tool)
    verify_programs(profile, programs)
    validate_selector(plan, selector, programs["brain/ingest/openalex_citations.py"])
    facts, _ = replay(plan, records, programs)
    raw = canonical({"schema": TRANSCRIPT_SCHEMA, "records": records})
    require(len(raw) <= POLICY["maximum_transcript_bytes"] < archive.MAX_FILE, "OpenAlex transcript exceeds source object bound")
    descriptors = {canonical(request(row["request"])): request(row["request"]) for row in records}
    requests = [descriptors[key] for key in sorted(descriptors)]
    indices = {canonical(item): index for index, item in enumerate(requests)}
    attempts = [{"request_index": indices[canonical(request(row["request"]))], "outcome": row["outcome"],
        "response_sha256": row["response"]["sha256"], "response_bytes": row["response"]["bytes"]} for row in records]
    receipt = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V2, "source": SOURCE,
        "pin": {"type": "content_sha256", "value": sha(raw)}, "upstream_uri": API,
        "tool": {"name": "wikilean-openalex-acquirer", "version": "1", "sha256": sha(canonical(tool))},
        "requests": requests, "attempts": attempts, "batch": {"status": "complete", "requests_total": len(records),
            "requests_succeeded": len(requests), "requests_failed": len(records) - len(requests),
            "request_set_root": contracts.acquisition_request_set_root(requests)},
        "outputs": [body_ref(raw)], "audit": {"acquired_at": when}}
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    return archive.manifest_files({"plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile),
        "facts.json": canonical(facts), "selector.json": selector, "receipt.json": canonical(receipt), "raw/transcript.json": raw,
        **{"requests/" + request(row["request"])["parameters_sha256"] + ".json": canonical(row["request"]) for row in records},
        **{"implementation/" + name: data for name, data in programs.items()}}, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan, tool = [parse(files[key + ".json"], key) for key in ("plan", "tool")]
    document = exact(parse(files["raw/transcript.json"], "transcript"), {"schema", "records"}, "transcript")
    require(document["schema"] == TRANSCRIPT_SCHEMA, "unsupported OpenAlex transcript")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipt.json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, document["records"], tool, programs, when, files["selector.json"])
    require(files == {name: data for name, data in expected.items() if name != "manifest.json"}, "OpenAlex capture differs from independently replayed closure")
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
    physical_root = "openalex_observation_export"
    def planned(name, data, roles, media="application/json"):
        path = "objects/sha256/" + sha(data)
        files.setdefault(path, data)
        return {"root": physical_root, "path": path, "name": name, "sha256": sha(data), "bytes": len(data),
            "media_type": media, "roles": sorted(roles), "redistribution": "restricted"}
    body = planned("citation_transcript", raw, ["raw", "normalized"])
    receipt = parse(capture["receipt.json"], "receipt")
    normalizer = {"name": "wikilean-openalex-response-identity", "version": "1", "sha256": sha(canonical(profile))}
    lineage = {"schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1, "source": SOURCE, "mode": "identity",
        "normalization_schema": NORMALIZATION_SCHEMA, "configuration_sha256": sha(canonical(plan)), "tool": normalizer,
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "inputs": [{**body_ref(raw), "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        "outputs": [body_ref(raw)], "result": "complete", "audit": {"normalized_at": when}}
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    files["evidence/lineage.json"] = canonical(lineage)
    support = [planned("normalization_profile", canonical(profile), ["receipt"]), planned("normalization_plan", canonical(plan), ["receipt"]),
        planned("theoremgraph_selector", capture["selector.json"], ["receipt"]), planned("acquisition_profile", capture["profile.json"], ["receipt"]), planned("acquisition_tool", canonical(tool), ["receipt"])]
    for prefix, retained in (("normalizer", programs), ("acquirer", {n: capture["implementation/" + n] for n in TOOL_FILES})):
        support.extend(planned(prefix + "_program_" + str(i), retained[n], ["receipt"], "text/x-python") for i, n in enumerate(TOOL_FILES))
    def evidence_ref(path, **identity):
        return {"root": physical_root, "path": path, "sha256": sha(files[path]), "bytes": len(files[path]), "media_type": "application/json", **identity}
    preimages = [evidence_ref("acquisition/requests/" + row["parameters_sha256"] + ".json", parameters_sha256=row["parameters_sha256"])
        for row in receipt["requests"]]
    source = {"source": SOURCE, "source_kind": "acquired_dataset", "pin": receipt["pin"], "objects": sorted([body, *support], key=lambda o: o["name"]),
        "license": {"expression": "LicenseRef-OpenAlex-arXiv-Observation-Review", "redistribution": "restricted",
            "notice": "OpenAlex metadata and arXiv descriptive metadata are CC0 under the original acquired official license documents. Documentation HTML and all raw evidence remain privately retained; no article full text is requested."},
        "acquisition": receipt["tool"], "normalization": {"schema": NORMALIZATION_SCHEMA, "tool": normalizer,
            "inputs": ["citation_transcript"], "outputs": ["citation_transcript"]},
        "evidence": {"acquisition_receipts": [evidence_ref("acquisition/receipt.json", acquisition_receipt_id=receipt["acquisition_receipt_id"])],
            "normalization_lineage": evidence_ref("evidence/lineage.json", normalization_lineage_id=lineage["normalization_lineage_id"]),
            "request_parameter_preimages": sorted(preimages, key=lambda p: p["parameters_sha256"])}}
    manifest = archive.source_plan_contracts._source_manifest_from_plan(source, "OpenAlex source")
    contracts.validate_source_manifest_evidence_documents(manifest, receipts={receipt["acquisition_receipt_id"]: receipt}, lineage=lineage,
        request_parameter_preimages={r["parameters_sha256"]: {k: r[k] for k in ("parameters_sha256", "bytes", "media_type")} for r in preimages}, parent_source_manifests={})
    files["source-manifest.json"] = canonical(manifest)
    files["source-fragment.json"] = canonical({"schema": "wikilean.openalex-source-fragment/v1", "scope": "source-plan-fragment",
        "physical_root": physical_root, "source_publishable": False, "redistribution": "restricted", "sources": [source], "input_bindings": []})
    return archive.manifest_files(files, EXPORT_SCHEMA)


def verify_export(path):
    origins()
    files, manifest = archive.read_bundle(path, EXPORT_SCHEMA)
    capture = {n.removeprefix("acquisition/"): raw for n, raw in files.items() if n.startswith("acquisition/")}
    profile = parse(files["normalization/profile.json"], "normalization profile")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["evidence/lineage.json"], "lineage")["audit"]["normalized_at"]
    require(build_export(capture, profile, programs, when) == {**files, "manifest.json": canonical(manifest)}, "OpenAlex source export differs from independent replay")
    source = contracts.validate_source_manifest(parse(files["source-manifest.json"], "source manifest"))
    contracts.verify_source_manifest_files(source, path)
    return {"source_manifest_id": source["source_manifest_id"], "export_id": manifest["identity"], "facts": parse(capture["facts.json"], "facts")}
