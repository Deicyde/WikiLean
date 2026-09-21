#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import brain_public_baseline as baseline


HERE = Path(__file__).resolve().parent


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


class BaselineFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.source = self.base / "source-public"
        self.source.mkdir()
        self.store = self.base / "baselines"
        self._populate_source()
        self._initialize_authority_repository()

    def tearDown(self) -> None:
        # Frozen artifacts are deliberately 0555/0444.  Make the temporary
        # hierarchy owner-writable so cleanup behaves consistently on macOS.
        for current, dirnames, filenames in os.walk(self.base, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in filenames:
                with contextlib.suppress(OSError):
                    (current_path / name).chmod(0o600)
            for name in dirnames:
                child = current_path / name
                if not child.is_symlink():
                    with contextlib.suppress(OSError):
                        child.chmod(0o700)
            with contextlib.suppress(OSError):
                current_path.chmod(0o700)
        self.temporary.cleanup()

    def _write(self, relative: str, data: bytes | str | None = None) -> Path:
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = data if data is not None else f"fixture:{relative}\n"
        if isinstance(payload, str):
            payload = payload.encode()
        target.write_bytes(payload)
        return target

    def _populate_source(self) -> None:
        for relative in sorted(baseline.CRITICAL_PATHS):
            if not relative.endswith("/manifest.json"):
                self._write(relative)
        self._write(
            "assets/decl-index/manifest.json",
            json.dumps({"shards": {"aa": 1}}, separators=(",", ":")),
        )
        self._write("assets/decl-index/aa.json", b'[["A.a","A"]]\n')
        self._write(
            "assets/suffix-index/manifest.json",
            json.dumps({"shards": {"aa": 1}}, separators=(",", ":")),
        )
        self._write("assets/suffix-index/aa.json", b'{"aa":[["A.a","A"]]}\n')
        self._write(
            "assets/premise-index/manifest.json",
            json.dumps({"shards": {"aa": 1}, "chunks": 1}, separators=(",", ":")),
        )
        self._write("assets/premise-index/aa.json", b'{"A.a":[0]}\n')
        self._write("assets/premise-index/names/0.json", b'["A.a"]\n')
        self._write("assets/icons/wiki.svg", b"<svg/>\n")
        self._write("brain.html", b"candidate brain page\n")
        self._write("assets/brain/current.json", b'{"release":"candidate"}\n')
        self._write("assets/brain/cells/shard.json", b"[]\n")

    def _source_attestation(self) -> dict[str, object]:
        files: list[dict[str, object]] = []
        for path in sorted(self.source.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.source).as_posix()
            if baseline._is_brain_owned(relative):
                continue
            payload = path.read_bytes()
            files.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                }
            )
        return {"schema": baseline.SOURCE_ATTESTATION_SCHEMA, "files": files}

    def _git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    @property
    def attestation_path(self) -> Path:
        return self.repo / baseline.SOURCE_ATTESTATION_PATH

    def _write_attestation(self, document: object | None = None) -> None:
        self.attestation_path.parent.mkdir(parents=True, exist_ok=True)
        self.attestation_path.write_bytes(canonical(document or self._source_attestation()))

    def _commit(self, message: str) -> str:
        self._git("add", baseline.SOURCE_ATTESTATION_PATH)
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _initialize_authority_repository(self) -> None:
        self._git("init", "-q", "--initial-branch=main")
        self._git("config", "user.name", "Baseline Test")
        self._git("config", "user.email", "baseline-test@example.invalid")
        self._git("config", "commit.gpgsign", "false")
        self._git("config", "core.hooksPath", "/dev/null")
        self._write_attestation()
        self.authority = self._commit("attest public assets")

    def freeze(self, authority: str | None = None) -> baseline.PublicAssetBaseline:
        return baseline.freeze_public_baseline(
            self.source,
            self.store,
            authority or self.authority,
            self.repo,
        )

    def thaw(self, root: Path) -> None:
        for current, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in filenames:
                (current_path / name).chmod(0o600)
            for name in dirnames:
                child = current_path / name
                if not child.is_symlink():
                    child.chmod(0o700)
            current_path.chmod(0o700)

    def rewrite_manifest(self, root: Path, value: object, *, canonical_bytes: bool = True) -> None:
        manifest = root / baseline.MANIFEST_NAME
        manifest.chmod(0o600)
        raw = canonical(value) if canonical_bytes else json.dumps(value, indent=2).encode()
        manifest.write_bytes(raw)
        manifest.chmod(0o444)

