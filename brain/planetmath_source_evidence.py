"""Fresh PlanetMath organization/default-head/tree evidence and closed parents.

The approved plan permits only the official public MSC repository listing and
its current default branches. Every returned SHA is captured once and every
archive must reconstruct that commit's complete Git tree. This is an ordered
observation of independent requests, not an upstream atomic snapshot.
"""
from __future__ import annotations

import base64
import copy
import gzip
import io as byteio
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "brain"))
import wikidata_crossref_sources as io
import mathlib_source_evidence as archive
import acquire_mathlib_sources as github
import external_pair_normalization as pair

contracts = io.contracts
canonical, artifact, sha, parse = io.canonical, io.artifact, io.sha, io.parse
require, exact = io.require, io.exact
PLAN_SCHEMA = "wikilean.planetmath-source-plan/v1"
CAPTURE_SCHEMA = "wikilean.planetmath-source-capture/v1"
EXPORT_SCHEMA = "wikilean.planetmath-source-export/v1"
PROFILE_SCHEMA = "wikilean.planetmath-source-profiles/v1"
TOOL_SCHEMA = "wikilean.planetmath-source-tool/v1"
TRANSCRIPT_SCHEMA = "wikilean.planetmath-repository-listing/v1"
NORMALIZATION_SCHEMA = "wikilean.planetmath-normalization/v1"
LISTING_SOURCE = "planetmath-repository-listing"
CHILD = "external-planetmath"
REGISTRY = ROOT / "brain/planetmath_source_profiles.json"
PHYSICAL_ROOT = "planetmath_export"
PARENTS = {"wikidata-wbgetentities", "wikidata-crossrefs", io.CURATED_SOURCE}
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
MSC = re.compile(r"^[0-9]{2}_")
POLICY = {"organization": "planetmath", "listing_page_size": 100, "maximum_listing_pages": 10,
    "maximum_repositories": 80, "maximum_listing_response_bytes": 8 * 1024 * 1024,
    "maximum_head_response_bytes": 8 * 1024 * 1024, "maximum_archive_bytes": 256 * 1024 * 1024,
    "maximum_expanded_archive_bytes": 512 * 1024 * 1024, "maximum_total_response_bytes": 2 * 1024 * 1024 * 1024,
    "maximum_total_tree_bytes": 4 * 1024 * 1024 * 1024, "maximum_total_tree_files": 100000,
    "maximum_pages": 100000, "maximum_links": 1000000, "minimum_request_interval_milliseconds": 500,
    "timeout_seconds": 600, "observation": "independent-live-requests/no-snapshot"}
TOOL_FILES = tuple(sorted({*io.TOOL_FILES, *pair.TOOL_FILES, "brain/mathlib_source_evidence.py", "brain/acquire_mathlib_sources.py",
    "brain/planetmath_source_evidence.py", "brain/planetmath_sources.py", "brain/planetmath_normalization.py", "brain/ingest/planetmath.py"}))


def origins():
    io.origins(); archive.validate_module_origins(); pair.origins()
    for module, path in ((io, "brain/wikidata_crossref_sources.py"), (archive, "brain/mathlib_source_evidence.py"),
                        (github, "brain/acquire_mathlib_sources.py"), (pair, "brain/external_pair_normalization.py")):
        require(Path(module.__file__).resolve(strict=True) == ROOT / path, "PlanetMath helper origin differs")
    require(io.contracts is archive.contracts and github.evidence is archive, "PlanetMath helper identities differ")


def profile_id(profile):
    return contracts.domain_hash("wikilean.planetmath-source-profile.v1", {k: v for k, v in profile.items() if k != "profile_id"})


def profiles():
    raw = io.read(REGISTRY)
    value = exact(parse(raw, "profiles"), {"schema", "current_profile", "profiles"}, "profiles")
    require(value["schema"] == PROFILE_SCHEMA and raw == canonical(value) and isinstance(value["profiles"], list), "invalid PlanetMath profiles")
    ids = []
    for profile in value["profiles"]:
        exact(profile, {"profile_id", "files", "policy"}, "profile")
        require(isinstance(profile["files"], list) and [p["path"] for p in profile["files"]] == list(TOOL_FILES), "incomplete PlanetMath closure")
        for item in profile["files"]:
            exact(item, {"path", "sha256"}, "program"); contracts._digest(item["sha256"], "program digest")
        require(profile["policy"] == POLICY and profile["profile_id"] == profile_id(profile), "unreviewed PlanetMath policy or generation")
        ids.append(profile["profile_id"])
    require(ids == sorted(set(ids)) and value["current_profile"] in ids, "invalid current PlanetMath profile")
    return value


