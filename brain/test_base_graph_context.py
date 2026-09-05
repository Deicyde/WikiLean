#!/usr/bin/env python3
"""Hermetic contract tests for the sealed base-graph stage."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_common  # noqa: E402
import build_context  # noqa: E402
import build_snapshot  # noqa: E402
import stage_io  # noqa: E402
from test_build_context import _document  # noqa: E402


SOURCE_MANIFEST_ID = "sha256:" + "d" * 64
COMMIT = "a" * 40
TREE = "b" * 40

REQUIRED_CONTENT = {
    "concept-graph": json.dumps(
        {
            "edges": [],
            "nodes": [
                {
                    "formalizations": [],
                    "label": "Álpha ∑",
                    "qid": "Q1",
                    "slug": "Alpha",
                }
            ],
        },
        separators=(",", ":"),
    ) + "\n",
    "decl-qid-roles": "{}\n",
    "decl-to-qid": "{}\n",
    "hierarchy": '{"libraries":{},"meta":{"source_sha256":"ambient-pin"}}\n',
    "mathlib-source-tree": "-- sealed fixture\n",
    "rebuild-grounding": "[]\n",
    "source-registry": '{"crossref_sources":{}}\n',
    "statement-formal": "decl_name,statement_id,module,kind,docstring\n",
    "theorem-matching": (
        "formal_decl,formal_module,arxiv_id,license_open,gpt54_label,"
        "deepseek_label,informal_ref,paper_title,query_sid,cand_sid,sim\n"
    ),
    "theoremgraph-links": '{"_meta":{"attribution":"fixture"},"links":{}}\n',
    "universe-extension": "",
    "wikidata-crossrefs": "{}\n",
    "wikidata-edges": "",
    "wikidata-universe": (
        '{"classes":[],"enwiki_slug":"Alpha","label":"Álpha ∑","qid":"Q1"}\n'
    ),
}

OPTIONAL_CONTENT = {
    "annotations": '{"annotations":[],"slug":"Alpha"}\n',
    "brain-ext-anchor-links": (
        '{"confidence":"high","db":"fixture","id":"p2","qid":"Q1"}\n'
    ),
    "external-links": (
        '{"context":"body","dst":"p2","src":"p1"}\n'
    ),
    "external-pages": (
        '{"db":"fixture","id":"p1","qid":"Q1",'
        '"qid_confidence":"low","qid_pin":"spoofed",'
        '"qid_source":"ext_anchor","title":"One",'
        '"url":"https://example.test/one"}\n'
        '{"db":"fixture","id":"p2","title":"Two",'
        '"url":"https://example.test/two"}\n'
    ),
    "formal-conjectures": '{"_meta":{"commit":"embedded-fc","n_files":0}}\n',
    "mathlib-tag-xrefs": "",
    "tauceti": '{"_meta":{"commit":"embedded-tc","n_files":0}}\n',
    "tauceti-links": "",
    "user-repos": (
        '{"_meta":{"commit":"embedded-user","lib":"FixtureLib",'
        '"n_files":0,"repo":"owner/repo"}}\n'
    ),
}


def _media_type(path: str) -> str:
    if path.endswith(".jsonl"):
        return "application/x-ndjson"
    if path.endswith(".csv"):
        return "text/csv"
    if path.endswith(".lean"):
        return "text/plain"
    return "application/json"


class BaseGraphContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        inventory = json.loads(
            (HERE / "authority/reducer-inputs-v2.json").read_text(encoding="utf-8")
        )
        cls.contracts = {
            item["id"]: item
            for item in inventory["inputs"]
            if "brain/build_common.py" in item["consumers"]
        }
        if set(cls.contracts) != set(build_common.BASE_GRAPH_INPUT_IDS):
            raise AssertionError("base-graph test bindings do not match the inventory")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _logical_path(contract: dict, input_id: str) -> str:
        if contract["cardinality"] == "one":
            return contract["path"]
        return {
            "annotations": "site/annotations/alpha.json",
            "external-links": "fixture_links.jsonl",
            "external-pages": "fixture_pages.jsonl",
            "mathlib-ilean-tree": ".lake/build/lib/lean/Mathlib/Fixture.ilean",
            "mathlib-source-tree": "Mathlib/Fixture.lean",
            "user-repos": "catalog/data/user_repos/fixture.jsonl",
        }[input_id]

    def _document(
        self,
        base: Path,
        *,
        present_optional: set[str] = frozenset(),
    ) -> dict:
        for root in ("code", "input", "output", "scratch"):
            path = base / root
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)

        document = copy.deepcopy(_document(base))
        bindings = []
        for input_id, contract in sorted(self.contracts.items()):
            required = contract["requirement"] == "required"
            present = required or input_id in present_optional
            members = []
            if present:
                logical_path = self._logical_path(contract, input_id)
                path = base / "input" / contract["root"] / logical_path
                path.parent.mkdir(parents=True, exist_ok=True)
                content = (
                    REQUIRED_CONTENT[input_id]
                    if required
                    else OPTIONAL_CONTENT[input_id]
                )
                path.write_text(content, encoding="utf-8")
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                pin = (
                    {"tree": TREE, "type": "git_commit", "value": COMMIT}
                    if contract["class"] == "curated_git_input"
                    else {"type": "content_sha256", "value": digest}
                )
                members.append(
                    {
                        "bytes": len(raw),
                        "materialized_path": str(path),
                        "media_type": _media_type(logical_path),
                        "object": f"fixture-{input_id}",
                        "path": logical_path,
                        "pin": pin,
                        "sha256": digest,
                        "source_manifest_id": SOURCE_MANIFEST_ID,
                    }
                )
            binding = {
                "cardinality": contract["cardinality"],
                "class": contract["class"],
                "input_id": input_id,
                "members": members,
                "requirement": contract["requirement"],
                "root": contract["root"],
                "source_manifest_ids": [SOURCE_MANIFEST_ID],
                "state": "present" if present else "absent",
            }
            selector = "path" if contract["cardinality"] == "one" else "path_pattern"
            binding[selector] = contract[selector]
            bindings.append(binding)
        document["bindings"] = bindings
        document["configuration"]["external_node_cap"] = 17
        document["replay"]["reducer"]["configuration_sha256"] = hashlib.sha256(
            build_context.canonical_json_bytes(document["configuration"])
        ).hexdigest()
        document["generation_id"] = build_context.generation_identity(document)
        return document

    def _context(
        self,
        base: Path,
        *,
        present_optional: set[str] = frozenset(),
    ) -> build_context.BuildContext:
        return build_context.BuildContext.from_document(
            self._document(base, present_optional=present_optional)
        )

    @staticmethod
    def _outputs(context: build_context.BuildContext) -> tuple[Path, ...]:
        return tuple(
            context.output_for("base-graph", relative)
            for relative in build_snapshot.BASE_CONTEXT_OUTPUTS
        )

    def test_relocation_hostile_ambient_mtimes_and_umask_are_byte_stable(self) -> None:
        first = self._context(self.root / "first")
        second = self._context(self.root / "second")
        self.assertEqual(first.generation_id, second.generation_id)
        for index, context in enumerate((first, second), start=1):
            for binding in context.bindings:
                for path in context.members(binding.input_id):
                    os.utime(path, (index * 1000, index * 1000))

        def forbidden(*_args, **_kwargs):
            raise AssertionError("context build attempted ambient discovery")

        previous_umask = os.umask(0o777)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "BRAIN_DECL_ORACLE": "/ambient/oracle.json",
                        "BRAIN_EXTERNAL_DIR": "/ambient/external",
                        "BRAIN_EXT_NODE_CAP": "999999",
                        "BRAIN_MATHLIB_CHECKOUT": "/ambient/mathlib",
                    },
                ),
                mock.patch.object(build_common, "_pin", side_effect=forbidden),
                mock.patch.object(build_common, "_mathlib_checkout", side_effect=forbidden),
                mock.patch.object(build_common, "external_dir", side_effect=forbidden),
                mock.patch.object(build_common, "ext_node_cap", side_effect=forbidden),
                mock.patch.object(Path, "glob", side_effect=forbidden),
                mock.patch.object(Path, "rglob", side_effect=forbidden),
            ):
                first_id = build_snapshot.build_base_graph_from_context(first)
                second_id = build_snapshot.build_base_graph_from_context(second)
        finally:
            os.umask(previous_umask)

        self.assertEqual(first_id, second_id)
        first_bytes = [path.read_bytes() for path in self._outputs(first)]
        second_bytes = [path.read_bytes() for path in self._outputs(second)]
        self.assertEqual(first_bytes, second_bytes)
        for context in (first, second):
            outputs = self._outputs(context)
            self.assertEqual({path.name for path in outputs}, {
                "edges.jsonl", "edges_links.jsonl", "nodes.jsonl",
            })
            for path in outputs:
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                meta = json.loads(path.read_text(encoding="utf-8").splitlines()[0])[
                    "_meta"
                ]
                self.assertEqual(meta["generated_at"], context.generation_id)
                self.assertEqual(meta["generation_id"], context.generation_id)
                self.assertEqual(meta["snapshot_id"], first_id)
                if "inputs" in meta:
                    self.assertEqual(
                        set(meta["inputs"]), set(build_common.BASE_GRAPH_INPUT_IDS)
                    )
                    self.assertIn("sealed Mathlib", meta["licenses"]["code"])
                    self.assertNotIn("live mathlib4 checkout", meta["licenses"]["code"])
            self.assertEqual(outputs[0].parent.stat().st_mode & 0o777, 0o700)
            self.assertFalse(context.scratch_for("base-graph", "jsonl").exists())
            self.assertEqual(
                sorted(
                    path.relative_to(context.roots.output).as_posix()
                    for path in context.roots.output.joinpath("brain/data").iterdir()
                ),
                list(build_snapshot.BASE_CONTEXT_OUTPUTS),
            )

    def test_present_binding_deletion_and_symlink_fail_before_reduction(self) -> None:
        for kind in ("missing", "symlink"):
            with self.subTest(kind=kind):
                context = self._context(
                    self.root / kind,
                    present_optional={"mathlib-tag-xrefs"},
                )
                source = context.optional_one("mathlib-tag-xrefs")
                assert source is not None
                source.unlink()
                if kind == "symlink":
                    target = self.root / f"{kind}-outside.jsonl"
                    target.write_text("", encoding="utf-8")
                    source.symlink_to(target)
                with (
                    mock.patch.object(build_snapshot, "build") as reducer,
                    self.assertRaisesRegex(
                        ValueError, "could not verify|missing|regular file|symlink"
                    ),
                ):
                    build_snapshot.build_base_graph_from_context(context)
                reducer.assert_not_called()
                self.assertFalse(any(path.exists() for path in self._outputs(context)))

    def test_mutated_member_bytes_never_publish_under_the_sealed_identity(self) -> None:
        context = self._context(self.root / "mutated")
        concept = context.require_one("concept-graph")
        document = json.loads(concept.read_text(encoding="utf-8"))
        document["nodes"][0]["label"] = "MUTATED"
        concept.write_text(json.dumps(document), encoding="utf-8")
        with (
            mock.patch.object(build_snapshot, "build") as reducer,
            self.assertRaisesRegex(
                build_context.BuildContextError, "bytes do not match"
            ),
        ):
            build_snapshot.build_base_graph_from_context(context)
        reducer.assert_not_called()
        self.assertFalse(any(path.exists() for path in self._outputs(context)))

    def test_reducer_consumes_private_copy_not_transiently_mutated_source(self) -> None:
        context = self._context(self.root / "transient-source-mutation")
        concept = context.require_one("concept-graph")
        original = concept.read_bytes()
        real_build = build_snapshot.build

        def mutate_original_during_reduction(*, source_set):
            private = source_set.require_one("concept-graph").path
            self.assertNotEqual(private, concept)
            self.assertEqual(private.stat().st_mode & 0o777, 0o400)
            document = json.loads(concept.read_text(encoding="utf-8"))
            document["nodes"][0]["label"] = "MUTATED_AFTER_VERIFY"
            concept.write_text(json.dumps(document), encoding="utf-8")
            try:
                return real_build(source_set=source_set)
            finally:
                concept.write_bytes(original)

        with mock.patch.object(
            build_snapshot,
            "build",
            side_effect=mutate_original_during_reduction,
        ):
            build_snapshot.build_base_graph_from_context(context)
        nodes = context.output_for("base-graph", "brain/data/nodes.jsonl")
        row = json.loads(nodes.read_text(encoding="utf-8").splitlines()[1])
        self.assertEqual(row["label"], "Álpha ∑")

    def test_private_copy_mutation_is_caught_before_any_output_write(self) -> None:
        context = self._context(self.root / "mutated-private-copy")

        def mutate_during_reduction(*, source_set):
            private = source_set.require_one("concept-graph").path
            private.chmod(0o600)
            document = json.loads(private.read_text(encoding="utf-8"))
            document["nodes"][0]["label"] = "MUTATED_PRIVATE_COPY"
            private.write_text(json.dumps(document), encoding="utf-8")
            return [], [], {
                "generated_at": source_set.generation_id,
                "generation_id": source_set.generation_id,
            }

        with (
            mock.patch.object(
                build_snapshot, "build", side_effect=mutate_during_reduction
            ),
            mock.patch.object(build_snapshot, "write_jsonl") as writer,
            self.assertRaisesRegex(
                build_context.BuildContextError, "bytes do not match"
            ),
        ):
            build_snapshot.build_base_graph_from_context(context)
        writer.assert_not_called()
        self.assertFalse(any(path.exists() for path in self._outputs(context)))

    def test_source_mutation_after_copy_is_caught_before_any_output_write(self) -> None:
        context = self._context(self.root / "mutated-source-after-copy")
        concept = context.require_one("concept-graph")

        def mutate_during_reduction(*, source_set):
            document = json.loads(concept.read_text(encoding="utf-8"))
            document["nodes"][0]["label"] = "PERSISTENT_SOURCE_MUTATION"
            concept.write_text(json.dumps(document), encoding="utf-8")
            return [], [], {
                "generated_at": source_set.generation_id,
                "generation_id": source_set.generation_id,
            }

        with (
            mock.patch.object(
                build_snapshot, "build", side_effect=mutate_during_reduction
            ),
            mock.patch.object(build_snapshot, "write_jsonl") as writer,
            self.assertRaisesRegex(
                build_context.BuildContextError, "bytes do not match"
            ),
        ):
            build_snapshot.build_base_graph_from_context(context)
        writer.assert_not_called()
        self.assertFalse(any(path.exists() for path in self._outputs(context)))

    def test_binding_contract_drift_is_rejected(self) -> None:
        cases = {
            "root": ("root", "external"),
            "cardinality": ("cardinality", "many"),
            "requirement": ("requirement", "optional"),
            "class": ("class", "immutable_source_object"),
            "path": ("path", "catalog/data/source_registry-other.json"),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                base = self.root / f"drift-{label}"
                document = self._document(base)
                binding = next(
                    item for item in document["bindings"]
                    if item["input_id"] == "source-registry"
                )
                member = binding["members"][0]
                old_path = Path(member["materialized_path"])
                raw = old_path.read_bytes()
                if field == "root":
                    binding["root"] = value
                    new_path = base / "input" / value / member["path"]
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    new_path.write_bytes(raw)
                    member["materialized_path"] = str(new_path)
                elif field == "cardinality":
                    binding["cardinality"] = value
                    binding["path_pattern"] = binding.pop("path")
                elif field == "path":
                    binding["path"] = value
                    member["path"] = value
                    new_path = base / "input/repo" / value
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    new_path.write_bytes(raw)
                    member["materialized_path"] = str(new_path)
                else:
                    binding[field] = value
                document["generation_id"] = build_context.generation_identity(document)
                context = build_context.BuildContext.from_document(document)
                with self.assertRaisesRegex(ValueError, "contract is"):
                    build_common.ContextBuildInputs.from_context(context)

    def test_member_pins_are_authoritative_for_frontier_and_external_layers(self) -> None:
        present = {
            "brain-ext-anchor-links",
            "external-links",
            "external-pages",
            "formal-conjectures",
            "tauceti",
            "tauceti-links",
            "user-repos",
        }
        context = self._context(self.root / "pins", present_optional=present)
        sources = build_common.ContextBuildInputs.from_context(context)

        with mock.patch.object(
            build_common,
            "_frontier_repo_layer",
            wraps=build_common._frontier_repo_layer,
        ) as frontier:
            build_common.build(source_set=sources)
        calls = {call.kwargs["lib"]: call.kwargs for call in frontier.call_args_list}
        self.assertEqual(
            calls["FormalConjectures"]["pin"],
            sources.optional_one("formal-conjectures").pin,
        )
        self.assertEqual(calls["TauCeti"]["pin"], sources.optional_one("tauceti").pin)
        self.assertEqual(
            calls["TauCeti"]["agent_links_pin"],
            sources.optional_one("tauceti-links").pin,
        )
        self.assertEqual(
            calls["FixtureLib"]["pin"], sources.members("user-repos")[0].pin
        )

        anchor = sources.optional_one("brain-ext-anchor-links")
        assert anchor is not None
        external = build_common.load_external(
            None,
            {"fixture": {"ingest": {"snippets": False}}},
            page_files=sources.members("external-pages"),
            link_files=sources.members("external-links"),
            anchor_path=anchor.path,
            anchor_pin=anchor.pin,
        )
        page_pin = sources.members("external-pages")[0].pin
        link_pin = sources.members("external-links")[0].pin
        self.assertEqual(external["fixture"]["pin"], page_pin)
        self.assertEqual(external["fixture"]["link_pin"], link_pin)
        _nodes, edges, _stats = build_common.external_layer(
            external,
            concept_qids={"Q1"},
            xref_dsts=set(),
            concept_anchor={},
            xref_pairs=set(),
            registry={"fixture": {"ingest": {"snippets": False}}},
            cap=17,
        )
        pins = {(edge["kind"], edge["dst"]): edge["provenance"]["pin"] for edge in edges}
        self.assertEqual(pins[("xref", "xref:fixture:p1")], page_pin)
        self.assertEqual(pins[("xref", "xref:fixture:p2")], anchor.pin)
        self.assertEqual(pins[("links", "xref:fixture:p2")], link_pin)

    def test_jsonl_writer_uses_utf8_and_lf_bytes(self) -> None:
        output = self.root / "unicode.jsonl"
        build_common.write_jsonl(
            output,
            {"generated_at": "fixture"},
            [{"id": "Q1", "label": "Álpha ∑"}],
        )
        raw = output.read_bytes()
        self.assertIn("Álpha ∑".encode("utf-8"), raw)
        self.assertNotIn(b"\r\n", raw)

    def test_existing_output_and_publish_race_never_replace_competitors(self) -> None:
        context = self._context(self.root / "existing")
        output = context.output_for("base-graph", "brain/data/edges.jsonl")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"competitor")
        before = (output.read_bytes(), output.stat().st_ino)
        with mock.patch.object(build_snapshot, "build") as reducer:
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_snapshot.build_base_graph_from_context(context)
        reducer.assert_not_called()
        self.assertEqual((output.read_bytes(), output.stat().st_ino), before)

        race_context = self._context(self.root / "race")
        race_target = race_context.output_for(
            "base-graph", "brain/data/edges_links.jsonl"
        )
        real_link = stage_io.os.link

        def race(source, destination, *, follow_symlinks=True):
            if Path(destination) == race_target and not race_target.exists():
                race_target.write_bytes(b"racer")
            return real_link(source, destination, follow_symlinks=follow_symlinks)

        with mock.patch.object(stage_io.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                build_snapshot.build_base_graph_from_context(race_context)
        self.assertEqual(race_target.read_bytes(), b"racer")
        self.assertFalse(
            race_context.output_for("base-graph", "brain/data/edges.jsonl").exists()
        )
        self.assertFalse(
            race_context.output_for("base-graph", "brain/data/nodes.jsonl").exists()
        )
        self.assertFalse(race_context.scratch_for("base-graph", "jsonl").exists())

    def test_stage_contract_cross_filesystem_and_cli_modes_fail_closed(self) -> None:
        cases = (
            ("program", {"program": "brain/not-snapshot.py"}, "program is"),
            ("argv", {"argv": []}, "argv is"),
            ("needs", {"needs": ["cells"]}, "dependencies must name earlier|needs are"),
            (
                "outputs",
                {"outputs": [{"kind": "file", "path": "brain/data/nodes.jsonl"}]},
                "outputs are",
            ),
        )
        for label, changes, message in cases:
            with self.subTest(label=label):
                document = self._document(self.root / f"stage-{label}")
                stage = next(item for item in document["stages"] if item["id"] == "base-graph")
                stage.update(changes)
                document["generation_id"] = build_context.generation_identity(document)
                if label == "needs":
                    with self.assertRaisesRegex(build_context.BuildContextError, message):
                        build_context.BuildContext.from_document(document)
                    continue
                context = build_context.BuildContext.from_document(document)
                with self.assertRaisesRegex(build_context.BuildContextError, message):
                    build_snapshot.build_base_graph_from_context(context)

        context = self._context(self.root / "cross-filesystem")
        with (
            mock.patch.object(
                build_snapshot,
                "require_same_filesystem",
                side_effect=OSError("injected cross-device workspace"),
            ),
            mock.patch.object(build_snapshot, "build") as reducer,
            self.assertRaisesRegex(OSError, "cross-device"),
        ):
            build_snapshot.build_base_graph_from_context(context)
        reducer.assert_not_called()
        self.assertFalse(context.scratch_for("base-graph", "jsonl").exists())

        cli_context = self._context(self.root / "cli")
        context_path = self.root / "build-context.json"
        context_path.write_bytes(
            build_context.canonical_json_bytes(cli_context.to_document())
        )
        invalid = (
            ["--build-context", str(context_path), "--stage-id", "base-graph"],
            [
                "--build-context", str(context_path), "--stage-id", "base-graph",
                "--from-jsonl",
            ],
            [
                "--build-context", str(context_path), "--stage-id", "base-graph",
                "--jsonl-only", "--data-dir", str(self.root / "ambient"),
            ],
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as raised:
                build_snapshot.main(arguments)
            self.assertEqual(raised.exception.code, 2)
        with mock.patch.object(
            build_snapshot, "build_base_graph_from_context", return_value="f" * 64
        ) as reducer, mock.patch("builtins.print"):
            self.assertEqual(
                build_snapshot.main(
                    [
                        "--build-context", str(context_path),
                        "--stage-id", "base-graph", "--jsonl-only",
                    ]
                ),
                0,
            )
        reducer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
