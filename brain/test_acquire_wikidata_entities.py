#!/usr/bin/env python3
"""Hermetic tests for the sealed Wikidata entity acquisition boundary."""
from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acquire_wikidata_entities as acquire  # noqa: E402
import wikidata_entity_bundle as verifier  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import authority_contracts as contracts  # noqa: E402

AUDIT_TIME = "2026-09-05T12:00:00Z"


def plan_bytes(qids: list[str]) -> bytes:
    return contracts.canonical_json_bytes({
        "schema": acquire.REQUEST_PLAN_SCHEMA,
        "qids": qids,
    })


def response(value: object) -> bytes:
    if isinstance(value, dict) and "entities" in value \
            and "error" not in value and "success" not in value:
        value = {"success": 1, **value}
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def entity(
    qid: str,
    *,
    requested: str | None = None,
    label: str | None = None,
) -> dict:
    value = {
        "id": qid,
        "labels": {"en": {"language": "en", "value": label or f"entity {qid}"}},
        "aliases": {"en": []},
        "descriptions": {"en": {"language": "en", "value": "description"}},
        "claims": {"P31": []},
        "sitelinks": {},
        "lastrevid": 123,
        "modified": "2026-09-01T00:00:00Z",
    }
    if requested is not None and requested != qid:
        value["redirects"] = {"from": requested, "to": qid}
    return value


def no_such(qid: str | None = None) -> bytes:
    return response({
        "error": {
            "code": "no-such-entity",
            "info": (
                f'Could not find an entity with the ID "{qid}".'
                if qid is not None
                else "one requested entity does not exist"
            ),
        }
    })


def fake_toolchain() -> dict:
    return {
        "schema": acquire.TOOLCHAIN_SCHEMA,
        "invocation": {
            "uri": acquire.UPSTREAM_URI,
            "arguments": acquire.CURL_ARGUMENTS,
            "forwarded_environment": acquire.FORWARDED_ENVIRONMENT,
            "forced_environment": acquire.FORCED_ENVIRONMENT,
            "response_policy": acquire.HTTP_RESPONSE_POLICY,
        },
        "curl": {
            "version": "curl test-runtime",
            "sha256": acquire._file_sha256(acquire._resolved_curl()),
        },
        "python": {
            "implementation": acquire.platform.python_implementation(),
            "version": acquire.platform.python_version(),
            "sha256": acquire._file_sha256(acquire._resolved_python()),
            "startup_flags": acquire.REQUIRED_PYTHON_STARTUP_FLAGS,
        },
        "local_dependencies": acquire._local_dependency_records(),
        "wrapper": {"sha256": acquire.LOADED_SCRIPT_SHA256},
    }


def fake_tool(toolchain: dict) -> dict:
    return {
        "name": "wikilean-wikidata-entity-acquirer",
        "version": "1",
        "sha256": acquire._sha256(contracts.canonical_json_bytes(toolchain)),
    }


def complex_fixture() -> tuple[bytes, list[bytes]]:
    qids = ["Q1", "Q2", "Q999"]
    q1 = entity("Q1", label="Cafe\u0301")
    q1["aliases"]["en"] = [
        {"language": "en", "value": "zeta"},
        {"language": "en", "value": "alpha"},
        {"language": "en", "value": "alpha"},
    ]
    q1["claims"]["P31"] = [
        {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}},
        {"mainsnak": {"datavalue": {"value": {"id": "Q3"}}}},
        {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}},
    ]
    q1["sitelinks"]["enwiki"] = {"site": "enwiki", "title": "Cafe one"}
    q2 = entity("Q20", requested="Q2", label="redirect target")
    return plan_bytes(qids), [
        no_such(),
        response({"entities": {"Q1": q1}}),
        no_such(),
        response({"entities": {"Q2": q2}}),
        no_such("Q999"),
    ]


class WikidataEntityAcquisitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.toolchain = fake_toolchain()
        self.tool = fake_tool(self.toolchain)

    def publish(
        self,
        store: Path,
        *,
        audit_time: str = AUDIT_TIME,
        before_publish=None,
    ) -> Path:
        plan, transcript = complex_fixture()
        return acquire.publish_transcript(
            plan,
            transcript,
            store=store,
            acquisition_tool=self.tool,
            acquisition_toolchain=self.toolchain,
            audit_time=audit_time,
            before_publish=before_publish,
        )

    def test_contract_constants_and_wrapper_pin_match(self) -> None:
        self.assertEqual(verifier.REQUEST_FIELDS, acquire.REQUEST_FIELDS)
        self.assertEqual(
            verifier.BUNDLE_GENERATION_DOMAIN,
            acquire.BUNDLE_GENERATION_DOMAIN,
        )
        self.assertEqual(
            verifier.BUNDLE_IDENTITY_BASIS,
            acquire.BUNDLE_IDENTITY_BASIS,
        )
        self.assertEqual(verifier.CURL_ARGUMENTS, acquire.CURL_ARGUMENTS)
        self.assertEqual(verifier.HTTP_RESPONSE_POLICY, acquire.HTTP_RESPONSE_POLICY)
        self.assertEqual(
            verifier.NORMALIZATION_CONFIGURATION,
            acquire.NORMALIZATION_CONFIGURATION,
        )
        self.assertEqual(
            verifier.NORMALIZATION_CONFIGURATION_SHA256,
            acquire.NORMALIZATION_CONFIGURATION_SHA256,
        )
        self.assertEqual(
            list(verifier.LOCAL_DEPENDENCY_PINS),
            [
                {"path": relative, "sha256": digest}
                for relative, _path, digest in acquire.LOCAL_DEPENDENCIES
            ],
        )
        self.assertEqual(
            verifier.ACQUIRER_WRAPPER_SHA256,
            acquire._file_sha256(Path(acquire.__file__)),
        )
        body = acquire._request_body(("Q1", "Q2"))
        self.assertIn(b"formatversion=2", body)
        self.assertIn(b"maxlag=5", body)
        self.assertIn(b"redirects=yes", body)
        self.assertIn(b"%7Cinfo", body)
        self.assertTrue(body.endswith(b"ids=Q1%7CQ2"))
        self.assertIn("--max-filesize", acquire.CURL_ARGUMENTS)
        self.assertEqual(acquire.HTTP_RESPONSE_POLICY["retry"], "none-fail-closed")
        self.assertEqual(
            verifier.requested_qid_set_root(["Q1", "Q2"]),
            acquire.requested_qid_set_root(["Q1", "Q2"]),
        )

    def test_request_plan_requires_exact_canonical_sorted_qids(self) -> None:
        self.assertEqual(
            acquire.validate_request_plan_bytes(plan_bytes(["Q1", "Q10", "Q2"]))["qids"],
            ["Q1", "Q10", "Q2"],
        )
        cases = (
            b'{"schema":"wikilean.wikidata-entity-request-plan/v1","qids":[]}',
            plan_bytes(["Q2", "Q1"]),
            plan_bytes(["Q1", "Q1"]),
            plan_bytes(["Q0"]),
            plan_bytes(["Q01"]),
            plan_bytes(["q1"]),
            plan_bytes(["Q1234567890123"]),
            plan_bytes(["Q1"]) + b"\n",
            b'{"schema":"wikilean.wikidata-entity-request-plan/v1","qids":["Q1"]}',
        )
        for data in cases:
            with self.subTest(data=data), self.assertRaises(
                acquire.WikidataEntityAcquisitionError
            ):
                acquire.validate_request_plan_bytes(data)

    def test_complete_bisection_redirect_and_missing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = self.publish(Path(temporary) / "store")
            loaded = verifier.verify_wikidata_entity_bundle(target)
            self.assertEqual(loaded.requested_qids, ("Q1", "Q2", "Q999"))
            self.assertEqual(loaded.entities["Q999"], {"missing": True})
            self.assertEqual(loaded.entities["Q2"]["qid"], "Q20")
            self.assertEqual(loaded.entities["Q2"]["requested"], "Q2")
            self.assertEqual(loaded.entities["Q1"]["label"], "Café")
            self.assertEqual(loaded.entities["Q1"]["aliases"], ["alpha", "zeta"])
            self.assertEqual(loaded.entities["Q1"]["classes"], ["Q3", "Q5"])
            self.assertEqual(loaded.entities["Q1"]["enwiki_slug"], "Cafe_one")
            self.assertEqual(loaded.entities["Q1"]["lastrevid"], 123)
            self.assertEqual(
                loaded.entities["Q1"]["modified"], "2026-09-01T00:00:00Z"
            )
            self.assertEqual(loaded.acquired_at, AUDIT_TIME)
            receipt = json.loads((target / "acquisition-receipt.json").read_text())
            lineage = json.loads((target / "normalization-lineage.json").read_text())
            contracts.validate_acquisition_receipt(receipt)
            contracts.validate_normalization_lineage(lineage)
            self.assertEqual(receipt["batch"]["requests_total"], 5)
            manifest = json.loads((target / "bundle.json").read_text())
            self.assertEqual(manifest["summary"], {
                "requested": 3, "direct": 1, "redirected": 1, "missing": 1,
            })
            self.assertEqual(
                manifest["requested_qid_set_root"],
                acquire.requested_qid_set_root(["Q1", "Q2", "Q999"]),
            )
            self.assertEqual(
                sorted(path.name for path in (target / "requests").iterdir()),
                [f"{index:06d}.form" for index in range(5)],
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertTrue(all(
                stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o644)
                for path in target.rglob("*")
            ))

    def test_entity_local_redirect_is_required_and_top_level_is_corroborating(self) -> None:
        plan = plan_bytes(["Q1"])
        good = response({
            "redirects": [{"from": "Q1", "to": "Q2"}],
            "entities": {"Q1": entity("Q2", requested="Q1")},
        })
        with tempfile.TemporaryDirectory() as temporary:
            target = acquire.publish_transcript(
                plan,
                [good],
                store=Path(temporary) / "good",
                acquisition_tool=self.tool,
                acquisition_toolchain=self.toolchain,
                audit_time=AUDIT_TIME,
            )
            self.assertEqual(
                verifier.verify_wikidata_entity_bundle(target).entities["Q1"]["qid"],
                "Q2",
            )
        bad_payloads = (
            {"entities": {"Q1": entity("Q2")}},
            {
                "redirects": [{"from": "Q1", "to": "Q3"}],
                "entities": {"Q1": entity("Q2", requested="Q1")},
            },
            {
                "redirects": [{"from": "Q9", "to": "Q2"}],
                "entities": {"Q1": entity("Q1")},
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(bad_payloads):
                store = root / str(index)
                with self.subTest(index=index), self.assertRaises(
                    acquire.WikidataEntityAcquisitionError
                ):
                    acquire.publish_transcript(
                        plan,
                        [response(payload)],
                        store=store,
                        acquisition_tool=self.tool,
                        acquisition_toolchain=self.toolchain,
                        audit_time=AUDIT_TIME,
                    )
                self.assertFalse(store.exists())

    def test_normal_missing_marker_is_complete_without_bisection(self) -> None:
        plan = plan_bytes(["Q7"])
        payload = response({"entities": {"Q7": {"id": "Q7", "missing": ""}}})
        with tempfile.TemporaryDirectory() as temporary:
            target = acquire.publish_transcript(
                plan,
                [payload],
                store=Path(temporary) / "store",
                acquisition_tool=self.tool,
                acquisition_toolchain=self.toolchain,
                audit_time=AUDIT_TIME,
            )
            loaded = verifier.verify_wikidata_entity_bundle(target)
            self.assertEqual(loaded.entities, {"Q7": {"missing": True}})

    def test_missing_rows_require_exact_marker_identity_and_field_shape(self) -> None:
        plan = plan_bytes(["Q7"])
        rows = (
            {"id": "Q7", "missing": False},
            {"id": "Q7", "missing": True},
            {"id": "Q7", "missing": None},
            {"id": "Q7", "missing": 0},
            {"id": "Q8", "missing": ""},
            {"missing": ""},
            {"id": "Q7", "missing": "", "labels": {}},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, row in enumerate(rows):
                payload = {"success": 1, "entities": {"Q7": row}}
                with self.subTest(index=index), self.assertRaisesRegex(
                    acquire.WikidataEntityAcquisitionError, "invalid missing row"
                ):
                    acquire.publish_transcript(
                        plan,
                        [response(payload)],
                        store=root / f"producer-{index}",
                        acquisition_tool=self.tool,
                        acquisition_toolchain=self.toolchain,
                        audit_time=AUDIT_TIME,
                    )
                with self.assertRaisesRegex(
                    verifier.WikidataEntityBundleError, "invalid missing row"
                ):
                    verifier._normalize_success_payload(
                        payload, ["Q7"], location="test response"
                    )

    def test_singleton_no_such_must_name_the_requested_qid(self) -> None:
        plan = plan_bytes(["Q7"])
        cases = (no_such(), no_such("Q8"), response({
            "error": {
                "code": "no-such-entity",
                "info": "Could not find Q7 or Q8",
            }
        }))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, payload in enumerate(cases):
                store = root / str(index)
                with self.subTest(index=index), self.assertRaisesRegex(
                    acquire.WikidataEntityAcquisitionError, "does not identify"
                ):
                    acquire.publish_transcript(
                        plan,
                        [payload],
                        store=store,
                        acquisition_tool=self.tool,
                        acquisition_toolchain=self.toolchain,
                        audit_time=AUDIT_TIME,
                    )
                self.assertFalse(store.exists())

    def test_malformed_truncated_extra_and_incomplete_responses_publish_nothing(self) -> None:
        plan = plan_bytes(["Q1", "Q2"])
        malformed = (
            [b""],
            [b'{"entities":'],
            [b'{"entities":{},"entities":{}}'],
            [response([])],
            [response({"entities": {"Q1": entity("Q1")}})],
            [response({"entities": {"Q1": entity("Q1"), "Q2": entity("Q2"), "Q3": entity("Q3")}})],
            [response({"entities": {
                "Q1": {key: value for key, value in entity("Q1").items() if key != "id"},
                "Q2": entity("Q2"),
            }})],
            [response({"error": {"code": "maxlag", "info": "busy"}})],
            [response({"success": True, "entities": {"Q1": entity("Q1"), "Q2": entity("Q2")}})],
            [response({"success": 2, "entities": {"Q1": entity("Q1"), "Q2": entity("Q2")}})],
            [json.dumps({"entities": {"Q1": entity("Q1"), "Q2": entity("Q2")}}, separators=(",", ":")).encode()],
            [no_such()],
            [response({"entities": {"Q1": entity("Q1"), "Q2": entity("Q2")}}), response({})],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, transcript in enumerate(malformed):
                store = root / str(index)
                with self.subTest(index=index), self.assertRaises(
                    acquire.WikidataEntityAcquisitionError
                ):
                    acquire.publish_transcript(
                        plan,
                        transcript,
                        store=store,
                        acquisition_tool=self.tool,
                        acquisition_toolchain=self.toolchain,
                        audit_time=AUDIT_TIME,
                    )
                self.assertFalse(store.exists())

    def test_request_order_and_bisection_order_are_verified(self) -> None:
        plan, transcript = complex_fixture()
        parsed_plan = acquire.validate_request_plan_bytes(plan)
        iterator = iter(transcript)
        records, _entities = acquire._execute_plan(
            parsed_plan, lambda _qids, _index: next(iterator)
        )
        for record in records:
            record["response"] = base64.b64decode(record["response_base64"])
        records[1], records[2] = records[2], records[1]
        for index, record in enumerate(records):
            record["request_index"] = index
        with self.assertRaisesRegex(
            verifier.WikidataEntityBundleError, "order/bisection"
        ):
            verifier._replay_transcript(parsed_plan, records)

    def test_curl_invocation_uses_exact_body_and_sanitized_environment(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def run(command, **kwargs):
            calls.append((list(command), dict(kwargs)))
            return subprocess.CompletedProcess(
                command,
                0,
                response({"entities": {"Q1": entity("Q1")}}),
                b"wikilean-http-v1\t200\tapplication/json; charset=utf-8\n",
            )

        output = acquire._run_curl_request(
            ("Q1",),
            0,
            curl=acquire._resolved_curl(),
            toolchain=self.toolchain,
            runner=run,
        )
        self.assertIn(b'"Q1"', output.body)
        self.assertEqual(output.http_status, 200)
        self.assertEqual(output.content_type, "application/json; charset=utf-8")
        command, kwargs = calls[0]
        self.assertEqual(command, [
            str(acquire._resolved_curl()), *acquire.CURL_ARGUMENTS, acquire.UPSTREAM_URI,
        ])
        self.assertEqual(kwargs["input"], acquire._request_body(("Q1",)))
        self.assertEqual(kwargs["env"], acquire.FORCED_ENVIRONMENT)
        self.assertFalse(set(kwargs["env"]) & {
            "HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "CURL_HOME",
            "SSL_CERT_FILE", "AWS_SECRET_ACCESS_KEY",
        })
        self.assertNotIn("--retry", command)

    def test_wrong_http_status_or_content_type_fails_closed_without_retry(self) -> None:
        cases = (
            b"wikilean-http-v1\t503\tapplication/json\n",
            b"wikilean-http-v1\t200\ttext/html\n",
            b"Retry-After: 5\nwikilean-http-v1\t200\tapplication/json\n",
        )
        for metadata in cases:
            calls = 0

            def run(command, **_kwargs):
                nonlocal calls
                calls += 1
                return subprocess.CompletedProcess(
                    command,
                    0,
                    response({"entities": {"Q1": entity("Q1")}}),
                    metadata,
                )

            with self.subTest(metadata=metadata), self.assertRaises(
                acquire.WikidataEntityAcquisitionError
            ):
                acquire._run_curl_request(
                    ("Q1",), 0, curl=acquire._resolved_curl(),
                    toolchain=self.toolchain, runner=run,
                )
            self.assertEqual(calls, 1)

    def test_curl_failure_does_not_reflect_response_or_stderr(self) -> None:
        secret = "PRIVATE_RESPONSE_AND_CREDENTIAL"

        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 22, secret.encode(), secret.encode())

        with self.assertRaises(acquire.WikidataEntityAcquisitionError) as raised:
            acquire._run_curl_request(
                ("Q1",),
                0,
                curl=acquire._resolved_curl(),
                toolchain=self.toolchain,
                runner=run,
            )
        self.assertNotIn(secret, str(raised.exception))

    def test_clock_changes_evidence_generation_but_not_logical_ids(self) -> None:
        plan, transcript = complex_fixture()
        parsed = acquire.validate_request_plan_bytes(plan)
        iterator = iter(transcript)
        records, entities = acquire._execute_plan(
            parsed, lambda _qids, _index: next(iterator)
        )
        first_id, first = acquire._bundle_files(
            plan,
            records,
            entities,
            acquisition_tool=self.tool,
            acquisition_toolchain=self.toolchain,
            audit_time=AUDIT_TIME,
        )
        later_id, later = acquire._bundle_files(
            plan,
            records,
            entities,
            acquisition_tool=self.tool,
            acquisition_toolchain=self.toolchain,
            audit_time="2030-01-01T00:00:00Z",
        )
        self.assertNotEqual(first_id, later_id)
        for path in set(first) - {
            "acquisition-receipt.json",
            "normalization-lineage.json",
            "bundle.json",
        }:
            self.assertEqual(first[path], later[path], path)
        self.assertNotEqual(first["acquisition-receipt.json"], later["acquisition-receipt.json"])
        self.assertNotEqual(first["normalization-lineage.json"], later["normalization-lineage.json"])
        first_receipt = json.loads(first["acquisition-receipt.json"])
        later_receipt = json.loads(later["acquisition-receipt.json"])
        first_lineage = json.loads(first["normalization-lineage.json"])
        later_lineage = json.loads(later["normalization-lineage.json"])
        self.assertEqual(
            first_receipt["acquisition_receipt_id"],
            later_receipt["acquisition_receipt_id"],
        )
        self.assertEqual(
            first_lineage["normalization_lineage_id"],
            later_lineage["normalization_lineage_id"],
        )
        self.assertEqual(
            first[acquire.NORMALIZED_PATH], later[acquire.NORMALIZED_PATH]
        )

    def test_fresh_audit_evidence_gets_distinct_immutable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            first = self.publish(store, audit_time="2020-01-01T00:00:00Z")
            later = self.publish(store, audit_time="2030-01-01T00:00:00Z")
            converged = self.publish(store, audit_time="2020-01-01T00:00:00Z")

            self.assertNotEqual(first, later)
            self.assertEqual(converged, first)
            self.assertEqual(
                verifier.verify_wikidata_entity_bundle(first).acquired_at,
                "2020-01-01T00:00:00Z",
            )
            self.assertEqual(
                verifier.verify_wikidata_entity_bundle(later).acquired_at,
                "2030-01-01T00:00:00Z",
            )
            self.assertEqual(
                sorted(
                    path
                    for path in store.iterdir()
                    if path.name != ".staging"
                ),
                sorted([first, later]),
            )

    def test_independent_verifier_rejects_tampering_and_extra_members(self) -> None:
        mutations = (
            lambda target: (target / "requests" / "000000.form").write_bytes(b"tampered"),
            lambda target: (target / acquire.NORMALIZED_PATH).write_bytes(b"{}"),
            lambda target: (target / "toolchain.json").write_bytes(b"{}"),
            lambda target: (target / "extra").write_text("extra"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, mutate in enumerate(mutations):
                target = self.publish(root / f"store-{index}")
                mutate(target)
                with self.subTest(index=index), self.assertRaises(
                    verifier.WikidataEntityBundleError
                ):
                    verifier.verify_wikidata_entity_bundle(target)

    def test_independent_verifier_recomputes_exact_evidence_generation(self) -> None:
        mutations = (
            ("acquisition-receipt.json", "acquired_at"),
            ("normalization-lineage.json", "normalized_at"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, (name, field) in enumerate(mutations):
                target = self.publish(root / f"store-{index}")
                path = target / name
                document = json.loads(path.read_bytes())
                document["audit"][field] = "2030-01-01T00:00:00Z"
                path.write_bytes(contracts.canonical_json_bytes(document))
                with self.subTest(name=name), self.assertRaisesRegex(
                    verifier.WikidataEntityBundleError,
                    "bundle identity closure mismatch",
                ):
                    verifier.verify_wikidata_entity_bundle(target)

    def test_independent_verifier_rejects_hardlinked_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.publish(root / "store")
            os.link(target / "request-plan.json", root / "external-hardlink")
            with self.assertRaisesRegex(
                verifier.WikidataEntityBundleError, "regular file"
            ):
                verifier.verify_wikidata_entity_bundle(target)

    def test_mutated_stage_fails_before_atomic_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"

            def mutate(scratch: Path, _target: Path) -> None:
                (scratch / acquire.NORMALIZED_PATH).write_bytes(b"{}")

            with self.assertRaisesRegex(
                acquire.WikidataEntityAcquisitionError, "member mismatch"
            ):
                self.publish(store, before_publish=mutate)
            self.assertEqual(
                [path for path in store.iterdir() if path.name != ".staging"],
                [],
            )
            self.assertFalse(any((store / ".staging").iterdir()))

    def test_concurrent_publishers_converge_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            barrier = threading.Barrier(2)

            def publish(audit_time: str) -> Path:
                return self.publish(
                    store,
                    audit_time=audit_time,
                    before_publish=lambda _scratch, _target: barrier.wait(timeout=10),
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(publish, AUDIT_TIME),
                    executor.submit(publish, AUDIT_TIME),
                ]
                targets = [future.result(timeout=30) for future in futures]
            self.assertEqual(targets[0], targets[1])
            self.assertEqual(
                [path for path in store.iterdir() if path.name != ".staging"],
                [targets[0]],
            )
            self.assertFalse(any((store / ".staging").iterdir()))
            verifier.verify_wikidata_entity_bundle(targets[0])

    def test_existing_bad_content_address_is_not_replaced_or_trusted(self) -> None:
        plan, transcript = complex_fixture()
        parsed = acquire.validate_request_plan_bytes(plan)
        iterator = iter(transcript)
        records, entities = acquire._execute_plan(
            parsed, lambda _qids, _index: next(iterator)
        )
        bundle_id, _files = acquire._bundle_files(
            plan,
            records,
            entities,
            acquisition_tool=self.tool,
            acquisition_toolchain=self.toolchain,
            audit_time=AUDIT_TIME,
        )
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            store.mkdir(mode=0o700)
            target = store / bundle_id.removeprefix("sha256:")
            target.mkdir(mode=0o700)
            sentinel = target / "intruder"
            sentinel.write_text("preserve")
            with self.assertRaisesRegex(
                acquire.WikidataEntityAcquisitionError, "member set"
            ):
                self.publish(store)
            self.assertEqual(sentinel.read_text(), "preserve")
            self.assertFalse(any((store / ".staging").iterdir()))

    def test_request_plan_mutation_during_acquisition_fails_before_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            plan_path.write_bytes(plan_bytes(["Q1"]))

            def run(command, **_kwargs):
                plan_path.write_bytes(plan_bytes(["Q2"]))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    response({"entities": {"Q1": entity("Q1")}}),
                    b"wikilean-http-v1\t200\tapplication/json\n",
                )

            with (
                mock.patch.object(
                    acquire, "_verify_isolated_startup",
                    return_value=acquire.REQUIRED_PYTHON_STARTUP_FLAGS,
                ),
                mock.patch.object(
                    acquire,
                    "_pinned_toolchain",
                    return_value=(acquire._resolved_curl(), self.tool, self.toolchain),
                ),
            ):
                with self.assertRaisesRegex(
                    acquire.WikidataEntityAcquisitionError, "request plan changed"
                ):
                    acquire.acquire_bundle(
                        plan_path,
                        store=root / "store",
                        audit_time=AUDIT_TIME,
                        runner=run,
                    )
            self.assertFalse((root / "store").exists())

    def test_launcher_enforces_isolated_python(self) -> None:
        launcher = Path(acquire.__file__).with_name("acquire-wikidata-entities.sh")
        self.assertTrue(os.access(launcher, os.X_OK))
        result = subprocess.run(
            [str(launcher), "--help"],
            capture_output=True,
            text=True,
            timeout=20,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "WIKILEAN_PYTHON": sys.executable,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("request_plan", result.stdout)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "requires SIGKILL")
    def test_sigkill_before_publish_exposes_no_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            marker = root / "ready"
            plan, transcript = complex_fixture()
            plan_path = root / "plan.json"
            transcript_path = root / "transcript.json"
            plan_path.write_bytes(plan)
            transcript_path.write_text(json.dumps([
                item.decode("utf-8") for item in transcript
            ]))
            code = """
import json, signal, sys
from pathlib import Path
sys.path.insert(0, %(brain)r)
import acquire_wikidata_entities as acquire
from brain.tools import authority_contracts as contracts
plan = Path(%(plan)r).read_bytes()
responses = [item.encode() for item in json.loads(Path(%(transcript)r).read_text())]
toolchain = %(toolchain)r
tool = {
    'name': 'wikilean-wikidata-entity-acquirer',
    'version': '1',
    'sha256': acquire._sha256(contracts.canonical_json_bytes(toolchain)),
}
def pause(_scratch, _target):
    Path(%(marker)r).write_text('ready')
    signal.pause()
acquire.publish_transcript(
    plan, responses, store=Path(%(store)r), acquisition_tool=tool,
    acquisition_toolchain=toolchain, audit_time=%(audit)r,
    before_publish=pause,
)
""" % {
                "brain": str(Path(acquire.__file__).parent),
                "plan": str(plan_path),
                "transcript": str(transcript_path),
                "toolchain": self.toolchain,
                "marker": str(marker),
                "store": str(store),
                "audit": AUDIT_TIME,
            }
            child = subprocess.Popen([sys.executable, "-c", code], cwd=acquire.ROOT)
            deadline = time.monotonic() + 15
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "child did not reach pre-publication barrier")
            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(
                [path for path in store.iterdir() if path.name != ".staging"],
                [],
            )
            self.assertEqual(len(acquire.staging_orphans(store)), 1)
            target = self.publish(store)
            verifier.verify_wikidata_entity_bundle(target)


if __name__ == "__main__":
    unittest.main()
