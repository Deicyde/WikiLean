#!/usr/bin/env python3
"""Tests for claims mode: ``run_package.py --claims`` and ``claims.py``.

The claims API is a stand-in (:class:`FakeApi`) with the Worker's answers and refusal
codes (wiki/src/claims.ts), patched in where ``claims.py`` reaches
``contrib_transport``; the package is a real envelope of a real plan
(``publish_package.publish_envelope``), and its gate code is held to a commit of a
small git repository standing in for the contributor's WikiLean checkout. No network.

Run: python3 scripts/pipeline/contribute/test_claims.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parent
for _path in (str(HERE), str(PIPELINE), str(PIPELINE / "gate")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import claims as claims_mod  # noqa: E402
import package as package_mod  # noqa: E402
import publish_package  # noqa: E402
import run_package  # noqa: E402
import stage as stage_mod  # noqa: E402
import work as work_mod  # noqa: E402
import workdir as workdir_mod  # noqa: E402
import test_contribute as contribute_tests  # noqa: E402 — imported whole, so its tests are not run twice

GIT_IDENTITY, quiet, sha = (contribute_tests.GIT_IDENTITY, contribute_tests.quiet,
                            contribute_tests.sha)

TOKEN = "wl_agent_test_token"
CLAIM_ID = "clm-" + "1" * 32
SUBMISSION_ID = "sub-" + "2" * 64


class Refusal(ValueError):
    """contrib_transport.Refusal's shape: the service's code beside its words."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


class FakeApi:
    """The claims API of one package, answering as wiki/src/claims.ts does."""

    BASE_URL = "https://wikilean.example"
    Refusal = Refusal

    def __init__(self, envelope: bytes, manifest: dict, bundle_dir: Path):
        self.envelope, self.manifest, self.bundle_dir = envelope, manifest, bundle_dir
        self.bundle_sha = sha((bundle_dir / "MANIFEST.json").read_bytes())
        self.calls: list[tuple[str, dict | None]] = []
        self.downloads: list[str] = []
        self.claim: dict | None = None
        self.links: list[str] = []
        self.lease = 1_000_000
        self.renew_refusals: list[str | None] = []   # one per renew, None to answer
        self.claim_states: list[str] = []            # what each POST /api/claims answers
        self.free = True

    def origin(self, base: str) -> str:
        return base.rstrip("/")

    def describe(self, state: str = "live", reason: str | None = None) -> dict:
        return {"id": CLAIM_ID, "run_id": self.manifest["run_id"], "agent_id": "agt-1",
                "state": state, "claimed_at": 1, "renewed_at": self.lease,
                "lease_until": self.lease, "expires_at": 7 * 86_400_000,
                "ended_at": None if state == "live" else self.lease, "end_reason": reason,
                "package": {"run_id": self.manifest["run_id"],
                            "envelope_sha256": sha(self.envelope),
                            "bytes": len(self.envelope), "bundle_sha256": self.bundle_sha,
                            "campaign": "run:" + self.manifest["run_id"],
                            "index_sha256": "c" * 64},
                "package_url": f"/api/claims/{CLAIM_ID}/package",
                "bundle_url": f"/api/index/bundles/{self.bundle_sha}"}

    def call(self, base, path, token, payload=None):
        assert token == TOKEN, "the token travels to every request, and only there"
        self.calls.append((path, payload))
        if path == "/api/packages":
            return {"ok": True, "packages": [{
                "run_id": self.manifest["run_id"], "available": self.claim is None,
                "mode": "locate", "tasks": len(self.manifest["tasks"]), "sources": 1,
                "bytes": len(self.envelope), "max_cost": 20.0}]}
        if path == "/api/claims":
            if not self.free:
                return {"ok": True, "claim": None}
            self.claim = self.describe(self.claim_states.pop(0) if self.claim_states
                                       else "live")
            return {"ok": True, "claim": self.claim}
        if path == f"/api/claims/{CLAIM_ID}/renew":
            code = self.renew_refusals.pop(0) if self.renew_refusals else None
            if code:
                raise Refusal(409, code, "the claim has ended")
            self.lease += 1000
            self.claim = self.describe()
            return {"ok": True, "claim": self.claim}
        if path == f"/api/claims/{CLAIM_ID}/submissions":
            self.links.append(payload["submission_id"])
            return {"ok": True, "link": {"submission_id": payload["submission_id"]}}
        if path == f"/api/claims/{CLAIM_ID}/release":
            if payload["reason"] == "done" and not self.links:
                raise Refusal(409, "no_submission", "a claim is done once an intake receipt "
                              "is linked to it")
            self.claim = self.describe("completed" if payload["reason"] == "done"
                                       else "released", payload["reason"])
            return {"ok": True, "claim": self.claim}
        raise Refusal(404, "not_found", path)

    def download(self, base, path, token, dest, *, size, sha256):
        assert token == TOKEN and path == f"/api/claims/{CLAIM_ID}/package"
        assert (size, sha256) == (len(self.envelope), sha(self.envelope))
        self.downloads.append(path)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.envelope)

    def fetch_bundle(self, base, token, sha256, cache, *, skip=(), execute=False, log=print):
        assert token == TOKEN and sha256 == self.bundle_sha
        assert skip == ("names/",)
        root = Path(cache) / sha256
        if execute:
            shutil.copytree(self.bundle_dir, root, dirs_exist_ok=True)
        files = [p for p in self.bundle_dir.rglob("*") if p.is_file()]
        return {"root": str(root), "files": len(files),
                "bytes": sum(p.stat().st_size for p in files),
                "missing": 0 if execute else len(files), "use_restricted": []}


