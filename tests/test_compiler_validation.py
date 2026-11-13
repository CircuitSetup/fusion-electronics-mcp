"""Offline validation over saved EAGLE bytes."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from test_compiler_schema import minimal_design


class CompilerValidationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = minimal_design()

    def compile(self):
        from fusion_offline.compiler.compile import compile_candidate

        source = self.root / "design.json"
        raw = json.dumps(self.data).encode()
        source.write_bytes(raw)
        return compile_candidate(source, sha256(raw).hexdigest(), self.root / "candidate")

    def set_asset(self, row, root):
        path = self.root / Path(row["path"]).name
        data = ET.tostring(root)
        path.write_bytes(data)
        row.update(path=str(path), sha256=sha256(data).hexdigest())

    def report(self, manifest, modify_board=None, **options):
        from fusion_offline.compiler.validate import validate_candidate

        board = manifest.read_artifact("board")
        if modify_board:
            root = ET.fromstring(board)
            modify_board(root)
            board = ET.tostring(root)
        return validate_candidate(manifest.read_artifact("schematic"), board,
                                  json.loads(manifest.read_artifact("routing_intent")), **options)

    def test_full_routing_counts_unconnected_contacts_without_layer_19_wires(self):
        self.data["routing"]["require_fully_routed"] = True
        candidate = self.compile()
        board = ET.fromstring(candidate.read_artifact("board"))
        self.assertFalse(board.findall(".//signal/wire[@layer='19']"))
        report = self.report(candidate)
        self.assertEqual(report["airwires"], 5)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(candidate.data["status"], "fail")

    def add_preroute(self):
        self.data["nets"][0].update(wires=[
            {"x1": 4, "y1": 10, "x2": 8, "y2": 16, "layer": 16, "width": 0.4},
            {"x1": 8, "y1": 16, "x2": 14.05, "y2": 21, "layer": 1, "width": 0.3},
        ], vias=[{"x": 8, "y": 16, "extent": "1-16", "drill": 0.3, "diameter": 0.6}])

    def test_via_connects_layers_and_endpoint_mismatch_is_rejected(self):
        self.add_preroute()
        candidate = self.compile()
        self.assertEqual(self.report(candidate)["airwires_by_net"]["VCC"], 0)

        def move_endpoint(root):
            root.find(".//signal[@name='VCC']/wire[@layer='1']").set("x2", "14.0501")

        report = self.report(candidate, move_endpoint)
        self.assertEqual(report["airwires_by_net"]["VCC"], 1)
        self.assertEqual(report["status"], "fail")

    def test_saved_unsplit_tee_and_offset_via_do_not_prove_continuity(self):
        self.add_preroute()
        candidate = self.compile()

        def unsplit(root):
            signal = root.find(".//signal[@name='VCC']")
            top = signal.find("wire[@layer='1']")
            top.set("x2", "12")
            top.set("y2", "19.2")
            ET.SubElement(signal, "wire", x1="10", y1="17.6", x2="14.05", y2="21", layer="1", width="0.3")

        def move_landing(root):
            root.find(".//signal[@name='VCC']/wire[@layer='16']").attrib.update(x2="8.0069", y2="15.9931")

        for edit in (unsplit, move_landing):
            with self.subTest(edit=edit.__name__):
                report = self.report(candidate, edit)
                self.assertEqual(report["airwires_by_net"]["VCC"], 1)
                self.assertEqual(report["status"], "fail")

    def test_pour_outline_is_pending_native_fill(self):
        candidate = self.compile()

        def add_pour(root):
            signal = root.find(".//signal[@name='VCC']")
            polygon = ET.SubElement(signal, "polygon", layer="1", width="0.2")
            for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
                ET.SubElement(polygon, "vertex", x=str(x), y=str(y))

        report = self.report(candidate, add_pour)
        self.assertEqual(report["airwires_by_net"]["VCC"], 1)
        finding = next(row for row in report["findings"] if row["check"] == "full_routing")
        self.assertIn("Native fill connectivity required for VCC", finding["detail"])

    def test_measured_plane_islands_do_not_hide_return_opens(self):
        candidate = self.compile()

        def add_pour(root):
            signal = root.find(".//signal[@name='VCC']")
            polygon = ET.SubElement(signal, "polygon", layer="1", width="0.2", rank="2", thermals="yes")
            for x, y in ((1, 1), (39, 1), (39, 29), (1, 29)):
                ET.SubElement(polygon, "vertex", x=str(x), y=str(y))

        def island(x, y, xx, yy):
            return {"net": "VCC", "layer": 1, "rings": [[(x, y), (xx, y), (xx, yy), (x, yy)]]}

        split = [island(2, 8, 6, 12), island(12, 19, 16, 23)]
        joined = [island(2, 8, 16, 23)]
        isolated_pad = (joined[0]["rings"][0], [(13, 20), (15, 20), (15, 22), (13, 22)])
        for fills, complete, status, opens in (
            (None, False, "unverified", 1), (joined, False, "unverified", 1),
            (split, True, "fail", 1), (joined, True, "pass", 0),
            ([{"net": "VCC", "layer": 1, "rings": isolated_pad}], True, "fail", 1),
        ):
            with self.subTest(status=status, fills=fills):
                report = self.report(candidate, add_pour, measured_fills=fills, measured_fills_complete=complete)
                self.assertEqual(report["planes"]["VCC"]["status"], status)
                self.assertEqual(report["airwires_by_net"]["VCC"], opens)

    def test_measured_plane_stitch_requires_copper_on_both_sides(self):
        upper = [(7, 15), (16, 15), (16, 23), (7, 23)]
        lower = [(3, 9), (9, 9), (9, 17), (3, 17)]
        self.data["board"]["pours"] = [
            {"net": "VCC", "layer": layer, "vertices": ring, "width": 0.2}
            for layer, ring in ((1, upper), (16, lower))
        ]
        self.data["nets"][0]["vias"] = [{"x": 8, "y": 16, "extent": "1-16", "drill": 0.3, "diameter": 0.8}]
        candidate = self.compile()
        for bottom_ring, status in ((lower, "pass"), ([(3, 9), (6, 9), (6, 12), (3, 12)], "fail")):
            fills = [{"net": "VCC", "layer": layer, "rings": [ring]}
                     for layer, ring in ((1, upper), (16, bottom_ring))]
            report = self.report(candidate, measured_fills=fills, measured_fills_complete=True)
            self.assertEqual(report["planes"]["VCC"]["status"], status)

    def test_clearance_polygon_corner_does_not_prove_contact(self):
        from fusion_offline.compiler.validate import _fill_contacts, _ring_samples

        for x, y, expected in ((0.5, 0.09945618, []), (0, 0, []), (0.35, 0, [(0, 0, 1)])):
            island = {"layer": 1, "rings": [[(x - .001, y - .001), (x + .001, y - .001),
                                              (x + .001, y + .001), (x - .001, y + .001)]]}
            self.assertEqual(_fill_contacts(island, {(0, 0, 1)}, [], {(0, 0, 1): _ring_samples(0, 0, .4, 1)}),
                             expected)

    def test_inner_keepout_checks_measured_fill_and_its_holes(self):
        for row in self.data["templates"].values():
            template = ET.parse(row["path"]).getroot()
            ET.SubElement(template.find("./drawing/layers"), "layer", number="2", name="Route2")
            setup = template.find(".//param[@name='layerSetup']")
            if setup is not None:
                setup.set("value", "(1*2*16)")
            self.set_asset(row, template)
        region = [(20, 4), (24, 4), (24, 8), (20, 8)]
        self.data["board"]["keepouts"] = [{"layers": [2], "vertices": region}]
        outer = [(17, 2), (29, 2), (29, 11), (17, 11)]
        hole = [(19, 3), (25, 3), (25, 9), (19, 9)]
        self.data["board"]["pours"] = [{"net": "GND", "layer": 2, "vertices": outer, "width": 0.2}]
        candidate = self.compile()
        for fills, expected in (
            (None, "unverified"),
            ([{"net": "GND", "layer": 1, "rings": [outer]}], "unverified"),
            ([{"net": "GND", "layer": 2, "rings": [outer]}], "fail"),
            ([{"net": "GND", "layer": 2, "rings": [outer, hole]}], "pass"),
        ):
            with self.subTest(fills=fills):
                report = self.report(candidate, measured_fills=fills)
                finding = next(row for row in report["findings"] if row["check"] == "keepouts")
                self.assertEqual(finding["status"], expected, finding)

    def test_contactrefs_require_exact_pad_names_and_unique_signals(self):
        candidate = self.compile()

        def rename_pad(root):
            root.find(".//signal[@name='LED_A']/contactref[@element='D1']").set("pad", "a")

        def duplicate_signal(root):
            ET.SubElement(root.find(".//signals"), "signal", name="LED_A")

        for edit in (rename_pad, duplicate_signal):
            with self.subTest(edit=edit):
                self.assertEqual(self.report(candidate, edit)["status"], "fail")

    def test_template_outline_governs_placement_when_spec_omits_outline(self):
        self.data["board"]["outline"] = []
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "placements")
        self.assertEqual(finding["status"], "fail")

    def test_open_template_outline_fails(self):
        self.data["board"]["outline"] = []
        row = self.data["templates"]["board"]
        board = ET.parse(row["path"]).getroot()
        plain = board.find(".//board/plain")
        plain.remove(plain.find("wire"))
        self.set_asset(row, board)
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "outline")
        self.assertEqual(finding["status"], "fail")

    def test_copper_width_hits_keepout_even_when_centerline_misses(self):
        self.add_preroute()
        self.data["board"]["keepouts"] = [
            {"layers": [16], "vertices": [[6, 12.5], [7, 12.5], [7, 12.75], [6, 12.75]]},
        ]
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "keepouts")
        self.assertEqual(finding["status"], "fail")

    def test_native_checks_are_informational_but_unverified_slots_block(self):
        row = self.data["libraries"][0]
        library = ET.parse(row["path"]).getroot()
        library.find(".//package[@name='HEADER3']/pad").set("shape", "slot")
        self.set_asset(row, library)
        candidate = self.compile()
        report = self.report(candidate)
        findings = {finding["check"]: finding for finding in report["findings"]}
        for name, required in (("native_erc", False), ("native_drc", False), ("keepouts", True)):
            with self.subTest(name=name):
                self.assertEqual(findings[name]["status"], "unverified")
                self.assertIs(findings[name]["required"], required)
        self.assertEqual(report["status"], "unverified")
        self.assertIn(b'shape="slot"', candidate.read_artifact("board"))


    def test_square_pad_corner_intersects_keepout(self):
        self.data["board"]["keepouts"] = [
            {"layers": [1], "vertices": [[4.75, 10.75], [4.85, 10.75], [4.85, 10.85], [4.75, 10.85]]},
        ]
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "keepouts")
        self.assertEqual(finding["status"], "fail")

    def test_full_routing_ignores_stale_layer_19_markers(self):
        self.data["routing"]["require_fully_routed"] = True
        candidate = self.compile()

        def route_all(root):
            endpoints = {
                "VCC": ((4, 10), (14.05, 21)), "CTRL": ((4, 15.08), (28.05, 10)),
                "GND": ((4, 12.54), (29.95, 10)), "LED_A": ((23.05, 21), (15.95, 21)),
                "LED_K": ((24.95, 21), (29, 12)),
            }
            for signal in root.findall(".//signal"):
                a, b = endpoints[signal.attrib["name"]]
                attributes = dict(x1=str(a[0]), y1=str(a[1]), x2=str(b[0]), y2=str(b[1]), width="0.2")
                ET.SubElement(signal, "wire", attributes, layer="1")
                ET.SubElement(signal, "wire", attributes, layer="19")

        report = self.report(candidate, route_all)
        self.assertEqual(report["airwires"], 0)
        self.assertEqual(report["status"], "pass")

    def test_disallowed_layer_and_excess_vias_fail_after_board_emission(self):
        self.add_preroute()
        self.data["routing"]["policies"]["VCC"] = {"max_vias_per_connection": 1}
        candidate = self.compile()

        def change_layers(root):
            root.find(".//signal[@name='VCC']/wire[@layer='1']").set("layer", "5")

        def add_via(root):
            signal = root.find(".//signal[@name='VCC']")
            ET.SubElement(signal, "via", x="4", y="10", extent="1-16", drill="0.3", diameter="0.6")

        for edit, phrase in ((change_layers, "disallowed layer"), (add_via, "via budget")):
            with self.subTest(edit=edit):
                report = self.report(candidate, edit)
                finding = next(row for row in report["findings"] if row["check"] == "routing_geometry")
                self.assertEqual(finding["status"], "fail")
                self.assertIn(phrase, finding["detail"])


    def test_offset_pad_uses_its_own_elongation_rule(self):
        asset = self.data["libraries"][0]
        library = ET.parse(asset["path"]).getroot()
        library.find(".//package[@name='HEADER3']/pad").set("shape", "offset")
        self.set_asset(asset, library)
        asset = self.data["templates"]["board"]
        board = ET.parse(asset["path"]).getroot()
        ET.SubElement(board.find(".//designrules"), "param", name="psElongationOffset", value="200")
        self.set_asset(asset, board)
        self.data["board"]["keepouts"] = [
            {"layers": [1], "vertices": [[7, 9.95], [7.1, 9.95], [7.1, 10.05], [7, 10.05]]},
        ]
        candidate = self.compile()
        finding = next(row for row in candidate.data["findings"] if row["check"] == "keepouts")
        self.assertEqual(finding["status"], "unverified")
        self.assertEqual(candidate.data["status"], "unverified")
        self.assertIn("elongation", finding["detail"])

    def test_bottom_pad_restring_enlarges_keepout_copper(self):
        row = self.data["templates"]["board"]
        board = ET.parse(row["path"]).getroot()
        rules = board.find(".//designrules")
        ET.SubElement(rules, "param", name="rvPadBottom", value="1")
        ET.SubElement(rules, "param", name="rlMaxPadBottom", value="2mm")
        self.set_asset(row, board)
        self.data["board"]["keepouts"] = [
            {"layers": [16], "vertices": [[5.2, 9.9], [5.3, 9.9], [5.3, 10.1], [5.2, 10.1]]},
        ]
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "keepouts")
        self.assertEqual(finding["status"], "fail")

    def test_larger_inner_via_ring_does_not_block_outer_only_keepout(self):
        for row in self.data["templates"].values():
            template = ET.parse(row["path"]).getroot()
            ET.SubElement(template.find("./drawing/layers"), "layer", number="2", name="Inner")
            rules = template.find(".//designrules")
            if rules is not None:
                rules.find("param[@name='layerSetup']").set("value", "(1*2*16)")
                ET.SubElement(rules, "param", name="rlMinViaInner", value="0.5mm")
            self.set_asset(row, template)
        self.add_preroute()
        self.data["board"]["keepouts"] = [
            {"layers": [1], "vertices": [[7.85, 16.5], [8.15, 16.5], [8.15, 16.6], [7.85, 16.6]]},
        ]
        report = self.report(self.compile())
        self.assertEqual(next(row for row in report["findings"] if row["check"] == "keepouts")["status"], "pass")

    def test_smd_on_extended_bottom_layer_intersects_bottom_keepout(self):
        for row in self.data["templates"].values():
            template = ET.parse(row["path"]).getroot()
            template.find("./drawing/layers/layer[@number='16']").set("number", "304")
            setup = template.find(".//designrules/param[@name='layerSetup']")
            if setup is not None:
                setup.set("value", "(1*304)")
            self.set_asset(row, template)
        asset = self.data["libraries"][0]
        library = ET.parse(asset["path"]).getroot()
        library.find(".//package[@name='RES_0805']/smd[@name='1']").set("layer", "304")
        self.set_asset(asset, library)
        self.data["board"]["keepouts"] = [
            {"layers": [304], "vertices": [[14, 20.8], [14.2, 20.8], [14.2, 21.1], [14, 21.1]]},
        ]
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "keepouts")
        self.assertEqual(finding["status"], "fail")

    def test_trace_cannot_leave_a_concave_outline_between_inside_endpoints(self):
        self.data["board"]["outline"] = [[0, 0], [40, 0], [40, 30], [11, 30],
                                         [11, 12], [7, 12], [7, 30], [0, 30]]
        self.data["nets"][0]["wires"] = [
            {"x1": 4, "y1": 10, "x2": 14.05, "y2": 21, "layer": 1, "width": 0.3},
        ]
        report = self.report(self.compile())
        finding = next(row for row in report["findings"] if row["check"] == "routing_geometry")
        self.assertEqual(finding["status"], "fail")
        self.assertIn("trace leaves the board outline", finding["detail"])

    def test_via_and_drill_centres_must_be_inside_the_outline(self):
        self.add_preroute()
        self.data["nets"][0]["vias"][0]["x"] = -2
        self.data["nets"][0]["wires"][0]["x2"] = -2
        self.data["nets"][0]["wires"][1]["x1"] = -2
        self.data["board"]["holes"] = [{"x": -1, "y": 10, "drill": 1}]
        report = self.report(self.compile())
        findings = {row["check"]: row for row in report["findings"]}
        self.assertEqual(report["status"], "fail")
        self.assertIn("via centre is outside the board outline", findings["routing_geometry"]["detail"])
        self.assertIn("drill centre is outside the board outline", findings["outline"]["detail"])
