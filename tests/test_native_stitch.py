import types
import unittest
from unittest.mock import Mock, patch

from fusion_offline.design import parse_board_design, read_xml
from fusion_offline.stitch import native_stitch_script
from fusion_mcp import server
from test_server import load_addin


class NativeStitchTest(unittest.TestCase):
    def setUp(self):
        self.points = [(0.635, 1.27), (5.715, 1.27), (5.715, 6.35), (0.635, 6.35)]

    def script(self, net="HOST_GND", points=None):
        return native_stitch_script(net, self.points if points is None else points,
                                    pitch_mm=5.08, drill_mm=0.2, offset_mm=0.635, grid_mm=0.635)

    def test_script_closes_polygon_before_committing(self):
        want = ("GRID MIL 25; CHANGE DRILL 7.87401575; "
                "STITCH 'HOST_GND' USEPOLYGON OFFSET 25 SPACING 200 AUTO 1-64 "
                "(25 50) (225 50) (225 250) (25 250) (25 50) COMMIT; GRID LAST;")
        self.assertEqual(self.script(), want)
        self.assertEqual(self.script(points=self.points + self.points[:1]), want)

    def test_rejects_off_grid_crossing_and_unsafe_net(self):
        for net, points in (("bad'; WRITE", self.points), ("GND", self.points[:-1] + [(0.5, 6.35)]),
                            ("GND", [self.points[i] for i in (0, 2, 1, 3)])):
            with self.subTest(net=net, points=points), self.assertRaises(ValueError):
                self.script(net, points)

    def test_parse_native_stitch_separately_from_manual_vias(self):
        root = read_xml(b"""<eagle><drawing><board><signals><signal name="HOST_GND">
          <via x="1" y="2" extent="1-64" drill="0.2"/>
          <viastitch stitchType="Area" offset="203200" spacing="1625600" staggered="no">
            <template><via x="0" y="0" extent="1-64" drill="0.2"/></template>
            <stitchwires><wire x1="0.635" y1="1.27" x2="5.715" y2="1.27" width="0" layer="18"/></stitchwires>
            <vias><pos x="2.54" y="3.175"/></vias>
          </viastitch></signal></signals></board></drawing></eagle>""")
        sig = parse_board_design(root).signals["HOST_GND"]
        self.assertEqual(len(sig.vias), 1)
        (native,) = sig.native_stitches
        self.assertEqual((native.kind, native.offset_mm, native.spacing_mm), ("Area", 0.635, 5.08))
        self.assertEqual((native.template.extent, native.template.drill), ("1-64", 0.2))
        self.assertEqual(native.positions, [(2.54, 3.175)])
        self.assertEqual(native.boundary[0].x2, 5.715)

    def test_mcp_previews_and_submits_the_offline_script(self):
        args = dict(expected_document_id="board-id", net="HOST_GND", points_mm=self.points,
                    pitch_mm=5.08, drill_mm=0.2, offset_mm=0.635, grid_mm=0.635)
        with patch.object(server.session.bridge, "call", return_value={"status": "requested"}) as call:
            self.assertEqual(server.apply_via_stitching(**args)["script"], self.script())
            call.assert_not_called()
            self.assertEqual(server.apply_via_stitching(**args, dry_run=False)["status"], "requested")
            call.assert_called_once_with("native_board_command", {"action": "apply_via_stitching",
                                         "expected_document_id": "board-id", "script": self.script()})

    def test_addin_only_accepts_the_native_stitch_sequence_and_active_document(self):
        addin = load_addin()
        doc = object()
        execute = Mock(return_value="")
        addin._app = types.SimpleNamespace(activeDocument=doc, executeTextCommand=execute)
        addin._ui = types.SimpleNamespace(activeCommand="Electron::Group", commandDefinitions=types.SimpleNamespace(
            itemById=Mock(return_value=types.SimpleNamespace(isNative=True))))
        addin._product_kind = lambda: "board"
        addin._settle = Mock()
        args = {"action": "apply_via_stitching", "expected_document_id": addin._document_id(doc),
                "script": self.script()}
        run = addin.OPS["native_board_command"]
        for invalid in ({"expected_document_id": "stale"}, {"script": self.script() + " WRITE;"},
                        {"script": self.script().replace("COMMIT;", "COMMIT; DELETE (0 0);")}):
            with self.subTest(invalid=invalid), self.assertRaises(addin.BridgeError):
                run(args | invalid)
        execute.assert_not_called()
        self.assertEqual(run(args)["status"], "requested")
        execute.assert_called_once_with("Electron.run " + self.script())
