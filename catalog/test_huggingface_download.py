#!/usr/bin/env python3
"""Hermetic tests for immutable Hugging Face artifact acquisition."""
from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fetch_math_graph
import fetch_mathlib_graph
import huggingface_download as hf
import ingest_theorem_graph

REVISION = "a1" * 20
OTHER_REVISION = "b2" * 20
DATASET = "owner/dataset"
ROOT = Path(__file__).resolve().parents[1]


def request(remote_path: str, destination: Path, payload: bytes) -> hf.ArtifactRequest:
    return hf.ArtifactRequest(
        remote_path,
        destination,
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )


def successful_runner(payloads: dict[str, bytes], commands: list[list[str]]):
    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        if check:
            raise AssertionError("download helper must inspect curl's return code")
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        remote_path = command[-1].split(f"/{REVISION}/", 1)[-1]
        remote_path = remote_path.split(f"/{OTHER_REVISION}/", 1)[-1]
        output.write_bytes(payloads[remote_path])
        return SimpleNamespace(returncode=0)

    return run


def run_writer(
    root: Path,
    revision: str,
    payloads: dict[str, bytes],
    delay: float,
) -> None:
    if delay:
        time.sleep(delay)
    requests = [
        request(name, root / name, payload)
        for name, payload in sorted(payloads.items())
    ]
    hf.fetch_huggingface_artifacts(
        dataset=DATASET,
        revision=revision,
        requests=requests,
        user_agent="WikiLean-test/1",
        force=True,
        runner=successful_runner(payloads, []),
    )