def current_profile():
    origins()
    registry = profiles()
    profile = next(p for p in registry["profiles"] if p["profile_id"] == registry["current_profile"])
    require(profile["files"] == [{"path": p, "sha256": sha(io.read(ROOT / p))} for p in TOOL_FILES], "unreviewed current PlanetMath generation")
    return profile


def verify_programs(profile, programs):
    require(profile in profiles()["profiles"] and set(programs) == set(TOOL_FILES) and
        profile["files"] == [{"path": p, "sha256": sha(programs[p])} for p in TOOL_FILES], "PlanetMath preimages differ from reviewed whole generation")


def validate_tool(tool):
    exact(tool, {"schema", "profile_id", "files", "python", "gh"}, "PlanetMath tool")
    profile = next((p for p in profiles()["profiles"] if p["profile_id"] == tool["profile_id"]), None)
    require(tool["schema"] == TOOL_SCHEMA and profile is not None and tool["files"] == profile["files"], "unreviewed PlanetMath tool")
    for key in ("python", "gh"):
        exact(tool[key], {"sha256", "version"}, "executable"); contracts._digest(tool[key]["sha256"], "executable digest")
    require(isinstance(tool["python"]["version"], str) and re.fullmatch(r"CPython 3\.12\.[0-9]+ -I -S", tool["python"]["version"]), "isolated CPython3.12 required")
    require(isinstance(tool["gh"]["version"], str) and tool["gh"]["version"].startswith("gh version "), "invalid GitHub CLI identity")
    return profile


def validate_plan(plan):
    exact(plan, {"schema", "parents", "reviewed_parent_manifest_ids", "minimum_repositories", "minimum_pages", "minimum_links"}, "PlanetMath plan")
    require(plan["schema"] == PLAN_SCHEMA and isinstance(plan["parents"], list) and
        [s["source"] for s in plan["parents"]] == sorted(PARENTS), "exact PlanetMath parent closure required")
    require(isinstance(plan["reviewed_parent_manifest_ids"], dict) and set(plan["reviewed_parent_manifest_ids"]) == PARENTS,
        "all PlanetMath parents require reviewed identities")
    for value in plan["reviewed_parent_manifest_ids"].values(): contracts._hash(value, "reviewed parent")
    for key, maximum in (("minimum_repositories", "maximum_repositories"), ("minimum_pages", "maximum_pages"), ("minimum_links", "maximum_links")):
        require(type(plan[key]) is int and 1 <= plan[key] <= POLICY[maximum], "invalid PlanetMath completeness floor")
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
    manifests = {name: io.source_plan_contracts._source_manifest_from_plan(source, "PlanetMath parent") for name, source in sources.items()}
    require({n: m["source_manifest_id"] for n, m in manifests.items()} == plan["reviewed_parent_manifest_ids"], "PlanetMath parent differs from explicitly reviewed identity")
    by_id = {m["source_manifest_id"]: m for m in manifests.values()}
    selected = {("wikidata-crossrefs", "wikidata_crossrefs"), ("wikidata-crossrefs", "requested_qid_scope"), (io.CURATED_SOURCE, "source_registry")}
    objects, captured = {}, {}
    for name, source in sources.items():
        curated = None
        if source["source_kind"] == "curated_git_tree":
            require(name == io.CURATED_SOURCE and len(source["objects"]) == 1 and source["objects"][0]["path"] == io.REGISTRY_PATH,
                "unexpected curated PlanetMath parent selection")
            curated, tree, _proof, _tool = io.capture_git(roots[source["objects"][0]["root"]], source["pin"]["value"])
            require(tree == source["pin"]["tree"], "curated parent tree differs")
        for item in source["objects"]:
            raw = checked(curated if curated is not None else io.read(physical(item, roots)), item)
            key = (name, item["name"])
            objects[key] = item
            if key in selected:
                require("normalized" in item["roles"], "PlanetMath scope input must be normalized")
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
        require(set(lineage["parent_source_manifest_ids"]) <= set(by_id), "missing PlanetMath parent evidence ancestor")
        contracts.validate_source_manifest_evidence_documents(manifests[name], receipts=receipts, lineage=lineage,
            request_parameter_preimages=preimages, parent_source_manifests={key: by_id[key] for key in lineage["parent_source_manifest_ids"]})
    require(set(captured) == selected, "PlanetMath parent scope closure is incomplete")
    qid_map(captured)
    return sources, manifests, objects, captured



