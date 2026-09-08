"""Experimental, offline assertion replay. No accepted authority or serving path.

This fixture policy deliberately excludes source transitions and generated edge
kinds. Semantic state retains inactive assertions and exact retraction history.
Chain order is separate from the state root so independent authored assertions
can commute without collapsing equivalent contributions.
"""
from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import authority_contracts as contracts

OP_SCHEMA = "wikilean.experimental-assertion-operation/v1"
FIXTURE_SCHEMA = "wikilean.experimental-assertion-fixture/v1"
LEDGER_SCHEMA = "wikilean.experimental-assertion-ledger/v1"
STATE_SCHEMA = "wikilean.experimental-assertion-state/v1"
POLICY = "wikilean.experimental-assertion-kinds/v1"
KINDS = frozenset({"formalizes", "mentions", "xref", "relates"})
CREATIONS = frozenset({"assert_entity", "assert_relationship"})
OPERATIONS = CREATIONS | {"retract_assertion", "restore_assertion"}
MAX_OPERATIONS = 100000
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
EMPTY_CHAIN_ROOT = contracts.domain_hash("wikilean.experimental-assertion-chain.v1", {"genesis": True, "policy": POLICY})


class AssertionError(ValueError):
    """The experimental assertion history is invalid."""


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), label + ": unexpected fields")
    return value


def canonical(value):
    return contracts.canonical_json_bytes(value)


def identity(domain, value, key):
    return contracts.domain_hash(domain, {k: v for k, v in value.items() if k != key})


def hash_id(value, label):
    require(isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value), label + ": invalid identity")


def stable_entity(value):
    require(isinstance(value, str) and 1 <= len(value) <= 1024 and not any(ord(c) < 32 for c in value), "invalid authored entity identifier")
    require(not value.startswith("cell:"), "authored operations cannot target generated cell identities")
    require(re.fullmatch(r"Q[1-9][0-9]{0,11}", value) is not None or
        any(value.startswith(prefix) and len(value) > len(prefix) for prefix in ("path:", "decl:", "ext:", "lit:")),
        "entity identifier is outside the explicit experimental organ policy")