class FreezeAndVerifyTest(BaselineFixture):
    def test_freezes_complete_non_brain_inventory_with_content_identity(self) -> None:
        result = self.freeze()
        self.assertEqual(result.root.name, result.baseline_hex)
        self.assertEqual(result.baseline_id, "sha256:" + result.baseline_hex)
        self.assertEqual(result.authority_git_commit, self.authority)
        paths = {item.path for item in result.files}
        self.assertIn("assets/icons/wiki.svg", paths)
        self.assertNotIn("brain.html", paths)
        self.assertFalse(any(path.startswith("assets/brain/") for path in paths))
        self.assertFalse((result.root / "brain.html").exists())
        self.assertFalse((result.root / "assets/brain").exists())

        document = json.loads(result.manifest_path.read_bytes())
        identity = {key: document[key] for key in ("schema", "authority", "files")}
        payload = (
            b"wikilean\0wikilean.public-asset-baseline.v1\0canonical-json-v1\0"
            + canonical(identity).removesuffix(b"\n")
        )
        self.assertEqual(document["baseline_id"], "sha256:" + hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.manifest_path.read_bytes(), canonical(document))

        verified = baseline.verify_public_baseline(
            result.root,
            self.repo,
            expected_baseline_id=result.baseline_id,
            expected_authority_git_commit=self.authority,
        )
        self.assertEqual(verified, result)
        for current, dirnames, filenames in os.walk(result.root):
            self.assertEqual(stat.S_IMODE(Path(current).stat().st_mode), 0o555)
            for name in filenames:
                self.assertEqual(stat.S_IMODE((Path(current) / name).stat().st_mode), 0o444)

    def test_freeze_is_idempotent_and_authority_changes_identity(self) -> None:
        first = self.freeze()
        second = self.freeze()
        self.assertEqual(first, second)
        marker = self.repo / "authority-marker"
        marker.write_text("second authority\n", encoding="utf-8")
        self._git("add", "authority-marker")
        self._git("commit", "-q", "-m", "advance authority")
        second_authority = self._git("rev-parse", "HEAD")
        third = self.freeze(second_authority)
        self.assertNotEqual(first.baseline_id, third.baseline_id)
        published = [path for path in self.store.iterdir() if path.is_dir()]
        self.assertEqual(len(published), 2)
        self.assertFalse(any(path.name.startswith(".pending-") for path in self.store.iterdir()))

    def test_publication_uses_pending_sibling_sync_and_atomic_rename(self) -> None:
        with mock.patch.object(
            baseline.os,
            "rename",
            wraps=baseline.os.rename,
        ) as rename_mock, mock.patch.object(
            baseline,
            "_fsync_directory",
            wraps=baseline._fsync_directory,
        ) as sync_mock:
            result = self.freeze()
        rename_mock.assert_called_once()
        pending_arg, final_arg = rename_mock.call_args.args
        self.assertEqual(Path(pending_arg).parent, self.store)
        self.assertTrue(Path(pending_arg).name.startswith(".pending-"))
        self.assertEqual(Path(final_arg), result.root)
        self.assertGreater(sync_mock.call_count, 1)

    def test_failed_freeze_removes_its_pending_directory(self) -> None:
        (self.source / "404.html").unlink()
        with self.assertRaisesRegex(baseline.BaselineValidationError, "missing required"):
            self.freeze()
        self.assertTrue(self.store.exists())
        self.assertFalse(any(path.name.startswith(".pending-") for path in self.store.iterdir()))