class RevisionAndUrlTest(unittest.TestCase):
    def test_reviewed_registry_loads_all_three_exact_pins(self) -> None:
        expected = {
            fetch_math_graph.DATASET: (
                "ced4ca9de1bd9e5b67aa09d1d515e270e438fa1e",
                set(fetch_math_graph.FILES),
            ),
            fetch_mathlib_graph.DATASET: (
                "8c706461fe266802197b62af324de12a3f1aa7fb",
                set(fetch_mathlib_graph.FILES),
            ),
            ingest_theorem_graph.DATASET: (
                "5caba941dd716f17dba4880bd7173edfb1cc36d1",
                {ingest_theorem_graph.REMOTE_FILE},
            ),
        }
        for dataset, (revision, files) in expected.items():
            with self.subTest(dataset=dataset):
                pin = hf.load_reviewed_pin(dataset)
                self.assertEqual(pin.revision, revision)
                self.assertEqual(set(pin.files), files)

    def test_revision_requires_full_reviewed_commit(self) -> None:
        self.assertEqual(hf.validate_revision("AB" * 20), "ab" * 20)
        for invalid in (None, "", "main", "v1.0", "a" * 39, "g" * 40, "a" * 41):
            with self.subTest(invalid=invalid):
                with self.assertRaises(hf.HuggingFaceArtifactError):
                    hf.validate_revision(invalid)
        pin = hf.load_reviewed_pin(fetch_math_graph.DATASET)
        with self.assertRaisesRegex(
            hf.HuggingFaceArtifactError, "not the reviewed pin"
        ):
            hf.require_reviewed_revision(REVISION, pin)

    def test_source_registry_points_to_all_reviewed_dataset_pins(self) -> None:
        registry = json.loads(
            (ROOT / "catalog/data/source_registry.json").read_text()
        )
        references = (
            registry["edge_sources"]["theoremgraph_dependencies"]["pin"],
            registry["edge_sources"]["mathnetwork"]["pin"],
            registry["literature_sources"]["theoremgraph"]["pin"],
            registry["brain_sources"]["theoremgraph_matching"]["pin"],
        )
        for reference in references:
            with self.subTest(dataset=reference["dataset"]):
                pin = hf.load_reviewed_pin(reference["dataset"])
                self.assertEqual(
                    reference["registry"],
                    "catalog/huggingface_pins.json",
                )
                self.assertEqual(reference["revision"], pin.revision)

    def test_sealed_build_common_import_excludes_acquisition_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            shutil.copy2(
                ROOT / "brain/build_common.py",
                workspace / "build_common.py",
            )
            shutil.copy2(
                ROOT / "brain/build_context.py",
                workspace / "build_context.py",
            )
            completed = subprocess.run(
                [sys.executable, "-c", "import build_common"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_exact_url_construction_and_path_validation(self) -> None:
        self.assertEqual(
            hf.build_file_url(DATASET, REVISION, "nested/file name.csv"),
            "https://huggingface.co/datasets/owner/dataset/resolve/"
            f"{REVISION}/nested/file%20name.csv",
        )
        self.assertNotIn(
            "/resolve/main/",
            hf.build_file_url(DATASET, REVISION, "artifact.csv"),
        )
        for invalid_path in ("", "/absolute.csv", "../escape.csv", "x/../y.csv"):
            with self.subTest(invalid_path=invalid_path):
                with self.assertRaises(hf.HuggingFaceArtifactError):
                    hf.build_file_url(DATASET, REVISION, invalid_path)

    def test_each_cli_requires_an_exact_pin_and_supports_its_env(self) -> None:
        modules = (
            fetch_math_graph,
            fetch_mathlib_graph,
            ingest_theorem_graph,
        )
        for module in modules:
            with self.subTest(module=module.__name__):
                with mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(
                        SystemExit, "immutable dataset revision"
                    ):
                        module.main([])
                    with self.assertRaisesRegex(SystemExit, "full 40-hex"):
                        module.main(["--revision", "main"])
                    pin = hf.load_reviewed_pin(module.DATASET)
                    with mock.patch.dict(
                        os.environ,
                        {module.REVISION_ENV: pin.revision},
                        clear=True,
                    ):
                        self.assertEqual(
                            module.parse_args([]).revision, pin.revision
                        )
                        self.assertEqual(
                            module.parse_args(
                                ["--revision", OTHER_REVISION]
                            ).revision,
                            OTHER_REVISION,
                        )

    def test_ingest_consumes_cache_inside_verified_lock_context(self) -> None:
        pin = hf.load_reviewed_pin(ingest_theorem_graph.DATASET)
        active = False
        metadata = {
            "schema": hf.SIDECAR_SCHEMA,
            "dataset": pin.dataset,
            "revision": pin.revision,
            "file_url": hf.build_file_url(
                pin.dataset,
                pin.revision,
                ingest_theorem_graph.REMOTE_FILE,
            ),
            "sha256": pin.files[ingest_theorem_graph.REMOTE_FILE][0],
            "size": pin.files[ingest_theorem_graph.REMOTE_FILE][1],
        }

        @contextmanager
        def guarded(**_kwargs):
            nonlocal active
            active = True
            try:
                yield [metadata]
            finally:
                active = False

        def consume(_args, received):
            self.assertTrue(active)
            self.assertEqual(received, metadata)
            return 0

        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "theorem_matching.csv"
            cache.write_bytes(b"fixture")
            with (
                mock.patch.object(ingest_theorem_graph, "CACHE", cache),
                mock.patch.object(
                    ingest_theorem_graph,
                    "verified_artifact_set",
                    side_effect=guarded,
                ),
                mock.patch.object(
                    ingest_theorem_graph, "ingest", side_effect=consume
                ),
            ):
                self.assertEqual(
                    ingest_theorem_graph.main(
                        ["--revision", pin.revision]
                    ),
                    0,
                )
        self.assertFalse(active)


class AcquisitionTest(unittest.TestCase):
    def assert_no_transaction_debris(self, root: Path) -> None:
        self.assertFalse(hf._state_paths(root, DATASET)[1].exists())
        token = hf._dataset_token(DATASET)
        self.assertEqual(list(root.glob(f".hf-{token}.*.tmp")), [])

    def test_download_uses_exact_url_and_writes_verifiable_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.csv"
            payload = b"header\nvalue\n"
            commands: list[list[str]] = []
            artifact = request("artifact.csv", destination, payload)
            result = hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[artifact],
                user_agent="WikiLean-test/1",
                runner=successful_runner({"artifact.csv": payload}, commands),
            )[0]

            self.assertTrue(result.downloaded)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][:2], ["curl", "--disable"])
            self.assertEqual(
                commands[0][-1],
                "https://huggingface.co/datasets/owner/dataset/resolve/"
                f"{REVISION}/artifact.csv",
            )
            self.assertIn("--fail", commands[0])
            self.assertIn("--retry-all-errors", commands[0])
            metadata = json.loads(hf.sidecar_path(destination).read_text())
            self.assertEqual(
                metadata,
                {
                    "schema": hf.SIDECAR_SCHEMA,
                    "dataset": DATASET,
                    "revision": REVISION,
                    "file_url": commands[0][-1],
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                },
            )
            with hf.verified_artifact_set(
                dataset=DATASET,
                revision=REVISION,
                requests=[artifact],
            ) as verified:
                self.assertEqual(verified, [metadata])
                self.assertEqual(destination.read_bytes(), payload)

            def unexpected_runner(*_args, **_kwargs):
                raise AssertionError("verified cache must not perform a download")

            cached = hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[artifact],
                user_agent="WikiLean-test/1",
                runner=unexpected_runner,
            )[0]
            self.assertFalse(cached.downloaded)

    def test_safe_adoption_never_downloads_and_rejects_wrong_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "artifact.csv"
            payload = b"reviewed legacy bytes"
            destination.write_bytes(payload)
            artifact = request("artifact.csv", destination, payload)
            hf.adopt_existing_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[artifact],
            )
            hf.verify_cached_artifact(
                artifact, dataset=DATASET, revision=REVISION
            )

            bad = root / "bad.csv"
            bad.write_bytes(b"not reviewed")
            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "does not match reviewed pin"
            ):
                hf.adopt_existing_artifacts(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=[request("bad.csv", bad, b"expected")],
                )
            self.assertFalse(hf.sidecar_path(bad).exists())

    def test_failed_download_preserves_existing_artifact_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.csv"
            old_payload = b"original bytes"
            new_payload = b"replacement bytes"
            old_request = request("artifact.csv", destination, old_payload)
            new_request = request("artifact.csv", destination, new_payload)
            hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[old_request],
                user_agent="WikiLean-test/1",
                runner=successful_runner(
                    {"artifact.csv": old_payload}, []
                ),
            )
            old_data = destination.read_bytes()
            old_sidecar = hf.sidecar_path(destination).read_bytes()

            def failing_runner(command: list[str], *, check: bool) -> SimpleNamespace:
                Path(command[command.index("--output") + 1]).write_bytes(
                    new_payload[:5]
                )
                return SimpleNamespace(returncode=22)

            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "download failed"
            ):
                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=OTHER_REVISION,
                    requests=[new_request],
                    user_agent="WikiLean-test/1",
                    force=True,
                    runner=failing_runner,
                )
            self.assertEqual(destination.read_bytes(), old_data)
            self.assertEqual(
                hf.sidecar_path(destination).read_bytes(), old_sidecar
            )

    def test_batch_stages_every_file_before_publishing_any(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_payloads = {"one.csv": b"old one", "two.csv": b"old two"}
            new_payloads = {"one.csv": b"new one", "two.csv": b"new two"}
            old_requests = [
                request(name, root / name, payload)
                for name, payload in old_payloads.items()
            ]
            new_requests = [
                request(name, root / name, payload)
                for name, payload in new_payloads.items()
            ]
            hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=old_requests,
                user_agent="WikiLean-test/1",
                runner=successful_runner(old_payloads, []),
            )
            before = {
                path: path.read_bytes()
                for artifact in old_requests
                for path in (
                    artifact.destination,
                    hf.sidecar_path(artifact.destination),
                )
            }
            calls = 0

            def second_fails(command: list[str], *, check: bool) -> SimpleNamespace:
                nonlocal calls
                calls += 1
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(
                    new_payloads[command[-1].rsplit("/", 1)[-1]]
                )
                return SimpleNamespace(returncode=0 if calls == 1 else 28)

            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "download failed"
            ):
                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=OTHER_REVISION,
                    requests=new_requests,
                    user_agent="WikiLean-test/1",
                    force=True,
                    runner=second_fails,
                )
            for path, expected in before.items():
                self.assertEqual(path.read_bytes(), expected)

    def test_real_sigkill_across_prepared_publication_recovers_cleanly(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        # Replacements are: prepared journal, data 1, sidecar 1, data 2,
        # sidecar 2. Until the committed journal is durable, every phase must
        # roll back to the complete old generation without transaction debris.
        for kill_after in range(1, 6):
            with self.subTest(kill_after=kill_after), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                old_payloads = {
                    "one.csv": b"old one",
                    "two.csv": b"old two",
                }
                new_payloads = {
                    "one.csv": b"new one",
                    "two.csv": b"new two",
                }
                old_requests = [
                    request(name, root / name, payload)
                    for name, payload in old_payloads.items()
                ]
                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=old_requests,
                    user_agent="WikiLean-test/1",
                    runner=successful_runner(old_payloads, []),
                )

                pid = os.fork()
                if pid == 0:
                    real_replace = hf.os.replace
                    replacements = 0

                    def kill_after_replacement(source, target):
                        nonlocal replacements
                        real_replace(source, target)
                        replacements += 1
                        if replacements == kill_after:
                            os.kill(os.getpid(), signal.SIGKILL)

                    hf.os.replace = kill_after_replacement
                    run_writer(root, OTHER_REVISION, new_payloads, 0)
                    os._exit(99)
                waited, status = os.waitpid(pid, 0)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)

                with hf.verified_artifact_set(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=old_requests,
                ):
                    for name, payload in old_payloads.items():
                        self.assertEqual((root / name).read_bytes(), payload)
                self.assert_no_transaction_debris(root)

    def test_real_sigkill_before_journal_leaves_no_orphans_after_reader(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        for kill_point in ("before", "during"):
            with self.subTest(kill_point=kill_point), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                old_payloads = {
                    "one.csv": b"old one",
                    "two.csv": b"old two",
                }
                new_payloads = {
                    "one.csv": b"new one",
                    "two.csv": b"new two",
                }
                old_requests = [
                    request(name, root / name, payload)
                    for name, payload in old_payloads.items()
                ]
                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=old_requests,
                    user_agent="WikiLean-test/1",
                    runner=successful_runner(old_payloads, []),
                )

                pid = os.fork()
                if pid == 0:
                    if kill_point == "before":
                        def kill_before_journal(*_args, **_kwargs):
                            os.kill(os.getpid(), signal.SIGKILL)

                        hf._write_journal = kill_before_journal
                    else:
                        journal = hf._state_paths(root, DATASET)[1]
                        real_replace = hf.os.replace

                        def kill_before_journal_install(source, target):
                            if Path(target) == journal:
                                os.kill(os.getpid(), signal.SIGKILL)
                            real_replace(source, target)

                        hf.os.replace = kill_before_journal_install
                    run_writer(root, OTHER_REVISION, new_payloads, 0)
                    os._exit(99)
                waited, status = os.waitpid(pid, 0)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFSIGNALED(status))
                self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)

                token = hf._dataset_token(DATASET)
                self.assertTrue(list(root.glob(f".hf-{token}.*.tmp")))
                with hf.verified_artifact_set(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=old_requests,
                ):
                    for name, payload in old_payloads.items():
                        self.assertEqual((root / name).read_bytes(), payload)
                self.assert_no_transaction_debris(root)

    def test_two_writers_serialize_complete_generations(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload_sets = (
                (
                    REVISION,
                    {"one.csv": b"generation A one", "two.csv": b"generation A two"},
                ),
                (
                    OTHER_REVISION,
                    {"one.csv": b"generation B one", "two.csv": b"generation B two"},
                ),
            )
            children: list[int] = []
            for index, (revision, payloads) in enumerate(payload_sets):
                pid = os.fork()
                if pid == 0:
                    try:
                        run_writer(root, revision, payloads, index * 0.02)
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                children.append(pid)
            for pid in children:
                waited, status = os.waitpid(pid, 0)
                self.assertEqual(waited, pid)
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)

            metadata = json.loads(
                hf.sidecar_path(root / "one.csv").read_text()
            )
            final_revision = metadata["revision"]
            final_payloads = dict(payload_sets)[final_revision]
            final_requests = [
                request(name, root / name, payload)
                for name, payload in final_payloads.items()
            ]
            with hf.verified_artifact_set(
                dataset=DATASET,
                revision=final_revision,
                requests=final_requests,
            ):
                for name, payload in final_payloads.items():
                    self.assertEqual((root / name).read_bytes(), payload)
            self.assert_no_transaction_debris(root)

    def test_active_staging_does_not_block_multiple_readers(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "artifact.csv"
            old_payload = b"old generation"
            new_payload = b"new generation"
            old_request = request("artifact.csv", destination, old_payload)
            hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[old_request],
                user_agent="WikiLean-test/1",
                runner=successful_runner(
                    {"artifact.csv": old_payload}, []
                ),
            )

            ready_read, ready_write = os.pipe()
            continue_read, continue_write = os.pipe()
            writer = os.fork()
            if writer == 0:
                os.close(ready_read)
                os.close(continue_write)

                def blocked_runner(command, *, check):
                    output = Path(command[command.index("--output") + 1])
                    output.write_bytes(new_payload)
                    os.write(ready_write, b"1")
                    os.read(continue_read, 1)
                    return SimpleNamespace(returncode=0)

                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=OTHER_REVISION,
                    requests=[
                        request(
                            "artifact.csv", destination, new_payload
                        )
                    ],
                    user_agent="WikiLean-test/1",
                    force=True,
                    runner=blocked_runner,
                )
                os._exit(0)

            os.close(ready_write)
            os.close(continue_read)
            readable, _, _ = select.select([ready_read], [], [], 5)
            if not readable:
                os.kill(writer, signal.SIGKILL)
                os.waitpid(writer, 0)
                self.fail("writer did not reach the staged-download barrier")
            self.assertEqual(os.read(ready_read, 1), b"1")
            os.close(ready_read)

            readers: set[int] = set()
            for _ in range(2):
                reader = os.fork()
                if reader == 0:
                    try:
                        with hf.verified_artifact_set(
                            dataset=DATASET,
                            revision=REVISION,
                            requests=[old_request],
                        ):
                            if destination.read_bytes() != old_payload:
                                os._exit(2)
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                readers.add(reader)

            statuses: dict[int, int] = {}
            deadline = time.monotonic() + 2
            while readers and time.monotonic() < deadline:
                for reader in tuple(readers):
                    waited, status = os.waitpid(reader, os.WNOHANG)
                    if waited:
                        readers.remove(reader)
                        statuses[reader] = status
                if readers:
                    time.sleep(0.01)
            blocked_readers = set(readers)

            os.write(continue_write, b"1")
            os.close(continue_write)
            waited, writer_status = os.waitpid(writer, 0)
            self.assertEqual(waited, writer)
            for reader in readers:
                waited, status = os.waitpid(reader, 0)
                self.assertEqual(waited, reader)
                statuses[reader] = status

            self.assertEqual(blocked_readers, set())
            for status in statuses.values():
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)
            self.assertTrue(os.WIFEXITED(writer_status))
            self.assertEqual(os.WEXITSTATUS(writer_status), 0)
            new_request = request(
                "artifact.csv", destination, new_payload
            )
            with hf.verified_artifact_set(
                dataset=DATASET,
                revision=OTHER_REVISION,
                requests=[new_request],
            ):
                self.assertEqual(destination.read_bytes(), new_payload)
            self.assert_no_transaction_debris(root)

    def test_verified_reader_holds_shared_lock_for_full_consumption(self) -> None:
        if not hasattr(os, "fork"):
            self.skipTest("requires fork")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination = root / "artifact.csv"
            old_payload = b"old generation"
            new_payload = b"new generation"
            old_request = request("artifact.csv", destination, old_payload)
            new_request = request("artifact.csv", destination, new_payload)
            hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[old_request],
                user_agent="WikiLean-test/1",
                runner=successful_runner(
                    {"artifact.csv": old_payload}, []
                ),
            )
            with hf.verified_artifact_set(
                dataset=DATASET,
                revision=REVISION,
                requests=[old_request],
            ):
                pid = os.fork()
                if pid == 0:
                    try:
                        run_writer(
                            root,
                            OTHER_REVISION,
                            {"artifact.csv": new_payload},
                            0,
                        )
                    except BaseException:
                        os._exit(1)
                    os._exit(0)
                time.sleep(0.1)
                waited, _status = os.waitpid(pid, os.WNOHANG)
                self.assertEqual(waited, 0)
                self.assertEqual(destination.read_bytes(), old_payload)
            waited, status = os.waitpid(pid, 0)
            self.assertEqual(waited, pid)
            self.assertTrue(os.WIFEXITED(status))
            self.assertEqual(os.WEXITSTATUS(status), 0)
            self.assertEqual(destination.read_bytes(), new_payload)

    def test_target_collisions_are_rejected_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = request("one.csv", root / "one.csv", b"one")
            collision = request(
                "other.csv", hf.sidecar_path(first.destination), b"other"
            )
            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "target collision"
            ):
                hf.fetch_huggingface_artifacts(
                    dataset=DATASET,
                    revision=REVISION,
                    requests=[first, collision],
                    user_agent="WikiLean-test/1",
                    runner=lambda *_args, **_kwargs: self.fail(
                        "collision must fail before download"
                    ),
                )

            protected = (
                *hf._state_paths(root, DATASET),
                hf._acquisition_lock_path(root, DATASET),
                root
                / (
                    f".hf-{hf._dataset_token(DATASET)}."
                    "artifact.csv.download-fixture.tmp"
                ),
            )
            for destination in protected:
                with self.subTest(destination=destination.name):
                    with self.assertRaisesRegex(
                        hf.HuggingFaceArtifactError, "collides"
                    ):
                        hf.fetch_huggingface_artifacts(
                            dataset=DATASET,
                            revision=REVISION,
                            requests=[
                                request(
                                    "protected.csv",
                                    destination,
                                    b"protected",
                                )
                            ],
                            user_agent="WikiLean-test/1",
                            runner=lambda *_args, **_kwargs: self.fail(
                                "collision must fail before download"
                            ),
                        )

    def test_tampered_cache_and_wrong_revision_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "artifact.csv"
            payload = b"source bytes"
            artifact = request("artifact.csv", destination, payload)
            hf.fetch_huggingface_artifacts(
                dataset=DATASET,
                revision=REVISION,
                requests=[artifact],
                user_agent="WikiLean-test/1",
                runner=successful_runner({"artifact.csv": payload}, []),
            )
            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "revision mismatch"
            ):
                hf.verify_cached_artifact(
                    artifact, dataset=DATASET, revision=OTHER_REVISION
                )
            destination.write_bytes(b"tampered")
            with self.assertRaisesRegex(
                hf.HuggingFaceArtifactError, "reviewed pin"
            ):
                hf.verify_cached_artifact(
                    artifact, dataset=DATASET, revision=REVISION
                )


if __name__ == "__main__":
    unittest.main()
