"""Contract tests for deterministic EAGLE board emission."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET

from fusion_offline.compiler.libraries import resolve_libraries
from fusion_offline.compiler.model import load_design_spec
from fusion_offline.compiler.schematic import build_schematic
from fusion_offline.design import parse_board_design
from fusion_offline.eagle import parse_board
from fusion_offline.stitch import board_obstacles
from test_compiler_schema import minimal_design


class CompilerBoardTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data = minimal_design()

    def build(self, mutate_template=None, mutate_schematic=None):
        from fusion_offline.compiler.board import build_board

        path = self.root / "design.json"
        raw = json.dumps(self.data).encode()
        path.write_bytes(raw)
        spec = load_design_spec(str(path), sha256(raw).hexdigest())
        template = spec.templates["board"].data
        libraries = resolve_libraries(spec)
        schematic = build_schematic(spec, spec.templates["schematic"].data, libraries)
        if mutate_template:
            root = ET.fromstring(template)
            mutate_template(root)
            template = ET.tostring(root)
        if mutate_schematic:
            root = ET.fromstring(schematic)
            mutate_schematic(root)
            schematic = ET.tostring(root)
        xml = build_board(spec, template, libraries, schematic)
        self.assertEqual(build_board(spec, template, libraries, schematic), xml)
        return ET.fromstring(template), ET.fromstring(xml), xml

    def test_elements_contacts_outline_and_rules(self):
        self.data["board"]["holes"] = [{"x": 3, "y": 3, "drill": 2}]
        self.data["board"]["net_classes"] = [
            {"number": 3, "name": "power", "width": 0.3, "drill": 0.4,
             "clearances": {"default": 0.2}},
        ]
        self.data["nets"][0]["class"] = "power"
        self.data["parts"][2]["board"].update(side="bottom", rotation=90)
        template, root, xml = self.build()
        for tag in ("settings", "grid", "layers", "board/designrules"):
            self.assertEqual(ET.tostring(template.find(f"./drawing/{tag}")),
                             ET.tostring(root.find(f"./drawing/{tag}")))
        board = parse_board(xml)
        self.assertEqual(board.outline, (0, 0, 40, 30))
        self.assertEqual(len(board.outline_wires), 4)
        self.assertEqual(root.find(".//board/plain/hole").attrib,
                         {"x": "3.0", "y": "3.0", "drill": "2.0"})
        self.assertEqual([(el.name, el.package, el.x, el.y, el.angle, el.mirror, el.value, el.attrs)
                          for el in board.elements], [
            ("D1", "LED_0805", 24, 21, 90, True, "RED", {"MPN": "TEST-LED"}),
            ("J1", "HEADER3", 4, 10, 0, False, "Power and control", {"MPN": "TEST-HEADER3"}),
            ("Q1", "SOT23", 29, 11, 0, False, "NPN", {"MPN": "TEST-NPN"}),
            ("R1", "RES_0805", 15, 21, 0, False, "330R", {"MPN": "TEST-RESISTOR"}),
        ])
        self.assertEqual(root.find(".//element[@name='J1']").get("locked"), "yes")
        design = parse_board_design(root)
        self.assertEqual(design.classes[0].name, "default")
        self.assertEqual(design.classes[3].clearances, {"0": 0.2})
        self.assertEqual(design.signals["VCC"].net_class, "power")
        self.assertEqual({n: s.contacts for n, s in design.signals.items()}, {
            "CTRL": [("J1", "3"), ("Q1", "1")], "GND": [("J1", "2"), ("Q1", "2")],
            "LED_A": [("D1", "A"), ("R1", "2")], "LED_K": [("D1", "K"), ("Q1", "3")],
            "VCC": [("J1", "1"), ("R1", "1")],
        })

    def test_template_outline_and_unused_library(self):
        self.data["board"]["outline"] = []

        def unused(root):
            ET.SubElement(root.find(".//board/libraries"), "library", name="UNUSED")

        template, root, _ = self.build(mutate_template=unused)
        self.assertEqual(ET.tostring(root.find(".//board/plain")),
                         ET.tostring(template.find(".//board/plain")))
        self.assertEqual([node.get("name") for node in root.findall(".//board/libraries/library")],
                         ["PROJECT"])

    def test_rejects_incompatible_template_and_schematic(self):
        self.data["board"]["copper_layers"] = [1, 2, 16]
        with self.assertRaisesRegex(ValueError, "copper stack"):
            self.build()
        del self.data["board"]["copper_layers"]
        with self.assertRaisesRegex(ValueError, "empty"):
            self.build(mutate_template=lambda r: ET.SubElement(r.find(".//signals"), "signal"))
        with self.assertRaisesRegex(ValueError, "flat"):
            self.build(mutate_schematic=lambda r: ET.SubElement(r.find(".//schematic"), "moduleinst"))
        with self.assertRaisesRegex(ValueError, "pin"):
            self.build(mutate_schematic=lambda r: r.find(".//pinref").set("pin", "MISSING"))

    def add_preroute(self):
        net = self.data["nets"][0]
        net["wires"] = [
            {"x1": 4, "y1": 10, "x2": 8, "y2": 16, "layer": 16, "width": 0.4},
            {"x1": 8, "y1": 16, "x2": 10, "y2": 20, "layer": 1, "width": 0.3},
            {"x1": 10, "y1": 20, "x2": 14.05, "y2": 21, "layer": 1, "width": 0.3},
        ]
        net["vias"] = [{"x": 8, "y": 16, "extent": "1-16", "drill": 0.3, "diameter": 0.6}]
        return net

    def test_preroute_keepout_and_pour(self):
        self.add_preroute()
        vertices = [[30, 20], [38, 20], [38, 28], [30, 28]]
        self.data["board"]["keepouts"] = [{"layers": [1, 16], "vertices": vertices}]
        self.data["board"]["pours"] = [
            {"net": "GND", "layer": 16, "vertices": [[1, 1], [39, 1], [39, 8]],
             "width": 0.2, "isolate": 0.4, "rank": 2, "thermals": True, "orphans": False},
        ]
        _, root, _ = self.build()
        keepouts = [obs for obs in board_obstacles(root)[0] if obs.restrict_layer]
        self.assertEqual([(obs.restrict_layer, obs.layers) for obs in keepouts],
                         [(41, {1}), (42, {16})])
        pour = root.find(".//signal[@name='GND']/polygon")
        self.assertEqual(pour.attrib, {"layer": "16", "width": "0.2", "isolate": "0.4",
                                      "rank": "2", "thermals": "yes", "orphans": "no"})
        self.assertEqual([(float(v.get("x")), float(v.get("y"))) for v in pour],
                         [(1, 1), (39, 1), (39, 8)])
        route = parse_board_design(root).signals["VCC"]
        self.assertEqual([(w.x1, w.y1, w.x2, w.y2, w.layer, w.width) for w in route.wires],
                         [(4, 10, 8, 16, 16, 0.4), (8, 16, 10, 20, 1, 0.3),
                          (10, 20, 14.05, 21, 1, 0.3)])
        self.assertEqual([(v.x, v.y, v.extent, v.drill, v.diameter) for v in route.vias],
                         [(8, 16, "1-16", 0.3, 0.6)])

    def test_rejects_invalid_preroute_copper(self):
        net = self.add_preroute()
        net["wires"][0]["layer"] = 2
        with self.assertRaisesRegex(ValueError, "layer"):
            self.build()
        net["wires"][0]["layer"] = 16

        self.data["routing"]["policies"]["VCC"] = {"allowed_layers": [1]}
        with self.assertRaisesRegex(ValueError, "layer"):
            self.build()
        self.data["routing"]["policies"].clear()
        net["vias"][0]["extent"] = "1-2"
        with self.assertRaisesRegex(ValueError, "span"):
            self.build()
        net["vias"][0]["extent"] = "1-16"
        net["wires"][-1]["x2"] = 14.0501
        with self.assertRaisesRegex(ValueError, "endpoint"):
            self.build()
        net["wires"][-1]["x2"] = 14.05
        net["wires"][1]["x1"] = 8.0001
        with self.assertRaisesRegex(ValueError, "endpoint"):
            self.build()

    def test_contacts_follow_actual_schematic_pinrefs(self):
        def swap_pins(root):
            refs = [root.find(f".//net[@name='{name}']/segment/pinref[@part='R1']")
                    for name in ("VCC", "LED_A")]
            refs[0].attrib["pin"], refs[1].attrib["pin"] = refs[1].get("pin"), refs[0].get("pin")

        _, root, _ = self.build(mutate_schematic=swap_pins)
        design = parse_board_design(root)
        self.assertEqual(design.signals["VCC"].contacts, [("J1", "1"), ("R1", "2")])
        self.assertEqual(design.signals["LED_A"].contacts, [("D1", "A"), ("R1", "1")])

    def test_schematic_only_parts_and_slots(self):
        asset = self.data["libraries"][0]
        library = ET.parse(asset["path"]).getroot()
        led = library.find(".//deviceset[@name='LED']/devices/device")
        del led.attrib["package"]
        led.remove(led.find("connects"))
        pad = library.find(".//package[@name='HEADER3']/pad")
        pad.attrib.update(shape="slot", slotLength="2.3", rot="R90")
        payload = ET.tostring(library)
        path = self.root / "project.lbr"
        path.write_bytes(payload)
        asset.update(path=str(path), sha256=sha256(payload).hexdigest())
        self.data["parts"][2]["board"] = None
        for net in self.data["nets"]:
            if net["name"] in ("LED_A", "LED_K"):
                net["route"] = False
            if net["name"] == "LED_K":
                net.update(pins=[p for p in net["pins"] if p["part"] == "D1"],
                           schematic_style="labels", port="inter-block")

        _, root, xml = self.build()
        board = parse_board(xml)
        self.assertEqual([el.name for el in board.elements], ["J1", "Q1", "R1"])
        signals = parse_board_design(root).signals
        self.assertNotIn("LED_K", signals)
        self.assertEqual(signals["LED_A"].contacts, [("R1", "2")])
        self.assertEqual(root.find(".//package[@name='HEADER3']/pad").attrib, pad.attrib)
        with self.assertRaisesRegex(ValueError, "Unverified.*SLOT.*J1.1"):
            board_obstacles(root)

    def test_per_connection_via_budget(self):
        net = self.add_preroute()
        net["vias"].append({"x": 10, "y": 20, "extent": "1-16", "drill": 0.3})
        net["wires"][1]["layer"] = 16
        net["wires"][0]["layer"] = 1
        self.data["routing"]["policies"]["VCC"] = {"max_vias_per_connection": 1}
        with self.assertRaisesRegex(ValueError, "via budget"):
            self.build()

    def test_refuses_unverified_inner_keepouts(self):
        def inner_layer(root):
            ET.SubElement(root.find("./drawing/layers"), "layer", number="2", name="Route2")

        def inner_board(root):
            inner_layer(root)
            root.find(".//param[@name='layerSetup']").set("value", "(1+2*16)")

        self.data["board"]["keepouts"] = [
            {"layers": [2], "vertices": [[0, 0], [5, 0], [5, 5]]},
        ]
        with self.assertRaisesRegex(ValueError, "unsupported native restrict geometry"):
            self.build(mutate_template=inner_board, mutate_schematic=inner_layer)

    def test_rejects_schematic_connections_or_part_identity_that_differ_from_pinned_library(self):
        def change_connect(root):
            root.find(".//deviceset[@name='RESISTOR']//connect[@pin='P$1']").set("pad", "2")

        def change_technology(root):
            root.find(".//part[@name='R1']").set("technology", "OTHER")

        with self.assertRaisesRegex(ValueError, "R1.*connection"):
            self.build(mutate_schematic=change_connect)
        with self.assertRaisesRegex(ValueError, "R1.*identity"):
            self.build(mutate_schematic=change_technology)

    def test_concave_outline_checks_polygon_and_honors_allow_outside(self):
        self.data["board"]["outline"] = [[0, 0], [40, 0], [40, 21], [25, 21],
                                          [25, 30], [10, 30], [10, 15], [0, 15]]
        self.build()
        placement = self.data["parts"][1]["board"]
        placement.update(x=4, y=15)
        self.build()
        placement.update(y=20)
        with self.assertRaisesRegex(ValueError, "R1.*outside"):
            self.build()
        placement["allow_outside"] = True
        self.build()

    def test_keeps_spec_declared_copper_on_protected_nets(self):
        self.add_preroute()
        self.data["routing"]["protected_nets"] = ["VCC"]
        self.data["board"]["pours"] = [
            {"net": "VCC", "layer": 16, "vertices": [[1, 1], [39, 1], [39, 8]],
             "width": 0.2},
        ]
        _, root, _ = self.build()
        signal = parse_board_design(root).signals["VCC"]
        self.assertEqual((len(signal.wires), len(signal.vias), signal.polygons), (3, 1, 1))

    def test_rejects_a_self_crossing_pour(self):
        self.data["board"]["pours"] = [
            {"net": "GND", "layer": 16, "vertices": [[1, 1], [9, 7], [1, 7], [8, 1]],
             "width": 0.2},
        ]
        with self.assertRaisesRegex(ValueError, "pour.*intersects itself"):
            self.build()
