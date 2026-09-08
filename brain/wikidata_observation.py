"""Offline contract for one sealed Wikidata observation generation.

WDQS universe/relations and Action API descriptions are independent live
requests. They are never an upstream transaction. One reviewed plan closes
their request scope, and one immutable bundle closes all three outputs.

Selection inputs are content-bound planning evidence, not acquisition receipts
for those inputs. Their origin/revision must still be reviewed in source-plan
authority. In particular the prior Brain node generation is named explicitly;
normalization never opens an ambient brain/data/nodes.jsonl.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "brain" / "tools"))
import authority_contracts as contracts  # noqa: E402

PLAN_SCHEMA = "wikilean.wikidata-observation-plan/v1"
PLAN_SCHEMA_V2 = "wikilean.wikidata-observation-plan/v2"
BUNDLE_SCHEMA = "wikilean.wikidata-observation-bundle/v1"
BUNDLE_SCHEMA_V2 = "wikilean.wikidata-observation-bundle/v2"
NORMALIZATION_SCHEMA = "wikilean.wikidata-observation-normalization/v1"
NORMALIZATION_SCHEMA_V2 = "wikilean.wikidata-observation-normalization/v2"
TOOLCHAIN_SCHEMA = "wikilean.wikidata-observation-toolchain/v1"
TOOLCHAIN_SCHEMA_V2 = "wikilean.wikidata-observation-toolchain/v2"
PROFILE_REGISTRY_SCHEMA = "wikilean.wikidata-observation-profiles/v1"
PROFILE_REGISTRY_SCHEMA_V2 = "wikilean.wikidata-observation-profiles/v2"
PROFILE_REGISTRY = ROOT / "brain" / "wikidata_observation_profiles.json"
OBSERVATION_POLICY = "independent-live-requests/no-snapshot"
SOURCE = "wikidata-observation"
WDQS = "https://query.wikidata.org/sparql"
API = "https://www.wikidata.org/w/api.php"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 512 * 1024 * 1024
MAX_PLAN_QIDS = 50_000
QID = re.compile(r"Q[1-9][0-9]{0,11}\Z")
PID = re.compile(r"P[1-9][0-9]{0,11}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CLASSES = (
    "Q65943", "Q319141", "Q207505", "Q11538", "Q1166625", "Q24034552",
    "Q20026918", "Q1936384", "Q976981", "Q6498784", "Q186509", "Q21550639",
)
SELECTION_NAMES = (
    "concept-layer", "grounding", "prior-brain-nodes", "universe-extension",
    "wikidata-crossrefs",
)
OUTPUTS = {
    "wikidata-universe": "normalized/wikidata_universe.jsonl",
    "wikidata-edges": "normalized/wikidata_edges.jsonl",
    "wikidata-descriptions": "normalized/wikidata_descriptions.json",
}
LOCAL_TOOL_FILES = (
    "brain/acquire_wikidata_observation.py", "brain/stage_io.py",
    "brain/tools/authority_contracts.py", "brain/tools/execution_environment.py",
    "brain/wikidata_observation.py",
)
TRANSPORT_POLICY = {
    "redirects": "deny", "retry": "none-fail-closed", "proxies": "deny",
    "curl_config": "disabled", "tls": "https-only-tls1.2-minimum",
    "response_limit": MAX_RESPONSE_BYTES, "aggregate_limit": MAX_TRANSCRIPT_BYTES,
    "credentials": "none", "request_order": "universe-edges-descriptions",
}
RETRY_POLICY = {
    "max_attempts_per_request": 5,
    "http_statuses": [429, 502, 503, 504],
    "curl_transport_codes": [6, 7, 28, 52, 56],
    "backoff_seconds": [10, 20, 40, 60],
    "max_retry_after_seconds": 300,
    "retry_after": "delay-seconds-only; date-or-excessive-aborts",
    "evidence": "retain-every-attempt-and-response; exactly-one-final-success",
}
TRANSPORT_POLICY_V2 = {**TRANSPORT_POLICY, "retry": RETRY_POLICY}


class ObservationError(RuntimeError):
    """Incomplete, substituted or incoherent observation evidence."""


def canonical(value: Any) -> bytes:
    return contracts.canonical_json_bytes(value)


def artifact(value: Any) -> bytes:
    return contracts.canonical_artifact_json_bytes(value) + b"\n"


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def exact(value: Any, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ObservationError(f"{label}: unexpected object fields")
    return value


def integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ObservationError(f"{label}: expected a nonnegative integer")
    return value


def qids(values: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_PLAN_QIDS or any(
        not isinstance(value, str) or not QID.fullmatch(value) for value in values
    ) or values != sorted(set(values), key=lambda q: (len(q), q)):
        raise ObservationError(f"{label}: expected sorted unique canonical QIDs")
    if nonempty and not values:
        raise ObservationError(f"{label}: empty QID scope")
    return values


def parse(raw: bytes, label: str, *, canonical_required: bool = False) -> Any:
    try:
        value = contracts.parse_json_bytes(raw, location=label)
    except contracts.VerificationError as exc:
        raise ObservationError(f"{label}: invalid JSON") from exc
    if canonical_required and canonical(value) != raw:
        raise ObservationError(f"{label}: noncanonical JSON")
    return value


def validate_plan(plan: Any) -> dict:
    exact(plan, {"schema", "observation_policy", "selection_inputs", "edge_qids",
                 "description_qids", "volume_floors"}, "plan")
    if plan["schema"] not in {PLAN_SCHEMA, PLAN_SCHEMA_V2} or plan["observation_policy"] != OBSERVATION_POLICY:
        raise ObservationError("unsupported observation plan or snapshot claim")
    inputs = plan["selection_inputs"]
    if not isinstance(inputs, list) or len(inputs) != len(SELECTION_NAMES):
        raise ObservationError("plan must bind all five QID-selection inputs")
    by_name = {}
    for item in inputs:
        exact(item, {"name", "state", "sha256", "bytes", "qids"}, "selection input")
        name = item["name"]
        if not isinstance(name, str) or name not in SELECTION_NAMES or name in by_name:
            raise ObservationError("duplicate or unknown QID-selection input")
        qids(item["qids"], name)
        integer(item["bytes"], name)
        if item["state"] == "absent":
            if name != "prior-brain-nodes" or item["sha256"] is not None \
                    or item["bytes"] != 0 or item["qids"]:
                raise ObservationError("invalid explicit absent selection input")
        elif item["state"] == "present":
            if not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
                raise ObservationError("selection input must bind exact bytes")
        else:
            raise ObservationError("invalid selection state")
        by_name[name] = item
    if [item["name"] for item in inputs] != list(SELECTION_NAMES):
        raise ObservationError("selection inputs must be ordered by name")
    for output, names in (
        ("edge_qids", ("concept-layer", "prior-brain-nodes")),
        ("description_qids", ("grounding", "universe-extension", "wikidata-crossrefs")),
    ):
        qids(plan[output], output, nonempty=True)
        expected = sorted({q for name in names for q in by_name[name]["qids"]},
                          key=lambda q: (len(q), q))
        if plan[output] != expected:
            raise ObservationError(f"{output}: does not equal bound selection inputs")
    floors = exact(plan["volume_floors"], {
        "universe", "universe_classes", "universe_labels", "universe_slugs",
        "edges", "edge_labels", "description_qids", "descriptions",
    }, "volume floors")
    exact(floors["universe_classes"], set(CLASSES), "class floors")
    for key, value in floors.items():
        if key == "universe_classes":
            for cls, floor in value.items():
                integer(floor, cls)
                if floor < 1:
                    raise ObservationError("each mathematical class requires a nonzero floor")
        else:
            integer(value, key)
    if len(plan["description_qids"]) < floors["description_qids"]:
        raise ObservationError("description QID population is below reviewed floor")
    return plan


@dataclass(frozen=True)
class Request:
    stage: str
    kind: str
    uri: str
    parameters: bytes
    subjects: tuple[str, ...]

    def descriptor(self) -> dict:
        return {"kind": self.kind, "uri": self.uri,
                "parameters_sha256": sha(self.parameters)}


def requests_for(plan: dict) -> list[Request]:
    validate_plan(plan)
    requests = []
    for cls in CLASSES:
        query = f'''
SELECT ?x ?xLabel ?article WHERE {{
  ?x wdt:P31 wd:{cls} .
  OPTIONAL {{
    ?article schema:about ?x ;
             schema:isPartOf <https://en.wikipedia.org/> .
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
'''
        requests.append(Request("universe", "http_post", WDQS,
                                urllib.parse.urlencode({"query": query}).encode(), (cls,)))
    edge_batch_size = 25 if plan["schema"] == PLAN_SCHEMA_V2 else 100
    for start in range(0, len(plan["edge_qids"]), edge_batch_size):
        batch = plan["edge_qids"][start:start + edge_batch_size]
        values = " ".join(f"wd:{q}" for q in batch)
        if plan["schema"] == PLAN_SCHEMA:
            # Frozen v1 request preimages must remain byte-identical.
            query = f'''
SELECT ?s ?p ?pLabel ?o WHERE {{
  VALUES ?s {{ {values} }}
  ?s ?pd ?o .
  ?p wikibase:directClaim ?pd .
  FILTER(isIRI(?o) && STRSTARTS(STR(?o), "http://www.wikidata.org/entity/Q"))
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
'''
        else:
            objects = ", ".join(f"wd:{q}" for q in plan["edge_qids"])
            # Restrict to exactly the objects normalization already retains,
            # and finish the data join before invoking the costly label service.
            query = f'''
SELECT ?s ?p ?pLabel ?o WHERE {{
  {{ SELECT DISTINCT ?s ?p ?o WHERE {{
    VALUES ?s {{ {values} }}
    ?s ?pd ?o .
    ?p wikibase:directClaim ?pd .
    FILTER(?o IN ({objects}))
  }} }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
'''
        requests.append(Request("edges", "http_post", WDQS,
                                urllib.parse.urlencode({"query": query}).encode(), tuple(batch)))
    for start in range(0, len(plan["description_qids"]), 50):
        batch = plan["description_qids"][start:start + 50]
        parameters = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch),
            "props": "descriptions", "languages": "en", "format": "json",
        }).encode()
        requests.append(Request("descriptions", "http_get", API, parameters, tuple(batch)))
    return requests


def response_record(index: int, request: Request, raw: bytes, status: int,
                    content_type: str) -> dict:
    if status != 200 or content_type.split(";", 1)[0].strip().lower() not in (
        {"application/json", "application/sparql-results+json"}
        if request.stage != "descriptions" else {"application/json"}
    ) or len(raw) > MAX_RESPONSE_BYTES:
        raise ObservationError(f"request {index}: unsuccessful or oversized response")
    return {"index": index, "stage": request.stage, **request.descriptor(),
            "http_status": status, "content_type": content_type,
            "body_sha256": sha(raw), "body_base64": base64.b64encode(raw).decode("ascii")}


def _bindings(data: Any) -> list:
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("results"), dict) \
            or not isinstance(data["results"].get("bindings"), list):
        raise ObservationError("WDQS response lacks results.bindings")
    return data["results"]["bindings"]


def _value(binding: dict, field: str, default: Any = None) -> Any:
    item = binding.get(field)
    if item is None:
        return default
    if not isinstance(item, dict) or "value" not in item:
        raise ObservationError(f"WDQS binding lacks {field}.value")
    return item["value"]


def _entity(value: Any, pattern: re.Pattern = QID) -> str:
    prefix = "http://www.wikidata.org/entity/"
    if not isinstance(value, str) or not value.startswith(prefix) \
            or not pattern.fullmatch(value[len(prefix):]):
        raise ObservationError("WDQS response has a noncanonical entity identity")
    return value[len(prefix):]


def _descriptions(data: Any, batch: Sequence[str]) -> dict[str, str]:
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("entities"), dict):
        raise ObservationError("wbgetentities response lacks entities or reports an error")
    entities = data["entities"]
    if set(entities) != set(batch):
        raise ObservationError("wbgetentities returned an incomplete or substituted batch")
    result = {}
    for qid in batch:
        entity = entities[qid]
        if not isinstance(entity, dict):
            raise ObservationError("wbgetentities entity is not an object")
        if "missing" in entity:
            if entity["missing"] != "":
                raise ObservationError("invalid missing entity marker")
            continue
        ident = entity.get("id")
        if not isinstance(ident, str) or not QID.fullmatch(ident) or entity.get("type") != "item":
            raise ObservationError("wbgetentities lacks canonical item identity")
        redirect = entity.get("redirects")
        if ident == qid:
            if redirect is not None:
                raise ObservationError("unexpected redirect identity")
        elif not isinstance(redirect, dict) or redirect.get("from") != qid or redirect.get("to") != ident:
            raise ObservationError("invalid redirect identity")
        descriptions = entity.get("descriptions")
        if not isinstance(descriptions, dict):
            raise ObservationError("descriptions must be an object")
        english = descriptions.get("en")
        if english is None:
            continue
        if not isinstance(english, dict) or not isinstance(english.get("value"), str):
            raise ObservationError("English description must be a string")
        if english["value"]:
            result[qid] = english["value"]
    return result


def validate_response_payload(request: Request, raw: bytes) -> None:
    """Reject incomplete/error responses before requesting another live batch.

    This is a producer boundary. Historical transcript normalization retains its
    frozen acceptance semantics, including filtering out-of-scope edge objects.
    """
    try:
        data = contracts.parse_artifact_json_bytes(raw, location="live response")
    except contracts.VerificationError as exc:
        raise ObservationError("response contains invalid or truncated JSON") from exc
    if request.stage == "descriptions":
        _descriptions(data, request.subjects)
        return
    if request.stage not in {"universe", "edges"}:
        raise ObservationError("unknown response stage")
    for binding in _bindings(data):
        if not isinstance(binding, dict):
            raise ObservationError("WDQS binding must be an object")
        if request.stage == "universe":
            qid = _entity(_value(binding, "x"))
            if not isinstance(_value(binding, "xLabel", qid), str):
                raise ObservationError("WDQS label must be a string")
            article = _value(binding, "article")
            if article is not None and (not isinstance(article, str) or
                                       not article.startswith("https://en.wikipedia.org/wiki/")):
                raise ObservationError("WDQS article must be an English Wikipedia URI")
        else:
            subject = _entity(_value(binding, "s"))
            _entity(_value(binding, "p"), PID)
            _entity(_value(binding, "o"))
            if subject not in request.subjects:
                raise ObservationError("WDQS subject lies outside its requested batch")
            if not isinstance(_value(binding, "pLabel", ""), str):
                raise ObservationError("predicate label must be a string")


def normalize(plan: dict, records: Sequence[dict]) -> dict[str, bytes]:
    """Reconstruct every normalized byte from the complete exact request sequence."""
    requests = requests_for(plan)
    if len(records) != len(requests):
        raise ObservationError("truncated or surplus request transcript")
    universe: dict[str, dict[str, set]] = {}
    edges: dict[tuple[str, str, str], set[str]] = {}
    descriptions: dict[str, str] = {}
    edge_scope = set(plan["edge_qids"])
    total = 0
    for index, (record, request) in enumerate(zip(records, requests)):
        exact(record, {"index", "stage", "kind", "uri", "parameters_sha256", "http_status",
                       "content_type", "body_sha256", "body_base64"}, "transcript record")
        try:
            raw = base64.b64decode(record["body_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ObservationError("invalid response encoding") from exc
        total += len(raw)
        if total > MAX_TRANSCRIPT_BYTES:
            raise ObservationError("aggregate transcript exceeds operational bound")
        if record != response_record(index, request, raw, record["http_status"], record["content_type"]):
            raise ObservationError("request transcript identity/order mismatch")
        try:
            data = contracts.parse_artifact_json_bytes(raw, location=f"response {index}")
        except contracts.VerificationError as exc:
            raise ObservationError(f"response {index}: invalid corpus JSON") from exc
        if request.stage == "descriptions":
            descriptions.update(_descriptions(data, request.subjects))
            continue
        for binding in _bindings(data):
            if not isinstance(binding, dict):
                raise ObservationError("WDQS binding must be an object")
            if request.stage == "universe":
                qid = _entity(_value(binding, "x"))
                label = _value(binding, "xLabel", qid)
                if not isinstance(label, str):
                    raise ObservationError("WDQS label must be a string")
                slug = None
                article = _value(binding, "article")
                if article is not None:
                    prefix = "https://en.wikipedia.org/wiki/"
                    if not isinstance(article, str) or not article.startswith(prefix):
                        raise ObservationError("WDQS article must be an English Wikipedia URI")
                    slug = urllib.parse.unquote(article[len(prefix):])
                entry = universe.setdefault(qid, {"labels": set(), "classes": set(), "slugs": set()})
                entry["labels"].add(label)
                entry["classes"].add(request.subjects[0])
                if slug:
                    entry["slugs"].add(slug)
            else:
                subject = _entity(_value(binding, "s"))
                predicate = _entity(_value(binding, "p"), PID)
                obj = _entity(_value(binding, "o"))
                if subject not in request.subjects:
                    raise ObservationError("WDQS subject lies outside its requested batch")
                if obj not in edge_scope:
                    continue
                label = _value(binding, "pLabel", "")
                if not isinstance(label, str):
                    raise ObservationError("predicate label must be a string")
                edges.setdefault((subject, predicate, obj), set()).add(label)
    universe_rows = []
    for qid in sorted(universe, key=lambda q: (len(q), q)):
        entry = universe[qid]
        labels = entry["labels"] - {qid}
        universe_rows.append({"qid": qid, "label": min(labels) if labels else qid,
                              "classes": sorted(entry["classes"], key=lambda q: (len(q), q)),
                              "enwiki_slug": min(entry["slugs"]) if entry["slugs"] else None})
    edge_rows = []
    for (subject, predicate, obj), labels in sorted(edges.items()):
        labels = labels - {"", predicate}
        edge_rows.append({"s": subject, "p": predicate, "o": obj,
                          "p_label": min(labels) if labels else ""})
    actual = {
        "universe": len(universe_rows),
        "universe_labels": sum(bool(row["label"] and row["label"] != row["qid"]) for row in universe_rows),
        "universe_slugs": sum(bool(row["enwiki_slug"]) for row in universe_rows),
        "edges": len(edge_rows), "edge_labels": sum(bool(row["p_label"]) for row in edge_rows),
        "description_qids": len(plan["description_qids"]), "descriptions": len(descriptions),
    }
    for name, count in actual.items():
        if count < plan["volume_floors"][name]:
            raise ObservationError(f"{name}: below reviewed volume floor")
    for cls in CLASSES:
        if sum(cls in row["classes"] for row in universe_rows) < plan["volume_floors"]["universe_classes"][cls]:
            raise ObservationError(f"universe class {cls}: below reviewed volume floor")
    # Preserve the legacy fresh-generation coverage guards as well as pinned prior floors.
    if len(universe_rows) >= 50 and (actual["universe_labels"] < min(50, len(universe_rows) // 2)
                                   or actual["universe_slugs"] < min(50, len(universe_rows) // 4)):
        raise ObservationError("universe label/slug coverage collapsed")
    if len(edge_rows) >= 50 and actual["edge_labels"] < min(50, len(edge_rows) // 2):
        raise ObservationError("edge label coverage collapsed")
    return {
        OUTPUTS["wikidata-universe"]: b"".join(artifact(row) for row in universe_rows),
        OUTPUTS["wikidata-edges"]: b"".join(artifact(row) for row in edge_rows),
        OUTPUTS["wikidata-descriptions"]: artifact({
            "_meta": {"source": "wikidata wbgetentities (props=descriptions, languages=en)",
                      "n_qids": len(plan["description_qids"]), "n_descriptions": len(descriptions)},
            "descriptions": dict(sorted(descriptions.items())),
        }),
    }


def object_ref(role: str, raw: bytes, media_type: str) -> dict:
    return {"object": role, "sha256": sha(raw), "bytes": len(raw), "media_type": media_type}


def member_ref(path: str, raw: bytes) -> dict:
    media_type = "application/x-ndjson" if path.endswith(".jsonl") else (
        "application/x-www-form-urlencoded" if path.endswith(".form") else "application/json")
    return {"path": path, **{k: v for k, v in object_ref("unused", raw, media_type).items() if k != "object"}}


def profile_identity(profile: dict) -> str:
    return contracts.domain_hash("wikilean.wikidata-observation-profile.v1", {
        key: value for key, value in profile.items() if key != "profile_id"})


def retryable_response(response: dict) -> bool:
    """Only explicit transient statuses/transport failures may be retried."""
    status, code = response["http_status"], response["curl_exit_code"]
    if status in RETRY_POLICY["http_statuses"]:
        return code in (0, 22)
    return status in (None, 0) and code in RETRY_POLICY["curl_transport_codes"]


def successful_attempt_records(plan: dict, attempts: Sequence[dict]) -> tuple[list[dict], list[dict]]:
    """Verify a complete ordered attempt transcript without omitting failed bytes."""
    requests = requests_for(plan)
    if not isinstance(attempts, (list, tuple)) or not attempts \
            or len(attempts) > len(requests) * RETRY_POLICY["max_attempts_per_request"]:
        raise ObservationError("invalid bounded attempt transcript")
    canonical_requests = sorted((request.descriptor() for request in requests), key=canonical)
    request_indices = {canonical(request): i for i, request in enumerate(canonical_requests)}
    successful, summaries = [], []
    current, ordinal, total = 0, 1, 0
    for item in attempts:
        exact(item, {"request_index", "attempt", "outcome", "response", "retry_delay_seconds"}, "attempt")
        if type(item["request_index"]) is not int or item["request_index"] != current \
                or current >= len(requests) or type(item["attempt"]) is not int or item["attempt"] != ordinal:
            raise ObservationError("attempt request sequence/order differs")
        request = requests[current]
        delay = integer(item["retry_delay_seconds"], "retry delay")
        response = item["response"]
        if item["outcome"] == "succeeded":
            if delay != 0 or not isinstance(response, dict):
                raise ObservationError("successful attempt has a retry delay or invalid response")
            if type(response.get("index")) is not int or type(response.get("http_status")) is not int \
                    or not isinstance(response.get("content_type"), str):
                raise ObservationError("successful attempt response has invalid status types")
            raw = _attempt_body(response)
            if response != response_record(current, request, raw, response.get("http_status"), response.get("content_type", "")):
                raise ObservationError("successful attempt response identity differs")
            validate_response_payload(request, raw)
            successful.append(response)
            current, ordinal = current + 1, 1
        elif item["outcome"] == "failed":
            exact(response, {"http_status", "curl_exit_code", "content_type", "body_sha256", "body_base64", "retry_after"}, "failed response")
            for key in ("http_status", "curl_exit_code"):
                if response[key] is not None and (type(response[key]) is not int or response[key] < 0):
                    raise ObservationError("invalid failed response status")
            if response["content_type"] is not None and (not isinstance(response["content_type"], str)
                    or len(response["content_type"]) > 1024 or any(ord(c) < 32 for c in response["content_type"])):
                raise ObservationError("invalid failed response content type")
            raw = _attempt_body(response)
            if not retryable_response(response) or ordinal >= RETRY_POLICY["max_attempts_per_request"]:
                raise ObservationError("nonretryable or exhausted failure in complete observation")
            minimum = RETRY_POLICY["backoff_seconds"][ordinal - 1]
            retry_after = response["retry_after"]
            if retry_after is not None:
                if not isinstance(retry_after, dict) or retry_after.get("kind") != "delay-seconds":
                    raise ObservationError("retry transcript requires bounded normalized Retry-After delay")
                exact(retry_after, {"kind", "seconds"}, "Retry-After")
                minimum = max(minimum, integer(retry_after["seconds"], "Retry-After"))
            if delay != minimum or delay > RETRY_POLICY["max_retry_after_seconds"]:
                raise ObservationError("retry delay differs from reviewed policy")
            ordinal += 1
        else:
            raise ObservationError("unknown attempt outcome")
        total += len(raw)
        if total > MAX_TRANSCRIPT_BYTES:
            raise ObservationError("aggregate attempt transcript exceeds operational bound")
        summaries.append({"request_index": request_indices[canonical(request.descriptor())],
                          "outcome": item["outcome"], "response_sha256": sha(raw), "response_bytes": len(raw)})
    if current != len(requests) or ordinal != 1:
        raise ObservationError("incomplete attempt transcript")
    return successful, summaries


def _attempt_body(response: dict) -> bytes:
    try:
        encoded = response["body_base64"]
        if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES * 4 // 3 + 4:
            raise ObservationError("attempt response exceeds bound")
        raw = base64.b64decode(encoded, validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise ObservationError("invalid attempt response encoding") from exc
    if len(raw) > MAX_RESPONSE_BYTES or sha(raw) != response.get("body_sha256"):
        raise ObservationError("attempt response bytes differ")
    return raw


def reviewed_profiles() -> dict:
    """Consumer policy: immutable whole generations, never ambient helper hashes.

    The reviewed registry is deliberately outside its own implementation closure.
    Updating producer code requires an explicit new registry generation; retaining
    a previous profile preserves offline verification of its immutable bundles.
    """
    registry = parse(read_regular(PROFILE_REGISTRY, max_bytes=1024 * 1024),
                     "profile registry", canonical_required=True)
    exact(registry, {"schema", "current_profile", "profiles"}, "profile registry")
    if registry["schema"] not in {PROFILE_REGISTRY_SCHEMA, PROFILE_REGISTRY_SCHEMA_V2} or not isinstance(registry["profiles"], list):
        raise ObservationError("unsupported reviewed profile registry")
    profiles = {}
    for profile in registry["profiles"]:
        fields = {"profile_id", "normalization_schema", "files"}
        if registry["schema"] == PROFILE_REGISTRY_SCHEMA_V2 and "plan_schemas" in profile:
            fields.add("plan_schemas")
        if registry["schema"] == PROFILE_REGISTRY_SCHEMA_V2 and "retry_normalization_schema" in profile:
            fields.add("retry_normalization_schema")
            if profile["retry_normalization_schema"] != NORMALIZATION_SCHEMA_V2:
                raise ObservationError("invalid reviewed retry normalization schema")
        exact(profile, fields, "reviewed profile")
        if "plan_schemas" in profile:
            supported = profile["plan_schemas"]
            if not isinstance(supported, list) or not supported or \
                    any(value not in {PLAN_SCHEMA, PLAN_SCHEMA_V2} for value in supported) or \
                    supported != sorted(set(supported)):
                raise ObservationError("invalid reviewed plan schema support")
        if profile["normalization_schema"] != NORMALIZATION_SCHEMA or not isinstance(profile["files"], list):
            raise ObservationError("unsupported reviewed normalization profile")
        if len(profile["files"]) != len(LOCAL_TOOL_FILES):
            raise ObservationError("reviewed profile must close every local tool file")
        for item, relative in zip(profile["files"], LOCAL_TOOL_FILES):
            exact(item, {"path", "sha256"}, "reviewed tool file")
            if item["path"] != relative or not isinstance(item["sha256"], str) or not DIGEST.fullmatch(item["sha256"]):
                raise ObservationError("invalid reviewed tool file closure")
        identity = profile_identity(profile)
        if profile["profile_id"] != identity or identity in profiles:
            raise ObservationError("invalid or duplicate reviewed profile identity")
        profiles[identity] = profile
    if not isinstance(registry["current_profile"], str) or registry["current_profile"] not in profiles:
        raise ObservationError("current producer profile must be explicitly reviewed")
    if list(profiles) != sorted(profiles):
        raise ObservationError("reviewed profiles must be ordered by identity")
    return registry


def validate_toolchain(toolchain: dict) -> dict:
    exact(toolchain, {"schema", "profile_id", "observation_policy", "transport_policy", "python", "curl", "files"}, "toolchain")
    policies = {TOOLCHAIN_SCHEMA: TRANSPORT_POLICY, TOOLCHAIN_SCHEMA_V2: TRANSPORT_POLICY_V2}
    if toolchain["schema"] not in policies or toolchain["observation_policy"] != OBSERVATION_POLICY \
            or toolchain["transport_policy"] != policies[toolchain["schema"]]:
        raise ObservationError("unreviewed toolchain policy")
    python = exact(toolchain["python"], {"implementation", "version", "sha256", "startup"}, "Python identity")
    if python["implementation"] != "CPython" or not isinstance(python["version"], str) \
            or not re.fullmatch(r"3\.12\.[0-9]+", python["version"]) or python["startup"] != ["-I", "-S"]:
        raise ObservationError("toolchain requires isolated CPython 3.12")
    curl = exact(toolchain["curl"], {"sha256", "version"}, "curl identity")
    for identity in (python, curl):
        if not isinstance(identity["sha256"], str) or not DIGEST.fullmatch(identity["sha256"]):
            raise ObservationError("invalid executable digest")
    if not isinstance(curl["version"], str) or not curl["version"].startswith("curl "):
        raise ObservationError("invalid curl version identity")
    registry = reviewed_profiles()
    profile = next((item for item in registry["profiles"] if item["profile_id"] == toolchain["profile_id"]), None)
    if profile is None or toolchain["files"] != profile["files"]:
        raise ObservationError("toolchain does not bind one reviewed implementation generation")
    if toolchain["schema"] == TOOLCHAIN_SCHEMA_V2 and profile.get("retry_normalization_schema") != NORMALIZATION_SCHEMA_V2:
        raise ObservationError("toolchain profile did not support explicit retry evidence")
    return profile


def evidence_generation_id(receipt: bytes, lineage: bytes) -> str:
    return contracts.domain_hash("wikilean.wikidata-observation-evidence-generation.v1", [
        member_ref("acquisition-receipt.json", receipt),
        member_ref("normalization-lineage.json", lineage),
    ])


def bundle_files(plan: dict, records: Sequence[dict], toolchain: dict, audit_time: str) -> tuple[str, dict[str, bytes]]:
    profile = validate_toolchain(toolchain)
    validate_plan(plan)
    if plan["schema"] not in profile.get("plan_schemas", [PLAN_SCHEMA]):
        raise ObservationError("reviewed tool profile did not support this request plan schema")
    explicit_attempts = toolchain["schema"] == TOOLCHAIN_SCHEMA_V2
    if explicit_attempts:
        successful, summaries = successful_attempt_records(plan, records)
    else:
        successful, summaries = records, None
    normalized = normalize(plan, successful)
    raw = b"".join(artifact(record) for record in records)
    plan_bytes = canonical(plan)
    toolchain_bytes = canonical(toolchain)
    requests = requests_for(plan)
    descriptors = sorted((request.descriptor() for request in requests), key=canonical)
    raw_ref = object_ref("wikidata_observation_raw", raw, "application/x-ndjson")
    receipt = {
        "schema": contracts.ACQUISITION_RECEIPT_SCHEMA_V2 if explicit_attempts else contracts.ACQUISITION_RECEIPT_SCHEMA_V1,
        "acquisition_receipt_id": "sha256:" + "0" * 64,
        "source": SOURCE, "upstream_uri": "https://www.wikidata.org",
        "pin": {"type": "content_sha256", "value": sha(raw)},
        "tool": {"name": "wikilean-wikidata-observation-acquirer", "version": "2" if explicit_attempts else "1", "sha256": sha(toolchain_bytes)},
        "requests": descriptors,
        "batch": {"status": "complete", "request_set_root": contracts.acquisition_request_set_root(descriptors),
                  "requests_total": len(records), "requests_succeeded": len(requests), "requests_failed": len(records) - len(requests)},
        "outputs": [raw_ref], "audit": {"acquired_at": audit_time},
    }
    if explicit_attempts:
        receipt["attempts"] = summaries
    receipt["acquisition_receipt_id"] = contracts.acquisition_receipt_identity(receipt)
    contracts.validate_acquisition_receipt(receipt)
    lineage = {
        "schema": contracts.NORMALIZATION_LINEAGE_SCHEMA_V1,
        "normalization_lineage_id": "sha256:" + "0" * 64,
        "source": SOURCE, "mode": "transform",
        "acquisition_receipt_ids": [receipt["acquisition_receipt_id"]], "parent_source_manifest_ids": [],
        "normalization_schema": NORMALIZATION_SCHEMA_V2 if explicit_attempts else NORMALIZATION_SCHEMA, "configuration_sha256": sha(plan_bytes),
        "tool": {"name": "wikilean-wikidata-observation-normalizer", "version": "2" if explicit_attempts else "1",
                 "sha256": sha(canonical(profile)) if explicit_attempts else next(
                     item["sha256"] for item in profile["files"] if item["path"] == "brain/wikidata_observation.py")},
        "inputs": [{**raw_ref, "origin": {"kind": "acquisition_receipt", "id": receipt["acquisition_receipt_id"]}}],
        "outputs": sorted((object_ref(key.replace("-", "_"), normalized[path], member_ref(path, normalized[path])["media_type"])
                           for key, path in OUTPUTS.items()), key=lambda item: item["object"]),
        "result": "complete", "audit": {"normalized_at": audit_time},
    }
    lineage["normalization_lineage_id"] = contracts.normalization_lineage_identity(lineage)
    contracts.validate_normalization_lineage(lineage)
    receipt_bytes, lineage_bytes = canonical(receipt), canonical(lineage)
    bundle_id = evidence_generation_id(receipt_bytes, lineage_bytes)
    files = {"request-plan.json": plan_bytes, "toolchain.json": toolchain_bytes,
             "acquired.jsonl": raw, **normalized,
             **{f"requests/{index:06d}.form": request.parameters for index, request in enumerate(requests)}}
    if explicit_attempts:
        files["normalization-profile.json"] = canonical(profile)
    manifest = {
        "schema": BUNDLE_SCHEMA_V2 if explicit_attempts else BUNDLE_SCHEMA, "bundle_id": bundle_id, "observation_policy": OBSERVATION_POLICY,
        "acquisition_receipt_id": receipt["acquisition_receipt_id"],
        "normalization_lineage_id": lineage["normalization_lineage_id"],
        "members": [member_ref(path, data) for path, data in sorted(files.items())],
        "bindings": OUTPUTS,
    }
    return bundle_id, {**files, "acquisition-receipt.json": receipt_bytes,
                       "normalization-lineage.json": lineage_bytes, "bundle.json": canonical(manifest)}


def read_regular(path: Path, *, max_bytes: int = MAX_TRANSCRIPT_BYTES * 2) -> bytes:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ObservationError("evidence paths must be explicit absolute paths")
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise ObservationError("symlink evidence path")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes or before.st_nlink != 1:
            raise ObservationError("evidence member must be a bounded regular unlinked file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
        signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if len(raw) > max_bytes or signature(before) != signature(after) or signature(after) != signature(current):
            raise ObservationError("evidence member changed while reading")
        return raw
    finally:
        os.close(descriptor)


def verify_bundle(path: Path, *, expected_id: str | None = None, allow_staging: bool = False) -> dict:
    """Verify closure and independently replay all response normalization offline."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ObservationError("bundle must be an explicit absolute real directory")

    def private_directory(directory: Path) -> None:
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() \
                or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ObservationError("bundle directories must be current-user-owned 0700 directories")

    def member_bytes(relative: str, *, max_bytes: int = MAX_TRANSCRIPT_BYTES * 2) -> bytes:
        member = path / relative
        metadata = member.lstat()
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o644:
            raise ObservationError("bundle members must be current-user-owned 0644 files")
        return read_regular(member, max_bytes=max_bytes)

    private_directory(path)
    manifest = parse(member_bytes("bundle.json", max_bytes=2 * 1024 * 1024), "manifest", canonical_required=True)
    exact(manifest, {"schema", "bundle_id", "observation_policy", "acquisition_receipt_id",
                     "normalization_lineage_id", "members", "bindings"}, "manifest")
    if manifest["schema"] not in {BUNDLE_SCHEMA, BUNDLE_SCHEMA_V2} or manifest["observation_policy"] != OBSERVATION_POLICY \
            or manifest["bindings"] != OUTPUTS:
        raise ObservationError("unsupported bundle or mixed output bindings")
    if expected_id is not None and manifest["bundle_id"] != expected_id:
        raise ObservationError("bundle identity differs from expected generation")
    if not allow_staging and path.name != manifest["bundle_id"].removeprefix("sha256:"):
        raise ObservationError("directory does not name evidence generation")
    members = manifest["members"]
    if not isinstance(members, list):
        raise ObservationError("invalid bundle members")
    files = {}
    for ref in members:
        exact(ref, {"path", "sha256", "bytes", "media_type"}, "member")
        try:
            relative = contracts.validate_literal_relative_path(ref["path"], "member path")
        except contracts.VerificationError as exc:
            raise ObservationError("invalid member path") from exc
        if relative in files:
            raise ObservationError("duplicate bundle member")
        raw = member_bytes(relative)
        if member_ref(relative, raw) != ref:
            raise ObservationError("bundle member digest/size/type mismatch")
        files[relative] = raw
    evidence = {name: member_bytes(name, max_bytes=2 * 1024 * 1024) for name in (
        "acquisition-receipt.json", "normalization-lineage.json")}
    actual = set()
    directories = set()
    for directory, names, filenames in os.walk(path, followlinks=False):
        for name in names:
            child = Path(directory) / name
            if child.is_symlink():
                raise ObservationError("linked bundle directory")
            private_directory(child)
            directories.add(child.relative_to(path).as_posix())
        actual.update((Path(directory) / name).relative_to(path).as_posix() for name in filenames)
    expected_paths = {*files, *evidence, "bundle.json"}
    if actual != expected_paths or directories != {"normalized", "requests"}:
        raise ObservationError("undeclared or missing bundle member")
    plan = validate_plan(parse(files["request-plan.json"], "request plan", canonical_required=True))
    toolchain = parse(files["toolchain.json"], "toolchain", canonical_required=True)
    records = [parse(line, "transcript row") for line in files["acquired.jsonl"].splitlines()]
    receipt = parse(evidence["acquisition-receipt.json"], "receipt", canonical_required=True)
    lineage = parse(evidence["normalization-lineage.json"], "lineage", canonical_required=True)
    contracts.validate_acquisition_receipt(receipt)
    contracts.validate_normalization_lineage(lineage)
    if receipt["audit"]["acquired_at"] != lineage["audit"]["normalized_at"]:
        raise ObservationError("observation evidence audit generation mismatch")
    recomputed_id, expected_files = bundle_files(plan, records, toolchain, receipt["audit"]["acquired_at"])
    all_files = {**files, **evidence, "bundle.json": canonical(manifest)}
    if recomputed_id != manifest["bundle_id"] or all_files != expected_files:
        raise ObservationError("bundle does not reproduce its receipt, lineage and normalized outputs")
    return {"bundle_id": recomputed_id, "plan": plan, "manifest": manifest,
            "receipt": receipt, "lineage": lineage, "toolchain": toolchain,
            "normalized": {key: files[value] for key, value in OUTPUTS.items()}}
