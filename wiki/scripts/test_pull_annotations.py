#!/usr/bin/env python3
"""Hermetic tests for sealed-bundle annotation mirroring."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "brain", ROOT / "brain" / "tools", ROOT / "wiki" / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pull_annotations as mirror  # noqa: E402
import test_harvest as harvest_fixture  # noqa: E402
from d1_snapshot_bundle import SnapshotBundle  # noqa: E402

AUDIT_TIME = "2026-09-05T00:00:00Z"
RECEIPT_ID = "sha256:" + "1" * 64
LINEAGE_ID = "sha256:" + "2" * 64


def article(
    slug: str = "Abelian_group",
    *,
    annotations: list[dict] | None = None,
    version: int = 7,
) -> dict:
    return {
        "slug": slug,
        "wikipedia_title": slug.replace("_", " "),
        "display_title": slug.replace("_", " "),
        "wikidata_qid": "Q181296",
        "revid": 123,
        "latest_revid": 124,
        "last_upstream_check": 1_700_000_000_000,
        "annotations": annotations if annotations is not None else [],
        "schema_version": 3,
        "version": version,
        "n_formalized": 0,
        "n_partial": 0,
        "n_not_formalized": 0,
        "created_at": 1_600_000_000_000,
        "updated_at": 1_700_000_000_000,
    }


def fake_bundle(*rows: dict) -> SnapshotBundle:
    return SnapshotBundle(
        path=Path("/sealed") / LINEAGE_ID.removeprefix("sha256:"),
        acquisition_receipt_id=RECEIPT_ID,
        normalization_lineage_id=LINEAGE_ID,
        acquired_at=AUDIT_TIME,
        articles=tuple(rows),
        edges=(),
        nodes=(),
    )


def write_json(path: Path, value: object) -> bytes:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(data)
    return data


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class AnnotationMirrorTests(unittest.TestCase):
    def make_cache(self, root: Path) -> Path:
        cache = root / "annotations"
        cache.mkdir()
        write_json(
            cache / "Abelian_group.json",
            {
                "slug": "Abelian_group",
                "wikipedia_title": "Old title",
                "display_title": "Old title",
                "schema_version": 2,
                "annotation_style": "theorem_article",
                "annotations": [{"id": "old", "provenance": "ai"}],
            },
        )
        write_json(cache / "Disk_only.json", {"slug": "Disk_only", "annotations": []})
        nested = cache / "_smoke"
        nested.mkdir()
        (nested / "keep.txt").write_text("preserve\n", encoding="utf-8")
        write_json(cache / mirror.MANIFEST_NAME, {"Abelian_group": {"version": 1}})
        return cache

    def test_valid_bundle_atomically_updates_cache_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            sealed = fake_bundle(
                article(annotations=[{"id": "human", "provenance": "human"}]),
                article("New_article", annotations=[{"id": "new", "provenance": "ai"}]),
            )
            old_root = cache.stat()
            with mock.patch.object(mirror, "verify_snapshot_bundle", return_value=sealed):
                result = mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)

            self.assertEqual((result.created, result.updated, result.unchanged), (1, 1, 0))
            self.assertEqual(result.disk_only, ("Disk_only",))
            self.assertFalse(result.dry_run)
            self.assertIsNone(result.cleanup_warning)
            self.assertFalse(os.path.samestat(old_root, cache.stat()))
            updated = json.loads((cache / "Abelian_group.json").read_text())
            self.assertEqual(updated["annotations"], [{"id": "human", "provenance": "human"}])
            self.assertEqual(updated["wikipedia_title"], "Abelian group")
            self.assertEqual(updated["schema_version"], 3)
            self.assertEqual(updated["annotation_style"], "theorem_article")
            self.assertFalse((cache / "Disk_only.json").exists())
            quarantined = (
                cache
                / mirror.QUARANTINE_DIR
                / LINEAGE_ID.removeprefix("sha256:")
                / "Disk_only.json"
            )
            self.assertTrue(quarantined.is_file())
            self.assertEqual((cache / "_smoke" / "keep.txt").read_text(), "preserve\n")

            manifest = json.loads((cache / mirror.MANIFEST_NAME).read_text())
            self.assertEqual(manifest["_meta"]["schema"], mirror.MANIFEST_SCHEMA)
            self.assertEqual(manifest["_meta"]["normalization_lineage_id"], LINEAGE_ID)
            self.assertEqual(manifest["_meta"]["acquisition_receipt_id"], RECEIPT_ID)
            self.assertEqual(manifest["_meta"]["acquired_at"], AUDIT_TIME)
            self.assertEqual(manifest["_meta"]["quarantined_count"], 1)
            self.assertEqual(manifest["_meta"]["quarantine_path"], mirror.QUARANTINE_DIR)
            sidecar = (cache / "Abelian_group.json").read_bytes()
            self.assertEqual(manifest["Abelian_group"]["pulled_at"], AUDIT_TIME)
            self.assertEqual(
                manifest["Abelian_group"]["sha256"], hashlib.sha256(sidecar).hexdigest()
            )
            self.assertEqual(manifest["Abelian_group"]["bytes"], len(sidecar))

    def test_repeat_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            sealed = fake_bundle(article(annotations=[{"id": "same"}]))
            with mock.patch.object(mirror, "verify_snapshot_bundle", return_value=sealed):
                mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
                first = tree_bytes(cache)
                result = mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
                second = tree_bytes(cache)
            self.assertEqual(first, second)
            self.assertEqual((result.created, result.updated, result.unchanged), (0, 0, 1))

    def test_decimal_annotations_are_exact_strict_json(self) -> None:
        precise = Decimal("0.123456789012345678901234567890123456789")
        tiny = Decimal("1.2300e-40")
        row = article(
            annotations=[
                {
                    "precise": precise,
                    "tiny": tiny,
                    "nested": [True, None, "decimal"],
                }
            ]
        )

        data = mirror._sidecar_bytes(row, None)
        text = data.decode("utf-8")
        precise_token = harvest_fixture.contracts.canonical_artifact_json_bytes(
            precise
        ).decode("ascii")
        tiny_token = harvest_fixture.contracts.canonical_artifact_json_bytes(tiny).decode(
            "ascii"
        )
        self.assertIn(f'"precise": {precise_token}', text)
        self.assertIn(f'"tiny": {tiny_token}', text)
        self.assertNotIn(f'"{precise_token}"', text)
        json.loads(data)
        parsed = harvest_fixture.contracts.parse_artifact_json_bytes(
            data, location="decimal sidecar"
        )
        self.assertEqual(parsed["annotations"][0]["precise"], precise)
        self.assertEqual(parsed["annotations"][0]["tiny"], tiny)

        row["annotations"] = [{"invalid": Decimal("NaN")}]
        with self.assertRaisesRegex(mirror.AnnotationMirrorError, "non-finite"):
            mirror._sidecar_bytes(row, None)

    def test_nested_existing_quarantine_count_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            old_quarantine = cache / mirror.QUARANTINE_DIR / ("a" * 64)
            old_quarantine.mkdir(parents=True)
            write_json(old_quarantine / "Older.json", {"annotations": []})
            sealed = fake_bundle(article())
            with mock.patch.object(mirror, "verify_snapshot_bundle", return_value=sealed):
                mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
                first = json.loads((cache / mirror.MANIFEST_NAME).read_text())
                mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
                second = json.loads((cache / mirror.MANIFEST_NAME).read_text())
            self.assertEqual(first["_meta"]["quarantined_count"], 2)
            self.assertEqual(second["_meta"]["quarantined_count"], 2)
            self.assertTrue((cache / mirror.QUARANTINE_DIR / ("a" * 64) / "Older.json").is_file())

    def test_dry_run_never_changes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            before = tree_bytes(cache)
            with mock.patch.object(
                mirror,
                "verify_snapshot_bundle",
                return_value=fake_bundle(article(annotations=[{"changed": True}])),
            ):
                result = mirror.pull(
                    Path("/absolute/bundle"), annotations_dir=cache, dry_run=True
                )
            self.assertTrue(result.dry_run)
            self.assertEqual(tree_bytes(cache), before)

    def test_relative_paths_fail_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            with mock.patch.object(mirror, "verify_snapshot_bundle") as verifier:
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "must be absolute"):
                    mirror.pull(Path("relative"), annotations_dir=cache)
                verifier.assert_not_called()
            with self.assertRaisesRegex(
                mirror.AnnotationMirrorError, "cache path must be absolute"
            ):
                mirror.pull(Path("/absolute"), annotations_dir=Path("relative"))

    def test_invalid_existing_sidecar_preserves_complete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            (cache / "Abelian_group.json").write_text("{broken", encoding="utf-8")
            before = tree_bytes(cache)
            with mock.patch.object(
                mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
            ):
                with self.assertRaises(mirror.AnnotationMirrorError):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)

    def test_staging_or_exchange_failure_preserves_complete_cache(self) -> None:
        for target, message in (
            ("_write_staged_files", "stage failed"),
            ("_exchange_directories", "exchange failed"),
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cache = self.make_cache(root)
                before = tree_bytes(cache)
                with (
                    mock.patch.object(
                        mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
                    ),
                    mock.patch.object(mirror, target, side_effect=OSError(message)),
                ):
                    with self.assertRaisesRegex(OSError, message):
                        mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
                self.assertEqual(tree_bytes(cache), before)
                self.assertFalse(list(root.glob(".annotations.pull.*.tmp")))

    def test_post_exchange_fsync_failure_rolls_back_complete_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            before = tree_bytes(cache)
            real_exchange = mirror._exchange_directories
            real_fsync = mirror.stage_io.fsync_directory
            state = {"exchanged": False, "raised": False}

            def exchange(left: Path, right: Path) -> None:
                real_exchange(left, right)
                state["exchanged"] = not state["exchanged"]

            def fsync(path: Path) -> None:
                if path == root and state["exchanged"] and not state["raised"]:
                    state["raised"] = True
                    raise OSError("parent fsync failed")
                real_fsync(path)

            with (
                mock.patch.object(
                    mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
                ),
                mock.patch.object(mirror, "_exchange_directories", side_effect=exchange),
                mock.patch.object(mirror.stage_io, "fsync_directory", side_effect=fsync),
            ):
                with self.assertRaisesRegex(OSError, "parent fsync failed"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)
            self.assertFalse(list(root.glob(".annotations.pull.*.tmp")))

    def test_same_size_source_mutation_with_restored_mtime_aborts_before_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            cache_root = cache.stat()
            old_article = (cache / "Abelian_group.json").read_bytes()
            old_manifest = (cache / mirror.MANIFEST_NAME).read_bytes()
            victim = cache / "Disk_only.json"
            victim_stat = victim.stat()
            real_clone = mirror._clone_tree

            def mutate_after_clone(
                source: Path,
                destination: Path,
                snapshot: dict[str, mirror.TreeEntry],
            ) -> None:
                real_clone(source, destination, snapshot)
                data = victim.read_bytes()
                victim.write_bytes(b"X" * len(data))
                os.utime(
                    victim,
                    ns=(victim_stat.st_atime_ns, victim_stat.st_mtime_ns),
                )

            with (
                mock.patch.object(
                    mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
                ),
                mock.patch.object(mirror, "_clone_tree", side_effect=mutate_after_clone),
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "changed"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertTrue(os.path.samestat(cache_root, cache.stat()))
            self.assertEqual((cache / "Abelian_group.json").read_bytes(), old_article)
            self.assertEqual((cache / mirror.MANIFEST_NAME).read_bytes(), old_manifest)
            self.assertFalse(list(root.glob(".annotations.pull.*.tmp")))

    def test_last_moment_source_mutation_is_detected_and_exchange_is_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            cache_root = cache.stat()
            old_article = (cache / "Abelian_group.json").read_bytes()
            old_manifest = (cache / mirror.MANIFEST_NAME).read_bytes()
            victim = cache / "Disk_only.json"
            real_exchange = mirror._exchange_directories
            calls = 0

            def mutate_then_exchange(left: Path, right: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    data = victim.read_bytes()
                    victim.write_bytes(b"Y" * len(data))
                real_exchange(left, right)

            with (
                mock.patch.object(
                    mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
                ),
                mock.patch.object(
                    mirror, "_exchange_directories", side_effect=mutate_then_exchange
                ),
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "atomic exchange"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(calls, 2, "the second exchange must restore the old root")
            self.assertTrue(os.path.samestat(cache_root, cache.stat()))
            self.assertEqual((cache / "Abelian_group.json").read_bytes(), old_article)
            self.assertEqual((cache / mirror.MANIFEST_NAME).read_bytes(), old_manifest)
            self.assertFalse(list(root.glob(".annotations.pull.*.tmp")))

    def test_replaced_lock_inode_is_rejected_after_lock_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            before = tree_bytes(cache)
            lock_path = root / ".annotations.pull.lock"

            def replace_lock(_descriptor: int, _operation: int) -> None:
                lock_path.unlink()
                lock_path.write_text("replacement", encoding="utf-8")

            with (
                mock.patch.object(
                    mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
                ),
                mock.patch.object(mirror.fcntl, "flock", side_effect=replace_lock),
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "replaced"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)
            self.assertFalse(list(root.glob(".annotations.pull.*.tmp")))

    def test_symlink_and_casefold_collision_are_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = self.make_cache(root)
            (cache / "link.json").symlink_to(cache / "Disk_only.json")
            before_target = (cache / "Disk_only.json").read_bytes()
            with mock.patch.object(
                mirror, "verify_snapshot_bundle", return_value=fake_bundle(article())
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "symlink"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual((cache / "Disk_only.json").read_bytes(), before_target)

        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            sealed = fake_bundle(article("Café"), article("Cafe\N{COMBINING ACUTE ACCENT}"))
            before = tree_bytes(cache)
            with mock.patch.object(mirror, "verify_snapshot_bundle", return_value=sealed):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "collide"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)

    def test_actual_bundle_verifier_is_used_and_tampering_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_root = root / "bundles"
            bundle_root.mkdir()
            bundle = harvest_fixture.publish_bundle(bundle_root, [], [])
            cache = self.make_cache(root)
            result = mirror.pull(bundle, annotations_dir=cache)
            self.assertEqual(result.bundle.acquired_at, harvest_fixture.AUDIT_TIME)
            self.assertEqual(len(result.bundle.articles), 1)

            before = tree_bytes(cache)
            article_path = bundle / "normalized" / "articles.jsonl"
            article_path.write_bytes(article_path.read_bytes() + b"\n")
            with self.assertRaises(mirror.SnapshotBundleError):
                mirror.pull(bundle, annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)

    def test_producer_verifier_mirror_preserves_decimal_annotations(self) -> None:
        precise = Decimal("0.123456789012345678901234567890123456789")
        tiny = Decimal("1.2300e-40")
        raw_article = harvest_fixture.article()
        payload = json.loads(raw_article["payload"])
        payload["annotations"] = (
            '[{"precise":0.123456789012345678901234567890123456789,'
            '"tiny":1.2300e-40}]'
        )
        raw_article["payload"] = harvest_fixture._payload(payload)
        rows = [raw_article, harvest_fixture.control(articles=1, edges=0, nodes=0)]
        canonical_rows = harvest_fixture.acquisition.parse_wrangler_output(
            json.dumps([{"results": rows, "success": True}])
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle_root = root / "bundles"
            bundle_root.mkdir()
            bundle_id, files = harvest_fixture.acquisition._bundle_files(
                canonical_rows,
                acquisition_tool=harvest_fixture.FAKE_TOOL,
                acquisition_toolchain=harvest_fixture.FAKE_TOOLCHAIN,
                audit_time=harvest_fixture.AUDIT_TIME,
            )
            bundle = bundle_root / bundle_id.removeprefix("sha256:")
            bundle.mkdir(mode=0o700)
            (bundle / "normalized").mkdir(mode=0o700)
            for relative, data in files.items():
                path = bundle / relative
                path.write_bytes(data)
                path.chmod(0o644)

            verified = mirror.verify_snapshot_bundle(bundle)
            verified_annotation = verified.articles[0]["annotations"][0]
            self.assertIsInstance(verified_annotation["precise"], Decimal)
            self.assertEqual(verified_annotation["precise"], precise)
            self.assertEqual(verified_annotation["tiny"], tiny)

            cache = root / "annotations"
            cache.mkdir()
            mirror.pull(bundle, annotations_dir=cache)
            sidecar = (cache / "Abelian_group.json").read_bytes()
            parsed = harvest_fixture.contracts.parse_artifact_json_bytes(
                sidecar, location="mirrored decimal sidecar"
            )
            self.assertEqual(parsed["annotations"][0]["precise"], precise)
            self.assertEqual(parsed["annotations"][0]["tiny"], tiny)

    def test_zero_article_snapshot_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            with mock.patch.object(
                mirror, "verify_snapshot_bundle", return_value=fake_bundle()
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "zero articles"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)

    def test_manifest_meta_slug_is_reserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = self.make_cache(Path(temporary))
            before = tree_bytes(cache)
            with mock.patch.object(
                mirror,
                "verify_snapshot_bundle",
                return_value=fake_bundle(article("_meta")),
            ):
                with self.assertRaisesRegex(mirror.AnnotationMirrorError, "reserved"):
                    mirror.pull(Path("/absolute/bundle"), annotations_dir=cache)
            self.assertEqual(tree_bytes(cache), before)

    def test_mirror_slug_policy_matches_portable_filename_boundary(self) -> None:
        cases = (
            ("Abelian_group", True),
            ("Café_群", True),
            ("a" * 250, True),
            (".", False),
            ("a..b", False),
            ("a/b", False),
            ("a\\b", False),
            ("_meta", False),
            ("_META", False),
            ("Draft.agent1", False),
            ("Draft.AGENT1", False),
            ("bad\tcontrol", False),
            ("bad\x7fcontrol", False),
            ("a" * 251, False),
        )
        for slug, valid in cases:
            with self.subTest(slug=slug, valid=valid):
                if valid:
                    self.assertEqual(mirror._slug_filename(slug), f"{slug}.json")
                else:
                    with self.assertRaises(mirror.AnnotationMirrorError):
                        mirror._slug_filename(slug)

        for left, right in (
            ("Abelian_group", "abelian_group"),
            ("Café", "Cafe\u0301"),
        ):
            self.assertEqual(
                mirror._filename_key(mirror._slug_filename(left)),
                mirror._filename_key(mirror._slug_filename(right)),
            )

    def test_npm_wrapper_requires_and_selects_python_312(self) -> None:
        wrapper = ROOT / "wiki" / "scripts" / "pull-annotations.sh"
        environment = {**os.environ, "WIKILEAN_PYTHON": sys.executable}
        valid = subprocess.run(
            ["bash", str(wrapper), "--help"],
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("--snapshot-bundle", valid.stdout)

        invalid = subprocess.run(
            ["bash", str(wrapper), "--help"],
            text=True,
            capture_output=True,
            env={**os.environ, "WIKILEAN_PYTHON": "/bin/false"},
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("Python 3.12 is required", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