class BoundaryAndSourceSafetyTest(BaselineFixture):
    def test_requires_absolute_external_nonoverlapping_store(self) -> None:
        with self.assertRaisesRegex(baseline.BaselineValidationError, "absolute"):
            baseline.freeze_public_baseline(
                self.source,
                Path("relative"),
                self.authority,
                self.repo,
            )
        with self.assertRaisesRegex(baseline.BaselineValidationError, "outside"):
            baseline.freeze_public_baseline(
                self.source,
                self.repo / "baseline-store",
                self.authority,
                self.repo,
            )
        with self.assertRaisesRegex(baseline.BaselineValidationError, "must not contain"):
            baseline.freeze_public_baseline(
                self.source,
                self.base,
                self.authority,
                self.repo,
            )

        overlapping_store = self.source / "baselines"
        with self.assertRaisesRegex(baseline.BaselineValidationError, "overlap"):
            baseline.freeze_public_baseline(
                self.source,
                overlapping_store,
                self.authority,
                self.repo,
            )
        self.assertFalse(overlapping_store.exists())

    def test_rejects_symlink_roots_and_unsafe_entries_even_when_brain_owned(self) -> None:
        linked_store = self.base / "linked-store"
        target_store = self.base / "target-store"
        target_store.mkdir()
        linked_store.symlink_to(target_store, target_is_directory=True)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "symlink"):
            baseline.freeze_public_baseline(
                self.source,
                linked_store,
                self.authority,
                self.repo,
            )

        source_link = self.base / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "symlink"):
            baseline.freeze_public_baseline(
                source_link,
                self.store,
                self.authority,
                self.repo,
            )

        (self.source / "assets/brain/unsafe").symlink_to(self.source / "404.html")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "symlink"):
            self.freeze()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_rejects_nonregular_source_entries(self) -> None:
        os.mkfifo(self.source / "assets" / "unsafe.fifo")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "non-regular"):
            self.freeze()

    def test_detects_source_mutation_while_copying(self) -> None:
        source_file = self.source / "404.html"
        source_fd = os.open(source_file, os.O_RDONLY)
        before = os.fstat(source_fd)
        with source_file.open("ab") as stream:
            stream.write(b"changed")
        pending = self.base / "pending"
        pending.mkdir()
        try:
            with self.assertRaisesRegex(baseline.BaselineFreezeError, "source changed"):
                baseline._copy_open_source(source_fd, before, pending / "404.html", "404.html")
            self.assertFalse((pending / "404.html").exists())
        finally:
            os.close(source_fd)


