"""Native acceptance evidence can differ from the reopened board XML."""

from contextlib import nullcontext
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from live import e2e_compiled_import as live


class LiveCompiledImportTest(unittest.TestCase):
    def test_native_evidence_controls_acceptance(self):
        board = b"""<eagle><drawing><board><signals><signal name="N1">
          <wire layer="19" x1="1" y1="2" x2="3" y2="4"/><polygon layer="1"/>
        </signal></signals></board></drawing></eagle>"""
        schematic = b"<eagle/>"
        source = Path(live.__file__).with_name("measure-repoured-copper.ulp")
        digest = sha256(source.read_bytes()).hexdigest()
        for area, airwires, drc_errors, error in (
            (8.0, 0, [], None),
            (0.0, 0, [], "filled area"),
            (8.0, 1, [], "native airwires"),
            (8.0, 0, [{"description": "Copper Clearance"}], "native board findings"),
        ):
            with self.subTest(area=area, airwires=airwires, drc_errors=drc_errors), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                report = live.Acceptance(root, "disposable", "acceptance")
                candidate = Mock(path=root / "candidate-manifest.json", sha256="a" * 64,
                                 data={"candidate_id": "candidate"})
                candidate.read_artifact.side_effect = {
                    "report": b'{"status":"pass","airwires":0}',
                    "schematic": schematic, "board": board,
                }.__getitem__
                doc = {"document_id": "reopened", "data_file_id": "saved-board",
                       "project_id": "disposable", "saved": True, "modified": False}
                context = {"documents": [], "active_document": doc, "active_editor": "board",
                           "server": {"version": "0.4.0"}}
                server = Mock()
                server.get_context.return_value = server.session.activate.return_value = context
                server.list_designs.return_value = {"project_id": "disposable", "project": "Disposable"}
                server.session.bridge.lock.hold.return_value = nullcontext()
                server.session.bridge.call.side_effect = [
                    {"fusion_version": "test", "addin_version": "0.14.23"}, {"raw": ""},
                ]
                server.session.export.side_effect = {"schematic": schematic, "board": board}.__getitem__
                server.run_registered_ulp.return_value = {
                    "document": {"document_id": doc["document_id"]}, "sha256": digest,
                    "data": {"airwires": airwires,
                             "airwire_spans": [{"net": "N1", "start_mm": [1, 2], "end_mm": [5, 6]}] * airwires,
                             "fills": [
                        {"net": "N1", "layer": 1, "contour_wires": 4, "area_mm2": area,
                         "hatched": 0, "edges_mm": [
                             [0, 0, 0, 2, 0], [0, 2, 4, 2, 0], [4, 2, 4, 0, 0], [4, 0, 0, 0, 0],
                         ]},
                    ]},
                }
                native_findings = [
                    {"command": {}, "findings": {"count": 0, "errors": [], "kind": "schematic"}},
                    {"command": {}, "findings": {"count": len(drc_errors), "errors": drc_errors, "kind": "board"}},
                ]
                outcome = self.assertRaisesRegex(RuntimeError, error) if error else nullcontext()
                job = Mock(data={"job_id": "routing-job"})
                with (patch.object(live, "advance_import"),
                      patch.object(live, "fresh_saved_family", return_value={"board": doc}),
                      patch.object(live, "reopen_family", return_value=context),
                      patch.object(live, "active_member", return_value=doc),
                      patch.object(live, "pair_signatures", return_value={}),
                      patch.object(live, "native_board_semantics", return_value={
                          "rules": {"status": "pass"}, "restrictions": {"status": "pass"},
                      }),
                      patch.object(live, "measured_fill_validation", return_value={"status": "pass"}),
                      patch.object(live, "native_check", side_effect=native_findings),
                      outcome):
                    live.run_acceptance(server, report, lambda *_: (None, job, candidate), lambda _: None)

                phases = json.loads((root / "report.json").read_bytes())["phases"]
                self.assertEqual(phases["export_signatures"]["raw_export_layer_19_spans"], 1)
                self.assertEqual(phases["native_repour_airwires"]["measurement"]["airwires"], airwires)
                comparison = phases["native_repour_airwires"]["airwire_comparison"]
                self.assertEqual(comparison["native_by_net"], {"N1": airwires} if airwires else {})
                self.assertEqual(comparison["export_by_net"], {"N1": 1})
                self.assertEqual(comparison["native_only"],
                                 [{"net": "N1", "start_mm": [1, 2], "end_mm": [5, 6]}] * airwires)
                self.assertEqual(comparison["export_only"], [{"net": "N1", "start_mm": [1, 2], "end_mm": [3, 4]}])
                self.assertEqual(phases["native_repour_airwires"]["measurement"]["fills"][0]["area_mm2"], area)
                self.assertEqual(phases["offline_compile_validate"]["validation"]["airwires"], 0)
                self.assertEqual(phases["native_erc"]["findings"]["errors"], [])
                self.assertEqual(phases["native_drc"]["findings"]["errors"], drc_errors)
                self.assertEqual(server.session.bridge.call.call_args.args, ("run", {
                    "commands": "RATSNEST;", "editor": "board", "expected_document_id": "reopened",
                }))
                ulp = f"compiler-repour-{report.data['request_id']}"
                server.register_ulp.assert_called_once_with(ulp, str(source), "read", ["board"], 0, digest)
                server.run_registered_ulp.assert_called_once_with(ulp, [], "reopened")
                server.unregister_ulp.assert_called_once_with(ulp)

    def test_measured_fill_rings_keep_holes_and_disconnected_islands(self):
        fill = {"net": "GND", "layer": 2, "hatched": 0, "area_mm2": 13,
                "contour_wires": 12, "edges_mm": [
                    [0, 0, 0, 4, 0], [0, 4, 4, 4, 0], [4, 4, 4, 0, 0], [4, 0, 0, 0, 0],
                    [1, 1, 3, 1, 0], [3, 1, 3, 3, 0], [3, 3, 1, 3, 0], [1, 3, 1, 1, 0],
                    [1.5, 1.5, 1.5, 2.5, 0], [1.5, 2.5, 2.5, 2.5, 0],
                    [2.5, 2.5, 2.5, 1.5, 0], [2.5, 1.5, 1.5, 1.5, 0],
                ]}
        rows = live.measured_fill_rows({"fills": [fill]})
        self.assertEqual([(row["net"], row["layer"], len(row["rings"])) for row in rows],
                         [("GND", 2, 2), ("GND", 2, 1)])
        self.assertEqual(rows[0]["rings"][1][0], (1, 1))

    def test_measured_fill_rejects_curved_hatched_and_incomplete_contours(self):
        fill = {"net": "GND", "layer": 2, "hatched": 0, "area_mm2": 1,
                "contour_wires": 4, "edges_mm": [
                    [0, 0, 0, 1, 0], [0, 1, 1, 1, 0], [1, 1, 1, 0, 0], [1, 0, 0, 0, 0],
                ]}
        for changes in ({"hatched": 1}, {"area_mm2": 2}, {"edges_mm": fill["edges_mm"][:-1]},
                        {"edges_mm": [[0, 0, 0, 1, 15], *fill["edges_mm"][1:]]}):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "contour"):
                live.measured_fill_rows({"fills": [dict(fill, **changes)]})

    def test_post_repour_disallowed_trace_fails_even_when_plane_connects(self):
        candidate = Mock(data={})
        candidate.read_artifact.side_effect = {"routing_intent": b"{}", "board": b"<eagle/>"}.__getitem__
        report = {"airwires": 0, "planes": {}, "findings": [
            {"check": check, "status": status, "required": True}
            for check, status in (("plane_connectivity", "pass"), ("keepouts", "pass"), ("routing_geometry", "fail"))
        ]}
        with patch("fusion_offline.compiler.validate.validate_candidate", return_value=report):
            result = live.measured_fill_validation(candidate, b"<eagle/>", b"<eagle/>", {"fills": []})
        self.assertEqual(result["status"], "fail")

    def test_trace_signature_ignores_direction_format_and_airwires(self):
        board = b'<eagle><drawing><board><signals><signal name="N">%s</signal></signals></board></drawing></eagle>'
        a = b'<wire x1="1.23456" y1="2" x2="3" y2="4" width=".2" layer="15"/>'
        b = b'<wire x2="1.23456004" y2="2.0" x1="3" y1="4" width="0.20" layer="15"/>'
        self.assertEqual(live.copper_signature(board % a),
                         live.copper_signature(board % (b + b'<wire layer="19"/>')))
        self.assertNotEqual(live.copper_signature(board % a),
                            live.copper_signature(board % b.replace(b'"15"', b'"2"')))

    def test_native_airwire_count_requires_complete_endpoint_evidence(self):
        with self.assertRaisesRegex(RuntimeError, "count does not match"):
            live.compare_airwires(b"<eagle/>", {"airwires": 1, "airwire_spans": []})

    def test_rules_and_restrictions_are_independent_acceptance_gates(self):
        for group in ("rules", "restrictions"):
            for status in ("fail", "unverified"):
                with self.subTest(group=group, status=status), self.assertRaisesRegex(RuntimeError, group):
                    semantics = {"rules": {"status": "pass"}, "restrictions": {"status": "pass"}}
                    semantics[group]["status"] = status
                    live.require_native_board_semantics(semantics)

    def test_modified_original_prevents_any_native_import(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            report = live.Acceptance(root, "disposable", "acceptance")
            candidate = Mock(path=root / "candidate-manifest.json", sha256="a" * 64,
                             data={"candidate_id": "candidate"})
            candidate.read_artifact.return_value = b'{"status":"pass","airwires":0}'
            server = Mock()
            doc = {"name": "Original", "kind": "board", "saved": True, "modified": True}
            server.get_context.return_value = {
                "documents": [doc], "active_document": doc, "server": {"version": "test"},
            }
            server.list_designs.return_value = {"project_id": "disposable", "project": "Disposable"}
            server.session.bridge.lock.hold.return_value = nullcontext()
            server.session.bridge.call.return_value = {"fusion_version": "test", "addin_version": "0.14.23"}
            with (patch.object(live, "advance_import") as start,
                  self.assertRaisesRegex(RuntimeError, "clear modifications")):
                live.run_acceptance(server, report, lambda *_: (None, None, candidate), lambda _: None)
            start.assert_not_called()
            server.session.bridge.call.assert_called_once_with("ping", timeout=15)

    def test_offline_preflight_defers_only_unverified_pour_connections(self):
        validation = {"status": "pass", "airwires": 1, "airwires_by_net": {"GND": 1, "DATA": 0},
                      "planes": {"GND": {"status": "unverified"}}}
        live.require_offline_acceptance(validation)
        for changes in (
            {"airwires_by_net": {"DATA": 1}},
            {"planes": {"GND": {"status": "fail"}}},
            {"status": "unverified"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(RuntimeError, "offline candidate"):
                live.require_offline_acceptance(dict(validation, **changes))