def qid_map(captured):
    refs = parse(captured[("wikidata-crossrefs", "wikidata_crossrefs")], "crossrefs", data=True)
    scope = exact(parse(captured[("wikidata-crossrefs", "requested_qid_scope")], "scope"), {"schema", "qids"}, "scope")
    require(scope["schema"] == io.SCOPE_SCHEMA and isinstance(scope["qids"], list) and scope["qids"] == sorted(set(scope["qids"]))
        and all(isinstance(q, str) and io.entities.QID_RE.fullmatch(q) for q in scope["qids"])
        and set(refs["xrefs"]) <= set(scope["qids"]), "invalid requested crossref scope")
    require("planetmath" in io.properties(captured[(io.CURATED_SOURCE, "source_registry")]).get("P7726", []) and
        "planetmath" in refs["properties"].get("P7726", []), "PlanetMath requires the curated P7726 mapping")
    result = {}
    for qid in sorted(refs["xrefs"], key=lambda q: (len(q), q)):
        values = refs["xrefs"][qid].get("planetmath", [])
        require(isinstance(values, list) and all(isinstance(v, str) and v for v in values), "PlanetMath identifiers must be concrete strings")
        for value in values: result.setdefault(value, qid)
    return result


def listing_spec(page):
    require(type(page) is int and 1 <= page <= POLICY["maximum_listing_pages"], "listing page limit exceeded")
    return ("listing-" + str(page), "/orgs/planetmath/repos?type=public&sort=full_name&direction=asc&per_page=100&page=" + str(page), "application/json", POLICY["maximum_listing_response_bytes"])


def head_spec(repo):
    return ("repo-" + repo["name"][:2] + "-head", "/repos/planetmath/" + repo["name"] + "/commits/" + urllib.parse.quote(repo["default_branch"], safe=""), "application/json", POLICY["maximum_head_response_bytes"])


def archive_spec(repo, commit):
    require(bool(archive.SHA1.fullmatch(commit)), "archive requires exact resolved commit")
    return ("repo-" + repo["name"][:2] + "-archive", "/repos/planetmath/" + repo["name"] + "/tarball/" + commit, "application/gzip", POLICY["maximum_archive_bytes"])


