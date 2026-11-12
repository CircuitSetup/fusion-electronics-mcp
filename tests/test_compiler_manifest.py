"""Candidate publication and verification without a live Fusion session."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
        self.assertEqual(first.data["compiler_version"], "0.3.0+workflow.35")
        self.assertEqual(first.data["inputs"]["spec_sha256"], sha256(self.raw).hexdigest())
        self.assertEqual(first.data["artifacts"]["board"]["path"], "candidate.brd")
        self.raw += b"\n"
        self.source.write_bytes(self.raw)
        second = self.compile("second")
        self.assertNotEqual(first.data["candidate_id"], second.data["candidate_id"])
        self.assertEqual(first.read_artifact("board"), second.read_artifact("board"))

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
