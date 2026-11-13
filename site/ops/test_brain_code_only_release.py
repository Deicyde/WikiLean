#!/usr/bin/env python3
"""Tests for the code-only release policy, record and Brain surface fence."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import brain_code_only_release as policy  # noqa: E402
import brain_activation_bundle as bundle  # noqa: E402
import brain_release_authority as release_authority  # noqa: E402

BRAIN_COMMIT = "a" * 40
CODE_COMMIT = "b" * 40
RELEASE_ID = "sha256:" + "1" * 64
RELEASE_MANIFEST = "2" * 64
PREVIOUS_ID = "sha256:" + "3" * 64
PREVIOUS_MANIFEST = "4" * 64
SELECTOR_SHA = "5" * 64


def selector_bytes(*, audited_at: str = "2030-01-01T00:00:00Z") -> bytes:
    return json.dumps(
        {
            "schema": "wikilean.release-selector/v2",
            "release_id": RELEASE_ID,
            "release": RELEASE_ID.removeprefix("sha256:"),
            "manifest_sha256": RELEASE_MANIFEST,
            "manifest": f"/assets/brain/releases/{RELEASE_MANIFEST}/release.json",
            "previous_release_id": PREVIOUS_ID,
            "previous_release": PREVIOUS_ID.removeprefix("sha256:"),
            "previous_manifest_sha256": PREVIOUS_MANIFEST,
            "previous_manifest": f"/assets/brain/releases/{PREVIOUS_MANIFEST}/release.json",
            "audited_at": audited_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


class PathPolicyTest(unittest.TestCase):
    def test_worker_and_asset_sources_are_eligible(self):
        for path in (
            "wiki/src/index.ts",
            "wiki/src/brain-api.ts",
            "wiki/scripts/build-public.ts",
            "wiki/wrangler.jsonc",
            "wiki/package-lock.json",
            "wiki/migrations/0021_x.sql",
            "wiki/public-asset-source-attestation.json",
        ):
            with self.subTest(path=path):
                self.assertEqual(policy.classify_path(path), "website")
                self.assertTrue(policy.is_code_only_eligible(path))
        for path in ("site/assets/style.css", "wiki/assets/editor.js"):
            with self.subTest(path=path):
                self.assertEqual(policy.classify_path(path), "asset-source")
        self.assertEqual(
            policy.classify_path("site/build_static_pages.py"), "public-asset-generator"
        )

    def test_release_neutral_paths_stay_neutral(self):
        for path in ("docs/ROADMAP.md", "site/ops/brain-canary.py", "brain/test_fold_xref.py"):
            with self.subTest(path=path):
                self.assertEqual(policy.classify_path(path), "release-neutral")
                self.assertTrue(release_authority.is_release_neutral(path))

    def test_brain_inputs_are_blocking(self):
        for path in (
            "brain/build_cells.py",
            "brain/data/nodes.jsonl",
            "catalog/data/source_registry.json",
            "site/annotations/x.json",
            "site/assets/brain/sources.json",
            "bot/state/seed_queue.json",
            "site/test_frontier_page.py",
        ):
            with self.subTest(path=path):
                self.assertEqual(policy.classify_path(path), "blocking")

    def test_the_frozen_brain_page_builder_can_never_ship_code_only(self):
        # site/out/brain.html is an artifact inside the frozen release, and the
        # promoter serves that frozen copy, so a change to its builder would be
        # reviewed as shipped and silently not ship.
        self.assertIn("site/build_brain_page.py", policy.BRAIN_INPUT_FILES)
        self.assertEqual(policy.classify_path("site/build_brain_page.py"), "blocking")
        self.assertEqual(policy.blocking_changes(["site/build_brain_page.py"]), [
            "site/build_brain_page.py"
        ])

    def test_unknown_and_malformed_paths_are_blocking(self):
        for path in (
            "newpipeline/reduce.py",
            "site/moderate.py",
            ".gitattributes",
            "wiki/public/assets/x.css",
            "",
            "/absolute",
            "../escape",
            "a//b",
            "back\\slash",
        ):
            with self.subTest(path=path):
                self.assertFalse(policy.is_code_only_eligible(path))

    def test_the_policy_never_widens_the_full_path_rules_for_reducer_inputs(self):
        for path in release_authority.AFFECTING_EXCEPTIONS:
            with self.subTest(path=path):
                if path.startswith("site/ops/"):
                    continue
                self.assertEqual(policy.classify_path(path), "blocking")

    def test_classification_buckets_and_blocking_description(self):
        classification = policy.classify_changes(
            ["docs/a.md", "wiki/src/index.ts", "brain/layout.py", "site/assets/style.css"]
        )
        self.assertEqual(classification.neutral, ("docs/a.md",))
        self.assertEqual(classification.website, ("wiki/src/index.ts",))
        self.assertEqual(classification.asset_sources, ("site/assets/style.css",))
        self.assertEqual(classification.blocking, ("brain/layout.py",))
        self.assertFalse(classification.eligible)
        message = policy.describe_blocking(BRAIN_COMMIT, CODE_COMMIT, classification.blocking)
        self.assertIn("brain/layout.py", message)
        self.assertIn("full path", message)

    def test_asset_sources_require_a_moved_attestation(self):
        with self.assertRaisesRegex(policy.CodeOnlyError, "attestation"):
            policy.require_attestation_consistency(
                policy.classify_changes(["site/assets/style.css"])
            )
        policy.require_attestation_consistency(
            policy.classify_changes(
                ["site/assets/style.css", policy.ATTESTATION_PATH]
            )
        )
        policy.require_attestation_consistency(policy.classify_changes(["wiki/src/a.ts"]))

    def test_worker_configuration_is_flagged_for_operator_review(self):
        self.assertEqual(
            policy.review_required(policy.classify_changes(["wiki/wrangler.jsonc"])),
            ["wiki/wrangler.jsonc"],
        )
        self.assertEqual(policy.review_required(policy.classify_changes(["wiki/src/a.ts"])), [])


def make_record(**overrides):
    arguments = {
        "brain_release_id": RELEASE_ID,
        "brain_manifest_sha256": RELEASE_MANIFEST,
        "brain_authority_commit": BRAIN_COMMIT,
        "brain_reducer_commit": BRAIN_COMMIT,
        "previous_release_id": PREVIOUS_ID,
        "previous_manifest_sha256": PREVIOUS_MANIFEST,
        "code_commit": CODE_COMMIT,
        "selector_sha256": SELECTOR_SHA,
        "classification": policy.classify_changes(["wiki/src/index.ts", "docs/a.md"]),
    }
    arguments.update(overrides)
    return policy.build_record(**arguments)


class RecordTest(unittest.TestCase):
    def test_round_trip(self):
        document = make_record()
        record = policy.validate_record_document(document)
        self.assertEqual(record.brain_release_id, RELEASE_ID)
        self.assertEqual(record.code_commit, CODE_COMMIT)
        self.assertEqual(record.classification.website, ("wiki/src/index.ts",))
        self.assertEqual(record.record_id, document["record_id"])
        self.assertEqual(record.summary()["policy"], policy.POLICY)

    def test_identity_covers_every_field(self):
        document = make_record()
        for key, value in (
            ("code_git_commit", "c" * 40),
            ("live_selector_sha256", "6" * 64),
        ):
            tampered = {**document, key: value}
            with self.subTest(key=key), self.assertRaisesRegex(
                policy.CodeOnlyError, "record_id does not match"
            ):
                policy.validate_record_document(tampered)

    def test_a_blocking_change_cannot_be_recorded(self):
        with self.assertRaisesRegex(policy.CodeOnlyError, "brain/layout.py"):
            make_record(classification=policy.classify_changes(["brain/layout.py"]))

    def test_a_mislabelled_change_is_rejected(self):
        document = make_record()
        changes = dict(document["changes"])
        changes["release_neutral"] = ["wiki/src/index.ts"]
        changes["website"] = []
        identity = {**document, "changes": changes}
        identity.pop("record_id")
        tampered = {**identity, "record_id": policy.record_id(identity)}
        with self.assertRaisesRegex(policy.CodeOnlyError, "classifies as 'website'"):
            policy.validate_record_document(tampered)

    def test_the_previous_release_must_differ_from_the_reused_one(self):
        with self.assertRaisesRegex(policy.CodeOnlyError, "previous release manifest"):
            make_record(previous_manifest_sha256=RELEASE_MANIFEST)

    def test_no_changes_requires_the_authority_commit(self):
        with self.assertRaisesRegex(policy.CodeOnlyError, "must list its changes"):
            make_record(classification=policy.classify_changes([]))
        unchanged = make_record(
            code_commit=BRAIN_COMMIT, classification=policy.classify_changes([])
        )
        self.assertEqual(policy.validate_record_document(unchanged).code_commit, BRAIN_COMMIT)

    def test_load_requires_an_absolute_canonical_file(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            document = make_record()
            canonical = base / "record.json"
            canonical.write_bytes(policy.canonical_json_bytes(document))
            self.assertEqual(policy.load_record(canonical).record_id, document["record_id"])

            pretty = base / "pretty.json"
            pretty.write_text(json.dumps(document, indent=2))
            with self.assertRaisesRegex(policy.CodeOnlyError, "canonical JSON"):
                policy.load_record(pretty)
            with self.assertRaisesRegex(policy.CodeOnlyError, "absolute"):
                policy.load_record(Path("record.json"))

    def test_require_record_matches_rejects_every_drift(self):
        record = policy.validate_record_document(make_record())
        good = {
            "release_id": RELEASE_ID,
            "manifest_sha256": RELEASE_MANIFEST,
            "authority_commit": BRAIN_COMMIT,
            "previous_release_id": PREVIOUS_ID,
            "previous_manifest_sha256": PREVIOUS_MANIFEST,
            "code_commit": CODE_COMMIT,
            "selector_sha256": SELECTOR_SHA,
        }
        policy.require_record_matches(record, **good)
        for key, value, pattern in (
            ("release_id", "sha256:" + "9" * 64, "different Brain release"),
            ("authority_commit", "c" * 40, "different Brain authority"),
            ("previous_release_id", None, "different retained previous"),
            ("code_commit", "c" * 40, "not the promotion commit"),
            ("selector_sha256", "9" * 64, "selector changed"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(policy.CodeOnlyError, pattern):
                policy.require_record_matches(record, **{**good, key: value})


class StagedTreeMixin(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.public = self.base / "public"
        brain = self.public / "assets" / "brain"
        (brain / "cells").mkdir(parents=True)
        (brain / "releases" / RELEASE_MANIFEST).mkdir(parents=True)
        (brain / "releases" / PREVIOUS_MANIFEST).mkdir(parents=True)
        self.bodies = {
            "assets/brain/current.json": selector_bytes(),
            f"assets/brain/releases/{RELEASE_MANIFEST}/release.json": b'{"release":"current"}',
            f"assets/brain/releases/{PREVIOUS_MANIFEST}/release.json": b'{"release":"previous"}',
            "assets/brain/sources.json": b'{"sources":[]}',
            "assets/brain/xref_index.json": b'{"xref":[]}',
            "assets/brain/cells/manifest.json": json.dumps(
                {"shards": {"a0": {"bytes": 1}, "z1": {"bytes": 1}}}, sort_keys=True
            ).encode(),
            "assets/brain/cells/aliases.json": b'{"aliases":{}}',
            "assets/brain/cells/labels.json": b'{"labels":{}}',
            "assets/brain/cells/supercells.json": b'{"supercells":[]}',
            "assets/brain/cells/explorer.json": b'{"explorer":[]}',
            "assets/brain/cells/frontier_graph.json": b'{"frontier":[]}',
            "assets/brain/cells/a0.json": b'{"cells":["a0"]}',
            "assets/brain/cells/z1.json": b'{"cells":["z1"]}',
            "brain.html": b"<!doctype html><title>Brain</title>",
        }
        for relative, body in self.bodies.items():
            target = self.public / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

    def tearDown(self):
        self.temp.cleanup()

    def fetcher(self, overrides=None):
        live = dict(self.bodies)
        live["assets/brain/current.json"] = selector_bytes(audited_at="2029-06-06T00:00:00Z")
        live.update(overrides or {})
        self.requested: list[str] = []

        def fetch(route: str) -> bytes:
            self.requested.append(route)
            relative = "brain.html" if route == "/brain" else route.lstrip("/")
            if relative not in live:
                raise policy.TransportError(f"GET {route} returned HTTP 404")
            return live[relative]

        return fetch


class FenceTest(StagedTreeMixin):
    def test_unchanged_surface_passes_and_tolerates_only_audited_at(self):
        result = policy.brain_surface_fence(
            self.public,
            current_manifest_sha256=RELEASE_MANIFEST,
            previous_manifest_sha256=PREVIOUS_MANIFEST,
            fetch=self.fetcher(),
        )
        self.assertEqual(result["schema"], "wikilean.code-only-brain-surface-fence/v1")
        self.assertEqual(result["paths"], len(self.bodies) - 1)  # only one shard is sampled
        self.assertIn("cells/a0.json", result["mutable_surface"])
        self.assertNotIn("brain.html", result["mutable_surface"])
        compared = {item["path"]: item["compared"] for item in result["checked"]}
        self.assertEqual(
            compared["assets/brain/current.json"], "identity-without-audited-at"
        )
        self.assertEqual(compared["brain.html"], "bytes")
        self.assertIn("/brain", self.requested)
        # The shard is chosen deterministically, matching the canary's choice.
        self.assertIn("assets/brain/cells/a0.json", compared)

    def test_a_selector_change_outside_audited_at_refuses(self):
        other = json.loads(selector_bytes())
        other["previous_release_id"] = "sha256:" + "7" * 64
        other["previous_release"] = "7" * 64
        with self.assertRaisesRegex(policy.CodeOnlyError, "outside\n?\\s*audited_at"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=self.fetcher(
                    {
                        "assets/brain/current.json": json.dumps(
                            other, separators=(",", ":"), sort_keys=True
                        ).encode()
                    }
                ),
            )

    def test_any_other_byte_difference_refuses(self):
        for relative in (
            "assets/brain/cells/manifest.json",
            "assets/brain/cells/a0.json",
            "assets/brain/sources.json",
            f"assets/brain/releases/{RELEASE_MANIFEST}/release.json",
            "brain.html",
        ):
            with self.subTest(relative=relative), self.assertRaisesRegex(
                policy.CodeOnlyError, "not a code-only release"
            ):
                policy.brain_surface_fence(
                    self.public,
                    current_manifest_sha256=RELEASE_MANIFEST,
                    previous_manifest_sha256=PREVIOUS_MANIFEST,
                    fetch=self.fetcher({relative: b"drifted"}),
                )

    def test_a_missing_production_path_refuses(self):
        live = dict(self.bodies)
        del live["assets/brain/cells/a0.json"]

        def fetch(route: str) -> bytes:
            relative = "brain.html" if route == "/brain" else route.lstrip("/")
            if relative not in live:
                raise policy.TransportError(f"GET {route} returned HTTP 404")
            return live[relative]

        with self.assertRaisesRegex(policy.CodeOnlyError, "could not read"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=fetch,
            )

    def test_a_missing_staged_path_refuses(self):
        (self.public / "assets" / "brain" / "sources.json").unlink()
        with self.assertRaisesRegex(policy.CodeOnlyError, r"missing=\['sources.json'\]"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=self.fetcher(),
            )

    def test_an_extra_staged_brain_file_refuses(self):
        (self.public / "assets" / "brain" / "surprise.json").write_bytes(b"{}")
        with self.assertRaisesRegex(policy.CodeOnlyError, "unexpected=\\['surprise.json'\\]"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=self.fetcher(),
            )

    def test_an_extra_immutable_namespace_refuses(self):
        extra = self.public / "assets" / "brain" / "releases" / ("8" * 64)
        extra.mkdir(parents=True)
        (extra / "release.json").write_bytes(b"{}")
        with self.assertRaisesRegex(policy.CodeOnlyError, "unexpected immutable namespace"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=self.fetcher(),
            )

    def test_a_missing_declared_shard_refuses(self):
        policy.require_brain_surface_shape(
            self.public,
            current_manifest_sha256=RELEASE_MANIFEST,
            previous_manifest_sha256=PREVIOUS_MANIFEST,
        )
        (self.public / "assets" / "brain" / "cells" / "z1.json").unlink()
        with self.assertRaisesRegex(policy.CodeOnlyError, "missing=\\['cells/z1.json'\\]"):
            policy.require_brain_surface_shape(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
            )

    def test_a_missing_frozen_page_refuses(self):
        (self.public / "brain.html").unlink()
        with self.assertRaisesRegex(policy.CodeOnlyError, "omitted the Brain-owned file"):
            policy.brain_surface_fence(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
                fetch=self.fetcher(),
            )

    def test_a_missing_retained_namespace_refuses(self):
        shutil.rmtree(self.public / "assets" / "brain" / "releases" / PREVIOUS_MANIFEST)
        with self.assertRaisesRegex(policy.CodeOnlyError, "wrong immutable release namespaces"):
            policy.require_brain_surface_shape(
                self.public,
                current_manifest_sha256=RELEASE_MANIFEST,
                previous_manifest_sha256=PREVIOUS_MANIFEST,
            )

    def test_targets_cover_the_whole_brain_surface(self):
        targets = policy.fence_targets(
            self.public,
            current_manifest_sha256=RELEASE_MANIFEST,
            previous_manifest_sha256=None,
        )
        paths = [relative for relative, _ in targets]
        self.assertNotIn(f"assets/brain/releases/{PREVIOUS_MANIFEST}/release.json", paths)
        self.assertEqual(paths[0], policy.SELECTOR_PUBLIC_PATH)
        self.assertEqual(targets[-1], ("brain.html", "/brain"))
        for relative in policy.FENCE_STATIC_PATHS:
            self.assertIn(relative, paths)


class EvaluateCommandTest(unittest.TestCase):
    """End-to-end `evaluate` against a real Git history, with no network."""

    def setUp(self):
        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("git is unavailable")
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git_run("init", "--initial-branch=main")
        self.git_run("config", "user.email", "test@example.test")
        self.git_run("config", "user.name", "Test")
        (self.repo / "brain").mkdir()
        (self.repo / "brain" / "layout.py").write_text("x = 1\n")
        (self.repo / "wiki" / "src").mkdir(parents=True)
        (self.repo / "wiki" / "src" / "index.ts").write_text("export default {};\n")
        self.git_run("add", "brain/layout.py", "wiki/src/index.ts")
        self.git_run("commit", "-m", "base")
        self.brain_commit = self.git_run("rev-parse", "HEAD")

        self.manifest = self.base / "release.json"
        self.manifest.write_bytes(
            json.dumps(
                {
                    "schema": "wikilean.release/v1",
                    "release_id": RELEASE_ID,
                    "authority": {"git_commit": self.brain_commit},
                    "reducer": {"git_commit": self.brain_commit},
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )
        self.manifest_sha256 = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.selector = self.base / "selector.json"

    def tearDown(self):
        self.temp.cleanup()

    def git_run(self, *args: str) -> str:
        return subprocess.run(
            [self.git, "-C", str(self.repo), *args],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()

    def write_selector(self, manifest_sha256: str) -> None:
        self.selector.write_bytes(
            json.dumps(
                {
                    "schema": "wikilean.release-selector/v2",
                    "release_id": RELEASE_ID,
                    "manifest_sha256": manifest_sha256,
                    "previous_release_id": PREVIOUS_ID,
                    "previous_manifest_sha256": PREVIOUS_MANIFEST,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        )

    def evaluate(self):
        return policy.main(
            [
                "evaluate",
                "--repo-root",
                str(self.repo),
                "--git",
                str(self.git),
                "--release-manifest",
                str(self.manifest),
                "--selector-file",
                str(self.selector),
            ]
        )

    def test_a_website_only_commit_is_eligible(self):
        (self.repo / "wiki" / "src" / "index.ts").write_text("export default {a: 1};\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "note.md").write_text("note\n")
        self.git_run("add", "wiki/src/index.ts", "docs/note.md")
        self.git_run("commit", "-m", "website")
        code_commit = self.git_run("rev-parse", "HEAD")
        self.write_selector(self.manifest_sha256)

        buffer = io.BytesIO()
        saved = sys.stdout
        sys.stdout = io.TextIOWrapper(buffer, encoding="utf-8", write_through=True)
        try:
            self.assertEqual(self.evaluate(), 0)
        finally:
            sys.stdout.flush()
            sys.stdout.detach()
            sys.stdout = saved
        raw = buffer.getvalue()
        document = json.loads(raw)
        self.assertEqual(policy.canonical_json_bytes(document), raw)
        record = policy.validate_record_document(document)
        self.assertEqual(record.code_commit, code_commit)
        self.assertEqual(record.brain_authority_commit, self.brain_commit)
        self.assertEqual(record.classification.website, ("wiki/src/index.ts",))
        self.assertEqual(record.classification.neutral, ("docs/note.md",))

    def refuse(self) -> int:
        with contextlib.redirect_stderr(io.StringIO()):
            return self.evaluate()

    def test_a_reducer_change_is_refused(self):
        (self.repo / "brain" / "layout.py").write_text("x = 2\n")
        self.git_run("add", "brain/layout.py")
        self.git_run("commit", "-m", "reducer")
        self.write_selector(self.manifest_sha256)
        self.assertEqual(self.refuse(), 1)

    def test_a_release_production_does_not_serve_is_refused(self):
        (self.repo / "wiki" / "src" / "index.ts").write_text("export default {a: 2};\n")
        self.git_run("add", "wiki/src/index.ts")
        self.git_run("commit", "-m", "website")
        self.write_selector("9" * 64)
        self.assertEqual(self.refuse(), 1)


class BundleIntegrationTest(unittest.TestCase):
    def test_the_code_only_profile_adds_one_evidence_file_before_ci(self):
        classic = bundle.evidence_paths(False)
        code_only = bundle.evidence_paths(True)
        self.assertEqual(classic, bundle.EVIDENCE_PATHS)
        self.assertEqual(len(code_only), len(classic) + 1)
        self.assertEqual(code_only[-1], ("ci_evidence", "ci-evidence.json"))
        self.assertEqual(code_only[-2], bundle.CODE_ONLY_EVIDENCE)
        self.assertEqual(
            bundle.external_evidence_paths(True),
            tuple(item for item in code_only if item[0] != "ci_evidence"),
        )

    def intent_block(self, record, **overrides):
        block = {
            "policy": policy.POLICY,
            "record_id": record.record_id,
            "code_git_commit": record.code_commit,
            "brain_authority_commit": record.brain_authority_commit,
            "reused_release_id": record.brain_release_id,
            "reused_manifest_sha256": record.brain_manifest_sha256,
            "review_required": list(record.review_required),
            "brain_surface_fence": {
                "schema": "wikilean.code-only-brain-surface-fence/v1",
                "paths": 2,
                "mutable_surface": ["current.json"],
                "response_bytes": 20,
                "checked": [
                    {
                        "path": policy.SELECTOR_PUBLIC_PATH,
                        "route": "/" + policy.SELECTOR_PUBLIC_PATH,
                        "compared": "identity-without-audited-at",
                        "bytes": 10,
                        "sha256": "0" * 64,
                    },
                    {
                        "path": policy.BRAIN_PAGE_PUBLIC_PATH,
                        "route": policy.BRAIN_PAGE_ROUTE,
                        "compared": "bytes",
                        "bytes": 10,
                        "sha256": "1" * 64,
                    },
                ],
            },
        }
        block.update(overrides)
        return block

    def test_the_bundle_requires_a_faithful_code_only_intent_block(self):
        record = policy.validate_record_document(make_record())
        bundle._validate_intent_code_only(self.intent_block(record), record)

        with self.assertRaisesRegex(bundle.BundleValidationError, "differs from the reviewed"):
            bundle._validate_intent_code_only(
                self.intent_block(record, record_id="sha256:" + "8" * 64), record
            )

    def test_the_bundle_requires_the_fence_to_have_checked_the_surface(self):
        record = policy.validate_record_document(make_record())
        block = self.intent_block(record)
        block["brain_surface_fence"] = {
            **block["brain_surface_fence"],
            "paths": 1,
            "checked": block["brain_surface_fence"]["checked"][:1],
        }
        with self.assertRaisesRegex(bundle.BundleValidationError, "frozen /brain page"):
            bundle._validate_intent_code_only(block, record)

        block = self.intent_block(record)
        block["brain_surface_fence"]["checked"][1]["compared"] = "identity-without-audited-at"
        with self.assertRaisesRegex(bundle.BundleValidationError, "without requiring equal bytes"):
            bundle._validate_intent_code_only(block, record)

        block = self.intent_block(record)
        block["brain_surface_fence"]["checked"][0]["compared"] = "bytes"
        with self.assertRaisesRegex(bundle.BundleValidationError, "did not compare the release selector"):
            bundle._validate_intent_code_only(block, record)


if __name__ == "__main__":
    unittest.main()