def parameters(spec):
    path, _separator, query = spec[1].partition("?")
    return {"method": "GET", "path": path, "query_urlencoded": query,
        "headers": {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        "transport": {"route": "gh-api-hostname-github.com; existing host authentication; redirects handled by gh",
            "pagination": False, "cache": False, "retries": 0, "timeout_seconds": POLICY["timeout_seconds"], "maximum_response_bytes": spec[3]}}


def request(spec):
    return {"kind": "http_get", "uri": "https://api.github.com" + parameters(spec)["path"], "parameters_sha256": sha(canonical(parameters(spec)))}


def record(spec, raw, exit_code=0):
    return {"object": spec[0], "request": request(spec), "result": "gh-api-exit-zero" if exit_code == 0 else "gh-api-nonzero", "exit_code": exit_code, "sha256": sha(raw), "bytes": len(raw)}


def response_record(spec, raw, row):
    require(type(row.get("exit_code")) is int and row["exit_code"] == 0 and row == record(spec, raw) and 0 < len(raw) <= spec[3],
        "failed, changed or oversized PlanetMath request")


class ListingWalk:
    def __init__(self, plan):
        validate_plan(plan)
        self.plan, self.names, self.ids, self.selected, self.done, self.pages = plan, set(), set(), {}, False, 0

    def accept(self, raw):
        require(not self.done, "listing has entries after terminal page")
        self.pages += 1
        listing_spec(self.pages)
        rows = parse(raw, "PlanetMath organization listing", data=True)
        require(isinstance(rows, list) and len(rows) <= POLICY["listing_page_size"], "invalid organization listing page")
        for row in rows:
            require(isinstance(row, dict) and isinstance(row.get("name"), str) and REPO.fullmatch(row["name"]), "invalid listed repository name")
            name = row["name"]
            require(type(row.get("id")) is int and row["id"] > 0 and row["id"] not in self.ids and name not in self.names,
                "duplicate repository identity in listing")
            owner = row.get("owner")
            require(isinstance(owner, dict) and owner.get("login") == "planetmath" and owner.get("type") == "Organization" and
                row.get("private") is False and row.get("full_name") == "planetmath/" + name and
                row.get("url") == "https://api.github.com/repos/planetmath/" + name and row.get("html_url") == "https://github.com/planetmath/" + name,
                "repository listing escaped official public organization")
            self.ids.add(row["id"]); self.names.add(name)
            if not MSC.match(name): continue
            branch = row.get("default_branch")
            require(isinstance(branch, str) and 0 < len(branch) <= 200 and all(ord(c) >= 32 and ord(c) != 127 for c in branch), "invalid listed default branch")
            require(name[:2] not in {r["name"][:2] for r in self.selected.values()}, "duplicate MSC repository category")
            self.selected[name] = {"name": name, "id": row["id"], "default_branch": branch}
            require(len(self.selected) <= POLICY["maximum_repositories"], "MSC repository limit exceeded")
        self.done = len(rows) < POLICY["listing_page_size"]
        if self.done: require(len(self.selected) >= self.plan["minimum_repositories"], "repository listing below reviewed completeness floor")
        return self.done

    def repositories(self):
        require(self.done, "organization listing is incomplete")
        return [self.selected[name] for name in sorted(self.selected)]


def head_identity(repo, raw):
    value = parse(raw, "PlanetMath default-branch commit", data=True)
    require(isinstance(value, dict) and isinstance(value.get("sha"), str) and archive.SHA1.fullmatch(value["sha"]), "invalid resolved Git commit")
    commit, prefix = value["sha"], "https://api.github.com/repos/planetmath/" + repo["name"]
    require(value.get("url") == prefix + "/commits/" + commit and isinstance(value.get("commit"), dict) and
        value["commit"].get("url") == prefix + "/git/commits/" + commit, "resolved commit belongs to a different repository")
    tree = value["commit"].get("tree")
    require(isinstance(tree, dict) and isinstance(tree.get("sha"), str) and archive.SHA1.fullmatch(tree["sha"]) and
        tree.get("url") == prefix + "/git/trees/" + tree["sha"], "resolved commit lacks its exact Git tree")
    return commit, tree["sha"]


def tree_from_archive(raw, expected_tree):
    # Bound decompression before the shared complete Git tree reconstructor can
    # allocate file payloads. Read to EOF so the original gzip CRC is checked.
    size = 0
    with gzip.GzipFile(fileobj=byteio.BytesIO(raw)) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            require(size <= POLICY["maximum_expanded_archive_bytes"], "expanded repository archive exceeds bound")
    return archive.extract_source(raw, expected_tree)


def replay(plan, raw, records, *, retain_trees=True):
    validate_plan(plan)
    require(isinstance(records, list) and len(records) <= POLICY["maximum_listing_pages"] + 2 * POLICY["maximum_repositories"], "too many PlanetMath responses")
    require(sum(map(len, raw.values())) <= POLICY["maximum_total_response_bytes"], "PlanetMath response budget exceeded")
    cursor, specs, walk = 0, [], ListingWalk(plan)
    def take(spec):
        nonlocal cursor
        require(cursor < len(records) and spec[0] in raw, "PlanetMath request transcript is incomplete")
        value = raw[spec[0]]
        response_record(spec, value, records[cursor]); cursor += 1; specs.append(spec)
        return value
    while not walk.done: walk.accept(take(listing_spec(walk.pages + 1)))
    trees, heads, tree_ids = {}, {}, {}
    total_files = total_bytes = 0
    for repo in walk.repositories():
        commit, tree = head_identity(repo, take(head_spec(repo)))
        files = tree_from_archive(take(archive_spec(repo, commit)), tree)
        total_files += len(files); total_bytes += sum(len(data) for _mode, data in files.values())
        require(total_files <= POLICY["maximum_total_tree_files"] and total_bytes <= POLICY["maximum_total_tree_bytes"], "PlanetMath complete tree budget exceeded")
        if retain_trees: trees[repo["name"]] = files
        heads[repo["name"]], tree_ids[repo["name"]] = commit, tree
    require(cursor == len(records) and set(raw) == {s[0] for s in specs}, "PlanetMath capture has undeclared responses")
    return walk.repositories(), trees, heads, tree_ids, specs


def receipt(source, pin, specs, outputs, tool, when):
    requests = sorted([request(s) for s in specs], key=canonical)
    value = {"schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V1, "source": source, "pin": pin, "upstream_uri": "https://github.com/planetmath",
        "tool": {"name": "wikilean-planetmath-acquirer", "version": "1", "sha256": sha(canonical(tool))}, "requests": requests,
        "batch": {"status": "complete", "requests_total": len(specs), "requests_succeeded": len(specs), "requests_failed": 0,
            "request_set_root": contracts.acquisition_request_set_root(requests)}, "outputs": sorted(outputs, key=lambda o: o["object"]), "audit": {"acquired_at": when}}
    value["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(value)
    contracts.validate_acquisition_receipt(value)
    return value


def capture_files(plan, raw, records, tool, programs, when):
    profile = validate_tool(tool); verify_programs(profile, programs)
    repos, _trees, heads, _tree_ids, specs = replay(plan, raw, records, retain_trees=False)
    listing_specs = [s for s in specs if s[0].startswith("listing-")]
    transcript = canonical({"schema": TRANSCRIPT_SCHEMA, "responses": [{**records[i], "body_base64": base64.b64encode(raw[s[0]]).decode("ascii")}
        for i, s in enumerate(listing_specs)]})
    listing_ref = {"object": "repository_listing", "sha256": sha(transcript), "bytes": len(transcript), "media_type": "application/json"}
    receipts = {LISTING_SOURCE: receipt(LISTING_SOURCE, {"type": "content_sha256", "value": sha(transcript)}, listing_specs, [listing_ref], tool, when)}
    for repo in repos:
        selected = [head_spec(repo), archive_spec(repo, heads[repo["name"]])]
        outputs = [{"object": s[0], "sha256": sha(raw[s[0]]), "bytes": len(raw[s[0]]), "media_type": s[2]} for s in selected]
        source = "planetmath-" + repo["name"][:2] + "-source"
        receipts[source] = receipt(source, {"type": "git_commit", "value": heads[repo["name"]]}, selected, outputs, tool, when)
    files = {"plan.json": canonical(plan), "tool.json": canonical(tool), "profile.json": canonical(profile), "listing.json": transcript,
        "request-results.json": canonical(records), "facts.json": canonical({"repositories": len(repos), "requests": len(specs), "response_bytes": sum(map(len, raw.values()))}),
        **{"raw/" + name: data for name, data in raw.items()}, **{"receipts/" + source + ".json": canonical(value) for source, value in receipts.items()},
        **{"requests/" + request(s)["parameters_sha256"] + ".json": canonical(parameters(s)) for s in specs},
        **{"implementation/" + name: value for name, value in programs.items()}}
    return archive.manifest_files(files, CAPTURE_SCHEMA)


def verify_capture_files(files):
    plan = validate_plan(parse(files["plan.json"], "PlanetMath plan"))
    tool = parse(files["tool.json"], "tool")
    raw = {name.removeprefix("raw/"): value for name, value in files.items() if name.startswith("raw/")}
    records = parse(files["request-results.json"], "request results")
    programs = {name: files["implementation/" + name] for name in TOOL_FILES}
    when = parse(files["receipts/" + LISTING_SOURCE + ".json"], "receipt")["audit"]["acquired_at"]
    expected = capture_files(plan, raw, records, tool, programs, when)
    require(files == {n: v for n, v in expected.items() if n != "manifest.json"}, "PlanetMath capture differs from complete independent request/tree replay")
    return plan, raw, records, tool


def verify_capture(path):
    origins()
    files, manifest = archive.read_bundle(path, CAPTURE_SCHEMA)
    verify_capture_files(files)
    return files, manifest
