"""Candidate publication and verification without a live Fusion session."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from test_compiler_schema import minimal_design


class CompilerManifestTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.source = self.root / "design.json"
        self.raw = json.dumps(minimal_design()).encode()
        self.source.write_bytes(self.raw)

    def compile(self, destination):
        from fusion_offline.compiler.compile import compile_candidate

        return compile_candidate(self.source, sha256(self.raw).hexdigest(), self.root / destination)

    def test_two_runs_publish_identical_path_free_artifacts(self):
        from fusion_offline.compiler.manifest import load_candidate_manifest

        first, second = self.compile("first"), self.compile("second")
        self.assertEqual(first.data["status"], "pass")
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.path.read_bytes(), second.path.read_bytes())
        self.assertEqual(first.data, load_candidate_manifest(first.path, first.sha256).data)
        self.assertEqual(set(first.data["artifacts"]),
                         {"schematic", "board", "report", "preview", "routing_intent", "board_svg",
                          "schematic_sheet_1", "routing_diagnostics", "summary"})
        self.assertNotIn(str(self.source).encode(), first.path.read_bytes())
        for key, record in first.data["artifacts"].items():
            with self.subTest(key=key):
                path = Path(record["path"])
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
                first_bytes = first.read_artifact(key)
                self.assertEqual(first_bytes, second.read_artifact(key))
                self.assertEqual(sha256(first_bytes).hexdigest(), record["sha256"])
                self.assertNotIn(str(first.path.parent).encode(), first_bytes)

    def test_mutated_artifact_invalidates_candidate(self):
        from fusion_offline.compiler.manifest import load_candidate_manifest

        first = self.compile("first")
        (first.path.parent / first.data["artifacts"]["board"]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            load_candidate_manifest(first.path, first.sha256)

    def test_native_templates_publish_matching_fusion_archives(self):
        from fusion_offline.compiler.routing_job import plan_routing_job, promote_routing_job

        design = json.loads(self.raw)
        payloads = {}
        for kind, suffix in (("board", ".brd"), ("schematic", ".sch")):
            asset = design["templates"][kind]
            template = self.root / f"empty.f{suffix[1:]}"
            document = f"{kind}/electron.BlobParts/ExtFile.test{suffix}"
            with ZipFile(template, "w") as archive:
                archive.writestr("Manifest.dat", b"template metadata")
                archive.writestr(document, Path(asset["path"]).read_bytes())
            design["templates"][kind] = {"path": str(template), "sha256": sha256(template.read_bytes()).hexdigest()}
            payloads[kind] = document
        self.raw = json.dumps(design).encode()
        self.source.write_bytes(self.raw)
        first = self.compile("native")
        with self.assertRaisesRegex(ValueError, "missing current evidence.*VCC"):
            plan_routing_job(first.path, first.sha256, ["VCC"])
        job = plan_routing_job(first.path, first.sha256)
        final = promote_routing_job(first.path, first.sha256, job.path, job.sha256, self.root / "routed")
        for candidate in (first, final):
            for kind, document in payloads.items():
                path = candidate.path.parent / f"candidate.f{Path(document).suffix[1:]}"
                with ZipFile(path) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertEqual(archive.read("Manifest.dat"), b"template metadata")
                    self.assertEqual(archive.read(document), candidate.read_artifact(kind))

    def test_out_of_sync_pair_is_rejected_before_publication(self):
        from fusion_offline.compiler.board import build_board

        for field, detail in (("value", "value differs"), ("library", "library differs")):
            with self.subTest(field=field):
                def stale_board(*args, **kwargs):
                    board = ET.fromstring(build_board(*args, **kwargs))
                    board.find(".//elements/element").set(field, "stale")
                    return ET.tostring(board, encoding="utf-8")

                with patch("fusion_offline.compiler.compile.build_board", side_effect=stale_board):
                    with self.assertRaisesRegex(ValueError, f"out of sync.*{detail}"):
                        self.compile("out-of-sync")
                self.assertFalse((self.root / "out-of-sync").exists())

    def test_routing_uses_current_with_twenty_percent_margin(self):
        from fusion_offline.compiler.routing_job import plan_routing_job
        from fusion_offline.compiler.validate import validate_candidate

        design = json.loads(self.raw)
        design["routing"]["selected_nets"] = ["VCC"]
        design["routing"]["policies"]["VCC"] = {
            "allowed_layers": [1],
            "current": {"expected_a": 1.0, "basis": "one 1 A load on the shared supply",
                        "sources": [{"part": "J1", "datasheet": "fixture datasheet", "section": "Load table"}]},
        }
        self.raw = json.dumps(design).encode()
        self.source.write_bytes(self.raw)
        base = self.compile("current")
        job = plan_routing_job(base.path, base.sha256)
        board = ET.fromstring(job.read_artifact("candidate_board"))
        wires = board.findall(".//signal[@name='VCC']/wire")
        self.assertTrue(wires)
        self.assertEqual({float(wire.attrib["width"]) for wire in wires}, {0.387})
        wires[0].set("width", "0.386")
        report = validate_candidate(base.read_artifact("schematic"), ET.tostring(board),
                                    json.loads(base.read_artifact("routing_intent")))
        finding = next(row for row in report["findings"] if row["check"] == "routing_geometry")
        self.assertEqual(finding["status"], "fail")
        self.assertIn("0.387", finding["detail"])

    def test_relative_traversal_in_re_pinned_manifest_is_refused(self):
        from fusion_offline.compiler.canonical import canonical_json_bytes
        from fusion_offline.compiler.manifest import load_candidate_manifest

        first = self.compile("first")
        manifest = first.data
        manifest["artifacts"]["board"]["path"] = "../escaped.brd"
        raw = canonical_json_bytes(manifest)
        first.path.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "relative"):
            load_candidate_manifest(first.path, sha256(raw).hexdigest())

    def test_nonempty_unrelated_directory_is_preserved(self):
        output = self.root / "occupied"
        output.mkdir()
        unrelated = output / "unrelated"
        unrelated.write_bytes(b"protected")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.compile(output.name)
        self.assertEqual(list(output.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_bytes(), b"protected")

    def test_manifest_binds_raw_spec_pin_and_release_version(self):
        first = self.compile("first")
        self.assertEqual(first.data["compiler_version"], "0.4.0")
        self.assertEqual(first.data["inputs"]["spec_sha256"], sha256(self.raw).hexdigest())
        self.assertEqual(first.data["artifacts"]["board"]["path"], "candidate.brd")
        self.raw += b"\n"
        self.source.write_bytes(self.raw)
        second = self.compile("second")
        self.assertNotEqual(first.data["candidate_id"], second.data["candidate_id"])
        self.assertEqual(first.read_artifact("board"), second.read_artifact("board"))

    def test_previous_workflow_manifest_retains_its_original_pin(self):
        from fusion_offline.compiler.canonical import canonical_json_bytes
        from fusion_offline.compiler.manifest import _identity, load_candidate_manifest

        candidate = self.compile("previous")
        manifest = candidate.data
        manifest["compiler_version"] = "0.3.0+workflow.35"
        manifest["candidate_id"] = _identity(manifest["inputs"], manifest["routing_job"], contract_version=1)
        raw = canonical_json_bytes(manifest)
        candidate.path.write_bytes(raw)
        previous = load_candidate_manifest(candidate.path, sha256(raw).hexdigest())
        self.assertEqual(previous.data, manifest)
        self.assertEqual(previous.path.read_bytes(), raw)

    def test_repinned_report_cannot_omit_or_downgrade_required_findings(self):
        from fusion_offline.compiler.compile import compile_candidate
        from fusion_offline.compiler.canonical import canonical_json_bytes
        from fusion_offline.compiler.manifest import load_candidate_manifest

        for edit, check in (("omit", "pair_signature"), ("downgrade", "keepouts"),
                            ("duplicate", "contacts"), ("downgrade", "full_routing")):
            with self.subTest(edit=edit, check=check):
                design = json.loads(self.raw)
                design["routing"]["require_fully_routed"] = check == "full_routing"
                raw = json.dumps(design).encode()
                self.source.write_bytes(raw)
                candidate = compile_candidate(self.source, sha256(raw).hexdigest(), self.root / check)
                manifest = candidate.data
                report = json.loads(candidate.read_artifact("report"))
                row = next(finding for finding in report["findings"] if finding["check"] == check)
                if edit == "omit":
                    report["findings"].remove(row)
                elif edit == "duplicate":
                    report["findings"].append(dict(row))
                else:
                    row["required"] = False
                report["status"] = "pass"
                record = manifest["artifacts"]["report"]
                raw = canonical_json_bytes(report)
                (candidate.path.parent / record["path"]).write_bytes(raw)
                record["sha256"] = sha256(raw).hexdigest()
                manifest.update(status="pass", findings=report["findings"])
                raw = canonical_json_bytes(manifest)
                candidate.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, check):
                    load_candidate_manifest(candidate.path, sha256(raw).hexdigest())