class SourceAuthorityAttestationTest(BaselineFixture):
    def test_rejects_source_content_not_attested_by_authority_commit(self) -> None:
        self._write("404.html", b"locally replaced output\n")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            r"inventory committed at .*content mismatch 404\.html",
        ):
            self.freeze()

    def test_rejects_unattested_extra_public_asset(self) -> None:
        self._write("assets/injected.js", b"unreviewed output\n")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "unattested assets/injected.js",
        ):
            self.freeze()

    def test_verify_rejects_self_consistent_forgery_not_in_committed_attestation(self) -> None:
        result = self.freeze()
        self.thaw(result.root)
        payload = result.root / "404.html"
        forged_bytes = b"self-consistent but unattested\n"
        payload.write_bytes(forged_bytes)
        document = json.loads(result.manifest_path.read_bytes())
        for item in document["files"]:
            if item["path"] == "404.html":
                item["bytes"] = len(forged_bytes)
                item["sha256"] = hashlib.sha256(forged_bytes).hexdigest()
                break
        identity = {key: document[key] for key in ("schema", "authority", "files")}
        document["baseline_id"] = baseline._baseline_id(identity)
        self.rewrite_manifest(result.root, document)
        baseline._seal_pending_tree(result.root)
        forged_root = result.root.with_name(document["baseline_id"].removeprefix("sha256:"))
        result.root.rename(forged_root)

        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "content mismatch 404.html",
        ):
            baseline.verify_public_baseline(forged_root, self.repo)

    def test_ignores_dirty_worktree_attestation_and_reads_exact_commit_blob(self) -> None:
        self._write("404.html", b"dirty output\n")
        self._write_attestation()
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "content mismatch 404.html",
        ):
            self.freeze()

        # Once the exact inventory is committed, that new commit—not mutable
        # worktree state—becomes a distinct valid authority.
        new_authority = self._commit("review changed public output")
        result = self.freeze(new_authority)
        self.assertEqual(result.authority_git_commit, new_authority)

    def test_rejects_missing_noncanonical_or_executable_committed_attestation(self) -> None:
        self._git("rm", baseline.SOURCE_ATTESTATION_PATH)
        self._git("commit", "-q", "-m", "remove attestation")
        missing_authority = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "must contain exactly one",
        ):
            self.freeze(missing_authority)

        self.attestation_path.parent.mkdir(parents=True, exist_ok=True)
        self.attestation_path.write_text(
            json.dumps(self._source_attestation(), indent=2),
            encoding="utf-8",
        )
        noncanonical_authority = self._commit("add noncanonical attestation")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "canonical JSON"):
            self.freeze(noncanonical_authority)

        self._write_attestation()
        self.attestation_path.chmod(0o755)
        executable_authority = self._commit("make attestation executable")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "non-executable regular file",
        ):
            self.freeze(executable_authority)

    def test_rejects_nonexistent_commit_and_non_git_repository(self) -> None:
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "resolve authority Git commit",
        ):
            self.freeze("f" * 40)

        not_repo = self.base / "not-repo"
        not_repo.mkdir()
        with self.assertRaisesRegex(baseline.BaselineValidationError, "locate repository root"):
            baseline.freeze_public_baseline(
                self.source,
                self.store,
                self.authority,
                not_repo,
            )


class RequiredPayloadTest(BaselineFixture):
    def test_rejects_missing_or_empty_critical_assets(self) -> None:
        (self.source / "assets/editor.js").unlink()
        with self.assertRaisesRegex(baseline.BaselineValidationError, "editor.js"):
            self.freeze()

        self._write("assets/editor.js", b"")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "must be nonempty"):
            self.freeze()

    def test_each_index_family_requires_nonempty_payload_beyond_manifest(self) -> None:
        (self.source / "assets/suffix-index/aa.json").unlink()
        with self.assertRaisesRegex(baseline.BaselineValidationError, "suffix-index.*payload"):
            self.freeze()
        self._write("assets/suffix-index/aa.json", b"")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "contains empty"):
            self.freeze()

    def test_rejects_invalid_authority_and_reserved_source_manifest(self) -> None:
        with self.assertRaisesRegex(baseline.BaselineValidationError, "40-character"):
            self.freeze("ABC")
        self._write("manifest.json", b"source metadata")
        with self.assertRaisesRegex(baseline.BaselineValidationError, "reserved"):
            self.freeze()

    def test_rejects_dynamic_or_retired_route_shadow_files(self) -> None:
        self._write("about.html", b"stale dynamic route\n")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "route-shadowing or retired path",
        ):
            self.freeze()