def validate_actor(actor):
    require(isinstance(actor, dict), "missing operation actor")
    if actor.get("kind") == "fixture":
        exact(actor, {"kind", "id"}, "fixture actor")
        require(isinstance(actor["id"], str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", actor["id"]), "invalid fixture actor")
    elif actor.get("kind") == "git-snapshot":
        exact(actor, {"kind", "commit", "path", "row_sha256"}, "Git snapshot actor")
        require(isinstance(actor["commit"], str) and re.fullmatch(r"[a-f0-9]{40}", actor["commit"]), "invalid source commit")
        contracts.validate_literal_relative_path(actor["path"], "actor source path")
        require(isinstance(actor["row_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", actor["row_sha256"]), "invalid source row hash")
    else:
        raise AssertionError("unregistered experimental actor kind")


def validate_operation(operation):
    exact(operation, {"schema", "operation_id", "operation_type", "assertion_id", "expected_revision", "actor", "payload"}, "operation")
    require(operation["schema"] == OP_SCHEMA and isinstance(operation["operation_type"], str) and
        operation["operation_type"] in OPERATIONS, "unregistered operation version/type")
    hash_id(operation["operation_id"], "operation"); hash_id(operation["assertion_id"], "assertion")
    require(type(operation["expected_revision"]) is int and 0 <= operation["expected_revision"] <= MAX_OPERATIONS,
        "invalid expected assertion revision")
    validate_actor(operation["actor"])
    payload, kind = operation["payload"], operation["operation_type"]
    if kind == "assert_entity":
        exact(payload, {"entity", "attributes"}, "entity assertion")
        stable_entity(payload["entity"])
    elif kind == "assert_relationship":
        exact(payload, {"src", "dst", "kind", "attributes"}, "relationship assertion")
        stable_entity(payload["src"]); stable_entity(payload["dst"])
        require(isinstance(payload["kind"], str) and payload["kind"] in KINDS, "authored relationship kind is absent from fixture policy")
    elif kind == "retract_assertion":
        exact(payload, {"reason"}, "retraction")
        require(isinstance(payload["reason"], str) and bool(payload["reason"].strip()) and len(payload["reason"]) <= 8192,
            "retraction needs a bounded reason")
    else:
        exact(payload, {"retraction_id"}, "restoration")
        hash_id(payload["retraction_id"], "exact retraction")
    if kind in CREATIONS:
        require(operation["expected_revision"] == 0 and isinstance(payload["attributes"], dict), "creation requires expected absence and attributes")
    else:
        require(operation["expected_revision"] > 0, "mutation requires an exact prior assertion revision")
    require(len(canonical(operation)) <= 1024 * 1024, "operation exceeds its canonical size bound")
    require(operation["operation_id"] == identity("wikilean.experimental-assertion-operation.v1", operation, "operation_id"), "operation content identity differs")
    return operation


def make_operation(operation_type, assertion_id, payload, *, expected_revision=0, actor=None):
    operation = {"schema": OP_SCHEMA, "operation_type": operation_type, "assertion_id": assertion_id,
        "expected_revision": expected_revision, "actor": actor or {"kind": "fixture", "id": "fixture-author"}, "payload": copy.deepcopy(payload)}
    operation["operation_id"] = identity("wikilean.experimental-assertion-operation.v1", operation, "operation_id")
    return validate_operation(operation)


def conflict_footprint(operation):
    validate_operation(operation)
    target = "assertion/" + operation["assertion_id"]
    reads = {"policy/" + POLICY, target + "/revision/" + str(operation["expected_revision"])}
    if operation["operation_type"] == "restore_assertion":
        reads.add("retraction/" + operation["payload"]["retraction_id"])
    writes = {target, "operation/" + operation["operation_id"]}
    return {"reads": sorted(reads), "writes": sorted(writes)}


def independently_commute(left, right):
    """Only disjoint assertion histories commute under this fixture policy."""
    validate_operation(left); validate_operation(right)
    return left["assertion_id"] != right["assertion_id"] and left["operation_id"] != right["operation_id"]


def semantic_key(operation):
    require(operation["operation_type"] in CREATIONS, "semantic equivalence applies to assertion creation")
    # Evidence, confidence and attribution remain in the assertion and its state
    # root. They do not make two claims about the same entity/edge inequivalent.
    fields = ("entity",) if operation["operation_type"] == "assert_entity" else ("src", "dst", "kind")
    return contracts.domain_hash("wikilean.experimental-assertion-equivalence.v1",
        {"operation_type": operation["operation_type"], "claim": {k: operation["payload"][k] for k in fields}})


def state_document(assertions, operations):
    return {"schema": STATE_SCHEMA, "policy": POLICY,
        "assertions": [copy.deepcopy(assertions[k]) for k in sorted(assertions)],
        "operations": [copy.deepcopy(operations[k]) for k in sorted(operations)]}


def state_root(state):
    return contracts.domain_hash("wikilean.experimental-assertion-state.v1", state)


@dataclass(frozen=True)
class Replay:
    state: dict
    semantic_root: str
    chain_root: str
    fixtures: tuple
    receipts: tuple


def empty_replay():
    state = state_document({}, {})
    return Replay(state, state_root(state), EMPTY_CHAIN_ROOT, (), ())


def validate_fixture(fixture):
    exact(fixture, {"schema", "fixture_id", "policy", "previous_state_root", "previous_chain_root", "operations"}, "fixture")
    require(fixture["schema"] == FIXTURE_SCHEMA and fixture["policy"] == POLICY, "accepted changesets are not experimental fixtures")
    for name in ("fixture_id", "previous_state_root", "previous_chain_root"):
        hash_id(fixture[name], name)
    require(isinstance(fixture["operations"], list) and 0 < len(fixture["operations"]) <= MAX_OPERATIONS, "fixture operation count is invalid")
    ids = []
    for operation in fixture["operations"]:
        validate_operation(operation); ids.append(operation["operation_id"])
    require(len(ids) == len(set(ids)), "duplicate operation ID within one fixture")
    require(fixture["fixture_id"] == identity("wikilean.experimental-assertion-fixture.v1", fixture, "fixture_id"), "fixture identity differs")
    return fixture


def make_fixture(previous, operations):
    fixture = {"schema": FIXTURE_SCHEMA, "policy": POLICY, "previous_state_root": previous.semantic_root,
        "previous_chain_root": previous.chain_root, "operations": copy.deepcopy(list(operations))}
    fixture["fixture_id"] = identity("wikilean.experimental-assertion-fixture.v1", fixture, "fixture_id")
    return validate_fixture(fixture)


def _apply_fixture(previous, fixture):
    validate_fixture(fixture)
    require(fixture["previous_state_root"] == previous.semantic_root and fixture["previous_chain_root"] == previous.chain_root,
        "fixture predecessor/root differs from replay state")
    require(fixture["fixture_id"] not in {f["fixture_id"] for f in previous.fixtures}, "fixture ID already occupies a history position")
    assertions = {r["assertion_id"]: copy.deepcopy(r) for r in previous.state["assertions"]}
    operations = {r["operation_id"]: copy.deepcopy(r) for r in previous.state["operations"]}
    require(len(operations) + len(fixture["operations"]) <= MAX_OPERATIONS, "ledger operation count exceeds bound")
    footprints = []
    for operation in fixture["operations"]:
        operation_id, assertion_id, kind = (operation[k] for k in ("operation_id", "assertion_id", "operation_type"))
        require(operation_id not in operations, "operation ID already occupies a history position")
        current = assertions.get(assertion_id)
        revision = current["revision"] if current is not None else 0
        require(revision == operation["expected_revision"], "assertion expected revision/absence differs")
        if kind in CREATIONS:
            require(current is None, "assertion ID is already allocated, including inactive history")
            current = {"assertion_id": assertion_id, "creation_operation_id": operation_id,
                "assertion_type": kind, "payload": copy.deepcopy(operation["payload"]), "actor": copy.deepcopy(operation["actor"]),
                "semantic_key": semantic_key(operation), "revision": 1, "active": True, "current_retraction_id": None,
                "history": [operation_id], "retractions": []}
            assertions[assertion_id] = current
        elif kind == "retract_assertion":
            require(current is not None and current["active"] and current["current_retraction_id"] is None, "only an active assertion can be retracted")
            current["active"] = False
            current["current_retraction_id"] = operation_id
            current["retractions"].append({"retraction_id": operation_id, "restoration_id": None})
            current["revision"] += 1; current["history"].append(operation_id)
        else:
            require(current is not None and not current["active"] and
                current["current_retraction_id"] == operation["payload"]["retraction_id"], "restoration must name the exact current retraction")
            entry = next(r for r in current["retractions"] if r["retraction_id"] == operation["payload"]["retraction_id"])
            require(entry["restoration_id"] is None, "retraction was already restored")
            entry["restoration_id"] = operation_id
            current["active"] = True; current["current_retraction_id"] = None
            current["revision"] += 1; current["history"].append(operation_id)
        operations[operation_id] = copy.deepcopy(operation)
        footprints.append({"operation_id": operation_id, **conflict_footprint(operation)})
    state = state_document(assertions, operations)
    semantic = state_root(state)
    chain = contracts.domain_hash("wikilean.experimental-assertion-chain.v1",
        {"previous_chain_root": previous.chain_root, "fixture_id": fixture["fixture_id"], "state_root": semantic})
    receipt = {"fixture_id": fixture["fixture_id"], "previous_state_root": previous.semantic_root, "state_root": semantic,
        "chain_root": chain, "footprints": footprints, "footprint_root": contracts.domain_hash("wikilean.experimental-assertion-footprints.v1", footprints)}
    return Replay(state, semantic, chain, (*previous.fixtures, copy.deepcopy(fixture)), (*previous.receipts, receipt))


def replay_ledger(ledger, *, initial=None):
    exact(ledger, {"schema", "fixtures"}, "ledger")
    require(ledger["schema"] == LEDGER_SCHEMA and isinstance(ledger["fixtures"], list), "invalid experimental ledger")
    require(len(canonical(ledger)) <= MAX_DOCUMENT_BYTES, "ledger document exceeds bound")
    if initial is None:
        state = empty_replay()
    else:
        require(isinstance(initial, Replay), "incremental replay requires verified prior history")
        # An imported state cannot authorize its own hashes. Reconstruct the
        # retained prefix before applying the suffix; authoritative checkpoint
        # acceleration belongs to a future accepted protocol.
        state = replay_ledger({"schema": LEDGER_SCHEMA, "fixtures": list(initial.fixtures)})
        def checkpoint_bytes(result):
            return canonical({"state": result.state, "semantic_root": result.semantic_root,
                "chain_root": result.chain_root, "fixtures": list(result.fixtures), "receipts": list(result.receipts)})
        # Python equality conflates True with 1. Check the exact canonical
        # representations, including receipts and retained history.
        require(checkpoint_bytes(state) == checkpoint_bytes(initial),
            "incremental checkpoint differs from its complete retained history")
    for fixture in ledger["fixtures"]:
        state = _apply_fixture(state, fixture)
    return state


def result_document(result):
    return {"schema": "wikilean.experimental-assertion-replay/v1", "authority": False, "production_writes": False,
        "state_root": result.semantic_root, "chain_root": result.chain_root, "state": result.state, "receipts": list(result.receipts),
        "counts": {"fixtures": len(result.fixtures), "operations": len(result.state["operations"]),
            "assertions": len(result.state["assertions"]), "active_assertions": sum(r["active"] for r in result.state["assertions"])}}
