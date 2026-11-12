"""Exercise the complete offline compiler and its native pair boundary."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from fusion_mcp.addin.FusionElectronicsMCP import eagle_import
from fusion_offline.compiler.compile import compile_candidate
from fusion_offline.compiler.manifest import load_candidate_manifest
from fusion_offline.compiler.render import render_candidate
from fusion_offline.compiler.routing_job import (
    load_routing_job, plan_routing_job, preview_routing_job, promote_routing_job,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "compiler" / "e2e"


def materialize_spec(directory, *, partial=False):
    data = json.loads((FIXTURE / "design.json").read_bytes())
    for asset in (*data["templates"].values(), *data["libraries"]):
        asset["path"] = str((FIXTURE / asset["path"]).resolve())
    if partial:
        data["routing"]["policies"].update(json.loads((FIXTURE / "impossible-policy.json").read_bytes()))
    source = directory / "design.json"
    raw = json.dumps(data, sort_keys=True).encode()
    source.write_bytes(raw)
    return source, sha256(raw).hexdigest()


def build_routed_fixture(directory, spec, *, clock=None):
    base = compile_candidate(*spec, directory / "base")
    job = plan_routing_job(base.path, base.sha256, clock=clock)
    final = promote_routing_job(base.path, base.sha256, job.path, job.sha256, directory / "final")
    return base, job, final


class CompilerEndToEndTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def assert_pins(self, manifest):
        raw = manifest.path.read_bytes()
        self.assertEqual(sha256(raw).hexdigest(), manifest.sha256)
        entries = json.loads(raw)["artifacts"]
        self.assertEqual({p.name for p in manifest.path.parent.iterdir()},
                         {manifest.path.name, *(entry["path"] for entry in entries.values())})
        for entry in entries.values():
            raw = (manifest.path.parent / entry["path"]).read_bytes()
            self.assertEqual(sha256(raw).hexdigest(), entry["sha256"], entry["path"])

    def assert_budget(self, job, diagnostics):
        budget = json.loads(job.read_artifact("request"))["policy"]["budget"]
        for row in diagnostics.values():
            self.assertLessEqual(row["attempts"], budget["max_attempts_per_connection"])
            self.assertLessEqual(row["expanded_nodes"], budget["max_nodes_per_connection"])
            self.assertLessEqual(row["expanded_nodes"], row["attempts"] * budget["max_nodes_per_attempt"])
            self.assertLessEqual(row["vias_used"] or 0, budget["max_vias_per_connection"])

    def test_complete_fixture_compiles_routes_and_promotes_repeatably(self):
        self.assertTrue((FIXTURE / "design.json").is_file(), "Missing pinned end-to-end design")
        spec = materialize_spec(self.root)
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        base, job, final = build_routed_fixture(first, spec)
        repeated_base, repeated_job, repeated_final = build_routed_fixture(second, spec)
        for original, repeated in zip((base, job, final), (repeated_base, repeated_job, repeated_final)):
            self.assert_pins(original)
            self.assertEqual(original.path.read_bytes(), repeated.path.read_bytes())
            for key in original.data["artifacts"]:
                self.assertEqual(original.read_artifact(key), repeated.read_artifact(key), key)

        saved_job = load_routing_job(job.path, job.sha256)
        self.assertEqual(preview_routing_job(job.path, job.sha256), final.read_artifact("board_svg"))
        self.assertEqual(render_candidate(final.path, final.sha256).read_artifact("preview"),
                         repeated_final.read_artifact("preview"))
        for key in ("board_svg", "schematic_sheet_1"):
            self.assertEqual(ET.fromstring(final.read_artifact(key)).tag, "{http://www.w3.org/2000/svg}svg")

        with patch("fusion_offline.compiler.routing_job.route_all",
                   side_effect=AssertionError("promotion called the router")):
            promoted = promote_routing_job(base.path, base.sha256, saved_job.path, saved_job.sha256,
                                           self.root / "promoted")
        self.assertEqual(promoted.path.read_bytes(), final.path.read_bytes())
        self.assertEqual(promoted.read_artifact("board"), saved_job.read_artifact("candidate_board"))
        report = json.loads(load_candidate_manifest(final.path, final.sha256).read_artifact("report"))
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["airwires"], 0)
        self.assertEqual(report["airwires_by_net"], {
            "BRIDGE": 0, "N1": 0, "N2": 0, "N3": 0, "N4": 0, "N5": 0, "N6": 0,
        })

        board = ET.fromstring(final.read_artifact("board"))
        schematic = ET.fromstring(final.read_artifact("schematic"))
        signature = eagle_import.signature(schematic, board)
        self.assertEqual(signature["elements"], ["R1", "R2", "R3", "R4", "R5", "R6", "U1"])
        self.assertEqual(signature["contacts"], [
            ("BRIDGE", "R1", "1"), ("BRIDGE", "U1", "2"),
            ("N1", "R1", "2"), ("N1", "R2", "1"),
            ("N2", "R2", "2"), ("N2", "R3", "1"),
            ("N3", "R3", "2"), ("N3", "R4", "1"),
            ("N4", "R4", "2"), ("N4", "R5", "1"),
            ("N5", "R5", "2"), ("N5", "R6", "1"),
            ("N6", "R6", "2"), ("N6", "U1", "1"),
        ])
        self.assertEqual({row.get("gate") for row in schematic.findall(".//instance[@part='U1']")},
                         {"A", "B"})
        self.assertEqual(board.find(".//element[@name='R1']").get("locked"), "yes")
        self.assertNotEqual(board.find(".//element[@name='R2']").get("locked"), "yes")
        self.assertEqual(len(board.findall(".//plain/polygon[@layer='41']")), 1)
        self.assertEqual(len(board.findall(".//signal[@name='N1']/polygon")), 1)

        request = json.loads(job.read_artifact("request"))
        bridge = next(row for row in request["connections"] if row["net"] == "BRIDGE")
        self.assertEqual({tuple(bridge["start_layers"]), tuple(bridge["goal_layers"])}, {(1,), (16,)})
        signal = board.find(".//signal[@name='BRIDGE']")
        self.assertEqual({wire.get("layer") for wire in signal.findall("wire")}, {"1", "16"})
        self.assertEqual([via.get("extent") for via in signal.findall("via")], ["1-16"])
        diagnostics = json.loads(job.read_artifact("diagnostics"))
        self.assertEqual(len(diagnostics), 7)
        self.assertEqual({row["termination"] for row in diagnostics.values()}, {"routed"})
        self.assert_budget(job, diagnostics)

    def test_impossible_layer_policy_retains_a_bounded_partial_candidate(self):
        self.assertTrue((FIXTURE / "impossible-policy.json").is_file(), "Missing impossible routing policy")
        _, job, final = build_routed_fixture(self.root, materialize_spec(self.root, partial=True))
        outcome = json.loads(job.read_artifact("routes"))
        self.assertEqual(len(outcome["routes"]), 6)
        [(connection, reason)] = outcome["failed"].items()
        self.assertTrue(connection.startswith("BRIDGE:"))
        self.assertRegex(reason, r"(?i)layer")
        diagnostics = json.loads(job.read_artifact("diagnostics"))
        self.assertNotEqual(diagnostics[connection]["termination"], "routed")
        self.assert_budget(job, diagnostics)
        report = json.loads(final.read_artifact("report"))
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["airwires"], 1)
        self.assertEqual(report["airwires_by_net"]["BRIDGE"], 1)
        self.assert_pins(job)
        self.assert_pins(final)

    def test_live_harness_requires_explicit_project_and_product(self):
        script = Path(__file__).resolve().parent / "live" / "e2e_compiled_import.py"
        self.assertTrue(script.is_file(), "Missing opt-in live import harness")
        for args, missing in [
            (["--name", "test"], "--disposable-project-id"),
            (["--disposable-project-id", "test"], "--name"),
        ]:
            with self.subTest(missing=missing):
                result = subprocess.run([sys.executable, str(script), *args],
                                        capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f"required: {missing}", result.stderr)

    def test_live_unknown_state_preserves_report_and_stops_resumption(self):
        from types import SimpleNamespace

        from live.e2e_compiled_import import Acceptance, advance_import

        report = Acceptance(self.root, "disposable", "new-product")
        candidate = SimpleNamespace(path=self.root / "manifest.json", sha256="a" * 64)
        arguments = []

        def native_import(*args):
            arguments.append(args)
            status = "pending" if len(arguments) == 1 else "state_unknown"
            return {"status": status, "request_id": report.data["request_id"], "step": "awaiting_schematic_picker"}

        with patch("live.e2e_compiled_import.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Native import state is unknown"):
                advance_import(SimpleNamespace(import_eagle_candidate=native_import), candidate, report)
        self.assertEqual(arguments, [(str(candidate.path), candidate.sha256, report.data["request_id"],
                                      "disposable", "new-product")] * 2)
        observed = json.loads((self.root / "report.json").read_bytes())
        self.assertEqual(observed["status"], "state_unknown")
        self.assertEqual(observed["phases"]["import_02"]["ticket"]["status"], "state_unknown")

    def test_live_signature_detects_pinrefs_and_gates_moved_between_sheets(self):
        from live.e2e_compiled_import import pair_signatures

        candidate = compile_candidate(*materialize_spec(self.root), self.root / "base")
        sch = ET.fromstring(candidate.read_artifact("schematic"))
        brd = candidate.read_artifact("board")
        sheets = sch.find("./drawing/schematic/sheets")
        second = ET.SubElement(sheets, "sheet")
        ET.SubElement(second, "instances")
        ET.SubElement(second, "nets")
        original = pair_signatures(ET.tostring(sch), brd)
        self.assertIn((1, "BRIDGE", "U1", "B", "P"), original.get("electrical_pins", ()))
        self.assertIn((1, "U1", "B"), original.get("placed_gates", ()))

        first_nets = sheets[0].find("nets")
        bridge = first_nets.find("net[@name='BRIDGE']")
        first_nets.remove(bridge)
        second.find("nets").append(bridge)
        moved_pins = pair_signatures(ET.tostring(sch), brd)
        self.assertEqual(moved_pins["physical_pair"], original["physical_pair"])
        self.assertIn((2, "BRIDGE", "U1", "B", "P"), moved_pins["electrical_pins"])
        self.assertNotEqual(moved_pins["electrical_pins"], original["electrical_pins"])

        first_instances = sheets[0].find("instances")
        gate = first_instances.find("instance[@part='U1'][@gate='B']")
        first_instances.remove(gate)
        second.find("instances").append(gate)
        moved_gate = pair_signatures(ET.tostring(sch), brd)
        self.assertEqual(moved_gate["electrical_pins"], moved_pins["electrical_pins"])
        self.assertIn((2, "U1", "B"), moved_gate["placed_gates"])
        self.assertNotEqual(moved_gate["placed_gates"], original["placed_gates"])

    def test_native_check_refuses_unexpected_erc_dialog(self):
        from unittest.mock import Mock

        from live.e2e_compiled_import import native_check

        server = Mock()
        doc = {"document_id": "reopened", "data_file_id": "saved-schematic", "project_id": "disposable",
               "saved": True, "modified": False}
        context = {"active_document": doc, "active_editor": "schematic"}
        server.session.activate.return_value = server.get_context.return_value = context
        server.session.bridge.call.return_value = {"dialogs": [{"expected": False, "title": "Unexpected prompt"}]}

        with self.assertRaisesRegex(RuntimeError, "Unexpected native ERC dialog"):
            native_check(server, "schematic", "ERC;", {"schematic": doc})
        server.session.errors.assert_not_called()

    def test_native_check_refuses_active_switch_before_or_after_reading_errors(self):
        from unittest.mock import Mock

        from live.e2e_compiled_import import native_check

        doc = {"document_id": "reopened", "data_file_id": "saved-board", "project_id": "disposable",
               "saved": True, "modified": False}
        current = {"active_document": doc, "active_editor": "board"}
        switched = {"active_document": dict(doc, document_id="other-revision"), "active_editor": "board"}
        for before, after, error_reads in [(switched, current, 0), (current, switched, 1)]:
            with self.subTest(error_reads=error_reads):
                server = Mock()
                server.session.activate.return_value = current
                server.session.bridge.call.return_value = {}
                server.get_context.side_effect = [before, after]
                with self.assertRaisesRegex(RuntimeError, "Active document changed during native DRC"):
                    native_check(server, "board", "DRC;", {"board": doc})
                self.assertEqual(server.session.errors.call_count, error_reads)
                server.session.activate.assert_called_once_with("board")

    def test_live_reopen_closes_verified_displayed_name_and_opens_exact_file_id(self):
        from unittest.mock import Mock

        from live.e2e_compiled_import import reopen_family

        server = Mock()
        saved = {
            kind: {"kind": kind, "name": f"Displayed {kind}", "document_id": f"old-{kind}",
                   "data_file_id": f"saved-{kind}", "project_id": "disposable", "saved": True, "modified": False}
            for kind in ("design", "schematic", "board")
        }
        server.get_context.return_value = {"documents": list(saved.values())}
        server.close_design.return_value = {"documents": []}
        server.open_design.return_value = {"documents": [],
                                           "active_document": dict(saved["design"], document_id="reopened")}

        self.assertEqual(reopen_family(server, "disposable", "New product", saved, []),
                         server.open_design.return_value)
        server.close_design.assert_called_once_with("Displayed design", discard_changes=False)
        server.open_design.assert_called_once_with("New product", folder="", data_file_id="saved-design")

    def build_with_template(self, modify_template, modify_spec):
        from fusion_offline.compiler.canonical import eagle_xml_bytes

        spec_path, _ = materialize_spec(self.root)
        spec = json.loads(spec_path.read_bytes())
        pinned_template = spec["templates"]["board"]
        raw = Path(pinned_template["path"]).read_bytes()
        root = ET.fromstring(raw)
        modify_template(root.find("./drawing/board/plain"))
        template = self.root / "template.brd"
        raw = eagle_xml_bytes(root, raw)
        template.write_bytes(raw)
        pinned_template.update(path=str(template), sha256=sha256(raw).hexdigest())
        modify_spec(spec)
        payload = json.dumps(spec).encode()
        spec_path.write_bytes(payload)
        return build_routed_fixture(self.root, (spec_path, sha256(payload).hexdigest()))

    def test_routing_uses_pinned_outline_independent_of_wire_order_and_direction(self):
        edges = ((38, 28, 38, 0), (0, 0, 38, 0), (38, 28, 0, 28), (0, 0, 0, 28))
        expected = [dict(zip(("x1", "y1", "x2", "y2"), map(str, edge)), layer="20", width="0")
                    for edge in edges]

        def replace_outline(plain):
            for wire in list(plain):
                plain.remove(wire)
            for attrs in expected:
                ET.SubElement(plain, "wire", attrs)

        base, job, final = self.build_with_template(replace_outline, lambda spec: spec["board"].update(outline=[]))
        initial_report = json.loads(base.read_artifact("report"))
        self.assertIn({"check": "outline", "detail": "", "required": True, "status": "pass"},
                      initial_report["findings"])
        self.assertEqual(json.loads(job.read_artifact("routes"))["failed"], {})
        self.assertEqual(json.loads(final.read_artifact("report"))["airwires"], 0)
        for candidate in (base, final):
            root = ET.fromstring(candidate.read_artifact("board"))
            self.assertEqual([wire.attrib for wire in root.findall(".//plain/wire[@layer='20']")], expected)

    def test_routing_preserves_top_restrict_circle_and_routes_under_it_on_bottom(self):
        circle = {"x": "13", "y": "8", "radius": "3", "width": "0", "layer": "41"}

        def route_only_n1_on_bottom(spec):
            for part in spec["parts"][:2]:
                part["board"].update(side="bottom", rotation=180)
            spec["board"]["pours"].clear()
            spec["routing"].update(selected_nets=["N1"], require_fully_routed=False,
                                   policies={"N1": {"allowed_layers": [16]}})

        _, job, final = self.build_with_template(
            lambda plain: ET.SubElement(plain, "circle", circle), route_only_n1_on_bottom,
        )
        self.assertEqual(json.loads(job.read_artifact("routes"))["failed"], {})
        report = json.loads(final.read_artifact("report"))
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["airwires_by_net"]["N1"], 0)
        board = ET.fromstring(final.read_artifact("board"))
        self.assertEqual(board.find(".//plain/circle").attrib, circle)
        signal = board.find(".//signal[@name='N1']")
        self.assertEqual({wire.attrib["layer"] for wire in signal.findall("wire")}, {"16"})
        self.assertEqual(signal.findall("via"), [])