class ClaimsCase(contribute_tests.ShippedCase):
    CREATED = contribute_tests.PublishApiTests.CREATED
    bundle = contribute_tests.PublishApiTests.bundle

    def setUp(self):
        super().setUp()
        self.checkout = self.gate_checkout()
        self.commit = subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "HEAD"],
                                     check=True, capture_output=True,
                                     text=True).stdout.strip()
        plan_root, workspace, sources, _raw = self.shipped_plan()
        self.workspace = workspace
        bundle_dir = self.bundle(workspace, "loc-7")
        search = bundle_dir / "search"
        search.mkdir()
        listing = json.loads((bundle_dir / "MANIFEST.json").read_text(encoding="utf-8"))
        for name in workdir_mod.SEARCH_FILES:
            data = f"{name}\n".encode("utf-8")
            (search / name).write_bytes(data)
            listing["files"][f"search/{name}"] = {"sha256": sha(data), "bytes": len(data)}
        (bundle_dir / "MANIFEST.json").write_text(json.dumps(listing, indent=1, sort_keys=True)
                                                  + "\n", encoding="utf-8")
        out = self.tmp / "out" / "loc-7.json"
        manifest = quiet(publish_package.publish_envelope, run_id="loc-7", plan=plan_root,
                         sources=sources, universe=workspace.universe, out=out,
                         sources_dir=workspace.sources,
                         candidates_dir=workspace.candidates,
                         inventory_dir=workspace.corpus / self.inventory_run,
                         bundle_dir=bundle_dir, producer_commit=self.commit,
                         created=self.CREATED, max_cost=20.0)
        self.api = FakeApi(out.read_bytes(), manifest, bundle_dir)
        self.workdir = self.tmp / "claims-w"
        for patcher in (patch.object(claims_mod, "transport", lambda: self.api),
                        patch.object(claims_mod, "CHECKOUT", self.checkout)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ[claims_mod.TOKEN_ENV] = TOKEN

    def gate_checkout(self) -> Path:
        """A git repository holding today's gate files at their paths: a public commit."""
        checkout = self.tmp / "wikilean"
        for source, _name in stage_mod.GATE_FILES:
            target = checkout / source.relative_to(stage_mod.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        env = {**os.environ, **GIT_IDENTITY}
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(checkout), "commit", "-q", "-m", "gate"], check=True,
                       capture_output=True, env=env)
        # the commit is on WikiLean's main, as a fetched clone's would be
        subprocess.run(["git", "-C", str(checkout), "update-ref", "refs/remotes/origin/main",
                        "HEAD"], check=True, capture_output=True)
        return checkout

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        os.environ[claims_mod.TOKEN_ENV] = TOKEN
        with redirect_stdout(out), redirect_stderr(err):
            code = run_package.main(["--claims", "--workdir", str(self.workdir),
                                     "--index-cache", str(self.tmp / "cache"), *argv])
        return code, out.getvalue(), err.getvalue()

    def posted(self, path: str) -> int:
        return sum(1 for called, _payload in self.api.calls if called == path)