class IndexManifestClosureTest(BaselineFixture):
    def test_rejects_declared_missing_and_undeclared_shards(self) -> None:
        self._write(
            "assets/decl-index/manifest.json",
            '{"shards":{"aa":1,"bb":1}}',
        )
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            r"decl-index payload.*missing assets/decl-index/bb\.json",
        ):
            self.freeze()

        self._write(
            "assets/decl-index/manifest.json",
            '{"shards":{"aa":1}}',
        )
        self._write("assets/decl-index/zz.json", b"[]\n")
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            r"decl-index payload.*unexpected assets/decl-index/zz\.json",
        ):
            self.freeze()

    def test_rejects_invalid_or_duplicate_shard_keys(self) -> None:
        self._write(
            "assets/suffix-index/manifest.json",
            '{"shards":{"../escape":1}}',
        )
        with self.assertRaisesRegex(baseline.BaselineValidationError, "invalid shard key"):
            self.freeze()

        self._write(
            "assets/suffix-index/manifest.json",
            '{"shards":{"aa":1,"aa":2}}',
        )
        with self.assertRaisesRegex(baseline.BaselineValidationError, "duplicate JSON key"):
            self.freeze()

    def test_premise_name_chunks_must_be_exact_contiguous_range(self) -> None:
        self._write(
            "assets/premise-index/manifest.json",
            '{"chunks":2,"shards":{"aa":1}}',
        )
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            r"premise-index payload.*missing assets/premise-index/names/1\.json",
        ):
            self.freeze()

        self._write(
            "assets/premise-index/manifest.json",
            '{"chunks":1,"shards":{"aa":1}}',
        )
        self._write("assets/premise-index/names/1.json", b'["extra"]\n')
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            r"premise-index payload.*unexpected assets/premise-index/names/1\.json",
        ):
            self.freeze()

    def test_rejects_impossible_premise_chunk_count_without_large_allocation(self) -> None:
        self._write(
            "assets/premise-index/manifest.json",
            f'{{"chunks":{baseline.MAX_SAFE_INTEGER},"shards":{{"aa":1}}}}',
        )
        with self.assertRaisesRegex(
            baseline.BaselineValidationError,
            "more name chunks than baseline files",
        ):
            self.freeze()


class FrozenArtifactAdversarialTest(BaselineFixture):
    def test_rejects_noncanonical_duplicate_unknown_and_traversal_manifests(self) -> None:
        result = self.freeze()
        original = json.loads(result.manifest_path.read_bytes())

        self.rewrite_manifest(result.root, original, canonical_bytes=False)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "canonical JSON"):
            baseline.verify_public_baseline(result.root, self.repo)

        self.rewrite_manifest(result.root, original)
        manifest = result.root / baseline.MANIFEST_NAME
        manifest.chmod(0o600)
        raw = canonical(original).decode().rstrip("\n}") + ',"schema":"duplicate"}\n'
        manifest.write_text(raw, encoding="utf-8")
        manifest.chmod(0o444)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "duplicate JSON key"):
            baseline.verify_public_baseline(result.root, self.repo)

        unknown = json.loads(canonical(original))
        unknown["unexpected"] = True
        self.rewrite_manifest(result.root, unknown)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "unknown unexpected"):
            baseline.verify_public_baseline(result.root, self.repo)

        traversal = json.loads(canonical(original))
        traversal["files"][0]["path"] = "../escape"
        self.rewrite_manifest(result.root, traversal)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "not normalized"):
            baseline.verify_public_baseline(result.root, self.repo)

    def test_rejects_duplicate_paths_unknown_file_fields_and_brain_paths(self) -> None:
        result = self.freeze()
        original = json.loads(result.manifest_path.read_bytes())

        duplicate = json.loads(canonical(original))
        duplicate["files"].insert(1, dict(duplicate["files"][0]))
        self.rewrite_manifest(result.root, duplicate)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "duplicate file path"):
            baseline.verify_public_baseline(result.root, self.repo)

        unknown = json.loads(canonical(original))
        unknown["files"][0]["mode"] = "0444"
        self.rewrite_manifest(result.root, unknown)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "unknown mode"):
            baseline.verify_public_baseline(result.root, self.repo)

        brain_owned = json.loads(canonical(original))
        brain_owned["files"][0]["path"] = "brain.html"
        self.rewrite_manifest(result.root, brain_owned)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "Brain-owned"):
            baseline.verify_public_baseline(result.root, self.repo)

    def test_rejects_tampered_unlisted_writable_symlink_and_nonregular_files(self) -> None:
        result = self.freeze()
        payload = result.root / "404.html"
        payload.chmod(0o644)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "permissions"):
            baseline.verify_public_baseline(result.root, self.repo)
        payload.chmod(0o444)

        result.root.chmod(0o755)
        unlisted = result.root / "unlisted.txt"
        unlisted.write_text("unexpected", encoding="utf-8")
        unlisted.chmod(0o444)
        result.root.chmod(0o555)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "unlisted"):
            baseline.verify_public_baseline(result.root, self.repo)
        result.root.chmod(0o755)
        unlisted.unlink()
        result.root.chmod(0o555)

        payload.parent.chmod(0o755)
        payload.unlink()
        payload.symlink_to(result.root / "robots.txt")
        payload.parent.chmod(0o555)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "symlink"):
            baseline.verify_public_baseline(result.root, self.repo)

    def test_rejects_unlisted_directories_and_external_hard_links(self) -> None:
        result = self.freeze()
        result.root.chmod(0o755)
        empty = result.root / "empty-unlisted"
        empty.mkdir(mode=0o555)
        result.root.chmod(0o555)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "unlisted directories"):
            baseline.verify_public_baseline(result.root, self.repo)

        result.root.chmod(0o755)
        empty.chmod(0o755)
        empty.rmdir()
        payload = result.root / "404.html"
        external_link = self.base / "external-hard-link"
        os.link(payload, external_link)
        result.root.chmod(0o555)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "hard links"):
            baseline.verify_public_baseline(result.root, self.repo)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_rejects_nonregular_frozen_file(self) -> None:
        result = self.freeze()
        payload = result.root / "404.html"
        result.root.chmod(0o755)
        payload.unlink()
        os.mkfifo(payload, mode=0o444)
        result.root.chmod(0o555)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "non-regular"):
            baseline.verify_public_baseline(result.root, self.repo)

    def test_rejects_content_tampering_wrong_root_name_and_expectations(self) -> None:
        result = self.freeze()
        payload = result.root / "404.html"
        payload.chmod(0o600)
        payload.write_bytes(b"tampered\n")
        payload.chmod(0o444)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "byte count|digest mismatch"):
            baseline.verify_public_baseline(result.root, self.repo)

        # Restore via a fresh fixture identity, then exercise name/expectation fences.
        self.thaw(result.root)
        shutil.rmtree(result.root)
        result = self.freeze()
        wrong = result.root.with_name("b" * 64)
        result.root.rename(wrong)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "does not match"):
            baseline.verify_public_baseline(wrong, self.repo)
        wrong.rename(result.root)
        with self.assertRaisesRegex(baseline.BaselineValidationError, "identity mismatch"):
            baseline.verify_public_baseline(
                result.root,
                self.repo,
                expected_baseline_id="sha256:" + "0" * 64,
            )
        with self.assertRaisesRegex(baseline.BaselineValidationError, "authority mismatch"):
            baseline.verify_public_baseline(
                result.root,
                self.repo,
                expected_authority_git_commit="b" * 40,
            )


