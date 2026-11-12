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
          <wire layer="19"/><polygon layer="1"/>
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
                    "data": {"airwires": airwires, "fills": [
                        {"net": "N1", "layer": 1, "contour_wires": 4, "area_mm2": area},
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
                      patch.object(live, "native_check", side_effect=native_findings),
                      outcome):
                    live.run_acceptance(server, report, lambda *_: (None, job, candidate), lambda _: None)

                phases = json.loads((root / "report.json").read_bytes())["phases"]
                self.assertEqual(phases["export_signatures"]["raw_export_layer_19_spans"], 1)
                self.assertEqual(phases["native_repour_airwires"]["measurement"]["airwires"], airwires)
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