class ClaimTests(ClaimsCase):
    def test_a_dry_run_lists_and_claims_nothing(self):
        code, out, _err = self.main("--stage", "claim")
        self.assertEqual(code, 0, out)
        self.assertIn("would claim it", out)
        self.assertEqual([path for path, _ in self.api.calls], ["/api/packages"])
        self.assertFalse(self.workdir.exists() and any(self.workdir.iterdir()))

    def test_the_default_dry_run_stops_after_the_claim_it_did_not_make(self):
        code, out, _err = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("claim it with --execute", out)
        self.assertEqual(self.api.downloads, [])

    def test_a_claim_records_its_key_first_and_unpacks_the_package(self):
        code, out, err = self.main("--stage", "claim", "--execute")
        self.assertEqual(code, 0, err)
        request = json.loads((self.workdir / claims_mod.REQUEST_JSON).read_text())
        (path, payload), = [c for c in self.api.calls if c[0] == "/api/claims"]
        self.assertEqual(payload, {"request_key": request["request_key"]})
        record = claims_mod.held(self.workdir)
        self.assertEqual((record["claim_id"], record["run_id"], record["state"]),
                         (CLAIM_ID, "loc-7", "live"))
        self.assertEqual(record["request_key"], request["request_key"])
        self.assertNotIn(TOKEN, (self.workdir / claims_mod.CLAIM_JSON).read_text())
        target = package_mod.package_dir(self.workdir, "loc-7")
        self.assertEqual(package_mod.read(target)["producer_commit"], self.commit)
        # the token was taken out of the environment, so no child process inherits it
        self.assertNotIn(claims_mod.TOKEN_ENV, os.environ)

    def test_a_second_claim_carries_on_with_the_one_held(self):
        self.main("--stage", "claim", "--execute")
        code, out, err = self.main("--stage", "claim", "--execute")
        self.assertEqual(code, 0, err)
        self.assertIn("held by this workdir", out)
        self.assertEqual(self.posted("/api/claims"), 1)
        self.assertEqual(self.posted(f"/api/claims/{CLAIM_ID}/renew"), 1)
        self.assertEqual(len(self.api.downloads), 1)

    def test_nothing_free_is_not_a_failure(self):
        self.api.claim = self.api.describe()   # another agent holds it
        code, out, _err = self.main("--stage", "claim")
        self.assertEqual(code, 0)
        self.assertIn("no package is free", out)

    def test_a_lost_answer_reuses_the_request_key(self):
        first = claims_mod.request_key(self.workdir, None)
        self.assertEqual(claims_mod.request_key(self.workdir, None), first)
        named = claims_mod.request_key(self.workdir, "loc-8")
        self.assertNotEqual(named, first)
        # a retry that names no package reuses the key of one that did, so a lost
        # answer to a named claim leaves no second claim behind
        self.assertEqual(claims_mod.request_key(self.workdir, None), named)

    def test_nothing_free_under_execute_claims_nothing(self):
        self.api.free = False
        code, out, _err = self.main("--stage", "claim", "--execute")
        self.assertEqual(code, 0)
        self.assertIn("no package is free", out)
        self.assertIsNone(claims_mod.held(self.workdir))

    def test_a_replayed_claim_that_ended_is_claimed_again_with_a_new_key(self):
        self.api.claim_states = ["expired"]
        code, out, err = self.main("--stage", "claim", "--execute")
        self.assertEqual(code, 0, err)
        self.assertIn("claiming again", out)
        keys = [payload["request_key"] for path, payload in self.api.calls
                if path == "/api/claims"]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertTrue(claims_mod.live(claims_mod.held(self.workdir)))
        self.assertEqual(len(self.api.downloads), 1)

    def test_a_dry_run_never_downloads_a_package_it_lacks(self):
        claims_mod.save(self.workdir, self.api.describe(), self.api.BASE_URL)
        code, _out, err = self.main("--stage", "run")
        self.assertEqual(code, 2)
        self.assertIn("not downloaded yet", err)
        self.assertEqual(self.api.downloads, [])

    def test_no_token_is_refused_before_any_request(self):
        os.environ.pop(claims_mod.TOKEN_ENV, None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = run_package.main(["--claims", "--workdir", str(self.workdir),
                                     "--stage", "claim"])
        self.assertEqual(code, 2)
        self.assertIn(claims_mod.TOKEN_ENV, err.getvalue())
        self.assertEqual(self.api.calls, [])

    def test_the_modes_and_their_stages_are_named(self):
        for argv in (["--workdir", "w"],
                     ["--claims", "--repo", "r", "--workdir", "w"],
                     ["--repo", "r", "--workdir", "w", "--stage", "index"],
                     ["--repo", "r", "--workdir", "w", "--stage", "release", "--reason", "done"],
                     ["--claims", "--workdir", "w", "--stage", "release"],
                     ["--claims", "--workdir", "w", "--take-over"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                run_package.main(argv)
            self.assertEqual(caught.exception.code, 2)


class GateCodeTests(ClaimsCase):
    def test_gate_code_that_is_not_its_commit_is_refused(self):
        verify = self.checkout / "scripts" / "pipeline" / "gate" / "verify_locate.py"
        verify.write_text(verify.read_text() + "\n# another gate\n", encoding="utf-8")
        env = {**os.environ, **GIT_IDENTITY}
        subprocess.run(["git", "-C", str(self.checkout), "commit", "-qam", "other"],
                       check=True, capture_output=True, env=env)
        other = subprocess.run(["git", "-C", str(self.checkout), "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.checkout), "update-ref",
                        "refs/remotes/origin/main", other], check=True, capture_output=True)
        self.main("--stage", "claim", "--execute")
        target = package_mod.package_dir(self.workdir, "loc-7")
        manifest = package_mod.read(target)
        self.assertEqual(claims_mod.gate_code_problems(target, manifest), [])
        problems = claims_mod.gate_code_problems(target, {**manifest, "producer_commit": other})
        self.assertEqual(problems, [f"gate/verify_locate.py is not "
                                    f"scripts/pipeline/gate/verify_locate.py at {other[:12]}"])

    def test_an_unknown_commit_says_to_fetch(self):
        self.main("--stage", "claim", "--execute")
        target = package_mod.package_dir(self.workdir, "loc-7")
        manifest = {**package_mod.read(target), "producer_commit": "d" * 40}
        problems = claims_mod.gate_code_problems(target, manifest)
        self.assertEqual(len(problems), 1)
        self.assertIn("fetch origin", problems[0])
        self.assertEqual(claims_mod.gate_code_problems(target, {**manifest,
                                                                "producer_commit": None}),
                         ["the package names no producer commit"])

    def test_a_file_the_gate_is_not_made_of_is_refused(self):
        self.main("--stage", "claim", "--execute")
        target = package_mod.package_dir(self.workdir, "loc-7")
        manifest = package_mod.read(target)
        files = {**manifest["files"], "gate/extra.py": "e" * 64}
        self.assertEqual(claims_mod.gate_code_problems(target, {**manifest, "files": files}),
                         ["gate/extra.py is not a file the gate is made of"])

    def package(self):
        self.main("--stage", "claim", "--execute")
        target = package_mod.package_dir(self.workdir, "loc-7")
        return target, package_mod.read(target)

    def test_code_outside_gate_and_another_spelling_of_gate_are_refused(self):
        target, manifest = self.package()
        for extra, words in (("vocab.py", "code outside gate/"),
                             ("prompts/x.PYC", "code outside gate/"),
                             ("tasks/json/__init__.py", "code outside gate/"),
                             ("Gate/verify_locate.py", "spelled otherwise than gate/")):
            with self.subTest(extra=extra):
                files = {**manifest["files"], extra: "e" * 64}
                problems = claims_mod.gate_code_problems(target, {**manifest, "files": files})
                self.assertEqual(problems, [f"{extra} is {words}"])

    def test_a_gate_missing_a_file_is_refused(self):
        target, manifest = self.package()
        files = {rel: digest for rel, digest in manifest["files"].items()
                 if rel != "gate/vocab.py"}
        self.assertEqual(claims_mod.gate_code_problems(target, {**manifest, "files": files}),
                         ["the package's gate lacks vocab.py"])

    def test_a_commit_off_origin_main_is_refused(self):
        target, manifest = self.package()
        subprocess.run(["git", "-C", str(self.checkout), "update-ref", "-d",
                        "refs/remotes/origin/main"], check=True, capture_output=True)
        problems = claims_mod.gate_code_problems(target, manifest)
        self.assertEqual(len(problems), 1)
        self.assertIn("not on this checkout's origin/main", problems[0])

    def test_code_left_in_the_root_stops_the_index_and_the_run(self):
        target, manifest = self.package()
        root = work_mod.materialise(target, self.workdir)
        self.assertEqual(claims_mod.root_problems(root, manifest), [])
        for rel, words in (("vocab.py", "code the package did not carry"),
                           ("gate/extra.txt", "a file the package did not carry"),
                           ("json/__init__.py", "holds code"),
                           ("gate/sub/x.json", "a directory the gate does not hold")):
            with self.subTest(rel=rel):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")
                problems = claims_mod.root_problems(root, manifest)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(words, problems[0])
                code, _out, err = self.main("--stage", "run")
                self.assertEqual(code, 2)
                self.assertIn("did not bring", err)
                path.unlink()
                if path.parent not in (root, root / "gate"):
                    path.parent.rmdir()

    def test_the_run_stage_refuses_a_package_whose_gate_changed_after_the_claim(self):
        self.main("--stage", "claim", "--execute")
        self.main("--stage", "index", "--execute")
        with patch.object(claims_mod, "gate_code_problems", lambda *_a, **_k: ["changed"]):
            code, _out, err = self.main("--stage", "run")
        self.assertEqual(code, 2)
        self.assertIn("gate code is not the commit it names", err)


class IndexTests(ClaimsCase):
    def test_the_index_lands_where_the_gate_reads_it_and_the_run_lacks_nothing(self):
        self.main("--stage", "claim", "--execute")
        code, out, err = self.main("--stage", "index")
        self.assertEqual(code, 0, err)
        self.assertIn("would download", out)
        root = self.workdir / work_mod.ROOT_DIR
        self.assertFalse((root / "index" / "decls.jsonl").exists())
        code, out, err = self.main("--stage", "index", "--execute")
        self.assertEqual(code, 0, err)
        index = root / "index" / "decls.jsonl"
        self.assertEqual(index.read_bytes(), self.workspace.index.read_bytes())
        # the dry run of the run stage now finds every input and stages every task
        code, out, err = self.main("--stage", "run")
        self.assertEqual(code, 0, err)
        self.assertNotIn("missing  :", out)
        self.assertNotIn("refused  :", out)
        self.assertEqual(work_mod.missing_inputs(root, shipped={self.sid}), [])

    def test_a_bundle_whose_index_is_not_the_pinned_one_is_refused(self):
        self.main("--stage", "claim", "--execute")
        root = work_mod.materialise(package_mod.package_dir(self.workdir, "loc-7"),
                                    self.workdir)
        bundle = json.loads((self.api.bundle_dir / "MANIFEST.json").read_text())
        bundle["index"]["sha256"] = "f" * 64
        with self.assertRaises(claims_mod.ClaimProblem) as caught:
            claims_mod.link_index(root, self.api.bundle_dir, bundle)
        self.assertIn("not the one the gate pins", str(caught.exception))

    def test_a_bundle_index_path_it_does_not_list_is_never_read(self):
        self.main("--stage", "claim", "--execute")
        root = work_mod.materialise(package_mod.package_dir(self.workdir, "loc-7"),
                                    self.workdir)
        bundle = json.loads((self.api.bundle_dir / "MANIFEST.json").read_text())
        for path in ("../../../../.zshrc", "index/other.jsonl"):
            with self.subTest(path=path):
                bundle["index"]["path"] = path
                with self.assertRaises(claims_mod.ClaimProblem) as caught:
                    claims_mod.link_index(root, self.api.bundle_dir, bundle)
                self.assertIn("does not list", str(caught.exception))
        self.assertFalse((root / "index" / "decls.jsonl").exists())

    def test_an_index_whose_bytes_are_not_the_pin_is_refused(self):
        self.main("--stage", "claim", "--execute")
        root = work_mod.materialise(package_mod.package_dir(self.workdir, "loc-7"),
                                    self.workdir)
        copy = self.tmp / "bundle-copy"
        shutil.copytree(self.api.bundle_dir, copy)
        (copy / "index" / "decls.jsonl").write_text("{}\n", encoding="utf-8")
        bundle = json.loads((copy / "MANIFEST.json").read_text())
        with self.assertRaises(claims_mod.ClaimProblem) as caught:
            claims_mod.link_index(root, copy, bundle)
        self.assertIn("does not hash", str(caught.exception))

    def test_an_index_path_outside_the_root_is_refused(self):
        self.main("--stage", "claim", "--execute")
        root = work_mod.materialise(package_mod.package_dir(self.workdir, "loc-7"),
                                    self.workdir)
        config = work_mod.gate_config(root)
        config["index"]["path"] = "../../elsewhere/decls.jsonl"
        (root / "gate" / "gate.json").write_text(json.dumps(config), encoding="utf-8")
        bundle = json.loads((self.api.bundle_dir / "MANIFEST.json").read_text())
        with self.assertRaises(claims_mod.ClaimProblem) as caught:
            claims_mod.link_index(root, self.api.bundle_dir, bundle)
        self.assertIn("outside", str(caught.exception))


class LeaseTests(ClaimsCase):
    def fake_task(self, root, task_id, **_kwargs):
        return {"task_id": task_id, "gate": {"ok": False}, "promoted": []}

    def setUp(self):
        super().setUp()
        patcher = patch.object(claims_mod, "RENEW_EVERY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_paid_run_renews_first_and_after_every_task(self):
        self.main("--stage", "claim", "--execute")
        self.main("--stage", "index", "--execute")
        tasks = work_mod.tasks_of(self.workdir / work_mod.ROOT_DIR)
        with patch.object(work_mod.task_mod, "run_task", self.fake_task), \
                patch.object(work_mod.task_mod, "describe", lambda r: r["task_id"]):
            code, out, err = self.main("--stage", "run", "--execute",
                                       "--worker-command", "true", "--no-reviewer")
        self.assertEqual(code, 0, err + out)
        # a claim made here is not renewed; the run renews once first and after each task
        self.assertEqual(self.posted(f"/api/claims/{CLAIM_ID}/renew"), 1 + len(tasks))

    def test_a_claim_that_ended_stops_the_run_and_is_recorded(self):
        self.main("--stage", "claim", "--execute")
        self.main("--stage", "index", "--execute")
        self.api.renew_refusals = [None, "claim_expired"]
        with patch.object(work_mod.task_mod, "run_task", self.fake_task), \
                patch.object(work_mod.task_mod, "describe", lambda r: r["task_id"]):
            code, out, err = self.main("--stage", "run", "--execute",
                                       "--worker-command", "true", "--no-reviewer")
        self.assertEqual(code, 0, err + out)
        self.assertIn("stopped  :", out)
        self.assertEqual(claims_mod.held(self.workdir)["state"], "ended")
        self.assertFalse(claims_mod.live(claims_mod.held(self.workdir)))
        code, _out, err = self.main("--stage", "run")
        self.assertEqual(code, 2)
        self.assertIn("has ended", err)

    def test_every_ended_code_is_recorded_and_a_network_failure_is_not(self):
        for code in claims_mod.ENDED_CODES:
            with self.subTest(code=code):
                claims_mod.save(self.workdir, self.api.describe(), self.api.BASE_URL)
                self.api.renew_refusals = [code]
                with self.assertRaises(claims_mod.ClaimProblem):
                    claims_mod.renew(base="x", token=TOKEN, workdir=self.workdir,
                                     log=lambda _line: None)
                self.assertEqual(claims_mod.held(self.workdir)["state"], "ended")
        claims_mod.save(self.workdir, self.api.describe(), self.api.BASE_URL)
        after = claims_mod.renewer(base="x", token=TOKEN, workdir=self.workdir,
                                   log=lambda _line: None)
        with patch.object(self.api, "call", side_effect=ValueError("connection reset")):
            self.assertTrue(after({}))
        self.assertTrue(claims_mod.live(claims_mod.held(self.workdir)))

    def test_a_refusal_that_is_not_an_end_lets_the_run_go_on(self):
        claims_mod.save(self.workdir, self.api.describe(), self.api.BASE_URL)
        lines = []
        after = claims_mod.renewer(base="x", token=TOKEN, workdir=self.workdir,
                                   log=lines.append)
        self.api.renew_refusals = ["rate_limit"]
        self.assertTrue(after({}))
        self.assertTrue(claims_mod.live(claims_mod.held(self.workdir)))
        self.assertIn("carrying on", lines[-1])

    def test_renewals_wait_for_the_interval(self):
        claims_mod.save(self.workdir, self.api.describe(), self.api.BASE_URL)
        now = [0.0]
        after = claims_mod.renewer(base="x", token=TOKEN, workdir=self.workdir,
                                   log=lambda _l: None, every=1800, clock=lambda: now[0])
        renews = f"/api/claims/{CLAIM_ID}/renew"
        for moment, expected in ((60, 0), (1799, 0), (1800, 1), (2000, 1), (3600, 2)):
            now[0] = moment
            self.assertTrue(after({}))
            self.assertEqual(self.posted(renews), expected, moment)

    def test_run_tasks_stops_before_the_next_task_when_the_hook_says_so(self):
        root = self.tmp / "root"
        (root / "prompts").mkdir(parents=True)
        (root / "prompts" / "worker.md").write_text("w\n", encoding="utf-8")
        seen = []
        with patch.object(work_mod.task_mod, "run_task", self.fake_task), \
                patch.object(work_mod.task_mod, "describe", lambda r: r["task_id"]), \
                patch.object(work_mod, "hands_out", lambda _root: (False, False)):
            records = work_mod.run_tasks(root, tasks=["a", "b", "c"], log=lambda _l: None,
                                         after_task=lambda r: seen.append(r["task_id"])
                                         or len(seen) < 2)
        self.assertEqual([r["task_id"] for r in records], ["a", "b"])
        self.assertEqual(seen, ["a", "b"])


class SealSubmitReleaseTests(ClaimsCase):
    def test_the_seal_takes_the_packages_campaign_unchallenged(self):
        self.main("--stage", "claim", "--execute")
        seen = {}
        with patch.object(run_package, "seal_stage", lambda **kw: seen.update(kw)):
            code, _out, err = self.main("--stage", "seal", "--set-id", "s1",
                                        "--producer-commit", self.commit,
                                        "--created", self.CREATED)
        self.assertEqual(code, 0, err)
        self.assertEqual((seen["campaign"], seen["unchallenged"], seen["repo"]),
                         ("run:loc-7", True, self.workdir))
        code, _out, err = self.main("--stage", "seal", "--set-id", "s1",
                                    "--created", self.CREATED)
        self.assertEqual(code, 2)
        self.assertIn("--producer-commit", err)

    def submitted(self, state: str):
        import submit as submit_mod  # noqa: PLC0415
        import submission_transport  # noqa: PLC0415

        receipt = {"dry_run": False, "submission": {"id": SUBMISSION_ID, "state": state}}
        calls = []
        self.intake_state = state

        def fake_submit(set_dir, *, base_url, execute, token, wait_seconds):
            calls.append((Path(set_dir), base_url, execute, token, wait_seconds))
            return receipt

        def fake_status(submission_id, *, base_url, token):
            assert (base_url, token) == (FakeApi.BASE_URL, TOKEN)
            return {"submission": {"id": submission_id, "state": self.intake_state}}
        return calls, (patch.object(submit_mod, "submit", fake_submit),
                       patch.object(submit_mod, "status", fake_status),
                       patch.object(submission_transport.intake.build_store,
                                    "interpreter_problem", lambda: None))

    def test_results_submit_link_the_receipt_and_release_done(self):
        self.main("--stage", "claim", "--execute")
        (self.workdir / run_package.SET_DIR).mkdir()
        calls, patches = self.submitted("queued")
        with patches[0], patches[1], patches[2]:
            code, out, err = self.main("--stage", "results")
            self.assertEqual((code, self.api.links), (0, []), err)
            self.assertFalse((self.workdir / "SUBMISSION.json").exists())
            renews = self.posted(f"/api/claims/{CLAIM_ID}/renew")
            code, out, err = self.main("--stage", "results", "--execute", "--wait", "5")
            self.assertEqual(code, 0, err)
            self.assertEqual(calls[-1], (self.workdir / run_package.SET_DIR,
                                         FakeApi.BASE_URL, True, TOKEN, 5.0))
            # the claim is renewed before the set is sent for it
            self.assertEqual(self.posted(f"/api/claims/{CLAIM_ID}/renew"), renews + 1)
            self.assertEqual(self.api.links, [SUBMISSION_ID])
            self.assertTrue((claims_mod.receipts_dir(self.workdir, CLAIM_ID)
                             / f"{SUBMISSION_ID}.json").is_file())
            code, out, err = self.main("--stage", "release", "--reason", "done")
            self.assertEqual(code, 0, err)
            self.assertIn("would release", out)
            # done waits for intake's verdict
            code, out, err = self.main("--stage", "release", "--reason", "done", "--execute")
            self.assertEqual(code, 2)
            self.assertIn("has not decided", err)
            self.intake_state = "rejected"
            code, out, err = self.main("--stage", "release", "--reason", "done", "--execute")
            self.assertEqual(code, 2)
            self.assertIn("accepted none", err)
            self.assertEqual(self.posted(f"/api/claims/{CLAIM_ID}/release"), 0)
            self.intake_state = "accepted"
            code, out, err = self.main("--stage", "release", "--reason", "done", "--execute")
            self.assertEqual(code, 0, err)
            self.assertEqual(claims_mod.held(self.workdir)["state"], "completed")
            code, _out, err = self.main("--stage", "release", "--reason", "done",
                                        "--execute")
        self.assertEqual(code, 2)
        self.assertIn("no live claim", err)

    def test_a_set_intake_turned_away_is_linked_to_nothing(self):
        self.main("--stage", "claim", "--execute")
        (self.workdir / run_package.SET_DIR).mkdir()
        _calls, patches = self.submitted("rejected")
        with patches[0], patches[1], patches[2]:
            code, _out, err = self.main("--stage", "results", "--execute")
        self.assertEqual(code, 2)
        self.assertIn("turned the set away", err)
        self.assertEqual(self.api.links, [])

    def test_done_without_a_linked_receipt_is_refused_here_and_by_the_service(self):
        self.main("--stage", "claim", "--execute")
        code, _out, err = self.main("--stage", "release", "--reason", "done", "--execute")
        self.assertEqual(code, 2)
        self.assertIn("no receipt is linked", err)
        with self.assertRaises(claims_mod.ClaimProblem) as caught:
            claims_mod.release(base="x", token=TOKEN, workdir=self.workdir, reason="done",
                               execute=True, log=lambda _l: None)
        self.assertIn("no_submission", str(caught.exception))
        self.assertTrue(claims_mod.live(claims_mod.held(self.workdir)))
        code, _out, err = self.main("--stage", "release", "--reason", "gave_up", "--execute")
        self.assertEqual(code, 0, err)
        self.assertEqual(claims_mod.held(self.workdir)["end_reason"], "gave_up")

    def test_a_link_refused_because_the_claim_ended_is_recorded(self):
        self.main("--stage", "claim", "--execute")
        with patch.object(self.api, "call", side_effect=Refusal(409, "claim_expired", "x")):
            with self.assertRaises(claims_mod.ClaimProblem):
                claims_mod.link(base="x", token=TOKEN, workdir=self.workdir,
                                submission_id=SUBMISSION_ID, log=lambda _l: None)
        self.assertEqual(claims_mod.held(self.workdir)["state"], "ended")


if __name__ == "__main__":
    unittest.main(verbosity=2)