class CommandLineTest(BaselineFixture):
    def test_freeze_and_verify_cli_emit_machine_readable_summary(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(HERE)
        attested = subprocess.run(
            [
                sys.executable,
                str(HERE / "brain_public_baseline.py"),
                "attest",
                "--source-public",
                str(self.source),
            ],
            capture_output=True,
            env=environment,
            timeout=20,
        )
        self.assertEqual(attested.returncode, 0, attested.stderr.decode(errors="replace"))
        self.assertEqual(attested.stdout, canonical(self._source_attestation()))

        frozen = subprocess.run(
            [
                sys.executable,
                str(HERE / "brain_public_baseline.py"),
                "freeze",
                "--source-public",
                str(self.source),
                "--store",
                str(self.store),
                "--repo-root",
                str(self.repo),
                "--authority-git-commit",
                self.authority,
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        self.assertEqual(frozen.returncode, 0, frozen.stderr)
        freeze_result = json.loads(frozen.stdout)
        self.assertTrue(freeze_result["ok"])
        self.assertEqual(freeze_result["schema"], baseline.BASELINE_SCHEMA)

        verified = subprocess.run(
            [
                sys.executable,
                str(HERE / "brain_public_baseline.py"),
                "verify",
                "--baseline",
                freeze_result["root"],
                "--repo-root",
                str(self.repo),
                "--expected-baseline-id",
                freeze_result["baseline_id"],
                "--expected-authority-git-commit",
                self.authority,
            ],
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["baseline_id"], freeze_result["baseline_id"])


if __name__ == "__main__":
    unittest.main()
